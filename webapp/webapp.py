# -*- coding: utf-8 -*-
"""
Веб-часть Mini App (Flask). Один и тот же код работает:
  — локально:  python webapp.py  →  http://127.0.0.1:8080/app
  — на Vercel: api/index.py импортирует отсюда Flask-приложение `app`
    (serverless-функция, никакого своего сервера не нужно).

Данные берутся из общего с ботом хранилища: Supabase (SUPABASE_URL +
SUPABASE_SERVICE_KEY). Права в беседах проверяются прямыми запросами
к Telegram Bot API (getChatMember).
"""
import hashlib
import hmac
import json
import logging
import os
import re
import urllib.parse

import httpx
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

import storage

load_dotenv()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

log = logging.getLogger("bot.web")
app = Flask(__name__, static_folder=None)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def verify_init_data(raw: str) -> dict | None:
    """Проверка подписи Telegram WebApp initData по официальному алгоритму."""
    if not raw or not BOT_TOKEN:
        return None
    vals = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
    got = vals.pop("hash", None)
    if not got:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    try:
        user = json.loads(vals.get("user", "{}"))
    except Exception:
        user = {}
    return user if isinstance(user, dict) and user.get("id") else None


def mini_app_url(chat_id: int) -> str | None:
    if not PUBLIC_URL:
        return None
    return f"{PUBLIC_URL}/app?chat={chat_id}"


def tg_member_status(chat_id: int, user_id: int) -> str | None:
    """Статус юзера в чате через Bot API (работает без запущенного бота)."""
    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",
            params={"chat_id": chat_id, "user_id": user_id},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("ok"):
            return d["result"].get("status") or ""
    except Exception:
        pass
    return None


def _validate(new: dict, cur: dict) -> dict:
    """Апдейт только разрешённых и корректных полей."""
    out = dict(cur)
    if isinstance(new.get("bot_name"), str):
        out["bot_name"] = new["bot_name"].strip()[:50] or storage.DEFAULT_BOT_NAME
    if new.get("persona") in ("friendly", "sarkast", "gopnik", "mudrets", "night", "custom"):
        out["persona"] = new["persona"]
    if isinstance(new.get("persona_text"), str):
        out["persona_text"] = new["persona_text"][:300]
    if new.get("group_mode") in ("off", "mention", "always"):
        out["group_mode"] = new["group_mode"]
    if isinstance(new.get("triggers"), str):
        out["triggers"] = new["triggers"][:300]
    if isinstance(new.get("stopwords"), str):
        out["stopwords"] = new["stopwords"][:300]
    out["antiflood"] = bool(new.get("antiflood", cur.get("antiflood", True)))
    try:
        out["flood_limit"] = max(3, min(60, int(new.get("flood_limit", cur.get("flood_limit", 8)))))
    except Exception:
        out["flood_limit"] = cur.get("flood_limit", 8)
    out["daily"] = bool(new.get("daily", cur.get("daily", True)))
    dt = str(new.get("daily_time") or cur.get("daily_time") or "22:00")
    if re.match(r"^\d{1,2}:\d{2}$", dt):
        h, m = dt.split(":")
        out["daily_time"] = f"{int(h) % 24:02d}:{int(m) % 60:02d}"
    try:
        out["tz_offset"] = max(-12, min(14, int(new.get("tz_offset", cur.get("tz_offset", 3)))))
    except Exception:
        out["tz_offset"] = cur.get("tz_offset", 3)
    return out


@app.route("/")
def health():
    return "бот жив 🟢", 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/index")
def health_alias():
    # страховка: если Vercel доставит в функцию переписанный путь /api/index,
    # health всё равно ответит (см. vercel.json rewrites)
    return health()


@app.route("/app")
def app_page():
    try:
        return send_from_directory(BASE_DIR, "app.html")
    except Exception:
        return "<h1>app.html не найден</h1>", 404, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/settings", methods=["POST"])
def settings_api():
    body = request.get_json(silent=True) or {}

    user = verify_init_data(body.get("initData") or "")
    if not user:
        return jsonify({"ok": False, "error": "подпись Telegram не прошла — открой приложение через бота"}), 401

    try:
        chat_id = int(body.get("chat") or 0)
    except Exception:
        chat_id = 0
    if not chat_id:
        return jsonify({"ok": False, "error": "не указан чат"}), 400

    # право доступа: в личке chat_id == id юзера, в беседе — должен быть участником
    if chat_id > 0:
        if user["id"] != chat_id:
            return jsonify({"ok": False, "error": "это настройки другого чата"}), 403
    else:
        status = tg_member_status(chat_id, user["id"])
        if not status:
            return jsonify({"ok": False, "error": "не удалось проверить, что ты в этой беседе"}), 403
        if status not in ("member", "administrator", "creator"):
            return jsonify({"ok": False, "error": "ты не участник этой беседы"}), 403

    action = body.get("action")
    cur = storage.get_settings(chat_id)
    if action == "load":
        return jsonify({"ok": True, "s": cur})
    if action == "save":
        storage.save_settings(chat_id, _validate(body.get("s") or {}, cur))
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "неизвестное действие"}), 400


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    log.info("Mini App-сервер на http://127.0.0.1:%s/app", PORT)
    app.run(host="0.0.0.0", port=PORT)
