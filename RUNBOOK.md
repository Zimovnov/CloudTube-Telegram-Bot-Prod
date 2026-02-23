# RUNBOOK (SoundCloud/YouTube Bot)

Короткая операционная инструкция: как запускать бота, как читать логи и что делать при ошибках.

## 1. Быстрый старт в Docker

Работать из папки проекта:

```powershell
cd c:\Users\zimov\soundcloud_bot
```

Первый запуск или после изменений в `bot.py`:

```powershell
docker compose up -d --build
```

Если меняли только код бота:

```powershell
docker compose up -d --build bot
```

Если меняли только `.env`:

```powershell
docker compose up -d --force-recreate bot
```

Посмотреть состояние:

```powershell
docker compose ps
```

Остановить:

```powershell
docker compose down
```

## 2. Где смотреть логи

Основные логи контейнера:

```powershell
docker compose logs -f bot
```

Локальный файл логов:

```powershell
Get-Content .\bot.log -Tail 200
```

Фильтр по error_code:

```powershell
Select-String -Path .\bot.log -Pattern '"error_code":"E_'
```

## 3. Как читать новую систему логов

Каждая запись в `bot.log` — JSON-событие.

Главные поля:

- `event` — что произошло (`job.failed.worker`, `redis.unavailable`, ...)
- `error_code` — стабильный код ошибки (`E_TIMEOUT`, `E_REDIS_UNAVAILABLE`, ...)
- `job_id` — ID задачи
- `user_id` — хешированный ID пользователя

Расшифровка кодов: см. `ERROR.md`.

## 4. Частые ситуации и что делать

### 4.1 `E_REDIS_UNAVAILABLE`

Что значит:
- бот не может подключиться к Redis.

Что сделать:
1. Проверить Redis контейнер:
```powershell
docker compose ps redis
```
2. Проверить логи Redis:
```powershell
docker compose logs --tail 200 redis
```
3. Проверить пароль в `.env`:
- `REDIS_PASSWORD`
- `REDIS_URL`
4. Перезапустить:
```powershell
docker compose restart redis bot
```

### 4.2 `E_FFMPEG_MISSING`

Что значит:
- ffmpeg не найден внутри контейнера/окружения.

Что сделать:
1. Пересобрать образ:
```powershell
docker compose up -d --build bot
```
2. Проверить Dockerfile (должен быть `apt-get install ... ffmpeg`).

### 4.3 `E_TIMEOUT` / `E_TELEGRAM_TIMEOUT` / `E_NETWORK`

Что значит:
- сетевой таймаут или проблемы Telegram/API.

Что сделать:
1. Подождать 1-2 минуты и повторить.
2. Проверить интернет/прокси/VPN на хосте.
3. Проверить, нет ли всплеска ошибок:
```powershell
Select-String -Path .\bot.log -Pattern 'E_TIMEOUT|E_TELEGRAM_TIMEOUT|E_NETWORK' | Measure-Object
```

### 4.4 `E_WORKER_UPLOAD_HTTP` / `E_WORKER_UPLOAD_FAILED`

Что значит:
- проблема загрузки большого файла на внешний хост.

Что сделать:
1. Проверить, массовая ли ошибка (несколько подряд).
2. Повторить задачу позже.
3. Если стабильно падает — проверить доступность внешних upload сервисов из сети сервера.

### 4.5 `E_DOWNLOAD_FAILED` / `E_HTTP_NOT_FOUND`

Что значит:
- контент недоступен, удалён, требует авторизацию, или блокируется платформой.

Что сделать:
1. Проверить саму ссылку вручную.
2. Обновить `yt-dlp` (через `requirements.txt` и rebuild).
3. Для YouTube при необходимости обновить cookies.

### 4.6 `E_JOB_ALREADY_RUNNING` / `E_COOLDOWN_ACTIVE`

Что значит:
- защита от спама/параллельного запуска сработала штатно.

Что сделать:
1. Подождать завершения текущей задачи.
2. Подождать cooldown и повторить.

## 5. Безопасность (обязательно)

1. Никогда не коммитить реальные секреты:
- `BOT_TOKEN`
- `REDIS_PASSWORD`
- любые cookies

2. После утечки секрета:
1. Сразу ротировать токен/пароль.
2. Перезапустить контейнер:
```powershell
docker compose up -d --force-recreate bot redis
```

3. Настроить соль для хеширования user_id:
- задать `LOG_USER_HASH_SALT` в `.env`.
- для строгого режима продакшена включить:
  - `LOG_HASH_SALT_STRICT=1`

## 6. Релизный чек перед продом

1. `docker compose up -d --build` проходит без ошибок.
2. `docker compose ps` — оба контейнера `Up`.
3. В логах есть `bot.started` и нет постоянных `E_REDIS_*`.
4. Тест-кейс:
- скачать небольшой файл,
- скачать большой файл (через ссылку),
- проверить, что `error_code` появляется при искусственной ошибке.

## 7. Мини-диагностика одной командой

Последние ошибки:

```powershell
Select-String -Path .\bot.log -Pattern '"error_code":"' | Select-Object -Last 50
```

## 9. FFmpeg для продакшена (без папки в репозитории)

Принцип:
1. Не хранить `ffmpeg/` в проекте.
2. В Docker устанавливать ffmpeg через пакетный менеджер (в `Dockerfile` это уже сделано).
3. Локально ставить ffmpeg в систему и использовать PATH или `FFMPEG_PATH`.

Проверка в Docker:
```powershell
docker compose up -d --build
docker compose exec bot ffmpeg -version
```

Проверка локально (без Docker):
```powershell
ffmpeg -version
```

Если ffmpeg не в PATH, можно задать в `.env`:
```env
FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe
```

Для строгого прод-режима (рекомендуется):
```env
FFMPEG_REQUIRED_ON_STARTUP=1
```
Тогда бот не запустится, если ffmpeg недоступен.

Важно:
1. `ffmpeg/` добавлен в `.gitignore` и `.dockerignore`, чтобы большие бинарники не попадали в git и Docker build context.
2. Рекомендуемая схема для релиза: только системный ffmpeg (через Docker image), без локальной папки внутри проекта.

## 8. Правила пользования автотестами (просто)

Когда запускать тесты:
1. После изменений в `bot.py`.
2. Перед релизом/деплоем.
3. Если что-то «странно работает» и нужно быстро проверить базовую логику.

Как запустить все тесты:
```powershell
cd c:\Users\zimov\soundcloud_bot
python -m unittest discover -s tests -v
```

Как запустить только один файл тестов:
```powershell
python -m unittest tests/test_redis_and_jobs.py -v
```

Если используешь виртуальное окружение (`venv`), сначала активируй его:
```powershell
.\venv\Scripts\Activate.ps1
```

Как читать результат:
1. `OK` в конце = все тесты прошли.
2. `FAILED` = есть упавшие тесты, смотри имя теста и текст ошибки чуть выше.

Что делать, если тест упал:
1. Не игнорировать.
2. Исправить код или тест.
3. Запустить тесты повторно и убедиться, что снова `OK`.
