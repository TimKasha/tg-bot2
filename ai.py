# -*- coding: utf-8 -*-
"""
«Мозг» бота: запросы к любому OpenAI-совместимому API,
расшифровка голосовых, синтез речи, генерация квизов.
"""
import base64
import datetime
import io
import json
import logging
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

AI_BASE_URL = os.getenv("AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").strip().rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
TTS_VOICE = os.getenv("TTS_VOICE", "ru-RU-DmitryNeural")

log = logging.getLogger("bot.ai")

BASE_SYSTEM = (
    "Ты — бот-собеседник в Telegram. Отвечай живо и по-человечески, как участник чата, "
    "на языке пользователя (по умолчанию — русский). Не начинай ответ с «Как ИИ…»/«Как языковая модель…» "
    "и не добавляй лишних дисклеймеров. Отвечай компактно — обычно 1–6 предложений, "
    "если не просят развёрнуто. Эмодзи — умеренно.\n"
    "В групповых чатах сообщения участников приходят в формате «Имя: текст» — ты тоже участник беседы.\n"
    "Сейчас: {now}.\n"
    "Твой характер: "
)

TONES = {
    "friendly": "Тёплый и поддерживающий друг. Подбадривай, радуйся за человека, шути мягко и добро.",
    "sarkast": "Остроумный саркаст. Любишь дружеские подколы и иронию, но уместно и без агрессии.",
    "gopnik": "Дворовый кореш: просто, по-свойски, «братан», но всегда отвечаешь по делу и с советом.",
    "mudrets": "Спокойный мудрец: вдумчиво, глубоко, иногда с образами и притчами.",
    "night": "Задушевный ночной собеседник: неспешно, внимательно, поддерживаешь любой разговор.",
}
TONE_LABELS = {
    "friendly": "Дружелюбный 🤝",
    "sarkast": "Саркастичный 😏",
    "gopnik": "Гопник 🧢",
    "mudrets": "Мудрец 🧙",
    "night": "Ночной собеседник 🌙",
}
GROUP_MODES = {"off": "выключен 🤐", "mention": "по обращению 👋", "always": "отвечает на всё 💬"}


def persona_text(s: dict) -> str:
    if s.get("persona") == "custom":
        return s.get("persona_text") or TONES["friendly"]
    return TONES.get(s.get("persona", "friendly"), TONES["friendly"])


def build_system(s: dict, chat_title: str = "", facts: str = "") -> str:
    p = BASE_SYSTEM.format(now=datetime.datetime.now().strftime("%d.%m.%Y %H:%M"))
    if chat_title:
        p += f"\nЭто групповой чат «{chat_title}»."
    if facts:
        p += f"\nЧто бот запомнил об участниках: {facts}"
    return p + persona_text(s)


async def chat(messages: list, temperature: float = 0.9, max_tokens: int = 800) -> str:
    """Базовый запрос к OpenAI-совместимому /chat/completions."""
    if not AI_API_KEY:
        return (
            "⚠️ Бот работает, но «мозг» не подключён: не задан AI_API_KEY.\n"
            "Посмотри шаг 3 в README.md — ключ берётся бесплатно за пару минут."
        )
    payload = {"model": AI_MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            f"{AI_BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {AI_API_KEY}"},
        )
        r.raise_for_status()
        data = r.json()
    try:
        content = data["choices"][0]["message"]["content"].strip()
    except Exception:
        content = ""
    if not content:
        raise RuntimeError("пустой ответ (возможно, сработал фильтр провайдера)")
    return content


async def transcribe(data: bytes, mime: str = "audio/ogg") -> str | None:
    """Расшифровка голосового. Groq (Whisper) или Google Gemini. Иначе None."""
    if not AI_API_KEY:
        return None
    if "groq" in AI_BASE_URL.lower():
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {AI_API_KEY}"},
                files={"file": ("voice.ogg", data, mime or "audio/ogg")},
                data={"model": "whisper-large-v3"},
            )
            r.raise_for_status()
            return (r.json().get("text") or "").strip()
    if "generativelanguage" in AI_BASE_URL.lower():
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{AI_MODEL}:generateContent",
                headers={"x-goog-api-key": AI_API_KEY, "Content-Type": "application/json"},
                json={
                    "contents": [{
                        "parts": [
                            {"text": "Сделай дословную расшифровку этого аудио на языке оригинала. Только текст расшифровки."},
                            {"inline_data": {"mime_type": mime or "audio/ogg", "data": base64.b64encode(data).decode()}},
                        ]
                    }]
                },
            )
            r.raise_for_status()
            jd = r.json()
            parts = jd.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip()
    return None


async def speak(text: str, voice: str = None) -> bytes:
    """Синтез речи (бесплатный Microsoft Edge TTS), возвращает mp3-байты."""
    import edge_tts

    buf = io.BytesIO()
    com = edge_tts.Communicate(text[:700], voice or TTS_VOICE)
    async for chunk in com.stream():
        if chunk.get("type") == "audio":
            buf.write(chunk["data"])
    return buf.getvalue()


QUIZ_SYSTEM = "Ты придумываешь весёлые вопросы-квизы для чата друзей. Отвечай СТРОГО одним JSON-объектом без пояснений и лишнего текста."


async def make_quiz(topic_hint: str = "") -> dict | None:
    """Генерит квиз: {"q": ..., "options": [4], "answer": 0..3} или None."""
    instr = "Придумай один лёгкий и весёлый вопрос-квиз с 4 вариантами ответа"
    if topic_hint:
        instr += f" (можно опираться на темы: {topic_hint})"
    instr += '. Верни СТРОГО JSON вида {"q":"вопрос?","options":["вариант1","вариант2","вариант3","вариант4"],"answer":0}, где answer — индекс правильного варианта (0-3).'
    try:
        raw = await chat(
            [{"role": "system", "content": QUIZ_SYSTEM}, {"role": "user", "content": instr}],
            temperature=1.1,
            max_tokens=350,
        )
    except Exception:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    q, opts, ans = d.get("q"), d.get("options"), d.get("answer")
    if not (isinstance(q, str) and isinstance(opts, list) and len(opts) == 4 and isinstance(ans, int) and 0 <= ans < 4):
        try:
            ans = int(ans)
        except Exception:
            return None
    return {"q": q.strip(), "options": [str(o).strip() for o in opts], "answer": ans}
