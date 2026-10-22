# -*- coding: utf-8 -*-
"""
Хранилище бота: настройки чатов, память диалога, дневной лог, статистика
(активность/юмор/победы), досье участников, напоминания, баны.

Два режима (включается автоматически):
  • Supabase — если заданы SUPABASE_URL и SUPABASE_SERVICE_KEY. Общая база для
    бота (GitHub Actions) и Mini App (Vercel): настройки больше не сбрасываются
    при перезапусках, а приложение настраивает тот же чат.
  • локальный SQLite (файл DB_PATH, по умолчанию bot.db) — если переменных
    Supabase нет (запуск «на столе», тесты без облака).
"""
import json
import logging
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

# Не ругаемся на частые ошибки копирования из дашборда Supabase:
# «...supabase.co/rest/v1/» и лишние пробелы — отрезаем автоматически.
def _clean_supa_url(u: str) -> str:
    u = (u or "").strip().rstrip("/")
    u = re.sub(r"/rest/v1/?$", "", u, flags=re.I)
    u = re.sub(r"/pg/?$", "", u, flags=re.I)
    return u

SUPA_URL = _clean_supa_url(os.getenv("SUPABASE_URL", ""))
SUPA_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
USE_SUPA = bool(SUPA_URL and SUPA_KEY)

DEFAULT_BOT_NAME = "Олег"
DEFAULTS = {
    "persona": "friendly",       # ключ готового характера или "custom"
    "persona_text": "",          # свой характер
    "bot_name": DEFAULT_BOT_NAME,  # имя, на которое бот отзывается в чате (/name)
    "group_mode": "mention",     # off / mention / always
    "triggers": "",              # слова-клички через запятую
    "daily": True,               # вечерняя сводка 22:00
    "daily_time": "22:00",       # HH:MM по местному времени чата
    "tz_offset": 3,              # часовой пояс чата (UTC+offset)
    "daily_last": "",            # внутреннее: дата последней отправки
    "stopwords": "",             # стоп-слова через запятую
    "antiflood": True,           # антифлуд вкл/выкл
    "flood_limit": 8,            # сообщений за 10 сек
}

log = logging.getLogger("bot.storage")

if USE_SUPA:
    log.info("Хранилище: Supabase (общая база, %s)", SUPA_URL)
    _client = httpx.Client(
        base_url=f"{SUPA_URL}/rest/v1",
        timeout=20,
        headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
                 "Content-Type": "application/json"},
    )

    def _rest(method: str, table: str, params: dict | None = None,
              body=None, prefer: str | None = None):
        headers = {"Prefer": prefer} if prefer else None
        r = _client.request(method, table, params=params or {}, json=body, headers=headers)
        r.raise_for_status()
        return r.json() if r.text else []

    def _one(table: str, params: dict, select: str):
        p = dict(params)
        p["select"] = select
        p["limit"] = "1"
        rows = _rest("GET", table, params=p)
        return rows[0] if rows else None

    def _upsert(table: str, row: dict, on_conflict: str) -> None:
        _rest("POST", table, params={"on_conflict": on_conflict},
              body=row, prefer="return=minimal,resolution=merge-duplicates")

else:
    log.info("Хранилище: локальный SQLite (SUPABASE_URL/SUPABASE_SERVICE_KEY не заданы)")
    import sqlite3

    DB_PATH = os.getenv("DB_PATH", "bot.db")
    _db = sqlite3.connect(DB_PATH, check_same_thread=False)
    _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings  (chat_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history   (chat_id INTEGER PRIMARY KEY, msgs TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, due INTEGER, text TEXT);
        CREATE TABLE IF NOT EXISTS bans      (chat_id INTEGER, user_id INTEGER, PRIMARY KEY (chat_id, user_id));
        CREATE TABLE IF NOT EXISTS daily (
            chat_id INTEGER, msg_id INTEGER, ts INTEGER, user_id INTEGER,
            name TEXT, text TEXT, laughs INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, msg_id)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_ts ON daily (chat_id, ts);
        CREATE TABLE IF NOT EXISTS stats (
            chat_id INTEGER, user_id INTEGER, name TEXT,
            msgs INTEGER DEFAULT 0, laughs INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0, points INTEGER DEFAULT 0, warns INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS dossier (
            chat_id INTEGER, user_id INTEGER, name TEXT, facts TEXT,
            PRIMARY KEY (chat_id, user_id)
        );
        """
    )
    _db.commit()


# ---------- настройки ----------
def get_settings(chat_id: int) -> dict:
    s = dict(DEFAULTS)
    if USE_SUPA:
        row = _one("settings", {"chat_id": f"eq.{chat_id}"}, "data")
        raw = (row or {}).get("data")
    else:
        row = _db.execute("SELECT data FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        raw = row[0] if row else None
    if raw:
        try:
            s.update(json.loads(raw))
        except Exception:
            pass
    return s


def all_settings():
    if USE_SUPA:
        for row in _rest("GET", "settings", params={"select": "chat_id,data"}):
            s = dict(DEFAULTS)
            try:
                s.update(json.loads(row.get("data") or "{}"))
            except Exception:
                continue
            yield int(row["chat_id"]), s
        return
    for cid, data in _db.execute("SELECT chat_id, data FROM settings"):
        s = dict(DEFAULTS)
        try:
            s.update(json.loads(data))
        except Exception:
            continue
        yield cid, s


def save_settings(chat_id: int, s: dict) -> None:
    payload = {"chat_id": chat_id, "data": json.dumps(s, ensure_ascii=False)}
    if USE_SUPA:
        _upsert("settings", payload, "chat_id")
        return
    _db.execute(
        "INSERT INTO settings(chat_id, data) VALUES(?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data",
        (chat_id, payload["data"]),
    )
    _db.commit()


# ---------- память диалога ----------
def _row_msgs(table: str, chat_id: int) -> str | None:
    if USE_SUPA:
        row = _one(table, {"chat_id": f"eq.{chat_id}"}, "msgs")
        return (row or {}).get("msgs")
    row = _db.execute(f"SELECT msgs FROM {table} WHERE chat_id=?", (chat_id,)).fetchone()
    return row[0] if row else None


def get_history(chat_id: int, limit: int = 14) -> list:
    raw = _row_msgs("history", chat_id)
    if not raw:
        return []
    try:
        msgs = json.loads(raw)
    except Exception:
        msgs = []
    return msgs[-limit:]


def push_history(chat_id: int, role: str, content: str, max_len: int = 14) -> None:
    msgs = get_history(chat_id, 200)
    msgs.append({"role": role, "content": content})
    payload = {"chat_id": chat_id, "msgs": json.dumps(msgs[-max_len:], ensure_ascii=False)}
    if USE_SUPA:
        _upsert("history", payload, "chat_id")
        return
    _db.execute(
        "INSERT INTO history(chat_id, msgs) VALUES(?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET msgs=excluded.msgs",
        (chat_id, payload["msgs"]),
    )
    _db.commit()


def clear_history(chat_id: int) -> None:
    if USE_SUPA:
        _rest("DELETE", "history", params={"chat_id": f"eq.{chat_id}"})
        return
    _db.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))
    _db.commit()


# ---------- баны ----------
def is_banned(chat_id: int, user_id: int) -> bool:
    if USE_SUPA:
        row = _one("bans", {"chat_id": f"eq.{chat_id}", "user_id": f"eq.{user_id}"}, "user_id")
        return row is not None
    return _db.execute(
        "SELECT 1 FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone() is not None


def ban(chat_id: int, user_id: int) -> None:
    if USE_SUPA:
        _upsert("bans", {"chat_id": chat_id, "user_id": user_id}, "chat_id,user_id")
        return
    _db.execute("INSERT OR IGNORE INTO bans VALUES(?, ?)", (chat_id, user_id))
    _db.commit()


def unban(chat_id: int, user_id: int) -> None:
    if USE_SUPA:
        _rest("DELETE", "bans", params={"chat_id": f"eq.{chat_id}", "user_id": f"eq.{user_id}"})
        return
    _db.execute("DELETE FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    _db.commit()


# ---------- напоминания ----------
def add_reminder(chat_id: int, due: int, text: str) -> None:
    if USE_SUPA:
        _rest("POST", "reminders", body={"chat_id": chat_id, "due": due, "text": text},
              prefer="return=minimal")
        return
    _db.execute("INSERT INTO reminders(chat_id, due, text) VALUES(?, ?, ?)", (chat_id, due, text))
    _db.commit()


def list_reminders(chat_id: int) -> list:
    if USE_SUPA:
        rows = _rest("GET", "reminders", params={
            "chat_id": f"eq.{chat_id}", "select": "due,text", "order": "due.asc"})
        return [(int(r["due"]), r.get("text") or "") for r in rows]
    return _db.execute(
        "SELECT due, text FROM reminders WHERE chat_id=? ORDER BY due", (chat_id,)
    ).fetchall()


def due_reminders(now: int) -> list:
    if USE_SUPA:
        rows = _rest("GET", "reminders", params={
            "due": f"lte.{now}", "select": "id,chat_id,text", "order": "due.asc"})
        return [(int(r["id"]), int(r["chat_id"]), r.get("text") or "") for r in rows]
    return _db.execute(
        "SELECT id, chat_id, text FROM reminders WHERE due<=?", (now,)
    ).fetchall()


def del_reminder(rid: int) -> None:
    if USE_SUPA:
        _rest("DELETE", "reminders", params={"id": f"eq.{rid}"})
        return
    _db.execute("DELETE FROM reminders WHERE id=?", (rid,))
    _db.commit()


# ---------- дневной лог (для вечерней сводки) ----------
def daily_upsert(chat_id: int, msg_id: int, ts: int, user_id: int, name: str, text: str) -> None:
    short = (text or "")[:400]
    if USE_SUPA:
        params = {"chat_id": f"eq.{chat_id}", "msg_id": f"eq.{msg_id}"}
        if _one("daily", params, "chat_id"):
            _rest("PATCH", "daily", params=params, body={"text": short})
        else:
            _rest("POST", "daily",
                  body={"chat_id": chat_id, "msg_id": msg_id, "ts": ts, "user_id": user_id,
                        "name": name, "text": short, "laughs": 0},
                  prefer="return=minimal")
        return
    _db.execute(
        "INSERT INTO daily(chat_id, msg_id, ts, user_id, name, text, laughs) "
        "VALUES(?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(chat_id, msg_id) DO UPDATE SET text=excluded.text",
        (chat_id, msg_id, ts, user_id, name, short),
    )
    _db.commit()


def daily_react(chat_id: int, msg_id: int, weight: int) -> None:
    """Кто-то поставил реакцию — начисляем автору сообщения «юмор» и баллы."""
    params = {"chat_id": f"eq.{chat_id}", "msg_id": f"eq.{msg_id}"}
    if USE_SUPA:
        row = _one("daily", params, "user_id,name,laughs")
        if not row:
            return
        _rest("PATCH", "daily", params=params,
              body={"laughs": int(row.get("laughs") or 0) + weight})
        bump_stats(chat_id, int(row["user_id"]), row.get("name"), laughs=weight, points=weight)
        return
    row = _db.execute(
        "SELECT user_id, name FROM daily WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)
    ).fetchone()
    if not row:
        return
    _db.execute(
        "UPDATE daily SET laughs = laughs + ? WHERE chat_id=? AND msg_id=?",
        (weight, chat_id, msg_id),
    )
    bump_stats(chat_id, row[0], row[1], laughs=weight, points=weight)
    _db.commit()


def day_rows(chat_id: int, from_ts: int, to_ts: int) -> list:
    if USE_SUPA:
        rows = _rest("GET",
                     f"daily?chat_id=eq.{chat_id}&ts=gte.{from_ts}&ts=lte.{to_ts}"
                     "&select=user_id,name,text,laughs,ts&order=ts.asc")
        return [(int(r["user_id"]), r.get("name"), r.get("text"), int(r.get("laughs") or 0),
                 int(r["ts"])) for r in rows]
    return _db.execute(
        "SELECT user_id, name, text, laughs, ts FROM daily "
        "WHERE chat_id=? AND ts>=? AND ts<=? ORDER BY ts",
        (chat_id, from_ts, to_ts),
    ).fetchall()


def prune_daily(before_ts: int) -> None:
    if USE_SUPA:
        _rest("DELETE", "daily", params={"ts": f"lt.{before_ts}"})
        return
    _db.execute("DELETE FROM daily WHERE ts<?", (before_ts,))
    _db.commit()


# ---------- статистика участников ----------
def bump_stats(chat_id: int, user_id: int, name: str = None,
               msgs: int = 0, laughs: int = 0, wins: int = 0,
               points: int = 0, warns: int = 0) -> None:
    if not user_id:
        return
    uid = int(user_id)
    cparams = {"chat_id": f"eq.{chat_id}", "user_id": f"eq.{uid}"}
    if USE_SUPA:
        row = _one("stats", cparams, "msgs,laughs,wins,points,warns")
        if row:
            body = {
                "name": name or "?",
                "msgs": int(row.get("msgs") or 0) + msgs,
                "laughs": int(row.get("laughs") or 0) + laughs,
                "wins": int(row.get("wins") or 0) + wins,
                "points": int(row.get("points") or 0) + points,
                "warns": int(row.get("warns") or 0) + warns,
            }
            _rest("PATCH", "stats", params=cparams, body=body)
        else:
            _upsert("stats", {"chat_id": chat_id, "user_id": uid, "name": name or "?",
                              "msgs": msgs, "laughs": laughs, "wins": wins,
                              "points": points, "warns": warns}, "chat_id,user_id")
        return
    _db.execute(
        "INSERT INTO stats(chat_id, user_id, name, msgs, laughs, wins, points, warns) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET name=excluded.name, "
        "msgs=stats.msgs+excluded.msgs, laughs=stats.laughs+excluded.laughs, "
        "wins=stats.wins+excluded.wins, points=stats.points+excluded.points, "
        "warns=stats.warns+excluded.warns",
        (chat_id, uid, name or "?", msgs, laughs, wins, points, warns),
    )
    _db.commit()


def get_warns(chat_id: int, user_id: int) -> int:
    if USE_SUPA:
        row = _one("stats", {"chat_id": f"eq.{chat_id}", "user_id": f"eq.{int(user_id)}"}, "warns")
        return int(row.get("warns") or 0) if row else 0
    row = _db.execute(
        "SELECT warns FROM stats WHERE chat_id=? AND user_id=?", (chat_id, int(user_id))
    ).fetchone()
    return row[0] if row else 0


def bump_warns(chat_id: int, user_id: int, name: str, delta: int = 1) -> None:
    bump_stats(chat_id, user_id, name, warns=delta)


def top(chat_id: int, limit: int = 10) -> list:
    if USE_SUPA:
        rows = _rest("GET", "stats", params={
            "chat_id": f"eq.{chat_id}", "order": "points.desc", "limit": str(limit),
            "select": "name,msgs,laughs,wins,points"})
        return [(r.get("name"), int(r.get("msgs") or 0), int(r.get("laughs") or 0),
                 int(r.get("wins") or 0), int(r.get("points") or 0)) for r in rows]
    return _db.execute(
        "SELECT name, msgs, laughs, wins, points FROM stats WHERE chat_id=? "
        "ORDER BY points DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()


# ---------- досье участников ----------
def remember(chat_id: int, user_id: int, name: str, fact: str) -> None:
    uid = int(user_id)
    fact = (fact or "").strip()[:300]
    if not fact:
        return
    cparams = {"chat_id": f"eq.{chat_id}", "user_id": f"eq.{uid}"}
    if USE_SUPA:
        row = _one("dossier", cparams, "facts")
        try:
            facts = json.loads((row or {}).get("facts") or "[]")
        except Exception:
            facts = []
        if fact in facts:
            return
        facts.append(fact)
        body = {"name": name or "?", "facts": json.dumps(facts[-20:], ensure_ascii=False)}
        if row:
            _rest("PATCH", "dossier", params=cparams, body=body)
        else:
            _upsert("dossier", {"chat_id": chat_id, "user_id": uid} | body, "chat_id,user_id")
        return
    row = _db.execute(
        "SELECT facts FROM dossier WHERE chat_id=? AND user_id=?", (chat_id, uid)
    ).fetchone()
    facts = []
    if row:
        try:
            facts = json.loads(row[0])
        except Exception:
            facts = []
    if fact not in facts:
        facts.append(fact)
        _db.execute(
            "INSERT INTO dossier(chat_id, user_id, name, facts) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET name=excluded.name, facts=excluded.facts",
            (chat_id, uid, name or "?", json.dumps(facts[-20:], ensure_ascii=False)),
        )
        _db.commit()


def my_facts(chat_id: int, user_id: int) -> list:
    if USE_SUPA:
        row = _one("dossier", {"chat_id": f"eq.{chat_id}", "user_id": f"eq.{int(user_id)}"}, "facts")
        raw = (row or {}).get("facts")
    else:
        row = _db.execute(
            "SELECT facts FROM dossier WHERE chat_id=? AND user_id=?", (chat_id, int(user_id))
        ).fetchone()
        raw = row[0] if row else None
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def forget(chat_id: int, user_id: int) -> None:
    if USE_SUPA:
        _rest("DELETE", "dossier",
              params={"chat_id": f"eq.{chat_id}", "user_id": f"eq.{int(user_id)}"})
        return
    _db.execute("DELETE FROM dossier WHERE chat_id=? AND user_id=?", (chat_id, int(user_id)))
    _db.commit()


def all_facts(chat_id: int, cap: int = 15) -> list:
    if USE_SUPA:
        rows = _rest("GET", "dossier", params={
            "chat_id": f"eq.{chat_id}", "select": "name,facts"})
    else:
        rows = _db.execute(
            "SELECT name, facts FROM dossier WHERE chat_id=?", (chat_id,)
        ).fetchall()
        rows = [{"name": r[0], "facts": r[1]} for r in rows]
    out = []
    for r in rows:
        try:
            for f in json.loads(r.get("facts") or "[]"):
                out.append(f"{r.get('name')}: {f}")
        except Exception:
            continue
    return out[:cap]
