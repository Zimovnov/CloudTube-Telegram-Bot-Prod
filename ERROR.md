# ERROR CODES (v3)

Справочник по `error_code` в structured logs.

Формат использования:
- `cause` — причина;
- `impact` — последствия;
- `recovery` — что делать.

## 1. Access / RBAC / Security

### `E_AUTH_DENIED`
- cause: доступ запрещен.
- impact: операция не выполняется.
- recovery: проверить роль/контекст пользователя.

### `E_RBAC_DENIED`
- cause: нет нужного permission.
- impact: защищенная операция отклонена.
- recovery: проверить `role`, private chat, RBAC-маршрут.

### `E_ADMIN_NONCE_INVALID`
- cause: некорректный nonce подтверждения.
- impact: admin-операция не применяется.
- recovery: выполнить команду заново, получить новый nonce.

### `E_ADMIN_NONCE_EXPIRED`
- cause: nonce истек по TTL или уже использован.
- impact: admin-операция не применяется.
- recovery: повторить admin-команду и подтвердить в течение TTL.

### `E_ADMIN_SELF_ESCALATION`
- cause: попытка self-escalation роли.
- impact: операция заблокирована.
- recovery: использовать superadmin-процедуру для другого target_user_id.

### `E_LAST_SUPERADMIN`
- cause: попытка снять роль у последнего superadmin.
- impact: операция заблокирована.
- recovery: сначала назначить второго superadmin, затем повторить.

### `E_SECURITY_WEAK_HASH_SALT`
- cause: используется дефолтный `LOG_USER_HASH_SALT`.
- impact: слабая приватность логов.
- recovery: задать уникальный `LOG_USER_HASH_SALT`, включить `LOG_HASH_SALT_STRICT=1`.

## 2. Limits / Policy

### `E_FREE_LIMIT_REACHED`
- cause: Free достиг 42 успешных выдач за UTC-месяц.
- impact: запуск новых задач блокируется.
- recovery: дождаться нового UTC-месяца или оформить Premium.

### `E_DURATION_LIMIT`
- cause: длительность контента превышает лимит плана.
- impact: задача отклоняется до скачивания.
- recovery: выбрать другой контент/диапазон или другой план.

### `E_COOLDOWN_ACTIVE`
- cause: сработал cooldown между запусками.
- impact: запрос не стартует.
- recovery: дождаться таймера.

### `E_JOB_ALREADY_RUNNING`
- cause: уже есть активная задача пользователя.
- impact: второй запуск блокируется.
- recovery: дождаться завершения текущей задачи.

## 3. Payments

### `E_PAYMENT_INVALID`
- cause: невалидный платеж (например, валюта/charge_id).
- impact: подписка не активируется.
- recovery: проверить invoice/currency `XTR`, повторить оплату.

### `E_PAYMENT_DUPLICATE`
- cause: duplicate `successful_payment` по `telegram_payment_charge_id`.
- impact: повторная активация игнорируется.
- recovery: действий не требуется; это защита идемпотентности.

## 4. Metadata Session

### `E_METADATA_NOT_ALLOWED`
- cause: попытка доступа к чужой metadata session.
- impact: операция отклоняется.
- recovery: использовать только собственную активную сессию.

### `E_METADATA_SESSION_EXPIRED`
- cause: TTL metadata session истек (1 час).
- impact: рабочий файл и состояние удалены.
- recovery: отправить исходную ссылку заново.

### `E_METADATA_INVALID_INPUT`
- cause: невалидный ввод title/artist (пусто/control chars/слишком длинно).
- impact: поле не обновляется, flow остается в ожидании.
- recovery: отправить корректное значение.

## 5. Telegram / Redis / Infra

### `E_UPDATE_DUPLICATE`
- cause: duplicate Telegram update (`update_id`).
- impact: бизнес-обработка обновления пропускается.
- recovery: действий не требуется (штатная dedup-защита).

### `E_REDIS_UNAVAILABLE`
- cause: Redis недоступен.
- impact: fallback в локальное состояние или stop (зависит от `REDIS_REQUIRED`).
- recovery: проверить Redis контейнер/сеть/пароль/URL.

### `E_REDIS_CLIENT_MISSING`
- cause: Redis python client отсутствует.
- impact: fallback в local state или stop.
- recovery: установить зависимость `redis`, пересобрать образ.

### `E_REDIS_DISABLED`
- cause: `REDIS_URL` пустой.
- impact: локальный fallback, нет полноценных гарантий shared-state.
- recovery: задать корректный `REDIS_URL`.

### `E_REDIS_ISSUE`
- cause: ошибка Redis операции.
- impact: возможен временный fallback.
- recovery: проверить latency/сетевую стабильность Redis.

### `E_FFMPEG_MISSING`
- cause: ffmpeg не найден.
- impact: скачивание/metadata apply невозможно.
- recovery: установить ffmpeg, проверить PATH/env.

## 6. Download / Worker

### `E_TIMEOUT` / `E_TELEGRAM_TIMEOUT`
- cause: сетевой или API timeout.
- impact: задача может завершиться с ошибкой.
- recovery: повторить позже, проверить сеть/Telegram API.

### `E_NETWORK`
- cause: сетевой сбой внешних сервисов.
- impact: ошибка скачивания/отправки.
- recovery: повторить позже, проверить сетевой маршрут.

### `E_DOWNLOAD_FAILED`
- cause: `yt-dlp` не смог скачать контент.
- impact: задача завершена с ошибкой.
- recovery: проверить ссылку/доступность/обновить `yt-dlp`.

### `E_HTTP_NOT_FOUND`
- cause: контент недоступен (404/removed/private).
- impact: задача завершена с ошибкой.
- recovery: проверить ссылку вручную.

### `E_FILE_NOT_FOUND` / `E_WORKER_FILE_NOT_FOUND`
- cause: итоговый файл не найден после обработки.
- impact: отправка невозможна.
- recovery: повторить задачу, проверить диск/ffmpeg output.

### `E_WORKER_TRIM_RANGE_INVALID`
- cause: недопустимый диапазон обрезки.
- impact: задача не завершена успешно.
- recovery: отправить корректный диапазон.

### `E_WORKER_TRIM_FAILED`
- cause: сбой обрезки медиа.
- impact: задача завершена с ошибкой.
- recovery: повторить или отключить trim.

### `E_WORKER_UPLOAD_HTTP` / `E_WORKER_UPLOAD_BAD_RESPONSE` / `E_WORKER_UPLOAD_FAILED`
- cause: ошибка внешнего upload-host.
- impact: нельзя выдать ссылку на большой файл.
- recovery: повторить позже, проверить доступность upload endpoints.

### `E_WORKER_UNKNOWN_MODE`
- cause: worker вернул неизвестный mode.
- impact: результат не может быть выдан.
- recovery: проверить логи и контракт результата worker.

### `E_WORKER_CANCELLED`
- cause: задача отменена пользователем/системой.
- impact: задача остановлена.
- recovery: запустить заново при необходимости.

### `E_WORKER_STALLED`
- cause: watchdog зафиксировал stall.
- impact: задача принудительно остановлена.
- recovery: повторить позже, проверить сеть.

### `E_WORKER_RUNTIME` / `E_WORKER_FAILED`
- cause: непредвиденная ошибка runtime.
- impact: задача завершена с ошибкой.
- recovery: смотреть stack/log context, фиксить код/конфиг.

### `E_UNKNOWN`
- cause: ошибка не классифицирована.
- impact: общая ошибка.
- recovery: смотреть событие/trace и добавить точную классификацию.
