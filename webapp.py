# -*- coding: utf-8 -*-
"""
Веб-часть бота:
  GET  /              — health («бот жив»)
  GET  /app           — Mini App: веб-приложение настроек (открывается из Telegram)
  POST /api/settings  — загрузить/сохранить настройки чата (с проверкой подписи Telegram)
"""
import hashlib
import hmac
import json
import logging
import os
import urllib.parse

from aiohttp import web
from dotenv import load_dotenv

import storage

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

log = logging.getLogger("bot.web")

HTML = ""


def load_html() -> None:
    global HTML
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.html")
    with open(path, encoding="utf-8") as f:
        HTML = f.read()


# доступ к живому Application (для getChatMember при сохранении настроек)
APP = None


def set_application(app) -> None:
    global APP
    APP = app


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


async def index(request: web.Request) -> web.Response:
    return web.Response(text="бот жив 🟢")


async def app_page(request: web.Request) -> web.Response:
    return web.Response(text=HTML or "<h1>app.html не найден</h1>", content_type="text/html")


def _validate(new: dict, cur: dict) -> dict:
    """Апдейт только разрешённых и корректных полей."""
    import ai

    out = dict(cur)
    if new.get("persona") in list(ai.TONES) + ["custom"]:
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
    import re
    if re.match(r"^\d{1,2}:\d{2}$", dt):
        h, m = dt.split(":")
        out["daily_time"] = f"{int(h) % 24:02d}:{int(m) % 60:02d}"
    try:
        out["tz_offset"] = max(-12, min(14, int(new.get("tz_offset", cur.get("tz_offset", 3)))))
    except Exception:
        out["tz_offset"] = cur.get("tz_offset", 3)
    return out


async def settings_api(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "битый запрос"}, status=400)

    user = verify_init_data(body.get("initData") or "")
    if not user:
        return web.json_response({"ok": False, "error": "подпись Telegram не прошла — открой приложение через бота"}, status=401)

    try:
        chat_id = int(body.get("chat") or 0)
    except Exception:
        chat_id = 0
    if not chat_id:
        return web.json_response({"ok": False, "error": "не указан чат"}, status=400)

    # право доступа: в личке chat_id == порядковый номер юзера, в беседе — надо быть участником
    if chat_id > 0:
        if user["id"] != chat_id:
            return web.json_response({"ok": False, "error": "это настройки другого чата"}, status=403)
    else:
        try:
            member = await APP.bot.get_chat_member(chat_id, user["id"])
        except Exception:
            return web.json_response({"ok": False, "error": "не удалось проверить, что ты в этой беседе"}, status=403)
        if getattr(member, "status", "") not in ("member", "administrator", "creator"):
            return web.json_response({"ok": False, "error": "ты не участник этой беседы"}, status=403)

    action = body.get("action")
    cur = storage.get_settings(chat_id)
    if action == "load":
        return web.json_response({"ok": True, "s": cur})
    if action == "save":
        storage.save_settings(chat_id, _validate(body.get("s") or {}, cur))
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "неизвестное действие"}, status=400)


_RUNNER = None


async def start() -> web.AppRunner:
    global _RUNNER
    load_html()
    webapp = web.Application()
    webapp.router.add_get("/", index)
    webapp.router.add_get("/app", app_page)
    webapp.router.add_post("/api/settings", settings_api)
    _RUNNER = web.AppRunner(webapp)
    await _RUNNER.setup()
    site = web.TCPSite(_RUNNER, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP-сервер на порту %s", PORT)
    return _RUNNER


async def stop() -> None:
    if _RUNNER:
        await _RUNNER.cleanup()
