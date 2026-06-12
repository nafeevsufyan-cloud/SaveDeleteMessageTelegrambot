"""
╔══════════════════════════════════════════════════════╗
║       SavedMessages Bot  —  Elite v2.0              ║
║   Telegram Business · SQLite · Groq · Stars · Rly   ║
╚══════════════════════════════════════════════════════╝
"""
import asyncio
import logging
import os
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

import database as db

# ══════════════════════════════════════════════════════
#  CONFIG  (Railway: задай переменные окружения)
# ══════════════════════════════════════════════════════
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
BOT_USERNAME = os.getenv("BOT_USERNAME", "SaveDeleteMessageTelegrambot")
GROQ_MODEL   = "llama-3.1-8b-instant"

# Сколько звёзд за что
PREMIUM_MONTHLY_STARS = 50   # 1 месяц premium
DONOR_BADGE_MIN       = 100  # минимум звёзд для значка + premium

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

# in-memory кэш истории ИИ (не нужно в БД, сбрасывается при рестарте)
ai_history: dict[int, list] = {}


# ══════════════════════════════════════════════════════
#  FSM
# ══════════════════════════════════════════════════════
class S(StatesGroup):
    ai_chat    = State()
    ai_search  = State()   # поиск по кэшу (premium)


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
    if donor:
        return "💎"
    if is_prem:
        return "⭐"
    return ""


# ══════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════
def kb_main(uid: int, is_prem: bool) -> InlineKeyboardMarkup:
    rows = []
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="🛡 Панель администратора", callback_data="adm")])
    rows += [
        [
            InlineKeyboardButton(text="📋 Сохранённые",  callback_data="show_all"),
            InlineKeyboardButton(text="📊 Статистика",   callback_data="stats"),
        ],
        [
            InlineKeyboardButton(text="👥 Рефералы",    callback_data="referrals"),
            InlineKeyboardButton(text="🗑 Очистить кэш", callback_data="clear_cache"),
        ],
    ]
    if is_prem:
        rows.append([InlineKeyboardButton(text="🔍 Поиск по кэшу", callback_data="search")])
    rows += [
        [InlineKeyboardButton(text="◈  Чат с ИИ", callback_data="ai_open")],
        [InlineKeyboardButton(text="💝 Premium · 50⭐/мес", callback_data="premium_info")],
        [InlineKeyboardButton(text="❓ Как подключить", callback_data="howto")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back(target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ Назад", callback_data=f"back_{target}")]
    ])


def kb_deleted(msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Понял",     callback_data=f"ack_{msg_id}"),
            InlineKeyboardButton(text="🗑 Из кэша",   callback_data=f"del_{msg_id}"),
        ],
        [InlineKeyboardButton(text="📋 Все сохранённые", callback_data="show_all")],
    ])


def kb_ai(calls_left: int | str) -> InlineKeyboardMarkup:
    label = f"Запросов сегодня: {calls_left}" if isinstance(calls_left, int) else calls_left
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💬 {label}", callback_data="noop")],
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
#  GROQ AI
# ══════════════════════════════════════════════════════
SYSTEM_PROMPT = (
    "Ты умный ассистент внутри Telegram-бота SavedMessages. "
    "Отвечай чётко, без лишней воды. Язык — язык пользователя."
)


async def groq_chat(uid: int, user_msg: str) -> str:
    history = ai_history.setdefault(uid, [])
    history.append({"role": "user", "content": user_msg})
    if len(history) > 14:
        ai_history[uid] = history[-14:]
        history = ai_history[uid]

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
        "max_tokens": 1024,
        "temperature": 0.7,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                reply = data["choices"][0]["message"]["content"].strip()
                ai_history[uid].append({"role": "assistant", "content": reply})
                return reply
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

    # Регистрация / обновление
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

    # Уведомление рефереру
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
    await msg.answer(
        f"👁 <b>SavedMessages Bot</b> {badge}\n{LINE}\n"
        "Твой личный детектив в <b>Telegram Business</b>.\n"
        "Перехватываю <b>все</b> удалённые сообщения.\n\n"
        "<b>Бесплатно:</b> перехват ∞ · кэш 20 · ИИ 5/день\n"
        "<b>Premium 50⭐:</b> кэш 200 · ИИ ∞ · поиск по кэшу\n\n"
        f"🔗 Реферальная ссылка:\n<code>{ref_link(uid)}</code>",
        reply_markup=kb_main(uid, is_prem),
    )


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
#  КЭШИРОВАНИЕ БИЗНЕС-СООБЩЕНИЙ
# ══════════════════════════════════════════════════════
@dp.business_message()
async def on_business_msg(msg: Message):
    owner_id = msg.chat.id
    if not owner_id:
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
#  УДАЛЁННЫЕ БИЗНЕС-СООБЩЕНИЯ
# ══════════════════════════════════════════════════════
async def _send_media(owner_id: int, file_id: str, mt: str):
    try:
        if "Фото"     in mt: await bot.send_photo(owner_id, file_id)
        elif "Видео"  in mt: await bot.send_video(owner_id, file_id)
        elif "Голос"  in mt: await bot.send_voice(owner_id, file_id)
        elif "Кружок" in mt: await bot.send_video_note(owner_id, file_id)
        elif "Документ" in mt: await bot.send_document(owner_id, file_id)
        elif "GIF"    in mt: await bot.send_animation(owner_id, file_id)
    except Exception as e:
        log.warning(f"Media: {e}")


@dp.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted):
    log.info(f"🚨 deleted conn={event.business_connection_id} ids={event.message_ids}")
    try:
        conn = await bot.get_business_connection(event.business_connection_id)
        owner_id = conn.user.id
    except Exception as e:
        log.error(f"get_business_connection: {e}")
        return

    for msg_id in event.message_ids:
        cached = await db.get_message(owner_id, msg_id)
        if not cached:
            log.warning(f"❓ msg={msg_id} not in cache for owner={owner_id}")
            continue

        text = (
            "🗑 <b>Удалённое сообщение</b>\n"
            f"{LINE}\n"
            f"👤 <b>{cached['from_name']}</b> {cached['username']}\n"
            f"💬 {cached['chat']}\n"
            f"🕐 {cached['date']}\n"
            f"📦 {cached['media_type']}"
        )
        if cached["text"]:
            text += f"\n{LINE}\n📝 {cached['text']}"

        try:
            await bot.send_message(owner_id, text, reply_markup=kb_deleted(msg_id))
        except Exception as e:
            log.error(f"send to owner={owner_id}: {e}")
            continue

        if cached["file_id"]:
            await _send_media(owner_id, cached["file_id"], cached["media_type"])


# ══════════════════════════════════════════════════════
#  ИИ ЧАТ
# ══════════════════════════════════════════════════════
@dp.callback_query(F.data == "ai_open")
async def cb_ai_open(call: CallbackQuery, state: FSMContext):
    uid     = call.from_user.id
    is_prem = await db.is_premium(uid)
    left    = await db.ai_calls_left(uid)
    label   = "∞ (Premium)" if is_prem else f"{left}/{db.FREE_AI_LIMIT} сегодня"

    await state.set_state(S.ai_chat)
    await call.answer()
    await call.message.edit_text(
        f"🤖 <b>ИИ-ассистент</b>\n{LINE}\n"
        f"Модель: <b>Llama 3.1 · 8B Instant</b>\n"
        f"Лимит: <b>{label}</b>\n"
        "Пиши что угодно — отвечу быстро.",
        reply_markup=kb_ai(label),
    )


@dp.message(S.ai_chat)
async def ai_msg(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if not msg.text:
        await msg.answer("⚠️ Отправь текстовое сообщение.")
        return

    if not await db.ai_allowed(uid):
        is_prem = await db.is_premium(uid)
        await msg.answer(
            f"⛔ <b>Лимит исчерпан</b>\n{LINE}\n"
            f"Бесплатно: {db.FREE_AI_LIMIT} запросов/день\n"
            "Купи <b>Premium 50⭐</b> — лимит станет безлимитным.",
            reply_markup=kb_premium(),
        )
        return

    thinking = await msg.answer("⏳")
    await db.increment_ai_calls(uid)
    reply = await groq_chat(uid, msg.text)
    await thinking.delete()

    left  = await db.ai_calls_left(uid)
    is_prem = await db.is_premium(uid)
    label = "∞ (Premium)" if is_prem else f"{left}/{db.FREE_AI_LIMIT} сегодня"
    await msg.answer(f"🤖 {reply}", reply_markup=kb_ai(label))


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
        "Введи имя, @username или ключевое слово из текста:",
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
        preview = (m["text"][:40] + "…") if len(m["text"]) > 40 else m["text"] or m["media_type"]
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
    await call.message.edit_text(
        f"📌 <b>Подключение за 2 минуты</b>\n{LINE}\n"
        "1️⃣ Открой <b>Настройки Telegram</b>\n"
        "2️⃣ Перейди в <b>Telegram Business</b>\n"
        "3️⃣ Нажми <b>Автоматизация чатов</b>\n"
        f"4️⃣ Выбери <code>@{BOT_USERNAME}</code>\n{LINE}\n"
        "✅ Готово! Бот перехватывает удалённые сообщения.",
        reply_markup=kb_back("menu"),
    )


@dp.callback_query(F.data == "referrals")
async def cb_referrals(call: CallbackQuery):
    uid  = call.from_user.id
    refs = await db.count_referrals(uid)
    await call.answer()
    await call.message.edit_text(
        f"👥 <b>Реферальная программа</b>\n{LINE}\n"
        f"Пригласи друга!\n\n"
        f"🔗 Твоя ссылка:\n<code>{ref_link(uid)}</code>\n\n"
        f"🤝 Приглашено: <b>{refs}</b>\n\n"
        "Бот бесплатен для всех — твои рефералы помогают\n"
        "развивать проект и снижать серверные расходы.",
        reply_markup=kb_back("menu"),
    )


@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    uid      = call.from_user.id
    is_prem  = await db.is_premium(uid)
    cached   = await db.count_messages(uid)
    refs     = await db.count_referrals(uid)
    left     = await db.ai_calls_left(uid)
    user     = await db.get_user(uid)
    badge    = premium_badge(is_prem, bool(user and user.get("donor_badge")))
    prem_txt = user["premium_until"] if user and user.get("premium_until") else "нет"

    await call.answer()
    await call.message.edit_text(
        f"📊 <b>Твоя статистика</b> {badge}\n{LINE}\n"
        f"💾 В кэше:          <b>{cached}</b>\n"
        f"👥 Рефералов:       <b>{refs}</b>\n"
        f"🤖 ИИ сегодня:      <b>{left if not is_prem else '∞'}</b>\n"
        f"⭐ Premium до:       <b>{prem_txt}</b>\n"
        f"{LINE}\n"
        f"Лимит кэша:  {'200 (premium)' if is_prem else '20 (free)'}\n"
        f"Лимит ИИ:   {'∞ (premium)' if is_prem else f'{db.FREE_AI_LIMIT}/день (free)'}",
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
        preview = (m["text"][:40] + "…") if len(m["text"]) > 40 else m["text"] or m["media_type"]
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
        "  • Перехват удалённых — безлимитно\n"
        "  • Кэш: 20 сообщений\n"
        "  • ИИ: 5 запросов в день\n\n"
        "⭐ <b>Premium · 50 звёзд/месяц:</b>\n"
        "  • Кэш: 200 сообщений\n"
        "  • ИИ: безлимитно\n"
        "  • Поиск по всему кэшу\n\n"
        "💎 <b>Донат 100⭐+ (единоразово):</b>\n"
        "  • Значок 💎 в статистике\n"
        "  • +30 дней Premium в подарок\n"
        "  • Моя искренняя благодарность 🙏",
        reply_markup=kb_premium(),
    )


@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay(call: CallbackQuery):
    parts = call.data.split("_")  # pay_premium_50 или pay_donate_100
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
    payload = msg.successful_payment.invoice_payload  # premium_50 / donate_100

    await db.save_payment(uid, stars, payload)
    kind = payload.split("_")[0]

    if kind == "premium":
        # Продляем (или устанавливаем) premium
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
            "Кэш расширен до 200 · ИИ без лимита · Поиск включён."
        )
    else:
        # Донат
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
    total = len(ids)
    await call.answer()
    await call.message.edit_text(
        f"👥 <b>Пользователи</b>\n{LINE}\n"
        f"Всего: <b>{total}</b>",
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
    log.info("🚀 SavedMessages Bot v2.0 запускается...")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✅ <b>Бот запущен</b> · v2.0 · SQLite · Railway"
        )
    except Exception:
        pass
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
