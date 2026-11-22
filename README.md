# Exchange Telegram Bot

Простой Telegram-бот для обмена криптовалют с хранением заявок в SQLite.

## Быстрый запуск

1. Создайте файл `.env` в корне проекта:

```env
BOT_TOKEN=your_telegram_bot_token
ADMIN_TELEGRAM_IDS=123456789
MINI_APP_URL=https://your-domain.example
MINI_APP_HOST=0.0.0.0
MINI_APP_PORT=8080
DB_PATH=exchange_bot.db
```

Переменная `ADMIN_TELEGRAM_IDS` — список числовых Telegram ID администраторов через запятую. После сохранения перезапустите бота: откройте команду **`/admin`** в чате с ботом (доступна только указанным ID).

**Пользователь:** регистрация по Telegram ID через `/start`, обмен через меню или Mini App, просмотр заявки по **`/status <id>`** или кнопке «Статус заявки», история операций — «📜 История операций».

**Администратор** (`/admin`): список заявок, смена статуса (полуавтомат: «На проверке», «Выполнена», «Отменена» и т.д.), множители курсов на базе CoinGecko, учёт резервов по тикерам (справочно в БД), блокировка/разблокировка пользователя по Telegram ID.

2. Запустите бот + Mini App одной командой:

```bash
docker compose up --build -d
```

3. Остановить сервисы:

```bash
docker compose down
```

4. Проверить логи:

```bash
docker compose logs -f
```

## Локальный запуск без Docker

```bash
pip install -r requirements.txt
python miniapp_server.py
# в другом терминале
python bot.py
```

## Важно для Mini App

- `MINI_APP_URL` должен быть публичным HTTPS-URL.
- Этот URL открывается внутри Telegram как Web App.
