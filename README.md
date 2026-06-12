# SavedMessages Bot v2.0

Telegram Business бот — перехватывает удалённые сообщения.

## Структура

```
bot.py          # основной файл бота
database.py     # весь слой данных (aiosqlite + SQLite)
requirements.txt
Procfile        # Railway / Heroku
railway.toml    # Railway: volume для data/bot.db
.env.example    # переменные окружения
```

## Деплой на Railway

### 1. Подготовка
```bash
# Залей проект на GitHub (приватный репо)
git init
git add .
git commit -m "init"
git remote add origin https://github.com/ТВОЙuser/savedmessages-bot.git
git push -u origin main
```

### 2. Railway
1. Зайди на [railway.app](https://railway.app)
2. New Project → Deploy from GitHub repo → выбери репо
3. Перейди в Variables → добавь:

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | токен от @BotFather |
| `ADMIN_ID` | твой Telegram ID |
| `GROQ_API_KEY` | ключ от [console.groq.com](https://console.groq.com) |
| `BOT_USERNAME` | юзернейм бота без @ |

4. Volumes → Add Volume → Mount path: `/app/data`
   (это сохраняет БД между перезапусками)

5. Deploy → бот запустится автоматически

### 3. Проверка
Напиши `/start` боту — должно прийти приветствие.
Администратору придёт сообщение `✅ Бот запущен`.

---

## Монетизация

| Уровень | Условие | Возможности |
|---------|---------|-------------|
| Free | Всегда | Перехват ∞ · Кэш 20 · ИИ 5/день |
| Premium | 50⭐/мес | Кэш 200 · ИИ ∞ · Поиск |
| Донат 100⭐+ | Единоразово | Значок 💎 · Premium 30 дней |

**Цель 100⭐/мес:** 2 пользователя premium = 100⭐.
При реферальной раскрутке — реалистично с первого месяца.

---

## Как работает бизнес-перехват

1. Пользователь подключает бота в Telegram Business → Автоматизация чатов
2. Все входящие сообщения проходят через `@dp.business_message()` → сохраняются в SQLite
3. При удалении — `@dp.deleted_business_messages()` → бот достаёт из БД и отправляет владельцу
