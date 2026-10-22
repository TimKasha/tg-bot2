# -*- coding: utf-8 -*-
"""
SQLite-хранилище бота: настройки чатов, память диалога, дневной лог,
статистика (активность/юмор/победы), досье участников, напоминания, баны.
"""
import json
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "bot.db")
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript(
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
db.commit()

DEFAULTS = {
    "persona": "friendly",       # ключ готового характера или "custom"
    "persona_text": "",          # свой характер
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


# ---------- настройки ----------
def get_settings(chat_id: int) -> dict:
    row = db.execute("SELECT data FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    s = dict(DEFAULTS)
    if row:
        try:
            s.update(json.loads(row[0]))
        except Exception:
            pass
    return s


def all_settings():
    for cid, data in db.execute("SELECT chat_id, data FROM settings"):
        s = dict(DEFAULTS)
        try:
            s.update(json.loads(data))
        except Exception:
            continue
        yield cid, s


def save_settings(chat_id: int, s: dict) -> None:
    db.execute(
        "INSERT INTO settings(chat_id, data) VALUES(?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data",
        (chat_id, json.dumps(s, ensure_ascii=False)),
    )
    db.commit()


# ---------- память диалога ----------
def get_history(chat_id: int, limit: int = 14) -> list:
    row = db.execute("SELECT msgs FROM history WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        return []
    try:
        msgs = json.loads(row[0])
    except Exception:
        msgs = []
    return msgs[-limit:]


def push_history(chat_id: int, role: str, content: str, max_len: int = 14) -> None:
    msgs = get_history(chat_id, 200)
    msgs.append({"role": role, "content": content})
    db.execute(
        "INSERT INTO history(chat_id, msgs) VALUES(?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET msgs=excluded.msgs",
        (chat_id, json.dumps(msgs[-max_len:], ensure_ascii=False)),
    )
    db.commit()


def clear_history(chat_id: int) -> None:
    db.execute("DELETE FROM history WHERE chat_id=?", (chat_id,))
    db.commit()


# ---------- баны ----------
def is_banned(chat_id: int, user_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone() is not None


def ban(chat_id: int, user_id: int) -> None:
    db.execute("INSERT OR IGNORE INTO bans VALUES(?, ?)", (chat_id, user_id))
    db.commit()


def unban(chat_id: int, user_id: int) -> None:
    db.execute("DELETE FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    db.commit()


# ---------- напоминания ----------
def add_reminder(chat_id: int, due: int, text: str) -> None:
    db.execute("INSERT INTO reminders(chat_id, due, text) VALUES(?, ?, ?)", (chat_id, due, text))
    db.commit()


def list_reminders(chat_id: int) -> list:
    return db.execute(
        "SELECT due, text FROM reminders WHERE chat_id=? ORDER BY due", (chat_id,)
    ).fetchall()


def due_reminders(now: int) -> list:
    return db.execute(
        "SELECT id, chat_id, text FROM reminders WHERE due<=?", (now,)
    ).fetchall()


def del_reminder(rid: int) -> None:
    db.execute("DELETE FROM reminders WHERE id=?", (rid,))
    db.commit()


# ---------- дневной лог (для вечерней сводки) ----------
def daily_upsert(chat_id: int, msg_id: int, ts: int, user_id: int, name: str, text: str) -> None:
    db.execute(
        "INSERT INTO daily(chat_id, msg_id, ts, user_id, name, text, laughs) "
        "VALUES(?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(chat_id, msg_id) DO UPDATE SET text=excluded.text",
        (chat_id, msg_id, ts, user_id, name, text[:400]),
    )
    db.commit()


def daily_react(chat_id: int, msg_id: int, weight: int) -> None:
    """Кто-то поставил реакцию — начисляем автору сообщения «юмор» и баллы."""
    row = db.execute(
        "SELECT user_id, name FROM daily WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)
    ).fetchone()
    if not row:
        return
    db.execute(
        "UPDATE daily SET laughs = laughs + ? WHERE chat_id=? AND msg_id=?",
        (weight, chat_id, msg_id),
    )
    bump_stats(chat_id, row[0], row[1], laughs=weight, points=weight)
    db.commit()


def day_rows(chat_id: int, from_ts: int, to_ts: int) -> list:
    return db.execute(
        "SELECT user_id, name, text, laughs, ts FROM daily "
        "WHERE chat_id=? AND ts>=? AND ts<=? ORDER BY ts",
        (chat_id, from_ts, to_ts),
    ).fetchall()


def prune_daily(before_ts: int) -> None:
    db.execute("DELETE FROM daily WHERE ts<?", (before_ts,))
    db.commit()


# ---------- статистика участников ----------
def bump_stats(chat_id: int, user_id: int, name: str = None,
               msgs: int = 0, laughs: int = 0, wins: int = 0,
               points: int = 0, warns: int = 0) -> None:
    if not user_id:
        return
    db.execute(
        "INSERT INTO stats(chat_id, user_id, name, msgs, laughs, wins, points, warns) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET name=excluded.name, "
        "msgs=stats.msgs+excluded.msgs, laughs=stats.laughs+excluded.laughs, "
        "wins=stats.wins+excluded.wins, points=stats.points+excluded.points, "
        "warns=stats.warns+excluded.warns",
        (chat_id, int(user_id), name or "?", msgs, laughs, wins, points, warns),
    )
    db.commit()


def get_warns(chat_id: int, user_id: int) -> int:
    row = db.execute(
        "SELECT warns FROM stats WHERE chat_id=? AND user_id=?", (chat_id, int(user_id))
    ).fetchone()
    return row[0] if row else 0


def bump_warns(chat_id: int, user_id: int, name: str, delta: int = 1) -> None:
    bump_stats(chat_id, user_id, name, warns=delta)


def top(chat_id: int, limit: int = 10) -> list:
    return db.execute(
        "SELECT name, msgs, laughs, wins, points FROM stats WHERE chat_id=? "
        "ORDER BY points DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()


# ---------- досье участников ----------
def remember(chat_id: int, user_id: int, name: str, fact: str) -> None:
    row = db.execute(
        "SELECT facts FROM dossier WHERE chat_id=? AND user_id=?", (chat_id, int(user_id))
    ).fetchone()
    facts = []
    if row:
        try:
            facts = json.loads(row[0])
        except Exception:
            facts = []
    fact = fact.strip()[:300]
    if fact and fact not in facts:
        facts.append(fact)
        db.execute(
            "INSERT INTO dossier(chat_id, user_id, name, facts) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET name=excluded.name, facts=excluded.facts",
            (chat_id, int(user_id), name or "?", json.dumps(facts[-20:], ensure_ascii=False)),
        )
        db.commit()


def my_facts(chat_id: int, user_id: int) -> list:
    row = db.execute(
        "SELECT facts FROM dossier WHERE chat_id=? AND user_id=?", (chat_id, int(user_id))
    ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row[0])
    except Exception:
        return []


def forget(chat_id: int, user_id: int) -> None:
    db.execute("DELETE FROM dossier WHERE chat_id=? AND user_id=?", (chat_id, int(user_id)))
    db.commit()


def all_facts(chat_id: int, cap: int = 15) -> list:
    out = []
    for name, facts in db.execute(
        "SELECT name, facts FROM dossier WHERE chat_id=?", (chat_id,)
    ).fetchall():
        try:
            for f in json.loads(facts):
                out.append(f"{name}: {f}")
        except Exception:
            continue
    return out[:cap]
