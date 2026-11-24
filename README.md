# Тренажер німецької мови (Telegram Bot + Mini App)

Гібридний застосунок для вивчення німецької мови (рівні **A1–B1**) із інтервальним повторенням **FSRS**, інтерактивними вправами через OpenAI-сумісний LLM API (**DeepSeek** у MVP) та аналітикою прогресу у **Telegram Mini App** (спайдерплот на 5 осей, керування словником і синхронізація тем оформлення `dark` / `light` / `system`).

---

## Архітектура проєкту (5 сервісів)

Проєкт побудований як модульний моноліт із чітким розділенням відповідальності та ізоляцією даних за `user_id`:

| Сервіс | Технології | Призначення |
| --- | --- | --- |
| **`postgres`** | PostgreSQL 16 (`pgvector/pgvector:pg16`) | Основне сховище користувачів, словників, карток FSRS, тренувальних сесій, спроб і тижневих зрізів. |
| **`backend`** | FastAPI, SQLAlchemy 2.0 (async), Alembic, `fsrs`, `openai` SDK | Єдиний REST API (`/api/*`) з авторизацією для Mini App (`Authorization: tma <initData>`) і бота (`X-Internal-API-Key` + `X-Telegram-User-Id`). При старті автоматично й ідемпотентно виконує `alembic upgrade head`. |
| **`scheduler`** | APScheduler (`python -m app.scheduler.main`) | Окремий контейнер для щогодинних FSRS-нагадувань за локальним часом користувача та щотижневих звітів (повністю ізольований від вебворкерів FastAPI). |
| **`bot`** | `aiogram 3`, `httpx` | Незалежний клієнт Telegram-бота: взаємодіє з ядром лише через HTTP API бекенду (нуль Python-імпортів із `backend/`). За потреби сервіс бота можна не запускати — бекенд і Mini App працюють автономно. |
| **`caddy`** | Multi-stage збірка `miniapp/` (Node 22 + Vite) → Caddy 2 | Віддає виробничу статику Mini App із `/srv/miniapp` з SPA-fallback на `index.html`, проксує `/api/*` на `backend:8000` і забезпечує автоматичний Let's Encrypt HTTPS у продакшені. |

### Структура репозиторію

```text
Deutsch_bot/
├── backend/               # FastAPI API, Alembic-міграції, FSRS, LLM, scheduler (uv)
├── bot/                   # Незалежний Telegram-бот на aiogram 3 (uv)
├── miniapp/               # React + TypeScript + Vite + Chart.js + Dockerfile для Caddy
├── Caddyfile              # Маршрутизація /api/* -> backend:8000 та статика /srv/miniapp
├── docker-compose.yml     # Продакшен-конфігурація всіх 5 сервісів
├── docker-compose.dev.yml # Локальний override (reload, порти 127.0.0.1:8080 і 127.0.0.1:5432)
└── .env.example           # Шаблон змінних середовища
```

---

## Налаштування `.env`

1. Скопіюйте шаблон у корені репозиторію:

   ```bash
   cp .env.example .env
   ```

   У PowerShell:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Заповніть `.env` власними значеннями (файл `.env` додано до `.gitignore` — ніколи не комітьте реальні ключі):

   | Змінна | Опис |
   | --- | --- |
   | `APP_ENV`, `LOG_LEVEL` | Середовище (`development` / `staging` / `production` / `test`) та рівень логування (`INFO`, `DEBUG` тощо). |
   | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Облікові дані PostgreSQL у Docker Compose. |
   | `DATABASE_URL` | Async DSN для бекенду та планувальника (`postgresql+asyncpg://...@postgres:5432/...` у Docker або `127.0.0.1:5432` локально). |
   | `TEST_DATABASE_URL` | Окрема ізольована база PostgreSQL для інтеграційних і E2E-тестів (тести відмовляються запускатися, якщо вона збігається з `DATABASE_URL`). |
   | `CADDY_SITE_ADDRESS` | `:80` для локальної розробки або ваш домен (наприклад, `app.example.com`) для автоматичного HTTPS у продакшені. |
   | `CADDY_HTTP_PORT` | Локальний порт Caddy у `docker-compose.dev.yml` (за замовчуванням `8080`). |
   | `BOT_TOKEN` | Токен бота від `@BotFather` (потрібен для Telegram-бота, валідації `initData` та надсилання сповіщень планувальником). |
   | `INTERNAL_API_KEY` | Спільний внутрішній секрет між сервісами `bot` і `backend`. |
   | `BACKEND_API_URL` | Базова адреса API для бота (`http://backend:8000` у Docker Compose або `http://127.0.0.1:8000` локально). |
   | `MINIAPP_URL` | Публічний `https://` URL Mini App (у локальній розробці без тунелю залиште порожнім). |
   | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `DAILY_LLM_LIMIT` | Налаштування OpenAI-сумісного LLM-провайдера (за замовчуванням `https://api.deepseek.com`, `deepseek-chat`, ліміт `50` запитів/добу на користувача). |

---

## Запуск через Docker Compose

### Локальна розробка (dev)

Повний стек усіх 5 сервісів (потрібен дійсний `BOT_TOKEN` у `.env` для сервісу `bot`):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
```

Запуск лише ядра та Mini App **без Telegram-бота** (працює навіть без `.env` із локальними dev-налаштуваннями за замовчуванням):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d postgres backend scheduler caddy
```

Після запуску:
- Mini App доступний на `http://127.0.0.1:8080/`
- Перевірка стану API через Caddy: `http://127.0.0.1:8080/api/health` → `{"status":"ok","database":"ok"}`
- PostgreSQL доступний локально на `127.0.0.1:5432`
- Міграції Alembic застосовуються автоматично й ідемпотентно перед стартом FastAPI; `scheduler`, `bot` і `caddy` стартують лише після переходу `backend` у стан `healthy`.

Перегляд статусу та логів:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml ps
docker compose -f docker-compose.yml -f docker-compose.dev.yml logs -f backend caddy
```

Зупинка (додайте `-v`, щоб повністю очистити локальні томи БД і Caddy):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml down
```

### Продакшен (prod)

Переконайтеся, що в `.env` задано надійні паролі, реальні `BOT_TOKEN`, `INTERNAL_API_KEY`, `LLM_API_KEY`, а `CADDY_SITE_ADDRESS` вказує на ваш домен (DNS A/AAAA записи спрямовані на сервер, порти `80` і `443` відкриті):

```bash
docker compose config
docker compose up --build -d
```

---

## Локальна розробка без Docker (окремими процесами)

Якщо зручніше запускати сервіси безпосередньо на хості (при піднятому `postgres`):

1. **PostgreSQL** (можна підняти лише контейнер БД):

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres
   ```

2. **Backend API (`backend/`):**

   ```bash
   cd backend
   uv sync
   uv run alembic upgrade head
   uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```

3. **Планувальник (`backend/`):**

   ```bash
   cd backend
   uv run python -m app.scheduler.main
   ```

4. **Telegram-бот (`bot/`):**

   ```bash
   cd bot
   uv sync
   uv run python -m bot_app.main
   ```

5. **Telegram Mini App (`miniapp/`):**

   ```bash
   cd miniapp
   npm install
   npm run dev
   ```

   Vite автоматично проксує запити `/api/*` на `http://localhost:8000`.

---

## Тести, лінтери та збірка

Усі автоматизовані тести працюють без реальних мережевих запитів до DeepSeek API, Google Sheets чи Telegram Bot API.

### 1. Підготовка ізольованої тестової бази PostgreSQL

Для запуску інтеграційних тестів БД, міграцій Alembic і наскрізного E2E smoke-тесту (`backend/tests/test_e2e_smoke.py`) створіть окрему базу даних (наприклад, `deutsch_learning_test`):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec postgres psql -U postgres -c "CREATE DATABASE deutsch_learning_test;"
```

### 2. Backend (`backend/`)

Bash:

```bash
cd backend
TEST_DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/deutsch_learning_test" uv run pytest
uv run ruff check .
```

PowerShell:

```powershell
cd backend
$env:TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/deutsch_learning_test"
uv run pytest
uv run ruff check .
```

Окремий запуск наскрізного E2E smoke-тесту MVP:

```bash
cd backend
uv run pytest tests/test_e2e_smoke.py
```

### 3. Telegram-бот (`bot/`)

```bash
cd bot
uv run pytest
uv run ruff check .
```

### 4. Telegram Mini App (`miniapp/`)

```bash
cd miniapp
npm run test
npm run build
```

---

## Підключення HTTPS-тунелю та тестування Mini App у Telegram

Telegram вимагає дійсний **HTTPS** URL для відкриття Mini App усередині клієнта (`WebAppInfo` відхиляє `http://`). Для локальної перевірки підніміть стек у dev-режимі (Caddy слухає `http://127.0.0.1:8080`) і прокиньте до нього тимчасовий HTTPS-тунель.

### Варіант A: Cloudflare Tunnel (`cloudflared`, безкоштовно й без реєстрації)

1. Встановіть [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).
2. Переконайтеся, що dev-стек запущений і `http://127.0.0.1:8080/api/health` повертає `200 OK`.
3. Відкрийте тунель до локального Caddy:

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8080
   ```

4. Скопіюйте видану адресу формату `https://<випадковий-субдомен>.trycloudflare.com`.

### Варіант B: `ngrok`

1. Встановіть [`ngrok`](https://ngrok.com/download) і виконайте авторизацію токеном (`ngrok config add-authtoken <ваш_токен>`).
2. Запустіть тунель на порт Caddy:

   ```bash
   ngrok http 8080
   ```

3. Скопіюйте видану адресу `https://<ваш-домен>.ngrok-free.app`.

### Налаштування `MINIAPP_URL` та `@BotFather`

1. Пропишіть отриману HTTPS-адресу в `.env`:

   ```env
   MINIAPP_URL=https://<ваш-тунель>
   ```

2. Перезапустіть сервіс `bot`, щоб він підхопив новий URL для кнопки Mini App:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d bot
   ```

3. За бажанням додайте кнопку меню у `@BotFather`:
   - Відкрийте чат із `@BotFather` → `/mybots` → оберіть свого бота.
   - Перейдіть у **Bot Settings → Menu Button → Configure menu button**.
   - Вставте вашу HTTPS-адресу тунелю та вкажіть підпис кнопки, наприклад `📊 Прогрес і словник`.
4. Надішліть боту `/start` і відкрийте Mini App: Telegram автоматично передасть підписаний `initData`, бекенд перевірить підпис за `BOT_TOKEN`, а на екранах **Статистика**, **Словник** і **Налаштування** завантажаться ваші дані та тема оформлення.
