"""
╔══════════════════════════════════════════════════════╗
║       SavedMessages Bot  —  Elite v3.0              ║
║   Telegram Business · SQLite · Groq · Stars · Rly   ║
╚══════════════════════════════════════════════════════╝
"""
import asyncio
import logging
import os
import re
from datetime import date, timedelta
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BusinessMessagesDeleted,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from html import escape as html_escape
import database as db

# ══════════════════════════════════════════════════════
#  CONFIG  (Railway: задай переменные окружения)
# ══════════════════════════════════════════════════════
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
BOT_USERNAME = os.getenv("BOT_USERNAME", "SaveDeleteMessageTelegrambot")
GROQ_MODEL   = "meta-llama/llama-4-scout-17b-16e-instruct"  # мультимодальная, бесплатная, видит фото

# Сколько звёзд за что
PREMIUM_MONTHLY_STARS = 50
DONOR_BADGE_MIN       = 100

# ══════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

# ══════════════════════════════════════════════════════
#  BOT & DISPATCHER
# ══════════════════════════════════════════════════════
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher(storage=MemoryStorage())

# in-memory кэш истории ИИ
ai_history: dict[int, list] = {}

# Последнее уведомление (deleted/edited) для каждого owner_id
# owner_id → message_id уведомления бота
last_notify_msg: dict[int, int] = {}

# Главное меню-сообщение бота для каждого пользователя (редактируется вместо отправки нового)
# uid → message_id главного сообщения
home_msg: dict[int, int] = {}


# ══════════════════════════════════════════════════════
#  FSM
# ══════════════════════════════════════════════════════
class S(StatesGroup):
    ai_chat   = State()
    ai_search = State()


# ══════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════
LINE = "━━━━━━━━━━━━━━━━━━━━"

def ref_link(uid: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"


MEDIA_MAP = {
    "photo":      "🖼 Фото",
    "video":      "🎬 Видео",
    "audio":      "🎵 Аудио",
    "voice":      "🎤 Голосовое",
    "document":   "📄 Документ",
    "sticker":    "✨ Стикер",
    "video_note": "⭕ Кружок",
    "animation":  "🎞 GIF",
}


def premium_badge(is_prem: bool, donor: bool) -> str:
    if donor:  return "💎"
    if is_prem: return "⭐"
    return ""


def fmt_sender(from_name: str, username: str) -> str:
    """Красиво форматирует имя + username отправителя."""
    if username:
        return f"{from_name} ({username})"
    return from_name


async def _show_home(uid: int, text: str, reply_markup, target_msg: "Message | None" = None):
    """
    Показывает главное меню, редактируя уже существующее сообщение если возможно,
    иначе отправляет новое и запоминает его id.
    target_msg — сообщение пользователя (для отправки нового если нет home).
    """
    existing_id = home_msg.get(uid)
    if existing_id and target_msg:
        try:
            await bot.edit_message_text(
                text, chat_id=uid, message_id=existing_id,
                reply_markup=reply_markup, parse_mode="HTML"
            )
            return
        except Exception:
            pass  # сообщение удалено — отправим новое
    if target_msg:
        sent = await target_msg.answer(text, reply_markup=reply_markup)
    else:
        sent = await bot.send_message(uid, text, reply_markup=reply_markup)
    home_msg[uid] = sent.message_id


async def _send_notify(owner_id: int, text: str, reply_markup=None) -> Optional[int]:
    """
    Отправляет уведомление об удалённом/изменённом сообщении,
    предварительно удаляя предыдущее уведомление (чтобы не засорять чат).
    Возвращает message_id нового уведомления.
    """
    # Удаляем старое уведомление если есть
    old_id = last_notify_msg.get(owner_id)
    if old_id:
        try:
            await bot.delete_message(owner_id, old_id)
        except Exception:
            pass  # уже удалено или недоступно
        last_notify_msg.pop(owner_id, None)

    try:
        sent = await bot.send_message(owner_id, text, reply_markup=reply_markup)
        last_notify_msg[owner_id] = sent.message_id
        return sent.message_id
    except Exception as e:
        log.error(f"send notify to owner={owner_id}: {e}")
        return None


# ══════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════
def kb_main(uid: int, is_prem: bool) -> InlineKeyboardMarkup:
    rows = []
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="🛡 Панель администратора", callback_data="adm")])
    rows += [
        [
            InlineKeyboardButton(text="📋 Сохранённые",   callback_data="show_all"),
            InlineKeyboardButton(text="📊 Статистика",    callback_data="stats"),
        ],
        [
            InlineKeyboardButton(text="👥 Рефералы",      callback_data="referrals"),
            InlineKeyboardButton(text="🗑 Очистить кэш",  callback_data="clear_cache"),
        ],
    ]
    if is_prem:
        rows.append([InlineKeyboardButton(text="🔍 Поиск по кэшу", callback_data="search")])
    rows += [
        [InlineKeyboardButton(text="◈  Чат с ИИ",             callback_data="ai_open")],
        [InlineKeyboardButton(text="💝 Premium · 50⭐/мес",    callback_data="premium_info")],
        [InlineKeyboardButton(text="❓ Как подключить",        callback_data="howto")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back(target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ Назад", callback_data=f"back_{target}")]
    ])


def kb_deleted(msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Понял",           callback_data=f"ack_{msg_id}"),
            InlineKeyboardButton(text="🗑 Из кэша",         callback_data=f"del_{msg_id}"),
        ],
        [InlineKeyboardButton(text="📋 Все сохранённые",   callback_data="show_all")],
    ])


def kb_ai() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Очистить историю", callback_data="ai_clear"),
            InlineKeyboardButton(text="✕ Выйти",             callback_data="ai_exit"),
        ],
    ])


def kb_premium() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Premium · 50 звёзд/мес",  callback_data="pay_premium_50")],
        [InlineKeyboardButton(text="💎 Донат · 100 звёзд",       callback_data="pay_donate_100")],
        [InlineKeyboardButton(text="💎 Донат · 200 звёзд",       callback_data="pay_donate_200")],
        [InlineKeyboardButton(text="💎 Донат · 500 звёзд",       callback_data="pay_donate_500")],
        [InlineKeyboardButton(text="◀ Назад",                    callback_data="back_menu")],
    ])


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи",  callback_data="adm_users")],
        [InlineKeyboardButton(text="📊 Статистика",    callback_data="adm_stats")],
        [InlineKeyboardButton(text="◀ Назад",          callback_data="back_menu")],
    ])


# ══════════════════════════════════════════════════════
#  GROQ AI  (без лимитов — Groq бесплатный)
# ══════════════════════════════════════════════════════
SYSTEM_PROMPT = (
    "Ты умный ассистент внутри Telegram-бота SavedMessages. "
    "Отвечай чётко, без лишней воды. Язык — язык пользователя. "
    "Будь дружелюбным и полезным."
)


async def _get_image_base64(bot: Bot, file_id: str) -> Optional[str]:
    """Скачивает фото из Telegram и возвращает base64 строку."""
    try:
        file = await bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    import base64
                    data = await resp.read()
                    return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        log.warning(f"Image download: {e}")
    return None


async def groq_chat(uid: int, user_msg: str, image_base64: Optional[str] = None) -> str:
    """
    Отправляет сообщение в Groq.
    image_base64 — опционально, если пользователь отправил фото.
    Llama 4 Scout понимает изображения нативно.
    """
    history = ai_history.setdefault(uid, [])

    # Формируем контент текущего сообщения
    if image_base64:
        # Multimodal: текст + фото
        content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{image_base64}"
                }
            },
            {
                "type": "text",
                "text": user_msg if user_msg else "Опиши что на фото."
            }
        ]
    else:
        content = user_msg

    history.append({"role": "user", "content": content})
    # Держим последние 10 пар (история с фото весит много)
    if len(history) > 10:
        ai_history[uid] = history[-10:]
        history = ai_history[uid]

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
        "max_tokens": 2048,
        "temperature": 0.7,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                raw = await resp.text()
                try:
                    import json as _json
                    data = _json.loads(raw)
                except Exception:
                    log.error(f"Groq non-JSON (status {resp.status}): {raw[:300]}")
                    return "⚠️ ИИ временно недоступен — попробуй позже."
                if "choices" not in data:
                    log.error(f"Groq unexpected: {data}")
                    return "⚠️ ИИ вернул неожиданный ответ — попробуй ещё раз."
                reply = data["choices"][0]["message"]["content"].strip()
                # В историю кладём только текст ответа (без base64)
                ai_history[uid].append({"role": "assistant", "content": reply})
                return reply
    except asyncio.TimeoutError:
        return "⚠️ ИИ не ответил вовремя — попробуй позже."
    except Exception as e:
        log.error(f"Groq: {e}")
        return "⚠️ ИИ временно недоступен — попробуй позже."


# ══════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid   = msg.from_user.id
    name  = msg.from_user.full_name or "—"
    uname = msg.from_user.username or ""

    referrer_id: Optional[int] = None
    parts = msg.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            rid = int(parts[1][4:])
            if rid != uid:
                referrer_id = rid
        except ValueError:
            pass

    existing = await db.get_user(uid)
    await db.upsert_user(uid, uname, name, referrer_id if not existing else None)

    if not existing and referrer_id:
        try:
            await bot.send_message(
                referrer_id,
                f"🎁 <b>Новый реферал!</b>\n{LINE}\n"
                f"<b>{name}</b> присоединился по твоей ссылке 🙌",
            )
        except Exception:
            pass

    is_prem = await db.is_premium(uid)
    badge   = "⭐ " if is_prem else ""
    home_text = (
        f"👁 <b>SavedMessages Bot</b> {badge}v3.0\n{LINE}\n"
        "Твой личный детектив в <b>Telegram Business</b>.\n"
        "Перехватываю <b>все</b> удалённые и изменённые сообщения.\n\n"
        "<b>Бесплатно:</b> перехват ∞ · кэш 20 · ИИ ∞\n"
        "<b>Premium 50⭐:</b> кэш 200 · поиск по кэшу\n\n"
        f"🔗 Реферальная ссылка:\n<code>{ref_link(uid)}</code>"
    )
    await _show_home(uid, home_text, kb_main(uid, is_prem), msg)


# ══════════════════════════════════════════════════════
#  /admin
# ══════════════════════════════════════════════════════
@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        f"🛡 <b>Панель администратора</b>\n{LINE}",
        reply_markup=kb_admin(),
    )


# ══════════════════════════════════════════════════════
#  .ai КОМАНДА В БИЗНЕС-ЧАТЕ
#  Пишешь: .ai вопрос
#  Бот редактирует твоё сообщение: ⏳ → ответ + @бот
# ══════════════════════════════════════════════════════

AI_PREFIX = ".ai"  # команда (без пробела — регистр игнорируется)

async def _business_edit_message(conn_id: str, chat_id: int, msg_id: int, text: str) -> bool:
    """
    Редактирует бизнес-сообщение напрямую через Bot API (HTTP),
    т.к. aiogram 3.7 не поддерживает business_connection_id в edit_message_text.
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "business_connection_id": conn_id,
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    log.warning(f"editMessageText API error: {data.get('description')}")
                    return False
                return True
    except Exception as e:
        log.warning(f"editMessageText HTTP: {e}")
        return False


@dp.business_message(F.text.regexp(r"(?i)^\.ai\s+.+"))
async def on_ai_inline(msg: Message):
    """
    Только от владельца бизнес-аккаунта.
    Редактирует сообщение прямо в чате собеседника.
    Не попадает в кэш и не вызывает уведомление об изменении.
    """
    if not msg.business_connection_id:
        return

    try:
        conn = await bot.get_business_connection(msg.business_connection_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection (.ai): {e}")
        return

    # Реагируем только на сообщения самого владельца
    if not msg.from_user or msg.from_user.id != owner_id:
        return

    # Текст после ".ai "
    raw_text = msg.text or msg.caption or ""
    question = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""

    # Шаг 1: редактируем → "ожидание..." с мигающим эффектом (~7 сек)
    ok = await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        "⏳ Ожидание ответа..."
    )
    if not ok:
        return

    await asyncio.sleep(2)
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "⏳ Ожидание ответа..")
    await asyncio.sleep(2)
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "⏳ Ожидание ответа...")
    await asyncio.sleep(2)
    await _business_edit_message(msg.business_connection_id, msg.chat.id, msg.message_id, "⏳ Ожидание ответа..")
    await asyncio.sleep(1)

    # Шаг 2: если есть фото — скачиваем
    image_b64 = None
    if msg.photo:
        image_b64 = await _get_image_base64(bot, msg.photo[-1].file_id)

    # Шаг 3: получаем ответ (с фото или без)
    answer = await groq_chat(owner_id, question or "Опиши что на фото.", image_base64=image_b64)

    # Шаг 4: редактируем → ответ + подпись
    result_text = f"{html_escape(answer)}\n\n— @{BOT_USERNAME}"
    await _business_edit_message(
        msg.business_connection_id, msg.chat.id, msg.message_id,
        result_text
    )
    log.info(f"🤖 .ai done owner={owner_id} chat={msg.chat.id} with_photo={image_b64 is not None}")


# ══════════════════════════════════════════════════════
#  .ai КОМАНДА В ГРУППАХ И КАНАЛАХ
#  Работает когда бот добавлен в группу/канал.
#  Пишешь: .ai вопрос  — бот отвечает в том же чате.
# ══════════════════════════════════════════════════════
@dp.message(F.text.regexp(r"(?i)^\.ai\s+.+"), F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_ai_group(msg: Message):
    """
    .ai в группах/каналах — бот отвечает обычным сообщением в том же чате.
    """
    if not msg.from_user:
        return

    uid = msg.from_user.id
    raw_text = msg.text or msg.caption or ""
    question = raw_text[raw_text.index(" ") + 1:].strip() if " " in raw_text else ""
    if not question:
        return

    # Регистрируем пользователя если ещё нет
    await db.upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name or "")

    thinking = await msg.reply("⏳ Думаю...")

    image_b64 = None
    if msg.photo:
        image_b64 = await _get_image_base64(bot, msg.photo[-1].file_id)

    answer = await groq_chat(uid, question, image_base64=image_b64)

    try:
        await thinking.edit_text(f"🤖 {html_escape(answer)}")
    except Exception:
        try:
            await thinking.delete()
            await msg.reply(f"🤖 {html_escape(answer)}")
        except Exception as e:
            log.error(f"ai_group reply: {e}")

    log.info(f"🤖 .ai group chat={msg.chat.id} user={uid}")


# ══════════════════════════════════════════════════════
#  КЭШИРОВАНИЕ БИЗНЕС-СООБЩЕНИЙ
#  FIX: owner_id = business_connection_id → user.id
# ══════════════════════════════════════════════════════
@dp.business_message()
async def on_business_msg(msg: Message):
    """
    Правильный owner_id: получаем через business_connection,
    чтобы совпадал с тем, что приходит в on_deleted.
    """
    if not msg.business_connection_id:
        return

    # Не кэшируем .ai команды — они будут отредактированы в ответ ИИ
    if msg.text and msg.text.lower().startswith(".ai "):
        return

    try:
        conn = await bot.get_business_connection(msg.business_connection_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection (save): {e}")
        return

    media_type = "💬 Текст"
    file_id: Optional[str] = None
    for attr, label in MEDIA_MAP.items():
        obj = getattr(msg, attr, None)
        if obj:
            media_type = label
            file_id = obj[-1].file_id if attr == "photo" else (getattr(obj, "file_id", None))
            break

    await db.save_message(owner_id, {
        "msg_id":     msg.message_id,
        "sender_id":  msg.from_user.id if msg.from_user else None,
        "from_name":  msg.from_user.full_name if msg.from_user else "Неизвестно",
        "username":   f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
        "chat":       msg.chat.title or getattr(msg.chat, "full_name", None) or "Личные",
        "date":       msg.date.strftime("%d.%m.%Y · %H:%M"),
        "text":       msg.text or msg.caption or "",
        "media_type": media_type,
        "file_id":    file_id,
    })
    log.info(f"📥 cached msg={msg.message_id} owner={owner_id}")


# ══════════════════════════════════════════════════════
#  ИЗМЕНЁННЫЕ БИЗНЕС-СООБЩЕНИЯ
# ══════════════════════════════════════════════════════
@dp.edited_business_message()
async def on_edited_business_msg(msg: Message):
    """
    Ловим изменения сообщений.
    Если это .ai ответ бота — тихо обновляем кэш, не уведомляем.
    Если это реальное изменение — уведомляем владельца было/стало.
    """
    if not msg.business_connection_id:
        return

    try:
        conn = await bot.get_business_connection(msg.business_connection_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection (edit): {e}")
        return

    new_text = msg.text or msg.caption or ""

    # Не уведомляем об изменениях сделанных самим ботом (.ai ответы)
    is_bot_edit = (
        f"— @{BOT_USERNAME}" in new_text
        or new_text.strip() == "⏳ ИИ думает..."
    )

    # Не уведомляем если владелец редактирует своё собственное сообщение
    sender_id = msg.from_user.id if msg.from_user else None
    is_owner_edit = (sender_id == owner_id)

    if not is_bot_edit and not is_owner_edit:
        cached = await db.get_message(owner_id, msg.message_id)
        old_text = cached["text"] if cached else None
        sender = fmt_sender(
            msg.from_user.full_name if msg.from_user else "Неизвестно",
            f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
        )
        notify = (
            f"✏️ <b>Сообщение изменено</b>\n"
            f"{LINE}\n"
            f"👤 <b>{html_escape(sender)}</b>\n"
            f"💬 {html_escape(msg.chat.title or getattr(msg.chat, 'full_name', None) or 'Личные')}\n"
            f"🕐 {msg.date.strftime('%d.%m.%Y · %H:%M')}\n"
            f"{LINE}\n"
        )
        if old_text:
            notify += f"📝 <b>Было:</b>\n{html_escape(old_text)}\n\n"
        else:
            notify += "📝 <b>Было:</b> <i>не в кэше</i>\n\n"
        notify += f"📝 <b>Стало:</b>\n{html_escape(new_text)}"
        await _send_notify(owner_id, notify)

    # В любом случае обновляем кэш новым содержимым
    media_type = "💬 Текст"
    file_id: Optional[str] = None
    for attr, label in MEDIA_MAP.items():
        obj = getattr(msg, attr, None)
        if obj:
            media_type = label
            file_id = obj[-1].file_id if attr == "photo" else (getattr(obj, "file_id", None))
            break

    await db.save_message(owner_id, {
        "msg_id":     msg.message_id,
        "sender_id":  msg.from_user.id if msg.from_user else None,
        "from_name":  msg.from_user.full_name if msg.from_user else "Неизвестно",
        "username":   f"@{msg.from_user.username}" if msg.from_user and msg.from_user.username else "",
        "chat":       msg.chat.title or getattr(msg.chat, "full_name", None) or "Личные",
        "date":       msg.date.strftime("%d.%m.%Y · %H:%M"),
        "text":       new_text,
        "media_type": media_type,
        "file_id":    file_id,
    })
    log.info(f"✏️ updated msg={msg.message_id} owner={owner_id} bot_edit={is_bot_edit}")


# ══════════════════════════════════════════════════════
#  УДАЛЁННЫЕ БИЗНЕС-СООБЩЕНИЯ
# ══════════════════════════════════════════════════════
async def _send_media(owner_id: int, file_id: str, mt: str):
    try:
        if "Фото"      in mt: await bot.send_photo(owner_id, file_id)
        elif "Видео"   in mt: await bot.send_video(owner_id, file_id)
        elif "Голос"   in mt: await bot.send_voice(owner_id, file_id)
        elif "Кружок"  in mt: await bot.send_video_note(owner_id, file_id)
        elif "Документ" in mt: await bot.send_document(owner_id, file_id)
        elif "GIF"     in mt: await bot.send_animation(owner_id, file_id)
        elif "Стикер"  in mt: await bot.send_sticker(owner_id, file_id)
    except Exception as e:
        log.warning(f"Media send: {e}")


@dp.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted):
    log.info(f"🚨 deleted conn={event.business_connection_id} ids={event.message_ids}")
    try:
        conn = await bot.get_business_connection(event.business_connection_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection (delete): {e}")
        return

    for msg_id in event.message_ids:
        cached = await db.get_message(owner_id, msg_id)
        if not cached:
            log.warning(f"❓ msg={msg_id} not in cache for owner={owner_id}")
            # Отправляем даже если не в кэше — хотя бы факт удаления
            await _send_notify(
                owner_id,
                f"🗑 <b>Сообщение удалено</b>\n"
                f"{LINE}\n"
                f"⚠️ Сообщение <b>#{msg_id}</b> было удалено,\n"
                "но его не было в кэше — возможно, бот был только что подключён.",
            )
            continue

        # Не уведомляем о своих собственных удалённых сообщениях
        if cached.get("sender_id") == owner_id:
            log.info(f"⏭ skip own deleted msg={msg_id} owner={owner_id}")
            continue

        sender = fmt_sender(cached["from_name"], cached["username"])

        text = (
            f"🗑 <b>Удалённое сообщение</b>\n"
            f"{LINE}\n"
            f"👤 Пользователь <b>{sender}</b>\n"
            f"   удалил сообщение\n"
            f"{LINE}\n"
            f"💬 Чат: {cached['chat']}\n"
            f"🕐 Время: {cached['date']}\n"
            f"📦 Тип: {cached['media_type']}"
        )
        if cached["text"]:
            # Красиво оборачиваем текст
            text += f"\n{LINE}\n📝 <b>Содержимое:</b>\n{cached['text']}"

        sent_id = await _send_notify(owner_id, text, reply_markup=kb_deleted(msg_id))
        if sent_id is None:
            continue

        if cached["file_id"]:
            await _send_media(owner_id, cached["file_id"], cached["media_type"])


# ══════════════════════════════════════════════════════
#  ИИ ЧАТ  (без лимитов — Groq бесплатный)
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data == "ai_open")
async def cb_ai_open(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.ai_chat)
    await call.answer()
    await call.message.edit_text(
        f"🤖 <b>ИИ-ассистент</b>\n{LINE}\n"
        f"Модель: <b>Llama 4 Scout · Vision</b>\n"
        f"Лимит: <b>∞ (бесплатно)</b>\n\n"
        "Пиши что угодно — отвечу быстро 🚀",
        reply_markup=kb_ai(),
    )


@dp.message(S.ai_chat)
async def ai_msg(msg: Message, state: FSMContext):
    uid = msg.from_user.id

    # Принимаем текст, фото (с подписью или без), или фото + текст
    has_photo = bool(msg.photo)
    has_text  = bool(msg.text or msg.caption)

    if not has_text and not has_photo:
        await msg.answer("⚠️ Отправь текст или фото (можно фото с подписью).")
        return

    text_content = msg.text or msg.caption or ""

    thinking = await msg.answer("⏳ Думаю...")

    image_b64 = None
    if has_photo:
        # Берём лучшее качество (последний элемент)
        file_id = msg.photo[-1].file_id
        image_b64 = await _get_image_base64(bot, file_id)
        if image_b64 is None:
            await thinking.edit_text("⚠️ Не смог загрузить фото — попробуй ещё раз.")
            return

    reply = await groq_chat(uid, text_content, image_base64=image_b64)
    await thinking.delete()
    await msg.answer(f"🤖 {html_escape(reply)}", reply_markup=kb_ai())


@dp.callback_query(F.data == "ai_clear")
async def cb_ai_clear(call: CallbackQuery):
    ai_history.pop(call.from_user.id, None)
    await call.answer("🗑 История очищена", show_alert=True)


@dp.callback_query(F.data == "ai_exit")
async def cb_ai_exit(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    await call.answer()
    await call.message.edit_text(
        f"👁 <b>SavedMessages Bot</b>\n{LINE}\nГлавное меню",
        reply_markup=kb_main(uid, is_prem),
    )


# ══════════════════════════════════════════════════════
#  ПОИСК ПО КЭШУ (только premium)
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery, state: FSMContext):
    if not await db.is_premium(call.from_user.id):
        await call.answer("⭐ Поиск — только для Premium", show_alert=True)
        return
    await state.set_state(S.ai_search)
    await call.answer()
    await call.message.edit_text(
        f"🔍 <b>Поиск по кэшу</b>\n{LINE}\n"
        "Введи имя, @username или ключевое слово:",
        reply_markup=kb_back("menu"),
    )


@dp.message(S.ai_search)
async def search_msg(msg: Message, state: FSMContext):
    if not msg.text:
        return
    await state.clear()
    uid     = msg.from_user.id
    results = await db.search_messages(uid, msg.text.strip())
    if not results:
        await msg.answer(
            f"🔍 <b>Ничего не найдено</b> по «{msg.text}»",
            reply_markup=kb_back("menu"),
        )
        return
    lines = []
    for m in results[:15]:
        preview = (m["text"][:40] + "…") if len(m["text"] or "") > 40 else (m["text"] or m["media_type"])
        lines.append(f"▪ <b>{m['from_name']}</b>  {m['date']}\n   {preview}")
    is_prem = await db.is_premium(uid)
    await msg.answer(
        f"🔍 <b>Найдено: {len(results)}</b>\n{LINE}\n" + "\n\n".join(lines),
        reply_markup=kb_main(uid, is_prem),
    )


# ══════════════════════════════════════════════════════
#  ОБЩИЕ CALLBACKS
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data.startswith("back_"))
async def cb_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    await call.answer()
    await call.message.edit_text(
        f"👁 <b>SavedMessages Bot</b>\n{LINE}\nГлавное меню",
        reply_markup=kb_main(uid, is_prem),
    )


@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data == "howto")
async def cb_howto(call: CallbackQuery):
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль (Business)", callback_data="howto_profile")],
        [InlineKeyboardButton(text="📢 Группа / Канал",     callback_data="howto_group")],
        [InlineKeyboardButton(text="◀ Назад",               callback_data="back_menu")],
    ])
    await call.message.edit_text(
        f"❓ <b>Как подключить бота?</b>\n{LINE}\n"
        "Выбери тип подключения:",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "howto_profile")
async def cb_howto_profile(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"👤 <b>Подключение к профилю (Business)</b>\n{LINE}\n"
        "Для этого нужен <b>Telegram Business</b> (платная подписка).\n\n"
        "1️⃣ Открой <b>Настройки Telegram</b>\n"
        "2️⃣ Перейди в <b>Telegram Business</b>\n"
        "3️⃣ Нажми <b>Автоматизация чатов</b>\n"
        f"4️⃣ Выбери <code>@{BOT_USERNAME}</code>\n"
        "5️⃣ Включи <b>Доступ к сообщениям</b>\n"
        f"{LINE}\n"
        "✅ Бот перехватывает <b>все</b> удалённые\n"
        "и изменённые сообщения в твоих личных чатах.\n\n"
        "💡 <i>Твои собственные удалённые сообщения\n"
        "бот не присылает — только чужие.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ Назад", callback_data="howto")],
        ]),
    )


@dp.callback_query(F.data == "howto_group")
async def cb_howto_group(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"📢 <b>Подключение к группе / каналу</b>\n{LINE}\n"
        "Бот работает бесплатно — Telegram Business не нужен!\n\n"
        "1️⃣ Добавь <code>@{BOT_USERNAME}</code> в группу или канал\n"
        "2️⃣ Дай боту права <b>Администратора</b>\n"
        "   (нужно: читать сообщения)\n"
        "3️⃣ Для групп: отключи Privacy Mode через\n"
        "   @BotFather → /setprivacy → Disabled\n"
        f"{LINE}\n"
        "✅ Готово! Теперь в группе/канале можно\n"
        "писать <code>.ai вопрос</code> — бот ответит прямо там.\n\n"
        "💡 <i>Пример: </i><code>.ai объясни квантовую физику</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ Назад", callback_data="howto")],
        ]),
    )


@dp.callback_query(F.data == "referrals")
async def cb_referrals(call: CallbackQuery):
    uid  = call.from_user.id
    refs = await db.count_referrals(uid)
    await call.answer()
    await call.message.edit_text(
        f"👥 <b>Реферальная программа</b>\n{LINE}\n"
        f"Пригласи друга — помоги проекту расти!\n\n"
        f"🔗 Твоя ссылка:\n<code>{ref_link(uid)}</code>\n\n"
        f"🤝 Приглашено: <b>{refs}</b>\n\n"
        "Бот бесплатен для всех — рефералы помогают\n"
        "развивать проект и снижать серверные расходы.",
        reply_markup=kb_back("menu"),
    )


@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    cached  = await db.count_messages(uid)
    refs    = await db.count_referrals(uid)
    user    = await db.get_user(uid)
    badge   = premium_badge(is_prem, bool(user and user.get("donor_badge")))
    prem_txt = user["premium_until"] if user and user.get("premium_until") else "нет"

    await call.answer()
    await call.message.edit_text(
        f"📊 <b>Твоя статистика</b> {badge}\n{LINE}\n"
        f"💾 В кэше:       <b>{cached}</b>\n"
        f"👥 Рефералов:    <b>{refs}</b>\n"
        f"🤖 ИИ:           <b>∞ (бесплатно)</b>\n"
        f"⭐ Premium до:   <b>{prem_txt}</b>\n"
        f"{LINE}\n"
        f"Лимит кэша: {'200 (premium)' if is_prem else '20 (free)'}",
        reply_markup=kb_main(uid, is_prem),
    )


@dp.callback_query(F.data == "clear_cache")
async def cb_clear(call: CallbackQuery):
    count = await db.clear_messages(call.from_user.id)
    await call.answer(f"🗑 Удалено {count} записей", show_alert=True)


@dp.callback_query(F.data == "show_all")
async def cb_show_all(call: CallbackQuery):
    uid      = call.from_user.id
    messages = await db.get_recent_messages(uid, 20)
    if not messages:
        await call.answer("📋 Кэш пуст", show_alert=True)
        return
    is_prem = await db.is_premium(uid)
    lines = []
    for m in messages:
        preview = (m["text"][:40] + "…") if len(m["text"] or "") > 40 else (m["text"] or m["media_type"])
        lines.append(f"▪ <b>{m['from_name']}</b>  {m['date']}\n   {preview}")
    await call.answer()
    await call.message.edit_text(
        f"📋 <b>Последние {len(messages)} сообщений</b>\n{LINE}\n" + "\n\n".join(lines),
        reply_markup=kb_main(uid, is_prem),
    )


@dp.callback_query(F.data.startswith("ack_"))
async def cb_ack(call: CallbackQuery):
    await call.answer("✅ Принято")
    await call.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data.startswith("del_"))
async def cb_del(call: CallbackQuery):
    msg_id = int(call.data.split("_")[1])
    await db.delete_message(call.from_user.id, msg_id)
    await call.answer("🗑 Удалено из кэша")
    await call.message.edit_reply_markup(reply_markup=None)


# ══════════════════════════════════════════════════════
#  PREMIUM & DONATES
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data == "premium_info")
async def cb_premium_info(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"⭐ <b>Premium — что даёт?</b>\n{LINE}\n"
        "🆓 <b>Бесплатно навсегда:</b>\n"
        "  • Перехват удалённых и изменённых — ∞\n"
        "  • Кэш: 20 сообщений\n"
        "  • ИИ: безлимитно\n\n"
        "⭐ <b>Premium · 50 звёзд/месяц:</b>\n"
        "  • Кэш: 200 сообщений\n"
        "  • Поиск по всему кэшу\n\n"
        "💎 <b>Донат 100⭐+ (единоразово):</b>\n"
        "  • Значок 💎 в статистике\n"
        "  • +30 дней Premium в подарок\n"
        "  • Моя искренняя благодарность 🙏",
        reply_markup=kb_premium(),
    )


@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay(call: CallbackQuery):
    parts = call.data.split("_")
    kind  = parts[1]
    stars = int(parts[2])

    if kind == "premium":
        title       = "⭐ Premium · 1 месяц"
        description = "Premium доступ к SavedMessages Bot на 30 дней"
    else:
        title       = f"💎 Донат {stars}⭐"
        description = f"Поддержка проекта SavedMessages Bot — {stars} звёзд"

    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=title,
        description=description,
        payload=f"{kind}_{stars}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)],
    )


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_payment(msg: Message):
    uid     = msg.from_user.id
    stars   = msg.successful_payment.total_amount
    payload = msg.successful_payment.invoice_payload

    await db.save_payment(uid, stars, payload)
    kind = payload.split("_")[0]

    if kind == "premium":
        user    = await db.get_user(uid)
        current = user["premium_until"] if user and user.get("premium_until") else None
        if current and date.fromisoformat(current) >= date.today():
            new_date = date.fromisoformat(current) + timedelta(days=30)
        else:
            new_date = date.today() + timedelta(days=30)
        await db.set_premium(uid, new_date)
        text = (
            f"⭐ <b>Premium активирован!</b>\n{LINE}\n"
            f"Действует до: <b>{new_date.strftime('%d.%m.%Y')}</b>\n"
            "Кэш расширен до 200 · Поиск включён."
        )
    else:
        if stars >= DONOR_BADGE_MIN:
            await db.set_donor_badge(uid)
            bonus_date = date.today() + timedelta(days=30)
            await db.set_premium(uid, bonus_date)
            text = (
                f"💎 <b>Спасибо за поддержку!</b>\n{LINE}\n"
                f"Ты отправил <b>{stars}⭐</b>\n"
                f"Значок донатера: 💎\n"
                f"Premium в подарок до: <b>{bonus_date.strftime('%d.%m.%Y')}</b>"
            )
        else:
            text = (
                f"💝 <b>Огромное спасибо!</b>\n{LINE}\n"
                f"Ты поддержал проект на <b>{stars}⭐</b>\n"
                "Эти средства идут на серверы и развитие 🚀"
            )

    is_prem = await db.is_premium(uid)
    await msg.answer(text, reply_markup=kb_main(uid, is_prem))

    try:
        await bot.send_message(
            ADMIN_ID,
            f"💰 <b>Оплата</b> · {payload}\n"
            f"👤 {msg.from_user.full_name} (ID: {uid})\n"
            f"⭐ {stars} звёзд",
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  ADMIN CALLBACKS
# ══════════════════════════════════════════════════════
def _is_admin(call: CallbackQuery) -> bool:
    return call.from_user.id == ADMIN_ID


@dp.callback_query(F.data == "adm")
async def cb_adm(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call):
        await call.answer("⛔", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await call.message.edit_text(
        f"🛡 <b>Панель администратора</b>\n{LINE}",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "adm_users")
async def cb_adm_users(call: CallbackQuery):
    if not _is_admin(call): return
    ids   = await db.all_user_ids()
    await call.answer()
    await call.message.edit_text(
        f"👥 <b>Пользователи</b>\n{LINE}\n"
        f"Всего: <b>{len(ids)}</b>",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery):
    if not _is_admin(call): return
    users   = await db.count_users()
    msgs    = await db.total_messages_all()
    stars   = await db.total_stars()
    await call.answer()
    await call.message.edit_text(
        f"📊 <b>Общая статистика</b>\n{LINE}\n"
        f"👥 Пользователей:  <b>{users}</b>\n"
        f"💾 Сообщений в БД: <b>{msgs}</b>\n"
        f"⭐ Всего звёзд:    <b>{stars}</b>",
        reply_markup=kb_admin(),
    )


# ══════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════
async def main():
    await db.init_db()
    log.info("🚀 SavedMessages Bot v3.0 запускается...")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✅ <b>Бот запущен</b> · v3.0 · SQLite · Railway\n"
            f"🤖 Модель: Llama 4 Scout (Vision)"
        )
    except Exception:
        pass
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
