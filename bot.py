import asyncio
import logging
import os
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, BusinessMessagesDeleted,
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, LabeledPrice, PreCheckoutQuery,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN    = "7793443906:AAEne93-Nc6bRfLJPQbwu1WlifjvJA3tnQg"
ADMIN_ID     = 5907310974
GROQ_API_KEY = "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"

PREMIUM_STARS = 50
AI_STARS      = 5
# ===================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


class AIChat(StatesGroup):
    chatting = State()


# ---------------------------------------------------
# Хранилище
# ---------------------------------------------------
user_cache:    dict[int, dict[int, dict]] = {}
user_stats:    dict[int, dict]            = {}
premium_users: set[int]                   = set()
ai_users:      set[int]                   = set()
registered:    set[int]                   = set()
ai_history:    dict[int, list]            = {}


def get_cache(uid): 
    user_cache.setdefault(uid, {})
    return user_cache[uid]

def get_stats(uid):
    user_stats.setdefault(uid, {"cached": 0, "deleted": 0})
    return user_stats[uid]

def is_premium(uid): return uid in premium_users or uid == ADMIN_ID
def has_ai(uid):     return uid in ai_users or uid == ADMIN_ID


# ---------------------------------------------------
# Groq API
# ---------------------------------------------------
async def ask_groq(user_id: int, user_message: str) -> str:
    history = ai_history.setdefault(user_id, [])
    history.append({"role": "user", "content": user_message})
    
    if len(history) > 10:
        history = history[-10:]
        ai_history[user_id] = history

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": "Ты полезный ассистент. Отвечай кратко и по делу на языке пользователя."}
        ] + history,
        "max_tokens": 1024,
        "temperature": 0.7
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                data = await resp.json()
                reply = data["choices"][0]["message"]["content"]
                ai_history[user_id].append({"role": "assistant", "content": reply})
                return reply
    except Exception as e:
        log.error(f"Groq error: {e}")
        return "⚠️ ИИ временно недоступен, попробуй позже."


# ---------------------------------------------------
# Клавиатуры
# ---------------------------------------------------
def main_keyboard(uid: int) -> InlineKeyboardMarkup:
    ai_btn = (
        InlineKeyboardButton(text="🤖 Чат с ИИ", callback_data="open_ai")
        if has_ai(uid) else
        InlineKeyboardButton(text=f"🤖 Открыть ИИ ({AI_STARS}⭐)", callback_data="buy_ai")
    )
    premium_btn = (
        InlineKeyboardButton(text="⭐ Premium активен", callback_data="premium_info")
        if is_premium(uid) else
        InlineKeyboardButton(text=f"🔓 Premium ({PREMIUM_STARS}⭐)", callback_data="buy_premium")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Сохранённые", callback_data="show_all"),
         InlineKeyboardButton(text="📊 Статистика",  callback_data="stats")],
        [InlineKeyboardButton(text="🗑 Очистить кэш", callback_data="clear_cache")],
        [ai_btn],
        [premium_btn],
        [InlineKeyboardButton(text="❓ Как подключить", callback_data="howto")],
    ])


def deleted_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Понял",          callback_data=f"ack_{msg_id}"),
            InlineKeyboardButton(text="🗑 Убрать из кэша", callback_data=f"del_{msg_id}"),
        ],
        [InlineKeyboardButton(text="📋 Все сохранённые", callback_data="show_all")],
    ])


def ai_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Очистить историю", callback_data="clear_ai_history")],
        [InlineKeyboardButton(text="❌ Выйти из чата",    callback_data="exit_ai")],
    ])


# ---------------------------------------------------
# /start
# ---------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    registered.add(uid)
    await message.answer(
        "👁 <b>SavedMessages Bot</b>\n\n"
        "Сохраняю удалённые сообщения и даю доступ к ИИ-ассистенту.\n\n"
        "📌 Подключи: <b>Настройки → Business → Автоматизация чатов</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(uid)
    )


# ---------------------------------------------------
# /admin
# ---------------------------------------------------
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    total_cached = sum(len(v) for v in user_cache.values())
    await message.answer(
        f"🛠 <b>Админ-панель</b>\n\n"
        f"👥 Всего пользователей: {len(registered)}\n"
        f"⭐ Premium: {len(premium_users)}\n"
        f"🤖 ИИ доступ: {len(ai_users)}\n"
        f"💾 В кэше всего: {total_cached}",
        parse_mode="HTML"
    )


# ---------------------------------------------------
# Кэш бизнес-сообщений
# ---------------------------------------------------
@dp.business_message()
async def cache_message(message: Message):
    owner_id = message.chat.id
    if not owner_id:
        return

    media_type = (
        "фото"      if message.photo      else
        "видео"     if message.video      else
        "аудио"     if message.audio      else
        "голосовое" if message.voice      else
        "документ"  if message.document   else
        "стикер"    if message.sticker    else
        "кружок"    if message.video_note else
        "текст"
    )
    file_id = None
    if message.photo:        file_id = message.photo[-1].file_id
    elif message.video:      file_id = message.video.file_id
    elif message.voice:      file_id = message.voice.file_id
    elif message.video_note: file_id = message.video_note.file_id
    elif message.document:   file_id = message.document.file_id

    get_cache(owner_id)[message.message_id] = {
        "from_name":  message.from_user.full_name if message.from_user else "Неизвестно",
        "username":   f"@{message.from_user.username}" if message.from_user and message.from_user.username else "",
        "chat":       message.chat.title or message.chat.full_name or "Личка",
        "date":       message.date.strftime('%d.%m.%Y %H:%M'),
        "text":       message.text or message.caption or "",
        "media_type": media_type,
        "file_id":    file_id,
    }
    get_stats(owner_id)["cached"] += 1


# ---------------------------------------------------
# Удаление
# ---------------------------------------------------
@dp.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted):
    try:
        conn = await bot.get_business_connection(event.business_connection_id)
        owner_id = conn.user.id
    except Exception:
        return

    get_stats(owner_id)["deleted"] += len(event.message_ids)
    cache = get_cache(owner_id)

    for msg_id in event.message_ids:
        cached = cache.get(msg_id)
        if cached:
            text = (
                f"🗑 <b>Удалённое сообщение!</b>\n\n"
                f"👤 {cached['from_name']} {cached['username']}\n"
                f"💬 {cached['chat']} | 🕐 {cached['date']}\n"
                f"📦 {cached['media_type']}"
            )
            if cached["text"]:
                text += f"\n\n📝 {cached['text']}"

            await bot.send_message(owner_id, text, parse_mode="HTML",
                                   reply_markup=deleted_keyboard(msg_id))

            if cached["file_id"]:
                if is_premium(owner_id):
                    try:
                        mt = cached["media_type"]
                        if mt == "фото":        await bot.send_photo(owner_id, cached["file_id"])
                        elif mt == "видео":     await bot.send_video(owner_id, cached["file_id"])
                        elif mt == "голосовое": await bot.send_voice(owner_id, cached["file_id"])
                        elif mt == "кружок":    await bot.send_video_note(owner_id, cached["file_id"])
                        elif mt == "документ":  await bot.send_document(owner_id, cached["file_id"])
                    except Exception as e:
                        log.warning(f"Медиа: {e}")
                else:
                    await bot.send_message(
                        owner_id,
                        f"🔒 Медиафайл — только в <b>Premium</b> ({PREMIUM_STARS}⭐)",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="⭐ Купить Premium", callback_data="buy_premium")
                        ]])
                    )
        else:
            await bot.send_message(owner_id, f"🗑 Удалено (ID: {msg_id}) — не было в кэше")


# ---------------------------------------------------
# ИИ — открыть чат
# ---------------------------------------------------
@dp.callback_query(F.data == "open_ai")
async def cb_open_ai(call: CallbackQuery, state: FSMContext):
    if not has_ai(call.from_user.id):
        await call.answer("Сначала купи доступ к ИИ!", show_alert=True)
        return
    await state.set_state(AIChat.chatting)
    await call.answer()
    await call.message.answer(
        "🤖 <b>ИИ-ассистент активен!</b>\n\n"
        "Просто напиши мне что угодно — я отвечу.\n"
        "История диалога сохраняется.\n\n"
        "Для выхода нажми кнопку ниже.",
        parse_mode="HTML",
        reply_markup=ai_keyboard()
    )


@dp.message(AIChat.chatting)
async def ai_chat_handler(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not has_ai(uid):
        await state.clear()
        return

    thinking = await message.answer("🤖 Думаю...")
    reply = await ask_groq(uid, message.text or "")
    await thinking.delete()
    await message.answer(f"🤖 {reply}", reply_markup=ai_keyboard())


@dp.callback_query(F.data == "clear_ai_history")
async def cb_clear_ai(call: CallbackQuery):
    ai_history.pop(call.from_user.id, None)
    await call.answer("🗑 История очищена!", show_alert=True)


@dp.callback_query(F.data == "exit_ai")
async def cb_exit_ai(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Вышел из чата с ИИ")
    await call.message.answer("Вернулся в главное меню:", reply_markup=main_keyboard(call.from_user.id))


# ---------------------------------------------------
# Callbacks общие
# ---------------------------------------------------
@dp.callback_query(F.data.startswith("ack_"))
async def cb_ack(call: CallbackQuery):
    await call.answer("✅")
    await call.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data.startswith("del_"))
async def cb_del(call: CallbackQuery):
    msg_id = int(call.data.split("_")[1])
    get_cache(call.from_user.id).pop(msg_id, None)
    await call.answer("🗑 Убрано")
    await call.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data == "show_all")
async def cb_show_all(call: CallbackQuery):
    cache = get_cache(call.from_user.id)
    if not cache:
        await call.answer("Кэш пуст!", show_alert=True)
        return
    await call.answer()
    lines = []
    for mid, m in list(cache.items())[-20:]:
        preview = (m["text"][:40] + "…") if len(m["text"]) > 40 else m["text"] or f"[{m['media_type']}]"
        lines.append(f"• <b>{m['from_name']}</b> | {m['date']}\n  {preview}")
    await call.message.answer(
        "📋 <b>Последние 20 сообщений:</b>\n\n" + "\n\n".join(lines),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    await call.answer()
    s = get_stats(call.from_user.id)
    await call.message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"📥 Закэшировано: {s['cached']}\n"
        f"🗑 Удалений поймано: {s['deleted']}\n"
        f"💾 В кэше: {len(get_cache(call.from_user.id))}\n"
        f"{'⭐ Premium' if is_premium(call.from_user.id) else '🔒 Базовый'} | "
        f"{'🤖 ИИ активен' if has_ai(call.from_user.id) else '🤖 ИИ не куплен'}",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "clear_cache")
async def cb_clear(call: CallbackQuery):
    cache = get_cache(call.from_user.id)
    count = len(cache)
    cache.clear()
    await call.answer(f"🗑 Очищено {count}", show_alert=True)


@dp.callback_query(F.data == "howto")
async def cb_howto(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "📌 <b>Подключение:</b>\n\n"
        "1. Настройки Telegram\n"
        "2. Telegram Business\n"
        "3. Автоматизация чатов\n"
        "4. Выбери этого бота\n\n"
        "Готово! Теперь я слежу за твоими чатами.",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "premium_info")
async def cb_premium_info(call: CallbackQuery):
    await call.answer("⭐ Premium активен!", show_alert=True)


# ---------------------------------------------------
# Покупки
# ---------------------------------------------------
@dp.callback_query(F.data == "buy_ai")
async def cb_buy_ai(call: CallbackQuery):
    if has_ai(call.from_user.id):
        await call.answer("🤖 ИИ уже активен!", show_alert=True)
        return
    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="🤖 ИИ-ассистент",
        description="Общайся с умным ИИ прямо в боте. Модель Llama 3 — быстро и умно!",
        payload="ai_purchase",
        currency="XTR",
        prices=[LabeledPrice(label="ИИ доступ", amount=AI_STARS)],
    )


@dp.callback_query(F.data == "buy_premium")
async def cb_buy_premium(call: CallbackQuery):
    if is_premium(call.from_user.id):
        await call.answer("⭐ Premium уже есть!", show_alert=True)
        return
    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="⭐ Premium",
        description="Сохранение фото, видео, голосовых из удалённых сообщений.",
        payload="premium_purchase",
        currency="XTR",
        prices=[LabeledPrice(label="Premium", amount=PREMIUM_STARS)],
    )


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_payment(message: Message):
    uid = message.from_user.id
    payload = message.successful_payment.invoice_payload

    if payload == "ai_purchase":
        ai_users.add(uid)
        await message.answer(
            "🤖 <b>ИИ-ассистент активирован!</b>\n\nНажми кнопку «Чат с ИИ» в меню.",
            parse_mode="HTML", reply_markup=main_keyboard(uid)
        )
        try:
            await bot.send_message(ADMIN_ID, f"🤖 Продан ИИ!\n👤 {message.from_user.full_name} (ID: {uid})")
        except: pass

    elif payload == "premium_purchase":
        premium_users.add(uid)
        await message.answer(
            "⭐ <b>Premium активирован!</b>\n\nТеперь я пересылаю медиафайлы.",
            parse_mode="HTML", reply_markup=main_keyboard(uid)
        )
        try:
            await bot.send_message(ADMIN_ID, f"⭐ Продан Premium!\n👤 {message.from_user.full_name} (ID: {uid})")
        except: pass


# ---------------------------------------------------
# Запуск
# ---------------------------------------------------
async def main():
    log.info("Запуск...")
    try:
        await bot.send_message(ADMIN_ID, "✅ Бот запущен (AI Edition)")
    except: pass
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())