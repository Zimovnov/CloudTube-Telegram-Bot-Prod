# Developer Guide (v3)

Документ для разработчика: где лежит логика `plan + role`, лимиты, Stars, metadata-flow и RBAC.

## 1. Ключевые модули

- `bot.py`
  - wiring хендлеров;
  - глобальный dedup `update_id`;
  - запуск metadata expiry sweeper;
  - bootstrap начального `superadmin`.

- `app/access.py`
  - модель профиля пользователя: `plan_type`, `plan_expires_at_utc`, `role`;
  - RBAC (`rbac_check`, permissions);
  - admin nonce (2-step подтверждение);
  - аудит изменений ролей/планов;
  - продление monthly-подписки.

- `app/usage.py`
  - Free usage counters `usage:{user_id}:{YYYYMM}`;
  - dedup счетчика по `job_id` (`job_counted:{job_id}`);
  - dedup платежей `payment_done:{telegram_payment_charge_id}`;
  - dedup апдейтов `update_done:{update_id}`.

- `app/policy.py`
  - вычисление пользовательской политики запуска (Free/Premium, лимиты, max duration).

- `app/metadata_store.py`
  - сессии редактирования метаданных;
  - TTL 1h;
  - валидация ввода title/artist;
  - применение тегов через `ffmpeg`;
  - очистка временных файлов.

- `app/handlers/downloads.py`
  - основной download-flow;
  - проверка Free-лимита до старта;
  - duration limit check по плану;
  - инкремент usage после успешной выдачи;
  - вызов metadata prompt для premium audio.

- `app/handlers/payments.py`
  - `/premium` и invoice `XTR`;
  - `pre_checkout_query`;
  - `successful_payment` с идемпотентностью.

- `app/handlers/admin.py`
  - админ-команды;
  - private-only + RBAC;
  - двухшаговое подтверждение (nonce).

- `app/handlers/metadata.py`
  - callback/text flow для metadata editing;
  - кнопки `Изменить данные / Оставить`;
  - `Назад / Отмена / Получить файл`;
  - фоновый sweeper истекших сессий.

## 2. Модель данных

### 2.1 Профиль пользователя
- key: `user:{user_id}:profile`
- поля:
  - `user_id`
  - `plan_type` (`free|premium_monthly|premium_lifetime`)
  - `plan_expires_at_utc`
  - `role` (`user|admin|superadmin`)
  - `updated_at_utc`

### 2.2 Настройки пользователя
- key: `user:{user_id}:settings`
- поля:
  - старые (`format`, `quality`, `trim`, `logs`, `language`)
  - новое: `metadata_prompt_enabled`

### 2.3 Лимиты/идемпотентность
- `usage:{user_id}:{YYYYMM}`: счетчик успешных выдач Free.
- `job_counted:{job_id}`: дедуп инкремента usage (TTL).
- `payment_done:{telegram_payment_charge_id}`: дедуп платежа (TTL).
- `update_done:{update_id}`: дедуп апдейта Telegram (TTL).

### 2.4 Metadata session
- `metadata:session:{session_id}` + TTL
- `metadata:active:{user_id}`
- `metadata:input:{user_id}`
- `metadata:expires` (zset)

### 2.5 Audit
- `audit:events` (list)
- event payload:
  - `target_user_id`
  - `granted_by/revoked_by`
  - `reason`
  - `created_at_utc`
  - `source`

## 3. Где проверяются критичные правила

### 3.1 Free 42/месяц
- до запуска: `app/handlers/downloads.py` -> `_start_download_flow()`.
- инкремент: `app/handlers/downloads.py` -> `download_content()` после успешной выдачи.
- атомарность/дедуп: `app/usage.py` -> Lua (`increment_usage_success_once_sync`).

### 3.2 Duration limits 3h/10h
- `app/handlers/downloads.py` -> metadata fetch до основного скачивания.
- max duration берется из `policy` snapshot на старт задачи.

### 3.3 Истечение monthly
- `app/access.py` -> `get_user_profile_sync()` auto-downgrade `premium_monthly -> free`.
- running job не прерывается: использует snapshot плана на старте.

### 3.4 Платежи Stars
- invoice: `app/handlers/payments.py` (`currency=XTR`, `75`).
- pre-checkout validation: `precheckout_handler`.
- успешный платеж: `successful_payment_handler` + dedup charge id.

### 3.5 RBAC
- централизованная проверка: `app/access.py` -> `rbac_check`.
- protected operations: `app/handlers/admin.py`.

### 3.6 2-step admin confirm
- nonce create: `create_admin_nonce`.
- nonce consume (one-time): `consume_admin_nonce`.
- apply operation после подтверждения: `apply_admin_payload_sync`.

## 4. Admin flow

1. Админ вызывает `/admin_setplan` или `/admin_setrole`.
2. Проверки:
   - private chat;
   - RBAC;
   - валидность параметров.
3. Создается nonce с TTL.
4. Пользователь нажимает `Confirm`.
5. nonce consume (один раз), повторный RBAC-check, применение операции.
6. Пишется audit event.

## 5. Payment flow

1. `/premium` или callback `sub:buy_monthly`.
2. `send_invoice` (`XTR`, `75`, `subscription_period=2592000`).
3. `pre_checkout_query -> ok`.
4. `successful_payment`:
   - dedup по `telegram_payment_charge_id`;
   - `activate_or_extend_monthly`;
   - лог `subscription.activated/renewed`.

## 6. Metadata flow

1. После успешной отправки `mp3` (Premium + `metadata_prompt_enabled`):
   - создается session + рабочий файл.
2. Пользователь проходит меню редактирования.
3. Ввод title/artist валидируется:
   - trim;
   - запрет control chars;
   - max length из config.
4. `Get file`:
   - apply metadata через `ffmpeg`;
   - отправка файла;
   - закрытие session (повторная выдача недоступна).
5. expiry sweeper удаляет истекшие session и уведомляет пользователя.

## 7. Конфиг (важное)

Смотри `app/config.py`:
- планы и лимиты:
  - `FREE_MONTHLY_LIMIT`
  - `FREE_MAX_DURATION_SECONDS`
  - `PREMIUM_MAX_DURATION_SECONDS`
- платежи:
  - `PREMIUM_MONTHLY_STARS`
  - `PREMIUM_PERIOD_SECONDS`
  - `TELEGRAM_STARS_PROVIDER_TOKEN`
- TTL/idempotency:
  - `PAYMENT_DEDUP_TTL_SECONDS`
  - `UPDATE_DEDUP_TTL_SECONDS`
  - `USAGE_COUNTER_TTL_SECONDS`
  - `JOB_COUNTED_TTL_SECONDS`
  - `ADMIN_NONCE_TTL_SECONDS`
  - `METADATA_SESSION_TTL_SECONDS`
- YouTube extraction/runtime:
  - `YTDLP_COOKIES_FILE`
  - `YTDLP_JS_RUNTIMES` (default: `node`)
  - `YTDLP_REMOTE_COMPONENTS` (default: `ejs:github`)

## 8. Тестирование

Запуск:

```powershell
python -m unittest discover -s tests -v
```

Минимум перед merge:
1. limit + usage dedup.
2. payment dedup + monthly extension.
3. RBAC deny/allow.
4. metadata validation + session lifecycle.

## 9. Definition of Done

Изменение считается завершенным только если:
- код + тесты + `RUNBOOK.md` + `DEVELOPER_GUIDE.md` + `ERROR.md` синхронны;
- новый инженер может поднять и проверить сценарии “с нуля” по документации.

## 10. Ручная приемка

Для QA-прогона критериев 1-12 используйте:

- `ACCEPTANCE_CHECKLIST.md`
