# -*- coding: utf-8 -*-
"""
Бесплатный бот-собеседник для Telegram: личка + беседы.

Умеет: любой характер (/tone, /persona, Mini App), голосовые (расшифровка + озвучка),
картинки (/img), новости (/news), пересказ видео (/yt), игры (квиз, угадай число,
кубик, монетка), репутацию (/top), память-досье (/remember), автомодерацию
(антифлуд, стоп-слова, /warn), вечернюю сводку дня в 22:00 с интерактивом.
"""
import logging
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
MAX_HISTORY = 14
LOCAL_JOKES = [
    "Программист ставит два стакана у кровати: полный — если захочет выпить, пустой — если не захочет.",
    "— Слушай, у меня две новости: плохая и хорошая. С какой начать?\n— С плохой.\n— Плохая: хорошей нет.",
    "Купил себе бота — всем доволен. Ставлю боту лайк 🤖",
    "Иду вчера мимо банка. Думаю — зайти? Зашёл. Через приложение банка.",
    "Оптимист учил английский и испанский. Пессимист — морзянку. А пессимист с аккаунтом — эмодзи.",
]

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

import ai      # noqa: E402
import storage # noqa: E402
import webapp  # noqa: E402

try:
    from telegram.ext import MessageReactionHandler
except ImportError:  # очень старая версия PTB
    MessageReactionHandler = None
    log.warning("В библиотеке нет MessageReactionHandler — реакции считаться не будут (обнови python-telegram-bot)")

# ---------- состояние в памяти ----------
NUM_GAME = {}   # chat_id -> загаданное число
QUIZ = {}       # chat_id -> {"q", "options", "answer", "claimed"}
FLOOD = {}      # (chat_id, user_id) -> [timestamps]
FLOOD_LAST = {} # (chat_id, user_id) -> ts последнего предупреждения
DEL_OK = {}     # chat_id -> (ts, может ли бот удалять сообщения)


# ---------- утилиты ----------
def name_of(user) -> str:
    if not user:
        return "друг"
    return (user.first_name or "друг").strip()


def display_content(update: Update, text: str) -> str:
    chat = update.effective_chat
    if chat.type == "private":
        return text
    return f"{name_of(update.effective_user)}: {text}"


def chunk(text: str, n: int = 3800) -> list:
    return [text[i:i + n] for i in range(0, len(text), n)] if text else []


async def send_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    for part in chunk(text):
        await update.effective_message.reply_text(part)


async def ensure_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user, chat = update.effective_user, update.effective_chat
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, user.id)
    return getattr(member, "status", "") in ("administrator", "creator")


async def can_i_delete(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    ts, ok = DEL_OK.get(chat_id, (0.0, False))
    if time.time() - ts < 300:
        return ok
    try:
        m = await context.bot.get_chat_member(chat_id, context.bot.id)
        ok = getattr(m, "status", "") == "administrator" and bool(getattr(m, "can_delete_messages", False))
    except Exception:
        ok = False
    DEL_OK[chat_id] = (time.time(), ok)
    return ok


def should_reply(update: Update, s: dict, context: ContextTypes.DEFAULT_TYPE, text_lower: str) -> bool:
    chat = update.effective_chat
    if chat.type == "private":
        return True
    mode = s.get("group_mode", "mention")
    if mode == "off":
        return False
    if mode == "always":
        return True
    msg = update.effective_message
    replied = msg.reply_to_message
    if replied and replied.from_user and replied.from_user.id == context.bot.id:
        return True
    username = context.bot.username
    if username and f"@{username.lower()}" in text_lower:
        return True
    triggers = [t.strip().lower() for t in s.get("triggers", "").split(",") if t.strip()]
    return any(t in text_lower for t in triggers)


# ---------- антифлуд и стоп-слова ----------
async def maybe_flood(update: Update, context: ContextTypes.DEFAULT_TYPE, s: dict) -> bool:
    chat, user = update.effective_chat, update.effective_user
    if not s.get("antiflood", True) or not user:
        return False
    key = (chat.id, user.id)
    now = time.time()
    lst = FLOOD.setdefault(key, [])
    while lst and now - lst[0] > 10:
        lst.pop(0)
    lst.append(now)
    if len(lst) <= int(s.get("flood_limit", 8)):
        return False
    lst.clear()
    if now - FLOOD_LAST.get(key, 0) < 60:
        return True  # недавно уже предупреждали — тихо игнорируем
    FLOOD_LAST[key] = now
    storage.bump_stats(chat.id, user.id, name_of(user), warns=1)
    n = storage.get_warns(chat.id, user.id)
    text = f"⚠️ {name_of(user)}, притормози: слишком много сообщений подряд."
    if n and n % 3 == 0:
        try:
            await context.bot.restrict_chat_member(
                chat.id, user.id,
                permissions=ChatPermissions.no_permissions(),
                until_date=int(now) + 300,
            )
            text = f"🔇 {name_of(user)} замьючен на 5 минут (антифлуд)."
        except Exception:
            pass
    try:
        await context.bot.send_message(chat.id, text)
    except Exception:
        pass
    return True


async def maybe_stopword(update: Update, context: ContextTypes.DEFAULT_TYPE, s: dict) -> bool:
    chat = update.effective_chat
    if chat.type == "private":
        return False
    lst = [w.strip().lower() for w in s.get("stopwords", "").split(",") if w.strip()]
    if not lst:
        return False
    tl = (update.effective_message.text or "").lower()
    if not any(w in tl for w in lst):
        return False
    if await can_i_delete(chat.id, context):
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        return True
    return False


# ---------- главный ответчик ----------
async def answ(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str | None = None,
               store: bool = True) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if question is not None and store:
        storage.push_history(chat.id, "user", display_content(update, question))
    s = storage.get_settings(chat.id)
    facts = "; ".join(storage.all_facts(chat.id, cap=12))[:1200]
    messages = [{"role": "system", "content": ai.build_system(s, chat.title or "", facts)}]
    messages += storage.get_history(chat.id)
    try:
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    except Exception:
        pass
    try:
        reply = await ai.chat(messages)
    except Exception as e:
        log.exception("Ошибка при запросе к модели")
        await msg.reply_text(f"⚠️ Модель не ответила: {str(e)[:200]}")
        return
    storage.push_history(chat.id, "assistant", reply)
    await send_text(update, context, reply)


def store_incoming(update: Update, text: str | None = None) -> None:
    """Память диалога + дневной лог + статистика активности."""
    msg = update.effective_message
    user = msg.from_user
    if not user or user.is_bot:
        return
    chat = update.effective_chat
    if chat.type not in ("private", "group", "supergroup"):
        return
    text = (text if text is not None else msg.text or "").strip()
    if not text or text.startswith("/"):
        return
    storage.push_history(chat.id, "user", display_content(update, text))
    if chat.type != "private":
        try:
            ts = int(msg.date.timestamp())
        except Exception:
            ts = int(time.time())
        storage.daily_upsert(chat.id, msg.message_id, ts, user.id, name_of(user), text)
        storage.bump_stats(chat.id, user.id, name_of(user), msgs=1)


# ---------- команды: разговоры и настройка ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Привет! Я {context.bot.first_name} — бот-собеседник 👋\n"
        "Пиши мне в личке или возьми в беседу. Основное:\n\n"
        "🎭 /tone — мой характер · /persona свой вариант\n"
        "⚙️ /settings — настройки (+ кнопка приложения)\n"
        "💬 /mode — как отвечать в беседах\n"
        "🌙 /daily — вечерняя сводка дня в 22:00 (кто активный, смешной + квиз)\n"
        "🎮 /quiz /num /dice /coin /top — игры и рейтинг\n"
        "🎙 Голосовое сообщение — просто расшифрую и отвечу\n"
        "🖼 /img — картинка · 📰 /news · 📺 /yt <ссылка>\n"
        "🧠 /remember — «досье» обо мне\n"
        "⏰ /remind — напомнить · /digest — пересказ беседы\n\n"
        "Полный список: /help"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📖 Команды\n\n"
        "🎭 Характер и настройка:\n"
        "/tone — готовые характеры (кнопки)\n"
        "/persona текст — свой характер одной фразой\n"
        "/settings — сводка настроек (в личке — кнопка приложения)\n"
        "/mode — режимы в беседах\n"
        "/triggers слова,через,запятую — слова-клички\n"
        "/reset — сброс памяти и настроек чата\n\n"
        "💬 Разговор:\n"
        "/ask вопрос — прямой вопрос\n"
        "/joke — шутка\n"
        "/digest — пересказ последних сообщений беседы\n"
        "/say текст — сказать от моего имени (в беседах — админам)\n\n"
        "🌙 Сводка дня (для бесед):\n"
        "/daily on|off · /daily time 21:30 · /daily zone -12..14 · /daily тест\n"
        "Каждый вечер в назначенное время: итоги дня, самый активный,\n"
        "самый смешной (по реакциям 😂) и квиз. Реакции считаются автоматически.\n\n"
        "🕹 Игры и рейтинг:\n"
        "/quiz — викторина · /num — угадай число · /dice — кубик · /coin — монетка\n"
        "/top — короли чата: активность, юмор, победы\n\n"
        "🎙 Голос и медиа:\n"
        "Голосовое — расшифрую и отвечу текстом\n"
        "/voice текст — озвучу текст голосом\n"
        "/img описание — нарисую картинку\n"
        "/news тема — свежие новости\n"
        "/yt ссылка — пересказ видео по субтитрам\n\n"
        "🧠 Память:\n"
        "/remember факт — запомни обо мне (можно и просто «запомни…» в чате)\n"
        "/memory — что я помню? · /forget — забудь всё обо мне\n\n"
        "🛡 Модерация (админам бесед):\n"
        "/stopwords реклама,спам — удалять такие сообщения\n"
        "/warn — предупреждение · /mute 10 — мут · /unmute\n"
        "/ban /unban — бот игнорирует участника по чёрному списку\n"
        "Антифлуд включается в /settings (или веб-приложении)."
    )


async def cmd_tone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = []
    items = list(ai.TONES.items())
    for i in range(0, len(items), 2):
        rows.append([InlineKeyboardButton(ai.TONE_LABELS[k], callback_data=f"tone:{k}") for k, _ in items[i:i + 2]])
    rows.append([InlineKeyboardButton("✍️ Свой характер", callback_data="tone:custom")])
    await update.effective_message.reply_text("Выбери мой характер:", reply_markup=InlineKeyboardMarkup(rows))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or q.message is None:
        return
    chat_id = q.message.chat_id
    data = q.data or ""
    if data.startswith("quiz:"):
        st = QUIZ.get(chat_id)
        if not st:
            await q.answer(); await q.edit_message_text("Раунд завершён 🎲"); return
        try:
            idx = int(data[5:])
        except ValueError:
            await q.answer(); return
        if idx == st["answer"]:
            winner = q.from_user
            storage.bump_stats(chat_id, winner.id, name_of(winner), wins=1, points=2)
            correct = st["options"][idx]
            QUIZ.pop(chat_id, None)
            await q.answer()
            await q.edit_message_text(
                f"🏆 {name_of(winner)} первый ответил верно! +2 балла\n\n❓ {st['q']}\n✅ Верно: {correct}"
            )
        else:
            await q.answer("Не-а 🙃, попробуй ещё!", show_alert=False)
        return
    await q.answer()
    s = storage.get_settings(chat_id)
    if data.startswith("tone:"):
        key = data[5:]
        s["persona"] = "custom" if key == "custom" else key
        storage.save_settings(chat_id, s)
        if key == "custom":
            await q.edit_message_text("Ок! Опиши характер одной фразой:\n/persona <описание>\n\nНапример: «грубоватый пират с лёгким сарказмом».")
        else:
            await q.edit_message_text(f"Принято! Мой характер: {ai.TONE_LABELS.get(key, key)}\nСвой вариант — /persona <описание>.")
    elif data.startswith("mode:"):
        key = data[5:]
        if key in ai.GROUP_MODES:
            s["group_mode"] = key
            storage.save_settings(chat_id, s)
            await q.edit_message_text(f"Режим в беседах: {ai.GROUP_MODES[key]}")


async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (" ".join(context.args) if context.args else "").strip()
    if not text:
        await update.effective_message.reply_text("Напиши так:\n/persona описаниеХарактера")
        return
    chat = update.effective_chat
    s = storage.get_settings(chat.id)
    s["persona"] = "custom"
    s["persona_text"] = text
    storage.save_settings(chat.id, s)
    await update.effective_message.reply_text(f"Готово! Теперь мой характер:\n«{text}»")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = storage.get_settings(update.effective_chat.id)
    rows = [
        [InlineKeyboardButton("👋 По обращению", callback_data="mode:mention")],
        [InlineKeyboardButton("💬 Отвечать на всё", callback_data="mode:always")],
        [InlineKeyboardButton("🤐 Молчать", callback_data="mode:off")],
    ]
    await update.effective_message.reply_text(
        f"Сейчас режим: {ai.GROUP_MODES.get(s['group_mode'], 'по обращению')}\n"
        "«По обращению» — отвечаю только на @упоминания, ответы на мои сообщения и триггер-слова.\n"
        "«На всё» — реагирую на все сообщения (для этого в @BotFather отключи Privacy Mode).",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    s = storage.get_settings(chat.id)
    if not context.args:
        await update.effective_message.reply_text(
            f"Триггер-слова: {s.get('triggers') or '—'}\nЗадать: /triggers слово1,слово2"
        )
        return
    s["triggers"] = " ".join(context.args)[:300]
    storage.save_settings(chat.id, s)
    await update.effective_message.reply_text(f"Триггер-слова сохранены: {s['triggers']}")


async def cmd_stopwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    s = storage.get_settings(chat.id)
    if not context.args or context.args[0].lower() in ("off", "выкл"):
        if not context.args:
            await update.effective_message.reply_text(
                f"Стоп-слова: {s.get('stopwords') or '—'}\nЗадать: /stopwords реклама,спам\nВыключить: /stopwords off"
            )
            return
        s["stopwords"] = ""
    else:
        s["stopwords"] = " ".join(context.args)[:300]
    storage.save_settings(chat.id, s)
    await update.effective_message.reply_text(
        f"Стоп-слова: {s.get('stopwords') or 'отключены (сообщения не удаляются)'}\n"
        "Удаляю их, если я админ беседы с правом удаления."
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    s = storage.get_settings(chat.id)
    persona = ("Свой: «" + (s["persona_text"] or "—") + "»") if s["persona"] == "custom" \
        else ai.TONE_LABELS.get(s["persona"], "?")
    kb = None
    url = webapp.mini_app_url(chat.id)
    if chat.type == "private" and url:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Открыть приложение настроек", web_app=WebAppInfo(url=url))]])
    await update.effective_message.reply_text(
        "⚙️ Настройки чата:\n"
        f"🎭 Характер: {persona}\n"
        f"🗣 Режим в беседах: {ai.GROUP_MODES.get(s['group_mode'], '?')}\n"
        f"🎯 Триггеры: {s.get('triggers') or '—'}\n"
        f"🛡 Стоп-слова: {s.get('stopwords') or '—'} · антифлуд {'вкл' if s.get('antiflood') else 'выкл'} ({s.get('flood_limit')}/10с)\n"
        f"🌙 Сводка: {'вкл' if s.get('daily') else 'выкл'} в {s.get('daily_time')} (UTC{s.get('tz_offset',3):+d})\n"
        f"🧠 Память: до {MAX_HISTORY} сообщений\n"
        f"🤖 Модель: {ai.AI_MODEL}",
        reply_markup=kb,
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    storage.save_settings(chat.id, dict(storage.DEFAULTS))
    storage.clear_history(chat.id)
    NUM_GAME.pop(chat.id, None)
    QUIZ.pop(chat.id, None)
    await update.effective_message.reply_text("Сбросил память и настройки. Напоминания остались: /reminders")


# ---------- разговоры ----------
async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (" ".join(context.args) if context.args else "").strip()
    if not q:
        await update.effective_message.reply_text("Напиши вопрос: /ask почему небо синее")
        return
    await answ(update, context, question=q)


async def cmd_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ai.AI_API_KEY:
        await update.effective_message.reply_text(random.choice(LOCAL_JOKES))
        return
    await answ(update, context, question="Расскажи свежую шутку или анекдот. Только сама шутка, без предисловий.")


async def cmd_say(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type != "private" and not await ensure_admin(update, context):
        await update.effective_message.reply_text("Команда только для админов.")
        return
    text = (" ".join(context.args) if context.args else "").strip()
    if not text:
        return
    try:
        await update.effective_message.delete()
    except Exception:
        pass
    await context.bot.send_message(chat.id, text)


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пересказ последних сообщений беседы (по памяти бота)."""
    chat = update.effective_chat
    msgs = storage.get_history(chat.id, 40)
    if len(msgs) < 2:
        await update.effective_message.reply_text("Пока нечего пересказывать — поболтайте сначала 🙂")
        return
    joined = "\n".join(m["content"] if m["role"] == "user" else "Бот: " + m["content"] for m in msgs)
    try:
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
        summary = await ai.chat([
            {"role": "system", "content": "Ты делаешь краткие пересказы бесед. Пиши по-русски, живо и дружелюбно."},
            {"role": "user", "content": "Вот лог беседы:\n" + joined +
             "\n\nКратко перескажи, о чём беседовали: 3–6 предложений, с именами участников."},
        ], temperature=0.7, max_tokens=450)
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Модель не ответила: {str(e)[:200]}")
        return
    await send_text(update, context, summary)


# ---------- напоминания ----------
async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = (" ".join(context.args) if context.args else "").strip()
    units = {"с": 1, "c": 1, "м": 60, "m": 60, "ч": 3600, "h": 3600, "д": 86400, "d": 86400}
    m = re.match(r"^\s*(\d+)\s*([сcмmчhдd])\s*(.+)$", args)
    if not m:
        await update.effective_message.reply_text("Напиши так:\n/remind 10м созвон\n(10м / 2ч / 1д / 30с)")
        return
    sec = int(m.group(1)) * units[m.group(2).lower()]
    text = m.group(3).strip()
    storage.add_reminder(update.effective_chat.id, int(time.time()) + sec, text)
    await update.effective_message.reply_text(f"⏰ Записал: через {m.group(1)}{m.group(2)} напомню «{text}».")


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = storage.list_reminders(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("Напоминаний нет. Добавить: /remind 10м текст")
        return
    lines = [f"• {datetime.fromtimestamp(d).strftime('%d.%m %H:%M')} — {t}" for d, t in rows]
    await update.effective_message.reply_text("⏰ Напоминания:\n" + "\n".join(lines))


async def reminders_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    for rid, chat_id, text in storage.due_reminders(now):
        storage.del_reminder(rid)
        try:
            await context.bot.send_message(chat_id, f"⏰ Напоминание: {text}")
        except Exception:
            pass


# ---------- вечерняя сводка дня ----------
def _fmt_top(counter: Counter, names: dict, medal: str = "🔥") -> str:
    medals = ["🥇", "🥈", "🥉", "▫️"]
    items = sorted(counter.items(), key=lambda kv: -kv[1])[:3]
    return " ".join(f"{medals[i] if i < len(medals) else '▫️'} {names.get(u, '?')} ({c})" for i, (u, c) in enumerate(items))


async def run_digest(context: ContextTypes.DEFAULT_TYPE, chat_id: int, s: dict, force: bool = False) -> bool:
    now = int(time.time())
    off = int(s.get("tz_offset", 3))
    lt = datetime.fromtimestamp(now, tz=timezone(timedelta(hours=off)))
    midnight = int(lt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    rows = storage.day_rows(chat_id, midnight, now)
    if not rows:
        if force:
            await context.bot.send_message(chat_id, "Сегодня пока тишина — нечего подводить 🙂")
        return False
    storage.prune_daily(now - 2 * 86400)

    msgs_by, laughs_by, names = Counter(), Counter(), {}
    texts = []
    for uid, name, textv, lg, _ts in rows:
        names[uid] = name or "?"
        msgs_by[uid] += 1
        laughs_by[uid] += lg or 0
        texts.append(f"{name}: {textv}")
    total, users = len(rows), len(msgs_by)

    summary = ""
    try:
        summary = await ai.chat([
            {"role": "system", "content": "Ты дружелюбный ведущий итогов дня в чате. Пиши живо, тепло, с лёгким юмором, по-русски."},
            {"role": "user", "content": "Сообщения чата за сегодня:\n" + "\n".join(texts[-150:]) +
             "\n\nСоставь сводку дня: 3–6 предложений, о чём говорили и кто чем запомнился. Без приветствий и подписи."},
        ], temperature=0.8, max_tokens=450)
    except Exception:
        summary = ""
    if not summary:
        summary = "Сегодня чат жил своей жизнью — застенографировать всё не успел 😉"

    wins_rows = sorted(
        [t for t in storage.top(chat_id, 20) if (t[3] or 0) > 0],
        key=lambda t: -(t[3] or 0),
    )[:3]

    lines = [
        "🌙 Итоги дня",
        f"📊 {total} сообщений от {users} участников",
        f"🔥 Активность: {_fmt_top(msgs_by, names)}",
    ]
    if laughs_by and any(v > 0 for v in laughs_by.values()):
        lines.append(f"😂 Самый смешной (по реакциям): {_fmt_top(laughs_by, names)}")
    if wins_rows:
        lines.append("🏆 В играх: " + " · ".join(f"{t[0]} ({t[3]} побед)" for t in wins_rows))
    lines += ["", "🤖 Как шёл день:", summary]

    quiz = None
    try:
        themes = "; ".join(t[:60] for t in texts[-12:])[:400]
        quiz = await ai.make_quiz(themes)
    except Exception:
        quiz = None
    kb = None
    if quiz:
        QUIZ[chat_id] = {"q": quiz["q"], "options": quiz["options"], "answer": quiz["answer"], "claimed": False}
        btns = [InlineKeyboardButton(o[:22], callback_data=f"quiz:{i}") for i, o in enumerate(quiz["options"])]
        kb = InlineKeyboardMarkup([btns[:2], btns[2:]])
        lines.append(f"🎮 Разминка: {quiz['q']}")
    try:
        await context.bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)
        return True
    except Exception:
        log.exception("Не удалось отправить сводку в %s", chat_id)
        return False


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    for chat_id, s in storage.all_settings():
        if chat_id >= 0:          # сводки только для бесед
            continue
        if not s.get("daily", True):
            continue
        off = int(s.get("tz_offset", 3))
        lt = datetime.fromtimestamp(now, tz=timezone(timedelta(hours=off)))
        try:
            th, tm = (s.get("daily_time", "22:00") or "22:00").split(":")[:2]
            target_min = int(th) * 60 + int(tm)
        except ValueError:
            target_min = 22 * 60
        now_min = lt.hour * 60 + lt.minute
        if now_min < target_min:  # до назначенного времени — ждём
            continue
        today = lt.date().isoformat()
        if s.get("daily_last") == today:  # уже отправляли сегодня
            continue
        s["daily_last"] = today
        storage.save_settings(chat_id, s)
        try:
            await run_digest(context, chat_id, s)
        except Exception:
            log.exception("Сводка не удалась в чате %s", chat_id)


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    s = storage.get_settings(chat.id)
    msg = update.effective_message
    if chat.type == "private":
        await msg.reply_text("Сводки — фишка для бесед 🙂 Но /digest перескажет нашу личную переписку всегда.")
        return
    a = [x.lower() for x in (context.args or [])]
    if not a:
        await msg.reply_text(
            f"🌙 Сводка: {'вкл' if s['daily'] else 'выкл'} в {s['daily_time']} (UTC{s['tz_offset']:+d})\n"
            "/daily on|off · /daily time 21:30 · /daily zone N · /daily тест"
        )
        return
    if a[0] == "on":
        s["daily"] = True
    elif a[0] == "off":
        s["daily"] = False
    elif a[0] == "тест":
        off = int(s.get("tz_offset", 3))
        today = datetime.fromtimestamp(int(time.time()), tz=timezone(timedelta(hours=off))).date().isoformat()
        s["daily_last"] = today
        storage.save_settings(chat.id, s)
        await msg.reply_text("Держи пробный выпуск сводки 👇")
        await run_digest(context, chat.id, s, force=True)
        return
    elif a[0] == "time" and len(a) > 1 and re.match(r"^\d{1,2}:\d{2}$", a[1]):
        h, mnt = a[1].split(":")
        s["daily_time"] = f"{int(h) % 24:02d}:{int(mnt) % 60:02d}"
    elif a[0] == "zone" and len(a) > 1:
        try:
            s["tz_offset"] = max(-12, min(14, int(float(a[1]))))
        except ValueError:
            await msg.reply_text("Например: /daily zone 3")
            return
    else:
        await msg.reply_text("Не понял 🙂 Формат: /daily on|off · /daily time HH:MM · /daily zone N · /daily тест")
        return
    storage.save_settings(chat.id, s)
    await msg.reply_text(f"Готово: сводка {'вкл' if s['daily'] else 'выкл'} в {s['daily_time']} (UTC{s['tz_offset']:+d})")


# ---------- голос: расшифровка и озвучка ----------
async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = msg.from_user
    if not user or user.is_bot:
        return
    if user and chat.type != "private" and storage.is_banned(chat.id, user.id):
        return
    if not ai.AI_API_KEY:
        await msg.reply_text("🎙 Расшифровка голоса работает, когда подключён ключ Groq или Gemini (шаг 3 в README).")
        return
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    try:
        f = await msg.voice.get_file()
        data = bytes(await f.download_as_bytearray())
        text = await ai.transcribe(data, getattr(msg.voice, "mime_type", None) or "audio/ogg")
    except Exception as e:
        await msg.reply_text(f"⚠️ Расшифровать не вышло: {str(e)[:160]}")
        return
    if not text or not text.strip():
        await msg.reply_text("Не разобрал звук 🙃 попробуй ещё раз")
        return
    store_incoming(update, text="[голос] " + text)
    s = storage.get_settings(chat.id)
    if chat.type != "private" and not should_reply(update, s, context, text.lower()):
        return
    await answ(update, context, store=False)


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (" ".join(context.args) if context.args else "").strip()
    if not text:
        await update.effective_message.reply_text("Напиши так: /voice Привет, я твоя озвучка")
        return
    msg = update.effective_message
    try:
        audio = await ai.speak(text)
    except Exception as e:
        await msg.reply_text(f"⚠️ Озвучить не получилось: {str(e)[:160]}")
        return
    await msg.reply_audio(audio=audio, filename="голос.mp3")


# ---------- картинки, новости, ютуб ----------
async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = (" ".join(context.args) if context.args else "").strip()
    if len(prompt) < 3:
        await update.effective_message.reply_text("Опиши, что нарисовать: /img кот в смокинге")
        return
    msg = update.effective_message
    await msg.reply_text("🎨 Рисую…")
    url = (f"https://image.pollinations.ai/prompt/{quote(prompt)}"
           f"?width=1024&height=1024&nologo=true&seed={random.randint(1, 99999)}")
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
            await msg.reply_photo(photo=r.content)
        else:
            raise RuntimeError(f"статус {r.status_code}")
    except Exception as e:
        await msg.reply_text(f"⚠️ Не нарисовалось ({str(e)[:120]})\nПопробуй ещё раз позже 🙃")


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = (" ".join(context.args) if context.args else "").strip() or "главное за день"
    msg = update.effective_message
    url = f"https://news.google.com/rss/search?q={quote(topic)}&hl=ru&gl=RU&ceid=RU:ru"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(url)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = root.findall("./channel/item")[:5]
        if not items:
            await msg.reply_text("Новостей не нашёл 🤷")
            return
        lines = [f"📰 «{topic}» — топ-{len(items)}:"]
        for it in items:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            lines.append(f"\n• {title}\n{link}")
        await msg.reply_text("\n".join(lines))
    except Exception as e:
        await msg.reply_text(f"⚠️ Новости не получились: {str(e)[:160]}")


YOUTUBE_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})")


async def cmd_yt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (" ".join(context.args) if context.args else "").strip()
    m = YOUTUBE_ID.search(url)
    msg = update.effective_message
    if not m:
        await msg.reply_text("Пришли ссылку: /yt https://www.youtube.com/watch?v=...")
        return
    vid = m.group(1)
    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        subtitles = ""
        try:
            api = YouTubeTranscriptApi()
            tr = api.fetch(vid, languages=["ru", "en"])
            snippets = getattr(tr, "snippets", tr)
            subtitles = " ".join(getattr(sn, "text", "") for sn in snippets)
        except AttributeError:
            subtitles = " ".join(t.get("text", "") for t in YouTubeTranscriptApi.get_transcript(vid, languages=["ru", "en"]))
        if not subtitles:
            raise RuntimeError("субтитры пусты")
    except Exception as e:
        await msg.reply_text(
            "⚠️ Субтитры вытащить не получилось (у видео их нет, либо API изменился).\n"
            f"{str(e)[:120]}"
        )
        return
    try:
        summary = await ai.chat([
            {"role": "system", "content": "Ты делаешь краткие пересказы видео по субтитрам. По-русски, живо."},
            {"role": "user", "content": "Субтитры видео:\n" + subtitles[:12000] +
             "\n\nПерескажи суть видео в 6–8 предложениях + один главный вывод."},
        ], temperature=0.7, max_tokens=500)
    except Exception as e:
        await msg.reply_text(f"⚠️ Модель не ответила: {str(e)[:160]}")
        return
    await send_text(update, context, summary)


# ---------- игры ----------
async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if not ai.AI_API_KEY:
        await msg.reply_text("Квизы требует ключ AI (шаг 3 в README) 🙃")
        return
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    quiz = await ai.make_quiz("")
    if not quiz:
        await msg.reply_text("Модель сейчас не в настроении 🙃 попробуй ещё раз")
        return
    QUIZ[chat.id] = {"q": quiz["q"], "options": quiz["options"], "answer": quiz["answer"], "claimed": False}
    btns = [InlineKeyboardButton(o[:22], callback_data=f"quiz:{i}") for i, o in enumerate(quiz["options"])]
    await msg.reply_text(f"❓ {quiz['q']}", reply_markup=InlineKeyboardMarkup([btns[:2], btns[2:]]))


def _num_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, guess: int) -> bool:
    chat = update.effective_chat
    secret = NUM_GAME.get(chat.id)
    if secret is None:
        return False
    user = update.effective_user
    msg = update.effective_message
    if guess == secret:
        NUM_GAME.pop(chat.id, None)
        storage.bump_stats(chat.id, user.id, name_of(user), wins=1, points=2)
        msg.reply_text(f"🎉 {name_of(user)} угадал(а) число {secret}! +2 балла. /num — новая партия")
    else:
        hint = "больше ⬆️" if guess < secret else "меньше ⬇️"
        msg.reply_text(f"Тайное число {hint}")
    return True


async def cmd_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if context.args and context.args[0].isdigit():
        if chat.id not in NUM_GAME:
            await msg.reply_text("Сначала начни игру: /num")
            return
        _num_answer(update, context, int(context.args[0]))
        return
    NUM_GAME[chat.id] = random.randint(1, 100)
    await msg.reply_text("🎲 Я загадал число от 1 до 100! Пиши варианты прямо в чат (или так: /num 45).")


async def cmd_dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.effective_message.reply_dice()
    v = m.dice.value if m.dice else 0
    if v == 6:
        txt = "🎲 Шестёрка! Джекпот удачи 🥳"
    elif v >= 4:
        txt = "🎲 Неплохо!"
    else:
        txt = "🎲 Бывает и хуже…"
    await m.reply_text(txt)


async def cmd_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    side = random.choice(["Орёл 🪙", "Решка 🌑"])
    await update.effective_message.reply_text(f"Монетка падает… {side}!")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    rows = storage.top(chat.id, 10)
    if not rows:
        await update.effective_message.reply_text("Пока никто ничего не заработал — поболтай и поиграй!\nРеакции 😂 на чужие сообщения тоже считаются 😏")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["👑 Короли чата", ""]
    for i, (name, msgs_, laughs, wins, points) in enumerate(rows):
        medal = medals[i] if i < 3 else "🎖"
        lines.append(f"{medal} {name} — {points} б. (💬 {msgs_} · 😂 {laughs} · 🏆 {wins})")
    lines.append("\nБаллы: победа +2, каждая реакция +1 (😂 🤣 — +2)")
    await update.effective_message.reply_text("\n".join(lines))


# ---------- память-досье ----------
REMEMBER_RE = re.compile(r"запом\w*[,:;]?\s*(.+)", re.I | re.S)


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fact = (" ".join(context.args) if context.args else "").strip()
    if not fact:
        await update.effective_message.reply_text(
            "Напиши факт: /remember люблю пиццу с ананасами (спорная позиция)\n"
            "А можно просто написать мне в чате «запомни: факт» — я пойму и сам."
        )
        return
    chat = update.effective_chat
    user = update.effective_user
    storage.remember(chat.id, user.id, name_of(user), fact)
    await update.effective_message.reply_text(
        f"🧠 Записал: «{fact}». Всего фактов: {len(storage.my_facts(chat.id, user.id))}. Посмотреть: /memory"
    )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    facts = storage.my_facts(chat.id, user.id)
    if not facts:
        await update.effective_message.reply_text("Досье пустое. Расскажи о себе: /remember факт")
        return
    await update.effective_message.reply_text("🧠 Что я про тебя помню:\n" + "\n".join(f"• {f}" for f in facts))


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    storage.forget(chat.id, user.id)
    await update.effective_message.reply_text("🧽 Забыл всё из твоего досье.")


# ---------- модерация ----------
async def _restrict(update, context, minutes: int, mute: bool) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target:
        await msg.reply_text("Ответь этой командой на сообщение участника.")
        return
    try:
        if mute:
            await context.bot.restrict_chat_member(
                chat.id, target.id,
                permissions=ChatPermissions.no_permissions(),
                until_date=int(time.time()) + minutes * 60,
            )
            await msg.reply_text(f"🔇 {name_of(target)} замьючен на {minutes} мин.")
        else:
            await context.bot.restrict_chat_member(chat.id, target.id, permissions=ChatPermissions.all_permissions())
            await msg.reply_text(f"📢 {name_of(target)} снова может писать.")
    except Exception as e:
        await msg.reply_text(f"⚠️ Не вышло: дай мне права админа с мутом. ({str(e)[:120]})")


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if chat.type == "private" or not await ensure_admin(update, context):
        await msg.reply_text("Только для админов беседы.")
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target:
        await msg.reply_text("Ответь /warn на сообщение нарушителя.")
        return
    if target.id == context.bot.id:
        await msg.reply_text("Сам себя варнить не дам 🙂")
        return
    storage.bump_warns(chat.id, target.id, name_of(target), 1)
    n = storage.get_warns(chat.id, target.id)
    if n >= 3:
        try:
            await context.bot.restrict_chat_member(
                chat.id, target.id,
                permissions=ChatPermissions.no_permissions(),
                until_date=int(time.time()) + 600,
            )
            storage.bump_warns(chat.id, target.id, name_of(target), -n)
            await msg.reply_text(f"🔇 Три предупреждения — {name_of(target)} отдыхает 10 минут.")
            return
        except Exception:
            pass
    await msg.reply_text(f"⚠️ {name_of(target)}: предупреждение {n}/3.")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private" or not await ensure_admin(update, context):
        await update.effective_message.reply_text("Только для админов беседы.")
        return
    minutes = 10
    if context.args and context.args[0].isdigit():
        minutes = max(1, min(1440, int(context.args[0])))
    await _restrict(update, context, minutes, True)


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private" or not await ensure_admin(update, context):
        await update.effective_message.reply_text("Только для админов беседы.")
        return
    await _restrict(update, context, 0, False)


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if chat.type == "private" or not await ensure_admin(update, context):
        await msg.reply_text("Только для админов.")
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target:
        await msg.reply_text("Ответь /ban на сообщение нарушителя.")
        return
    if target.id == context.bot.id:
        await msg.reply_text("Себя забанить? Нет уж 🙂")
        return
    storage.ban(chat.id, target.id)
    await msg.reply_text(f"🚫 {name_of(target)} заглушён(а): я больше отвечаю всем, кроме него/неё.")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if chat.type == "private" or not await ensure_admin(update, context):
        await msg.reply_text("Только для админов.")
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target:
        await msg.reply_text("Ответь /unban на сообщение человека.")
        return
    storage.unban(chat.id, target.id)
    await msg.reply_text(f"✅ {name_of(target)} разбанен(а).")


# ---------- weather ----------
WMO = {
    0: "Ясно ☀️", 1: "В основном ясно 🌤", 2: "Переменная облачность ⛅", 3: "Пасмурно ☁️",
    45: "Туман 🌫", 48: "Изморозь 🌫",
    51: "Морось 🌦", 53: "Морось 🌦", 55: "Сильная морось 🌦", 56: "Ледяная морось 🌧", 57: "Ледяная морось 🌧",
    61: "Дождь 🌧", 63: "Дождь 🌧", 65: "Ливень 🌧", 66: "Ледяной дождь 🌧", 67: "Ледяной дождь 🌧",
    71: "Снег ❄️", 73: "Снег ❄️", 75: "Сильный снег ❄️", 77: "Снежные зёрна ❄️",
    80: "Ливни 🌧", 81: "Ливни 🌧", 82: "Сильные ливни 🌧", 85: "Снегопад 🌨", 86: "Сильный снегопад 🌨",
    95: "Гроза ⛈", 96: "Гроза с градом ⛈", 99: "Гроза с градом ⛈",
}


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = (" ".join(context.args) if context.args else "").strip() or "Москва"
    msg = update.effective_message
    await msg.reply_text(f"Ищу погоду: {city}…")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            g = await c.get("https://geocoding-api.open-meteo.com/v1/search",
                            params={"name": city, "count": 1, "language": "ru", "format": "json"})
            g.raise_for_status()
            res = g.json().get("results") or []
            if not res:
                await msg.reply_text("Не нашёл такой город 🤷 Попробуй написать иначе.")
                return
            loc = res[0]
            w = await c.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": loc["latitude"], "longitude": loc["longitude"],
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min",
                "forecast_days": 1, "timezone": "auto",
            })
            w.raise_for_status()
            d = w.json()
        cur, daily = d["current"], d["daily"]
        wc = WMO.get(int(cur["weather_code"]), "Погода как погода")
        await msg.reply_text(
            f"🌤 {loc.get('name', city)}\n{wc}\n"
            f"🌡 Сейчас {cur['temperature_2m']}° (ощущается {cur['apparent_temperature']}°)\n"
            f"💨 Ветер {cur['wind_speed_10m']} м/с\n"
            f"📊 Сегодня: {daily['temperature_2m_min'][0]}…{daily['temperature_2m_max'][0]}°"
        )
    except Exception as e:
        await msg.reply_text(f"⚠️ Не получилось: {str(e)[:200]}")


# ---------- обработчики сообщений ----------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if user and user.is_bot:
        return
    if user and chat.type != "private" and storage.is_banned(chat.id, user.id):
        return
    text = (msg.text or "").strip()
    if not text:
        return
    s = storage.get_settings(chat.id)
    if chat.type != "private":
        if await maybe_flood(update, context, s):
            return
        if await maybe_stopword(update, context, s):
            return
    # игра «угадай число» — сначала она
    if user and chat.id in NUM_GAME and text.isdigit() and len(text) <= 3:
        _num_answer(update, context, int(text))
        return
    store_incoming(update)
    # «запомни …» → в досье (если обращаются к боту)
    if should_reply(update, s, context, text.lower()):
        rm = REMEMBER_RE.search(text)
        if rm and user:
            fact = rm.group(1).strip().lstrip(":—-").strip()
            if len(fact) >= 3:
                storage.remember(chat.id, user.id, name_of(user), fact)
                await msg.reply_text(f"🧠 Запомнил: «{fact[:200]}». Досье: /memory")
                return
        await answ(update, context, store=False)


async def on_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.message_reaction
    if r is None or not r.new_reaction or not r.user or r.user.is_bot:
        return
    weight = 0
    for rt in r.new_reaction:
        emoji = getattr(rt, "emoji", None)
        if not emoji:
            continue
        weight += 2 if emoji in ("😂", "🤣") else 1
    if weight:
        storage.daily_react(r.chat.id, r.message_id, weight)


# ---------- запуск ----------
async def post_init(app: Application):
    webapp.set_application(app)
    if app.job_queue:
        app.job_queue.run_repeating(reminders_job, interval=30, first=10)
        app.job_queue.run_repeating(daily_job, interval=300, first=90)
    else:
        log.warning("JobQueue недоступен — напоминания и сводка не заработают (поставь python-telegram-bot[job-queue])")
    me = await app.bot.get_me()
    log.info("Бот @%s запущен", me.username)
    await webapp.start()


async def post_shutdown(app: Application):
    await webapp.stop()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка обработки апдейта: %s", context.error)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Скопируй .env.example в .env и заполни (см. README.md).")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    commands = {
        "start": cmd_start, "help": cmd_help, "ask": cmd_ask, "tone": cmd_tone,
        "persona": cmd_persona, "mode": cmd_mode, "triggers": cmd_triggers,
        "stopwords": cmd_stopwords, "settings": cmd_settings, "reset": cmd_reset,
        "joke": cmd_joke, "weather": cmd_weather, "remind": cmd_remind,
        "reminders": cmd_reminders, "digest": cmd_digest, "daily": cmd_daily,
        "say": cmd_say, "voice": cmd_voice, "img": cmd_img, "news": cmd_news, "yt": cmd_yt,
        "quiz": cmd_quiz, "num": cmd_num, "dice": cmd_dice, "coin": cmd_coin, "top": cmd_top,
        "remember": cmd_remember, "memory": cmd_memory, "forget": cmd_forget,
        "warn": cmd_warn, "mute": cmd_mute, "unmute": cmd_unmute,
        "ban": cmd_ban, "unban": cmd_unban,
    }
    for name, fn in commands.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(
            (filters.ChatType.PRIVATE | filters.ChatType.GROUPS) & filters.TEXT & ~filters.COMMAND,
            on_message,
        )
    )
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    if MessageReactionHandler:
        app.add_handler(MessageReactionHandler(on_reaction))
    app.add_error_handler(on_error)
    log.info("Стартую polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
