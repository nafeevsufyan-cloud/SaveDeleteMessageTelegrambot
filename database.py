"""
database.py — весь слой данных (aiosqlite)
"""
import aiosqlite
import logging
from datetime import datetime, date
from typing import Optional

DB_PATH = "data/bot.db"
log = logging.getLogger("db")


# ──────────────────────────────────────────────
#  INIT
# ──────────────────────────────────────────────
async def init_db():
    import os
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY,
            username    TEXT,
            full_name   TEXT,
            referrer_id INTEGER,
            premium_until TEXT,           -- ISO date str, NULL = нет
            donor_badge INTEGER DEFAULT 0,-- 1 = есть значок
            joined      TEXT NOT NULL,
            ai_calls_today INTEGER DEFAULT 0,
            ai_date     TEXT              -- дата последнего сброса счётчика
        );

        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id    INTEGER NOT NULL,
            msg_id      INTEGER NOT NULL,
            sender_id   INTEGER,
            from_name   TEXT,
            username    TEXT,
            chat        TEXT,
            date        TEXT,
            text        TEXT,
            media_type  TEXT,
            file_id     TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(owner_id, msg_id)
        );

        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            stars       INTEGER NOT NULL,
            payload     TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ideas (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            username    TEXT,
            full_name   TEXT,
            text        TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_owner ON messages(owner_id);
        -- migration: add sender_id if not exists

        CREATE INDEX IF NOT EXISTS idx_messages_owner_msg ON messages(owner_id, msg_id);
        """)
        await db.commit()
    # Миграция: добавляем sender_id если ещё нет (для существующих БД)
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN sender_id INTEGER")
            await db.commit()
            log.info("🔧 Миграция: добавлена колонка sender_id")
        except Exception:
            pass  # уже есть
    log.info("✅ DB инициализирована")


# ──────────────────────────────────────────────
#  USERS
# ──────────────────────────────────────────────
async def get_user(uid: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_user(uid: int, username: str, full_name: str, referrer_id: Optional[int] = None):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (id, username, full_name, referrer_id, joined)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username  = excluded.username,
                full_name = excluded.full_name
        """, (uid, username, full_name, referrer_id, now))
        await db.commit()


async def set_premium(uid: int, until: date):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET premium_until=? WHERE id=?",
            (until.isoformat(), uid)
        )
        await db.commit()


async def set_donor_badge(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET donor_badge=1 WHERE id=?", (uid,))
        await db.commit()


async def is_premium(uid: int) -> bool:
    user = await get_user(uid)
    if not user or not user["premium_until"]:
        return False
    return date.fromisoformat(user["premium_until"]) >= date.today()


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            return (await cur.fetchone())[0]


async def count_referrals(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (uid,)) as cur:
            return (await cur.fetchone())[0]


async def all_user_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM users") as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_all_users(limit: int = 50, offset: int = 0) -> list[dict]:
    """Список пользователей с username/full_name/referrer_id, новые сверху."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, full_name, referrer_id, joined FROM users "
            "ORDER BY joined DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ──────────────────────────────────────────────
#  AI RATE LIMIT  (free = 5 запросов/день)
# ──────────────────────────────────────────────
FREE_AI_LIMIT = 5

async def ai_allowed(uid: int) -> bool:
    """True если пользователю можно делать запрос к ИИ."""
    if await is_premium(uid):
        return True
    user = await get_user(uid)
    if not user:
        return True
    today = date.today().isoformat()
    if user["ai_date"] != today:
        # Новый день — сбрасываем
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET ai_calls_today=0, ai_date=? WHERE id=?",
                (today, uid)
            )
            await db.commit()
        return True
    return user["ai_calls_today"] < FREE_AI_LIMIT


async def ai_calls_left(uid: int) -> int:
    if await is_premium(uid):
        return 999
    user = await get_user(uid)
    if not user:
        return FREE_AI_LIMIT
    today = date.today().isoformat()
    if user["ai_date"] != today:
        return FREE_AI_LIMIT
    return max(0, FREE_AI_LIMIT - user["ai_calls_today"])


async def increment_ai_calls(uid: int):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users
            SET ai_calls_today = CASE WHEN ai_date=? THEN ai_calls_today+1 ELSE 1 END,
                ai_date = ?
            WHERE id=?
        """, (today, today, uid))
        await db.commit()


# ──────────────────────────────────────────────
#  MESSAGES CACHE (персистентный)
# ──────────────────────────────────────────────
FREE_CACHE_LIMIT    = 20
PREMIUM_CACHE_LIMIT = 200


async def save_message(owner_id: int, msg: dict):
    """Сохраняем сообщение, при переполнении — удаляем самое старое."""
    limit = PREMIUM_CACHE_LIMIT if await is_premium(owner_id) else FREE_CACHE_LIMIT
    now   = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Вставка / игнор дублей
        await db.execute("""
            INSERT OR IGNORE INTO messages
              (owner_id, msg_id, sender_id, from_name, username, chat, date, text, media_type, file_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            owner_id, msg["msg_id"], msg.get("sender_id"), msg["from_name"], msg["username"],
            msg["chat"], msg["date"], msg["text"],
            msg["media_type"], msg["file_id"], now
        ))
        # Обрезаем кэш
        await db.execute("""
            DELETE FROM messages
            WHERE owner_id=? AND id NOT IN (
                SELECT id FROM messages WHERE owner_id=?
                ORDER BY id DESC LIMIT ?
            )
        """, (owner_id, owner_id, limit))
        await db.commit()


async def get_message(owner_id: int, msg_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM messages WHERE owner_id=? AND msg_id=?",
            (owner_id, msg_id)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_recent_messages(owner_id: int, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM messages WHERE owner_id=? ORDER BY id DESC LIMIT ?",
            (owner_id, limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_message(owner_id: int, msg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM messages WHERE owner_id=? AND msg_id=?",
            (owner_id, msg_id)
        )
        await db.commit()


async def clear_messages(owner_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE owner_id=?", (owner_id,)
        ) as cur:
            count = (await cur.fetchone())[0]
        await db.execute("DELETE FROM messages WHERE owner_id=?", (owner_id,))
        await db.commit()
    return count


async def count_messages(owner_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE owner_id=?", (owner_id,)
        ) as cur:
            return (await cur.fetchone())[0]


async def search_messages(owner_id: int, query: str) -> list[dict]:
    """Поиск по тексту — только для premium."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM messages
            WHERE owner_id=? AND (text LIKE ? OR from_name LIKE ? OR username LIKE ?)
            ORDER BY id DESC LIMIT 30
        """, (owner_id, f"%{query}%", f"%{query}%", f"%{query}%")) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def total_messages_all() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM messages") as cur:
            return (await cur.fetchone())[0]


# ──────────────────────────────────────────────
#  PAYMENTS
# ──────────────────────────────────────────────
async def save_payment(uid: int, stars: int, payload: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments (user_id, stars, payload, created_at) VALUES (?,?,?,?)",
            (uid, stars, payload, datetime.now().isoformat())
        )
        await db.commit()


async def total_stars() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(SUM(stars),0) FROM payments") as cur:
            return (await cur.fetchone())[0]


# ──────────────────────────────────────────────
#  IDEAS
# ──────────────────────────────────────────────
async def save_idea(user_id: int, username: str, full_name: str, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO ideas (user_id, username, full_name, text, created_at) VALUES (?,?,?,?,?)",
            (user_id, username, full_name, text, datetime.now().isoformat())
        )
        await db.commit()


async def get_ideas(limit: int = 30) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ideas ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_idea(idea_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM ideas WHERE id=?", (idea_id,))
        await db.commit()


async def clear_ideas():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM ideas")
        await db.commit()


async def count_ideas() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM ideas") as cur:
            return (await cur.fetchone())[0]
