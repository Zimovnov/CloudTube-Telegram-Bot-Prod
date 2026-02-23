# Developer Guide (SoundCloud Bot)

Этот документ про структуру кода после рефакторинга: что где лежит и куда идти, если нужно что-то поменять.


- `bot.py` — только запуск приложения, регистрация хендлеров, wiring.
- Основная логика — в папке `app/`.
- Переводы — в `locales/`.
- Тесты — в `tests/`.

## Текущая структура

- `bot.py`
  - Точка входа: `main()`, подключение хендлеров и запуск polling.

- `app/config.py`
  - Конфиг из `.env`, лимиты, константы, состояния диалога (`ASK_*`).

- `app/errors.py`
  - Коды ошибок (`ERR_*`) и `WorkerCancelledError`.

- `app/logging_utils.py`
  - Логгер, sanitize, классификация ошибок, `log_event`.

- `app/state.py`
  - In-memory состояние/словари и lock-объекты.

- `app/jobs.py`
  - Redis init, cooldown, ограничения параллелизма, cancel/abort задач.

- `app/settings_store.py`
  - Чтение/запись пользовательских настроек (Redis + fallback local).

- `app/i18n.py`
  - `t()`, `tf()`, `get_lang()`, загрузка переводов из JSON.

- `app/handlers/base.py`
  - `/start`, `/help`, restart.

- `app/handlers/settings.py`
  - Меню настроек и callback-логика кнопок настроек.

- `app/handlers/downloads.py`
  - Сценарий получения ссылки, trim/cancel callbacks, orchestration загрузки.

- `app/services/worker.py`
  - Тяжелая обработка: yt-dlp, trim, upload, progress/watchdog.

- `locales/ru.json`, `locales/en.json`
  - Все пользовательские тексты.

- `tests/`
  - Unit-тесты.

## Что где менять (быстрый маршрут)

### 1. Поменять текст бота

1. Измени ключ в `locales/ru.json`.
2. Добавь/обнови тот же ключ в `locales/en.json`.
3. В коде используй `t("key", lang)` или `tf("key", lang, ...)`.

Важно: не хардкодь тексты в хендлерах, если это пользовательский UI.

### 2. Добавить новую кнопку/раздел в /settings

1. Добавь кнопку в `build_main_settings_markup()` в `app/handlers/settings.py`.
2. Добавь callback route в `settings_callback()`.
3. Если нужна новая настройка — обнови `normalize_settings()` в `app/settings_store.py`.
4. Добавь тексты в `locales/*.json`.

### 3. Добавить новую команду (/something)

1. Реализуй хендлер в `app/handlers/base.py` (или новом файле в `app/handlers/`).
2. Подключи `CommandHandler` в `bot.py`.
3. Добавь описание команды в `locales/*.json` и `app/i18n.py` (через `_build_bot_commands`).

### 4. Поменять логику скачивания/trim/upload

- Оркестрация диалога и запусков: `app/handlers/downloads.py`.
- Низкоуровневое скачивание/обрезка/upload: `app/services/worker.py`.

Правило:
- Хендлеры: принимают решение по сценарию.
- Worker: делает тяжелую работу с файлами/сетями.

### 5. Поменять лимиты, таймауты, флаги

Меняй в `app/config.py` (через env-переменные).

Примеры:
- `MAX_DURATION`
- `YTDLP_*`
- `DOWNLOAD_STALL_*`
- `FFMPEG_REQUIRED_ON_STARTUP`

### 6. Поменять логи / приватность

- Формат/санитизацию логов — `app/logging_utils.py`.
- Коды ошибок — `app/errors.py`.

### 7. Redis / состояние задач / отмены

- Redis-клиент и ограничения — `app/jobs.py`.
- In-memory словари/locks — `app/state.py`.

## Как запускать и проверять

### Локально

```powershell
python bot.py
```

### Тесты

```powershell
python -m unittest discover -s tests -v
```

### Docker

```powershell
docker compose build bot
docker compose up -d --force-recreate bot
docker compose logs -f bot
```

## Рекомендации по изменениям (чтобы не ломалось)

1. Сначала меняй маленькими шагами (1 зона за раз).
2. После каждого шага: `unittest` + `docker compose logs`.
3. Новые пользовательские тексты — только через `locales`.
4. Не смешивай heavy logic в `bot.py`: он должен оставаться тонким entry point.

## Частый сценарий: «добавить новую настройку»

Мини-чеклист:

1. Добавить поле в `normalize_settings()` (`app/settings_store.py`).
2. Добавить UI-кнопку/маршрут в `app/handlers/settings.py`.
3. Добавить ключи текста в `locales/ru.json` и `locales/en.json`.
4. Прогнать:
   - `python -m unittest discover -s tests -v`
   - `docker compose up -d --build bot`

## Итого

Текущая архитектура:
- `bot.py` в корне — оставляем.
- Все бизнес-части — в `app/`.
- Локализация — в `locales/`.
- Тесты — в `tests/`.

