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
import asyncpg

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN    = "7793443906:AAEne93-Nc6bRfLJPQbwu1WlifjvJA3tnQg"
ADMIN_ID     = 5907310974
GROQ_API_KEY = "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"

PREMIUM_STARS = 50
AI_STARS      = 5
REFERRAL_DAYS = 3

# Railway сам подставит DATABASE_URL когда добавишь PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/savedmessages")
# ===================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())
db_pool: asyncpg.Pool = None


# ==================== СОСТОЯНИЯ ====================
class AIChat(StatesGroup):
    chatting = State()

class AdminGiveAccess(StatesGroup):
    waiting_username = State()
    waiting_duration = State()

class AdminRemoveAccess(StatesGroup):
    waiting_username = State()


# ==================== БАЗА ДАННЫХ ====================
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at TIMESTAMP DEFAULT NOW(),
                referrer_id BIGINT
            );
            
            CREATE TABLE IF NOT EXISTS premium_access (
                user_id BIGINT PRIMARY KEY,
                expire_date TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS ai_access (
                user_id BIGINT PRIMARY KEY,
                expire_date TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS message_cache (
                id BIGSERIAL PRIMARY KEY,
                owner_id BIGINT,
                message_id BIGINT,
                from_name TEXT,
                username TEXT,
                chat_name TEXT,
                date TEXT,
                text TEXT,
                media_type TEXT,
                file_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_cache_owner_msg ON message_cache(owner_id, message_id);
            
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id BIGINT PRIMARY KEY,
                cached_count INTEGER DEFAULT 0,
                deleted_count INTEGER DEFAULT 0
            );
            
            CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                purchase_type TEXT,
                amount INTEGER,
                purchased_at TIMESTAMP DEFAULT NOW()
            );
            
            CREATE TABLE IF NOT EXISTS ai_history (
                user_id BIGINT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_ai_history_user ON ai_history(user_id);
        """)
        log.info("✅ База данных готова")


# ==================== ФУНКЦИИ БД ====================
async def register_user_db(uid: int, full_name: str, username: str | None, referrer: int | None = None):
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT user_id FROM users WHERE user_id = $1", uid)
        if not existing:
            await conn.execute(
                "INSERT INTO users (user_id, username, full_name, referrer_id) VALUES ($1, $2, $3, $4)",
                uid, username, full_name, referrer
            )
            await conn.execute(
                "INSERT INTO user_stats (user_id, cached_count, deleted_count) VALUES ($1, 0, 0) ON CONFLICT DO NOTHING",
                uid
            )
            return True
        return False

async def is_premium_db(uid: int) -> bool:
    if uid == ADMIN_ID:
        return True
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT expire_date FROM premium_access WHERE user_id = $1", uid)
        if row:
            expire = row["expire_date"]
            if expire is None:
                return True
            if datetime.now() < expire:
                return True
            await conn.execute("DELETE FROM premium_access WHERE user_id = $1", uid)
        return False

async def has_ai_db(uid: int) -> bool:
    if uid == ADMIN_ID:
        return True
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT expire_date FROM ai_access WHERE user_id = $1", uid)
        if row:
            expire = row["expire_date"]
            if expire is None:
                return True
            if datetime.now() < expire:
                return True
            await conn.execute("DELETE FROM ai_access WHERE user_id = $1", uid)
        return False

async def get_premium_expire_db(uid: int) -> str:
    if uid == ADMIN_ID:
        return "♾ Навсегда"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT expire_date FROM premium_access WHERE user_id = $1", uid)
        if row:
            return "♾ Навсегда" if row["expire_date"] is None else row["expire_date"].strftime('%d.%m.%Y')
        return "—"

async def get_ai_expire_db(uid: int) -> str:
    if uid == ADMIN_ID:
        return "♾ Навсегда"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT expire_date FROM ai_access WHERE user_id = $1", uid)
        if row:
            return "♾ Навсегда" if row["expire_date"] is None else row["expire_date"].strftime('%d.%m.%Y')
        return "—"

async def grant_access_db(access_type: str, uid: int, days: int | None):
    expire = None if days is None else datetime.now() + timedelta(days=days)
    table = "premium_access" if access_type == "premium" else "ai_access"
    async with db_pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO {table} (user_id, expire_date) VALUES ($1, $2) "
            f"ON CONFLICT (user_id) DO UPDATE SET expire_date = $2",
            uid, expire
        )

async def remove_access_db(access_type: str, uid: int) -> bool:
    table = "premium_access" if access_type == "premium" else "ai_access"
    async with db_pool.acquire() as conn:
        result = await conn.execute(f"DELETE FROM {table} WHERE user_id = $1", uid)
        return "DELETE 1" in result

async def get_uid_by_username(username: str) -> int | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM users WHERE LOWER(username) = $1", 
            username.lower().replace("@", "")
        )
        return row["user_id"] if row else None

async def cache_message_db(owner_id: int, msg_id: int, data: dict):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO message_cache (owner_id, message_id, from_name, username, chat_name, date, text, media_type, file_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            owner_id, msg_id, data["from_name"], data["username"], data["chat"],
            data["date"], data["text"], data["media_type"], data["file_id"]
        )
        await conn.execute(
            "UPDATE user_stats SET cached_count = cached_count + 1 WHERE user_id = $1",
            owner_id
        )

async def get_cached_message(owner_id: int, msg_id: int) -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM message_cache WHERE owner_id = $1 AND message_id = $2",
            owner_id, msg_id
        )
        if row:
            return {
                "from_name": row["from_name"],
                "username": row["username"],
                "chat": row["chat_name"],
                "date": row["date"],
                "text": row["text"],
                "media_type": row["media_type"],
                "file_id": row["file_id"],
            }
        return None

async def remove_cached_message(owner_id: int, msg_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM message_cache WHERE owner_id = $1 AND message_id = $2",
            owner_id, msg_id
        )

async def get_recent_messages(owner_id: int, limit: int = 20) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM message_cache WHERE owner_id = $1 ORDER BY created_at DESC LIMIT $2",
            owner_id, limit
        )
        return [dict(r) for r in reversed(rows)]

async def get_stats_db(uid: int) -> dict:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT cached_count, deleted_count FROM user_stats WHERE user_id = $1", uid)
        if row:
            return {"cached": row["cached_count"], "deleted": row["deleted_count"]}
        return {"cached": 0, "deleted": 0}

async def increment_deleted_db(owner_id: int, count: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_stats SET deleted_count = deleted_count + $1 WHERE user_id = $2",
            count, owner_id
        )

async def clear_cache_db(owner_id: int) -> int:
    async with db_pool.acquire() as conn:
        count_row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM message_cache WHERE owner_id = $1", owner_id)
        count = count_row["cnt"]
        await conn.execute("DELETE FROM message_cache WHERE owner_id = $1", owner_id)
        return count

async def get_total_cached() -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM message_cache")
        return row["cnt"]

async def get_registered_count() -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM users")
        return row["cnt"]

async def get_premium_count() -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM premium_access")
        return row["cnt"]

async def get_ai_count() -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM ai_access")
        return row["cnt"]

async def get_referral_count(uid: int) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM users WHERE referrer_id = $1", uid)
        return row["cnt"]

async def get_total_referral_count() -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM users WHERE referrer_id IS NOT NULL")
        return row["cnt"]

async def get_total_deleted() -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT SUM(deleted_count) as total FROM user_stats")
        return row["total"] or 0

async def get_users_list(limit: int = 20) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT u.user_id, u.username, "
            "CASE WHEN p.user_id IS NOT NULL THEN TRUE ELSE FALSE END as has_premium, "
            "CASE WHEN a.user_id IS NOT NULL THEN TRUE ELSE FALSE END as has_ai "
            "FROM users u "
            "LEFT JOIN premium_access p ON u.user_id = p.user_id "
            "LEFT JOIN ai_access a ON u.user_id = a.user_id "
            "ORDER BY u.joined_at DESC LIMIT $1",
            limit
        )
        return [dict(r) for r in rows]

async def save_purchase_db(uid: int, purchase_type: str, amount: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO purchases (user_id, purchase_type, amount) VALUES ($1, $2, $3)",
            uid, purchase_type, amount
        )

async def get_ai_history_db(uid: int) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, content FROM ai_history WHERE user_id = $1 ORDER BY created_at ASC",
            uid
        )
        return [{"role": r["role"], "content": r["content"]} for r in rows]

async def add_ai_message_db(uid: int, role: str, content: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO ai_history (user_id, role, content) VALUES ($1, $2, $3)",
            uid, role, content
        )

async def clear_ai_history_db(uid: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM ai_history WHERE user_id = $1", uid)


# ==================== GROQ API ====================
async def ask_groq(user_id: int, user_message: str) -> str:
    history = await get_ai_history_db(user_id)
    await add_ai_message_db(user_id, "user", user_message)
    history.append({"role": "user", "content": user_message})
    
    if len(history) > 10:
        history = history[-10:]

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
                await add_ai_message_db(user_id, "assistant", reply)
                return reply
    except Exception as e:
        log.error(f"Groq error: {e}")
        return "⚠️ ИИ временно недоступен, попробуй позже."


# ==================== КЛАВИАТУРЫ ====================
async def main_keyboard(uid: int) -> InlineKeyboardMarkup:
    has_premium = await is_premium_db(uid)
    has_ai = await has_ai_db(uid)
    
    ai_label = "◈  Чат с ИИ" if has_ai else f"◈  ИИ · {AI_STARS} ⭐"
    ai_data  = "open_ai" if has_ai else "buy_ai"
    
    premium_label = "◆  Premium" if has_premium else f"◆  Premium · {PREMIUM_STARS} ⭐"
    premium_data  = "premium_info" if has_premium else "buy_premium"
    
    kb = [
        [
            InlineKeyboardButton(text="📋 Сохранённые", callback_data="show_all"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats")
        ],
        [
            InlineKeyboardButton(text="👥 Рефералы", callback_data="referrals"),
            InlineKeyboardButton(text="🗑 Очистить кэш", callback_data="clear_cache")
        ],
        [InlineKeyboardButton(text=ai_label, callback_data=ai_data)],
        [InlineKeyboardButton(text=premium_label, callback_data=premium_data)],
        [InlineKeyboardButton(text="💝 Поддержать", callback_data="donate")],
        [InlineKeyboardButton(text="❓ Как подключить", callback_data="howto")],
    ]
    
    if uid == ADMIN_ID:
        kb.insert(0, [InlineKeyboardButton(text="🛡 Админ-панель", callback_data="admin_panel")])
    
    return InlineKeyboardMarkup(inline_keyboard=kb)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Выдать Premium", callback_data="admin_give_premium")],
        [InlineKeyboardButton(text="🤖 Выдать ИИ", callback_data="admin_give_ai")],
        [InlineKeyboardButton(text="🗑 Забрать Premium", callback_data="admin_remove_premium")],
        [InlineKeyboardButton(text="🗑 Забрать ИИ", callback_data="admin_remove_ai")],
        [InlineKeyboardButton(text="📋 Список пользователей", callback_data="admin_list_users")],
        [InlineKeyboardButton(text="📊 Общая статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_to_main")],
    ])


def duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 7 дней", callback_data="dur_7")],
        [InlineKeyboardButton(text="📅 14 дней", callback_data="dur_14")],
        [InlineKeyboardButton(text="📅 30 дней", callback_data="dur_30")],
        [InlineKeyboardButton(text="♾ Навсегда", callback_data="dur_forever")],
        [InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")],
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
    
    args = message.text.split()
    referrer = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer = int(args[1].replace("ref_", ""))
        except:
            pass
    
    is_new = await register_user_db(uid, full_name, username, referrer)
    
    if referrer and referrer != uid and is_new:
        ref_has_premium = await is_premium_db(referrer)
        if not ref_has_premium:
            await grant_access_db("premium", referrer, REFERRAL_DAYS)
            try:
                await bot.send_message(
                    referrer,
                    "🎁 <b>Новый реферал!</b>\n"
                    "─────────────────\n"
                    f"Пользователь {full_name} присоединился по твоей ссылке.\n"
                    f"Ты получил <b>{REFERRAL_DAYS} дня Premium</b>!",
                    parse_mode="HTML"
                )
            except:
                pass
            try:
                await bot.send_message(ADMIN_ID, f"👥 Реферал: {full_name} пришёл от ID:{referrer}")
            except:
                pass
    
    await message.answer(
        "👁‍🗨 <b>SavedMessages</b>\n"
        "─────────────────\n"
        "Твой личный детектив в Telegram Business.\n"
        "Сохраняю <b>все</b> удалённые сообщения.\n\n"
        "📌 <b>Быстрый старт:</b>\n"
        "Профиль → Изменить → Автоматизация чатов\n\n"
        f"👥 Пригласи друга и получи <b>{REFERRAL_DAYS} дня Premium</b>!\n"
        f"🔗 Твоя ссылка: <code>https://t.me/SaveDeleteMessageTelegrambot?start=ref_{uid}</code>",
        parse_mode="HTML",
        reply_markup=await main_keyboard(uid)
    )


# ==================== /admin ====================
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    total_cached = await get_total_cached()
    reg_count = await get_registered_count()
    prem_count = await get_premium_count()
    ai_count = await get_ai_count()
    
    await message.answer(
        "🛡 <b>Админ-панель</b>\n"
        "─────────────────\n"
        f"▪️ Пользователей: <b>{reg_count}</b>\n"
        f"▪️ Premium: <b>{prem_count}</b>\n"
        f"▪️ ИИ доступ: <b>{ai_count}</b>\n"
        f"▪️ В кэше: <b>{total_cached}</b>",
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
    total_cached = await get_total_cached()
    reg_count = await get_registered_count()
    prem_count = await get_premium_count()
    ai_count = await get_ai_count()
    
    await call.message.edit_text(
        "🛡 <b>Админ-панель</b>\n"
        "─────────────────\n"
        f"▪️ Пользователей: <b>{reg_count}</b>\n"
        f"▪️ Premium: <b>{prem_count}</b>\n"
        f"▪️ ИИ доступ: <b>{ai_count}</b>\n"
        f"▪️ В кэше: <b>{total_cached}</b>",
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard()
    )


@dp.callback_query(F.data.in_(["admin_give_premium", "admin_give_ai"]))
async def cb_give_access_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    access_type = "premium" if "premium" in call.data else "ai"
    emoji = "⭐" if access_type == "premium" else "🤖"
    name = "Premium" if access_type == "premium" else "ИИ доступ"
    
    await state.set_state(AdminGiveAccess.waiting_username)
    await state.update_data(access_type=access_type)
    await call.message.edit_text(
        f"{emoji} <b>Выдать {name}</b>\n"
        "─────────────────\n"
        "Введи @username пользователя:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]
        ])
    )


@dp.message(AdminGiveAccess.waiting_username)
async def process_give_username(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    username = message.text.strip().lower().replace("@", "")
    
    uid = await get_uid_by_username(username)
    if not uid:
        await message.answer("❌ Пользователь не найден в боте.", reply_markup=admin_panel_keyboard())
        await state.clear()
        return
    
    await state.update_data(target_uid=uid, target_username=f"@{username}")
    await state.set_state(AdminGiveAccess.waiting_duration)
    
    data = await state.get_data()
    name = "Premium" if data["access_type"] == "premium" else "ИИ доступ"
    await message.answer(
        f"🎯 <b>Выдать {name}</b>\n"
        "─────────────────\n"
        f"Пользователь: <b>@{username}</b>\n"
        "Выбери срок:",
        parse_mode="HTML",
        reply_markup=duration_keyboard()
    )


@dp.callback_query(F.data.startswith("dur_"), AdminGiveAccess.waiting_duration)
async def process_duration(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    
    duration = call.data.split("_")[1]
    data = await state.get_data()
    uid = data["target_uid"]
    username = data["target_username"]
    access_type = data["access_type"]
    
    if duration == "forever":
        days = None
        expire_text = "♾ Навсегда"
    else:
        days = int(duration)
        expire_text = (datetime.now() + timedelta(days=days)).strftime('%d.%m.%Y')
    
    await grant_access_db(access_type, uid, days)
    
    name = "⭐ Premium" if access_type == "premium" else "🤖 ИИ доступ"
    await state.clear()
    await call.message.edit_text(
        f"✅ <b>Успешно!</b>\n"
        "─────────────────\n"
        f"{name} выдан\n"
        f"👤 {username}\n"
        f"📅 До: <b>{expire_text}</b>",
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard()
    )
    try:
        await bot.send_message(uid, 
            f"🎁 <b>Подарок от админа!</b>\n"
            "─────────────────\n"
            f"Тебе выдан {name}\n"
            f"📅 До: <b>{expire_text}</b>",
            parse_mode="HTML"
        )
    except:
        pass


@dp.callback_query(F.data.in_(["admin_remove_premium", "admin_remove_ai"]))
async def cb_remove_access_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    access_type = "premium" if "premium" in call.data else "ai"
    name = "Premium" if access_type == "premium" else "ИИ доступ"
    
    await state.set_state(AdminRemoveAccess.waiting_username)
    await state.update_data(access_type=access_type)
    await call.message.edit_text(
        f"🗑 <b>Забрать {name}</b>\n"
        "─────────────────\n"
        "Введи @username пользователя:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]
        ])
    )


@dp.message(AdminRemoveAccess.waiting_username)
async def process_remove_username(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    username = message.text.strip().lower().replace("@", "")
    
    uid = await get_uid_by_username(username)
    data = await state.get_data()
    access_type = data["access_type"]
    name = "Premium" if access_type == "premium" else "ИИ доступ"
    
    await state.clear()
    
    if not uid:
        await message.answer("❌ Пользователь не найден.", reply_markup=admin_panel_keyboard())
        return
    
    removed = await remove_access_db(access_type, uid)
    if removed:
        await message.answer(
            f"✅ <b>{name} забран</b>\n"
            "─────────────────\n"
            f"👤 @{username}",
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard()
        )
        try:
            await bot.send_message(uid, f"⚠️ Админ забрал у тебя {name}.", parse_mode="HTML")
        except:
            pass
    else:
        await message.answer(f"❌ У @{username} нет {name}.", reply_markup=admin_panel_keyboard())


@dp.callback_query(F.data == "admin_list_users")
async def cb_list_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    
    users = await get_users_list(20)
    if not users:
        await call.answer("Список пуст", show_alert=True)
        return
    
    lines = []
    for u in users:
        username = u["username"] or f"ID:{u['user_id']}"
        p = "⭐" if u["has_premium"] else "—"
        a = "🤖" if u["has_ai"] else "—"
        lines.append(f"@{username}  {p}{a}")
    
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
    total_cached = await get_total_cached()
    total_deleted = await get_total_deleted()
    reg_count = await get_registered_count()
    prem_count = await get_premium_count()
    ai_count = await get_ai_count()
    ref_count = await get_total_referral_count()
    
    await call.message.edit_text(
        "📊 <b>Общая статистика</b>\n"
        "─────────────────\n"
        f"👥 Пользователей: <b>{reg_count}</b>\n"
        f"⭐ Premium: <b>{prem_count}</b>\n"
        f"🤖 ИИ: <b>{ai_count}</b>\n"
        f"👥 По рефералам: <b>{ref_count}</b>\n"
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
        reply_markup=await main_keyboard(call.from_user.id)
    )


# ==================== РЕФЕРАЛЫ И ДОНАТ ====================
@dp.callback_query(F.data == "referrals")
async def cb_referrals(call: CallbackQuery):
    uid = call.from_user.id
    my_refs = await get_referral_count(uid)
    ref_link = f"https://t.me/SaveDeleteMessageTelegrambot?start=ref_{uid}"
    expire = await get_premium_expire_db(uid)
    is_prem = await is_premium_db(uid)
    
    text = (
        "👥 <b>Реферальная система</b>\n"
        "─────────────────\n"
        f"Пригласи друга — получи <b>{REFERRAL_DAYS} дня Premium</b> бесплатно!\n\n"
        f"🔗 Твоя ссылка:\n<code>{ref_link}</code>\n\n"
        f"👤 Приведено друзей: <b>{my_refs}</b>\n"
        f"⭐ Premium: <b>{'Активен до ' + expire if is_prem else 'Не активен'}</b>"
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


# ==================== КЭШ СООБЩЕНИЙ ====================
@dp.business_message()
async def cache_message(message: Message):
    owner_id = message.chat.id
    if not owner_id:
        return

    if message.from_user:
        await register_user_db(
            message.from_user.id,
            message.from_user.full_name or "Неизвестно",
            message.from_user.username
        )

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

    data = {
        "from_name": message.from_user.full_name if message.from_user else "Неизвестно",
        "username": f"@{message.from_user.username}" if message.from_user and message.from_user.username else "",
        "chat": message.chat.title or message.chat.full_name or "Личные сообщения",
        "date": message.date.strftime('%d.%m.%Y · %H:%M'),
        "text": message.text or message.caption or "",
        "media_type": media_type,
        "file_id": file_id,
    }
    
    await cache_message_db(owner_id, message.message_id, data)


# ==================== УДАЛЕНИЕ ====================
@dp.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted):
    try:
        conn = await bot.get_business_connection(event.business_connection_id)
        owner_id = conn.user.id
    except Exception:
        return

    await increment_deleted_db(owner_id, len(event.message_ids))
    has_premium = await is_premium_db(owner_id)

    for msg_id in event.message_ids:
        cached = await get_cached_message(owner_id, msg_id)
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
            if has_premium:
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
            else:
                await bot.send_message(
                    owner_id,
                    "🔒 <b>Медиафайл скрыт</b>\n"
                    "─────────────────\n"
                    f"Открой Premium за {PREMIUM_STARS} ⭐",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text=f"◆  Premium · {PREMIUM_STARS} ⭐", callback_data="buy_premium")
                    ]])
                )


# ==================== ИИ ЧАТ ====================
@dp.callback_query(F.data == "open_ai")
async def cb_open_ai(call: CallbackQuery, state: FSMContext):
    if not await has_ai_db(call.from_user.id):
        await call.answer("◈ Сначала активируй ИИ!", show_alert=True)
        return
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
    uid = message.from_user.id
    if not await has_ai_db(uid):
        await state.clear()
        return

    thinking = await message.answer("⏳")
    reply = await ask_groq(uid, message.text or "")
    await thinking.delete()
    await message.answer(f"🤖 <b>Ответ:</b>\n{reply}", reply_markup=ai_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "clear_ai_history")
async def cb_clear_ai(call: CallbackQuery):
    await clear_ai_history_db(call.from_user.id)
    await call.answer("🗑 История очищена", show_alert=True)


@dp.callback_query(F.data == "exit_ai")
async def cb_exit_ai(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("✕ Вышел из чата")
    await call.message.edit_text(
        "👁‍🗨 <b>Главное меню</b>",
        parse_mode="HTML",
        reply_markup=await main_keyboard(call.from_user.id)
    )


# ==================== CALLBACKS ====================
@dp.callback_query(F.data.startswith("ack_"))
async def cb_ack(call: CallbackQuery):
    await call.answer("✅ Принято")
    await call.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data.startswith("del_"))
async def cb_del(call: CallbackQuery):
    msg_id = int(call.data.split("_")[1])
    await remove_cached_message(call.from_user.id, msg_id)
    await call.answer("🗑 Удалено из кэша")
    await call.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data == "show_all")
async def cb_show_all(call: CallbackQuery):
    messages = await get_recent_messages(call.from_user.id, 20)
    if not messages:
        await call.answer("📋 Кэш пуст", show_alert=True)
        return
    await call.answer()
    lines = []
    for m in messages:
        preview = (m["text"][:35] + "…") if len(m["text"]) > 35 else m["text"] or m["media_type"]
        lines.append(f"▪️ <b>{m['from_name']}</b>\n   {m['date']}\n   {preview}")
    await call.message.edit_text(
        "📋 <b>Последние 20 сообщений</b>\n"
        "─────────────────\n" + "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=await main_keyboard(call.from_user.id)
    )


@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    await call.answer()
    s = await get_stats_db(call.from_user.id)
    my_refs = await get_referral_count(call.from_user.id)
    async with db_pool.acquire() as conn:
        cnt_row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM message_cache WHERE owner_id = $1", call.from_user.id)
        cache_now = cnt_row["cnt"]
    
    await call.message.edit_text(
        "📊 <b>Статистика</b>\n"
        "─────────────────\n"
        f"📥 Закэшировано: <b>{s['cached']}</b>\n"
        f"🗑 Поймано удалений: <b>{s['deleted']}</b>\n"
        f"💾 В кэше сейчас: <b>{cache_now}</b>\n"
        f"👥 Рефералов: <b>{my_refs}</b>\n"
        "─────────────────\n"
        f"⭐ Premium: до <b>{await get_premium_expire_db(call.from_user.id)}</b>\n"
        f"🤖 ИИ: до <b>{await get_ai_expire_db(call.from_user.id)}</b>",
        parse_mode="HTML",
        reply_markup=await main_keyboard(call.from_user.id)
    )


@dp.callback_query(F.data == "clear_cache")
async def cb_clear(call: CallbackQuery):
    count = await clear_cache_db(call.from_user.id)
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
        "✅ Готово! Бот следит за чатами.",
        parse_mode="HTML",
        reply_markup=await main_keyboard(call.from_user.id)
    )


@dp.callback_query(F.data == "premium_info")
async def cb_premium_info(call: CallbackQuery):
    expire = await get_premium_expire_db(call.from_user.id)
    await call.answer(f"◆ Premium до: {expire}", show_alert=True)


# ==================== ПОКУПКИ ====================
@dp.callback_query(F.data == "buy_ai")
async def cb_buy_ai(call: CallbackQuery):
    if await has_ai_db(call.from_user.id):
        await call.answer("◈ ИИ уже активен!", show_alert=True)
        return
    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="🤖 ИИ-ассистент",
        description="Llama 3.1 8B — умный чат внутри бота. Отвечает мгновенно.",
        payload="ai_purchase",
        currency="XTR",
        prices=[LabeledPrice(label="ИИ доступ", amount=AI_STARS)],
    )


@dp.callback_query(F.data == "buy_premium")
async def cb_buy_premium(call: CallbackQuery):
    if await is_premium_db(call.from_user.id):
        await call.answer("◆ Premium уже есть!", show_alert=True)
        return
    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="⭐ Premium",
        description="Фото, видео, голосовые из удалённых сообщений. Полный доступ.",
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
        await grant_access_db("ai", uid, None)
        await save_purchase_db(uid, "ai", AI_STARS)
        await message.answer(
            "🤖 <b>ИИ-ассистент активирован!</b>\n"
            "─────────────────\n"
            "Нажми «◈ Чат с ИИ» в меню.",
            parse_mode="HTML",
            reply_markup=await main_keyboard(uid)
        )
        try:
            await bot.send_message(ADMIN_ID, f"💎 Продан ИИ!\n👤 {message.from_user.full_name} (ID: {uid})")
        except: pass

    elif payload == "premium_purchase":
        await grant_access_db("premium", uid, None)
        await save_purchase_db(uid, "premium", PREMIUM_STARS)
        await message.answer(
            "⭐ <b>Premium активирован!</b>\n"
            "─────────────────\n"
            "Теперь медиафайлы сохраняются полностью.",
            parse_mode="HTML",
            reply_markup=await main_keyboard(uid)
        )
        try:
            await bot.send_message(ADMIN_ID, f"💎 Продан Premium!\n👤 {message.from_user.full_name} (ID: {uid})")
        except: pass

    elif payload.startswith("donate_"):
        amount = int(payload.split("_")[1])
        await save_purchase_db(uid, "donate", amount)
        await message.answer(
            f"💝 <b>Спасибо за поддержку!</b>\n"
            "─────────────────\n"
            f"Ты отправил <b>{amount} ⭐</b>\n"
            "Эти средства пойдут на развитие бота!",
            parse_mode="HTML",
            reply_markup=await main_keyboard(uid)
        )
        try:
            await bot.send_message(ADMIN_ID, f"💝 Донат {amount}⭐ от {message.from_user.full_name} (ID: {uid})")
        except: pass


# ==================== ЗАПУСК ====================
async def main():
    log.info("🚀 Инициализация БД...")
    await init_db()
    log.info("🚀 Бот запускается (polling)...")
    try:
        await bot.send_message(ADMIN_ID, "✅ Бот запущен · PostgreSQL · Railway")
    except: pass
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())