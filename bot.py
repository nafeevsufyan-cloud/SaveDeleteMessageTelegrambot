import asyncio
import logging
import os
import aiohttp
from datetime import datetime, timedelta
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

REFERRAL_DAYS = 3
# ===================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


class AIChat(StatesGroup):
    chatting = State()


class AdminGiveAccess(StatesGroup):
    waiting_username = State()
    waiting_duration = State()


class AdminRemoveAccess(StatesGroup):
    waiting_username = State()


# ==================== ХРАНИЛИЩЕ ====================
user_cache:    dict[int, dict[int, dict]] = {}
user_stats:    dict[int, dict]            = {}
registered:    dict[int, dict]            = {}
ai_history:    dict[int, list]            = {}
username_to_id: dict[str, int]           = {}


# Все бесплатно — всегда возвращаем True
def is_premium(uid: int) -> bool:
    return True

def has_ai(uid: int) -> bool:
    return True

def get_premium_expire(uid: int) -> str:
    return "♾ Бесплатно"

def get_ai_expire(uid: int) -> str:
    return "♾ Бесплатно"


# ==================== GROQ API ====================
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


# ==================== КЛАВИАТУРЫ ====================
def main_keyboard(uid: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="📋 Сохранённые", callback_data="show_all"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats")
        ],
        [
            InlineKeyboardButton(text="👥 Рефералы", callback_data="referrals"),
            InlineKeyboardButton(text="🗑 Очистить кэш", callback_data="clear_cache")
        ],
        [InlineKeyboardButton(text="◈  Чат с ИИ", callback_data="open_ai")],
        [InlineKeyboardButton(text="💝 Поддержать", callback_data="donate")],
        [InlineKeyboardButton(text="❓ Как подключить", callback_data="howto")],
    ]
    
    if uid == ADMIN_ID:
        kb.insert(0, [InlineKeyboardButton(text="🛡 Админ-панель", callback_data="admin_panel")])
    
    return InlineKeyboardMarkup(inline_keyboard=kb)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список пользователей", callback_data="admin_list_users")],
        [InlineKeyboardButton(text="📊 Общая статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_to_main")],
    ])


def deleted_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Понял", callback_data=f"ack_{msg_id}"),
            InlineKeyboardButton(text="🗑 Убрать", callback_data=f"del_{msg_id}"),
        ],
        [InlineKeyboardButton(text="📋 Все сохранённые", callback_data="show_all")],
    ])


def ai_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Очистить историю", callback_data="clear_ai_history"),
            InlineKeyboardButton(text="✕ Выйти", callback_data="exit_ai"),
        ],
    ])


def donate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ 50 звёзд", callback_data="donate_50")],
        [InlineKeyboardButton(text="⭐ 100 звёзд", callback_data="donate_100")],
        [InlineKeyboardButton(text="⭐ 200 звёзд", callback_data="donate_200")],
        [InlineKeyboardButton(text="💎 500 звёзд", callback_data="donate_500")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_to_main")],
    ])


# ==================== /start ====================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    full_name = message.from_user.full_name or "Неизвестно"
    username = message.from_user.username
    
    if uid not in registered:
        registered[uid] = {
            "username": username,
            "full_name": full_name,
            "joined": datetime.now(),
            "referrer": None,
        }
        if username:
            username_to_id[f"@{username.lower()}"] = uid
    
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer = int(args[1].replace("ref_", ""))
            if referrer != uid and referrer in registered:
                registered[uid]["referrer"] = referrer
                try:
                    await bot.send_message(
                        referrer,
                        f"🎁 <b>Новый реферал!</b>\n"
                        "─────────────────\n"
                        f"Пользователь {full_name} присоединился по твоей ссылке.\n"
                        f"Спасибо за приглашение!",
                        parse_mode="HTML"
                    )
                except:
                    pass
        except:
            pass
    
    await message.answer(
        "👁‍🗨 <b>SavedMessages</b>\n"
        "─────────────────\n"
        "Твой личный детектив в Telegram Business.\n"
        "Сохраняю <b>все</b> удалённые сообщения.\n\n"
        "🎉 <b>Сейчас всё бесплатно!</b>\n"
        "Premium и ИИ доступ открыты для всех.\n\n"
        "📌 <b>Быстрый старт:</b>\n"
        "Профиль → Изменить → Автоматизация чатов\n\n"
        f"👥 Пригласи друга!\n"
        f"🔗 Твоя ссылка: <code>https://t.me/SaveDeleteMessageTelegrambot?start=ref_{uid}</code>",
        parse_mode="HTML",
        reply_markup=main_keyboard(uid)
    )


# ==================== /admin ====================
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    total_cached = sum(len(v) for v in user_cache.values())
    await message.answer(
        "🛡 <b>Админ-панель</b>\n"
        "─────────────────\n"
        f"▪️ Пользователей: <b>{len(registered)}</b>\n"
        f"▪️ В кэше: <b>{total_cached}</b>\n"
        f"▪️ Всё бесплатно",
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard()
    )


# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    total_cached = sum(len(v) for v in user_cache.values())
    await call.message.edit_text(
        "🛡 <b>Админ-панель</b>\n"
        "─────────────────\n"
        f"▪️ Пользователей: <b>{len(registered)}</b>\n"
        f"▪️ В кэше: <b>{total_cached}</b>\n"
        f"▪️ Всё бесплатно",
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard()
    )


@dp.callback_query(F.data == "admin_list_users")
async def cb_list_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    
    if not registered:
        await call.answer("Список пуст", show_alert=True)
        return
    
    lines = []
    for uid, data in list(registered.items())[:20]:
        username = data.get("username") or f"ID:{uid}"
        lines.append(f"@{username}")
    
    await call.message.edit_text(
        "📋 <b>Пользователи (первые 20)</b>\n"
        "─────────────────\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard()
    )


@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    total_cached = sum(len(v) for v in user_cache.values())
    total_deleted = sum(s["deleted"] for s in user_stats.values())
    referrers = sum(1 for d in registered.values() if d.get("referrer"))
    
    await call.message.edit_text(
        "📊 <b>Общая статистика</b>\n"
        "─────────────────\n"
        f"👥 Пользователей: <b>{len(registered)}</b>\n"
        f"👥 По рефералам: <b>{referrers}</b>\n"
        f"💾 В кэше всего: <b>{total_cached}</b>\n"
        f"🗑 Всего удалений: <b>{total_deleted}</b>",
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard()
    )


@dp.callback_query(F.data == "back_to_main")
async def cb_back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "👁‍🗨 <b>SavedMessages</b>\n"
        "─────────────────\n"
        "Главное меню",
        parse_mode="HTML",
        reply_markup=main_keyboard(call.from_user.id)
    )


# ==================== РЕФЕРАЛЫ И ДОНАТ ====================
@dp.callback_query(F.data == "referrals")
async def cb_referrals(call: CallbackQuery):
    uid = call.from_user.id
    my_refs = sum(1 for d in registered.values() if d.get("referrer") == uid)
    ref_link = f"https://t.me/SaveDeleteMessageTelegrambot?start=ref_{uid}"
    
    text = (
        "👥 <b>Реферальная система</b>\n"
        "─────────────────\n"
        f"Пригласи друга!\n\n"
        f"🔗 Твоя ссылка:\n<code>{ref_link}</code>\n\n"
        f"👤 Приведено друзей: <b>{my_refs}</b>\n"
        "🎉 Сейчас всё бесплатно для всех!"
    )
    
    await call.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ Назад", callback_data="back_to_main")]
        ])
    )


@dp.callback_query(F.data == "donate")
async def cb_donate(call: CallbackQuery):
    await call.message.edit_text(
        "💝 <b>Поддержать проект</b>\n"
        "─────────────────\n"
        "Выбери сумму доната в звёздах Telegram.\n"
        "Это поможет развитию бота!",
        parse_mode="HTML",
        reply_markup=donate_keyboard()
    )


@dp.callback_query(F.data.startswith("donate_"))
async def cb_donate_amount(call: CallbackQuery):
    amount = int(call.data.split("_")[1])
    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="💝 Поддержка бота",
        description=f"Донат {amount} ⭐ на развитие SavedMessages Bot",
        payload=f"donate_{amount}",
        currency="XTR",
        prices=[LabeledPrice(label="Поддержка", amount=amount)],
    )


# ==================== КЭШ БИЗНЕС-СООБЩЕНИЙ ====================
@dp.business_message()
async def cache_message(message: Message):
    owner_id = message.chat.id
    if not owner_id:
        return

    if message.from_user and message.from_user.username:
        username_to_id[f"@{message.from_user.username.lower()}"] = message.from_user.id

    media_types = {
        "photo": "🖼 Фото",
        "video": "🎬 Видео",
        "audio": "🎵 Аудио",
        "voice": "🎤 Голосовое",
        "document": "📄 Документ",
        "sticker": "✨ Стикер",
        "video_note": "⭕ Кружок",
    }
    
    media_type = "💬 Текст"
    file_id = None
    
    for attr, label in media_types.items():
        if hasattr(message, attr) and getattr(message, attr):
            media_type = label
            if attr == "photo":
                file_id = message.photo[-1].file_id
            elif attr in ("video", "voice", "video_note", "document"):
                file_id = getattr(message, attr).file_id
            break

    user_cache.setdefault(owner_id, {})
    user_cache[owner_id][message.message_id] = {
        "from_name": message.from_user.full_name if message.from_user else "Неизвестно",
        "username": f"@{message.from_user.username}" if message.from_user and message.from_user.username else "",
        "chat": message.chat.title or message.chat.full_name or "Личные сообщения",
        "date": message.date.strftime('%d.%m.%Y · %H:%M'),
        "text": message.text or message.caption or "",
        "media_type": media_type,
        "file_id": file_id,
    }
    
    user_stats.setdefault(owner_id, {"cached": 0, "deleted": 0})
    user_stats[owner_id]["cached"] += 1


# ==================== УДАЛЕНИЕ ====================
@dp.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted):
    try:
        conn = await bot.get_business_connection(event.business_connection_id)
        owner_id = conn.user.id
    except Exception:
        return

    user_stats.setdefault(owner_id, {"cached": 0, "deleted": 0})
    user_stats[owner_id]["deleted"] += len(event.message_ids)
    cache = user_cache.get(owner_id, {})

    for msg_id in event.message_ids:
        cached = cache.get(msg_id)
        if not cached:
            continue
        
        text = (
            "🗑 <b>Удалённое сообщение</b>\n"
            "─────────────────\n"
            f"👤 <b>{cached['from_name']}</b> {cached['username']}\n"
            f"💬 {cached['chat']}\n"
            f"🕐 {cached['date']}\n"
            f"📦 {cached['media_type']}"
        )
        if cached["text"]:
            text += f"\n─────────────────\n📝 {cached['text']}"

        await bot.send_message(owner_id, text, parse_mode="HTML",
                               reply_markup=deleted_keyboard(msg_id))

        if cached["file_id"]:
            try:
                mt = cached["media_type"]
                if "Фото" in mt:
                    await bot.send_photo(owner_id, cached["file_id"])
                elif "Видео" in mt:
                    await bot.send_video(owner_id, cached["file_id"])
                elif "Голосовое" in mt:
                    await bot.send_voice(owner_id, cached["file_id"])
                elif "Кружок" in mt:
                    await bot.send_video_note(owner_id, cached["file_id"])
                elif "Документ" in mt:
                    await bot.send_document(owner_id, cached["file_id"])
            except Exception as e:
                log.warning(f"Медиа: {e}")


# ==================== ИИ ЧАТ ====================
@dp.callback_query(F.data == "open_ai")
async def cb_open_ai(call: CallbackQuery, state: FSMContext):
    await state.set_state(AIChat.chatting)
    await call.answer()
    await call.message.edit_text(
        "🤖 <b>ИИ-ассистент активен</b>\n"
        "─────────────────\n"
        "Модель: <b>Llama 3.1 8B</b>\n"
        "Пиши что угодно — отвечаю мгновенно.\n"
        "История диалога сохраняется.\n"
        "─────────────────",
        parse_mode="HTML",
        reply_markup=ai_keyboard()
    )


@dp.message(AIChat.chatting)
async def ai_chat_handler(message: Message, state: FSMContext):
    thinking = await message.answer("⏳")
    reply = await ask_groq(message.from_user.id, message.text or "")
    await thinking.delete()
    await message.answer(f"🤖 <b>Ответ:</b>\n{reply}", reply_markup=ai_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "clear_ai_history")
async def cb_clear_ai(call: CallbackQuery):
    ai_history.pop(call.from_user.id, None)
    await call.answer("🗑 История очищена", show_alert=True)


@dp.callback_query(F.data == "exit_ai")
async def cb_exit_ai(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("✕ Вышел из чата")
    await call.message.edit_text(
        "👁‍🗨 <b>Главное меню</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(call.from_user.id)
    )


# ==================== CALLBACKS ====================
@dp.callback_query(F.data.startswith("ack_"))
async def cb_ack(call: CallbackQuery):
    await call.answer("✅ Принято")
    await call.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data.startswith("del_"))
async def cb_del(call: CallbackQuery):
    msg_id = int(call.data.split("_")[1])
    cache = user_cache.get(call.from_user.id, {})
    cache.pop(msg_id, None)
    await call.answer("🗑 Удалено из кэша")
    await call.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data == "show_all")
async def cb_show_all(call: CallbackQuery):
    cache = user_cache.get(call.from_user.id, {})
    if not cache:
        await call.answer("📋 Кэш пуст", show_alert=True)
        return
    await call.answer()
    lines = []
    for m in list(cache.values())[-20:]:
        preview = (m["text"][:35] + "…") if len(m["text"]) > 35 else m["text"] or m["media_type"]
        lines.append(f"▪️ <b>{m['from_name']}</b>\n   {m['date']}\n   {preview}")
    await call.message.edit_text(
        "📋 <b>Последние 20 сообщений</b>\n"
        "─────────────────\n" + "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_keyboard(call.from_user.id)
    )


@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    await call.answer()
    s = user_stats.get(call.from_user.id, {"cached": 0, "deleted": 0})
    my_refs = sum(1 for d in registered.values() if d.get("referrer") == call.from_user.id)
    cache_now = len(user_cache.get(call.from_user.id, {}))
    
    await call.message.edit_text(
        "📊 <b>Статистика</b>\n"
        "─────────────────\n"
        f"📥 Закэшировано: <b>{s['cached']}</b>\n"
        f"🗑 Поймано удалений: <b>{s['deleted']}</b>\n"
        f"💾 В кэше сейчас: <b>{cache_now}</b>\n"
        f"👥 Рефералов: <b>{my_refs}</b>\n"
        "─────────────────\n"
        "🎉 Premium и ИИ — бесплатно для всех!",
        parse_mode="HTML",
        reply_markup=main_keyboard(call.from_user.id)
    )


@dp.callback_query(F.data == "clear_cache")
async def cb_clear(call: CallbackQuery):
    cache = user_cache.get(call.from_user.id, {})
    count = len(cache)
    cache.clear()
    await call.answer(f"🗑 Очищено {count} сообщений", show_alert=True)


@dp.callback_query(F.data == "howto")
async def cb_howto(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        "📌 <b>Подключение за 2 минуты</b>\n"
        "─────────────────\n"
        "1️⃣ Настройки Telegram\n"
        "2️⃣ Telegram Business\n"
        "3️⃣ Автоматизация чатов\n"
        "4️⃣ Выбери @SaveDeleteMessageTelegrambot\n"
        "─────────────────\n"
        "✅ Готово! Бот следит за чатами.\n"
        "🎉 Всё бесплатно!",
        parse_mode="HTML",
        reply_markup=main_keyboard(call.from_user.id)
    )


# ==================== ПОКУПКИ (ТОЛЬКО ДОНАТ) ====================
@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_payment(message: Message):
    uid = message.from_user.id
    payload = message.successful_payment.invoice_payload

    if payload.startswith("donate_"):
        amount = payload.split("_")[1]
        await message.answer(
            f"💝 <b>Спасибо за поддержку!</b>\n"
            "─────────────────\n"
            f"Ты отправил <b>{amount} ⭐</b>\n"
            "Эти средства пойдут на развитие бота!",
            parse_mode="HTML",
            reply_markup=main_keyboard(uid)
        )
        try:
            await bot.send_message(ADMIN_ID, f"💝 Донат {amount}⭐ от {message.from_user.full_name} (ID: {uid})")
        except: pass


# ==================== ЗАПУСК ====================
async def main():
    log.info("🚀 Бот запускается...")
    try:
        await bot.send_message(ADMIN_ID, "✅ Бот запущен · Railway · Всё бесплатно")
    except: pass
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())