# Error Codes Reference

Этот файл описывает `error_code`, которые бот пишет в структурные JSON-логи.

## Назначение

- Быстро фильтровать инциденты в логах (`error_code=...`).
- Разделять пользовательские ошибки, системные проблемы и сетевые сбои.
- Упростить поддержку при выходе в продакшен.

## Формат в логах

Каждое событие может содержать:

- `event`: тип события (`job.failed.worker`, `redis.unavailable`, ...)
- `error_code`: стабильный код ошибки (`E_TIMEOUT`, `E_WORKER_UPLOAD_HTTP`, ...)
- `job_id`: идентификатор задачи (если есть)
- `user_id`: хешированный user id

## Каталог кодов

| Code | Категория | Что означает | Где обычно встречается |
|---|---|---|---|
| `E_UNKNOWN` | Общая | Не удалось точно классифицировать ошибку | `user.error.handled`, `job.failed.exception` |
| `E_AUTH_DENIED` | Security | Доступ к боту запрещен (не в allowlist) | `auth.denied.start` |
| `E_COOLDOWN_ACTIVE` | User/Input | Пользователь слишком часто стартует задачи | `job.rejected.cooldown` |
| `E_JOB_ALREADY_RUNNING` | User/Flow | У пользователя уже есть активная задача | `job.rejected.parallel_limit` |
| `E_INVALID_LINK` | User/Input | Некорректная ссылка (не SoundCloud/YouTube) | `input.invalid_link` |
| `E_INVALID_RANGE_FORMAT` | User/Input | Неверный формат диапазона обрезки | `input.invalid_trim_format` |
| `E_INVALID_RANGE_ORDER` | User/Input | Начало диапазона >= конца диапазона | `input.invalid_trim_range` |
| `E_TIMEOUT` | Network/Runtime | Таймаут запроса/операции | `user.error.handled`, `job.failed.exception`, worker fallback |
| `E_FILE_NOT_FOUND` | Runtime | Файл не найден | `user.error.handled`, worker fallback |
| `E_HTTP_NOT_FOUND` | Network/HTTP | HTTP 404/ресурс недоступен | `user.error.handled`, worker fallback |
| `E_DOWNLOAD_FAILED` | Downloader | Ошибка `yt_dlp` при скачивании | `user.error.handled`, worker fallback |
| `E_NETWORK` | Network | Ошибка сети/соединения | `user.error.handled`, worker fallback |
| `E_STALE_BUTTON` | User/Flow | Устаревшая callback-кнопка / stale state | `user.error.handled` |
| `E_TELEGRAM_TIMEOUT` | Telegram API | Таймаут Telegram API | `bot.error_handler`, `telegram.file_send_failed` |
| `E_TELEGRAM_BAD_REQUEST` | Telegram API | Некорректный запрос к Telegram API | `bot.error_handler`, `telegram.file_send_failed` |
| `E_REDIS_ISSUE` | Redis | Ошибка операции Redis (throttled log) | `redis.issue` |
| `E_REDIS_DISABLED` | Redis/Config | Redis отключён (пустой `REDIS_URL`) | `redis.disabled` |
| `E_REDIS_CLIENT_MISSING` | Redis/Dependency | Не установлен Python-клиент Redis | `redis.client_missing` |
| `E_REDIS_UNAVAILABLE` | Redis/Infra | Redis недоступен | `redis.unavailable` |
| `E_SECURITY_WEAK_HASH_SALT` | Security/Config | Используется дефолтная соль хеширования user_id | `security.log_hash_salt_default` |
| `E_FFMPEG_MISSING` | Dependency | Не найден `ffmpeg` | `ffmpeg.missing` |
| `E_WORKER_FAILED` | Worker | Общая ошибка worker-а без точной причины | `job.failed.worker` |
| `E_WORKER_RUNTIME` | Worker | Непредвиденная runtime-ошибка worker-а | `job.failed.worker` |
| `E_WORKER_FILE_NOT_FOUND` | Worker/File | После скачивания не найден итоговый файл | `job.failed.worker` |
| `E_WORKER_TRIM_RANGE_INVALID` | Worker/Trim | Некорректный диапазон для обрезки | `job.failed.worker` |
| `E_WORKER_TRIM_FAILED` | Worker/Trim | Ошибка обрезки медиа | `job.failed.worker` |
| `E_WORKER_UPLOAD_HTTP` | Worker/Upload | HTTP-ошибка загрузки на внешний хост | `job.failed.worker` |
| `E_WORKER_UPLOAD_BAD_RESPONSE` | Worker/Upload | Некорректный/не-JSON ответ upload-сервиса | `job.failed.worker` |
| `E_WORKER_UPLOAD_FAILED` | Worker/Upload | Общий провал загрузки на внешние хосты | `job.failed.worker` |
| `E_WORKER_UNKNOWN_MODE` | Worker/Logic | Worker вернул неизвестный `mode` | `job.failed.unknown_mode` |

## Что проверять в первую очередь

1. `E_REDIS_UNAVAILABLE`, `E_REDIS_CLIENT_MISSING`, `E_FFMPEG_MISSING` — инфраструктура и зависимости.
2. `E_TELEGRAM_TIMEOUT`, `E_NETWORK` — сеть/Telegram API.
3. `E_WORKER_UPLOAD_*`, `E_DOWNLOAD_FAILED` — внешние сервисы и контент-провайдеры.
4. `E_INVALID_*`, `E_COOLDOWN_ACTIVE`, `E_JOB_ALREADY_RUNNING` — пользовательский поток.

## Примеры фильтрации

- Все ошибки worker:
  - `event=job.failed.worker`
- Все сетевые таймауты:
  - `error_code=E_TIMEOUT` или `error_code=E_TELEGRAM_TIMEOUT`
- Все проблемы Redis:
  - `error_code` начинается с `E_REDIS_`
