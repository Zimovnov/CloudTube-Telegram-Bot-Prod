# RUNBOOK (Production v4)

Короткая эксплуатационная инструкция для Telegram-бота с Free/Premium, оплатой через Stars/ЮKassa, PostgreSQL-хранилищем платежей, RBAC и metadata-flow.

## 1. Что теперь работает

### 1.1 Планы
- `free`:
  - 42 успешные выдачи в UTC-месяц (`YYYYMM`).
  - лимит длительности контента: 3 часа.
  - metadata edit недоступен.
- `premium_monthly`:
  - оплата через Telegram Stars (`XTR`) и/или ЮKassa (`RUB`).
  - период: 30 дней.
  - безлимит по количеству выдач.
  - лимит длительности: 10 часов.
  - metadata edit доступен.
- `premium_lifetime`:
  - по возможностям как `premium_monthly`, без даты истечения.
  - выдается вручную админ-процедурой.

### 1.2 Роли
- `user`: обычный пользователь.
- `admin`: поддержка и управление планами.
- `superadmin`: полный контроль ролей (включая выдачу/снятие `admin`).

Важно: `role` и `plan` независимы.

### 1.3 Антиспам
- 1 параллельная задача на пользователя.
- cooldown между запусками.

## 2. Как считается лимит Free (42/месяц)

1. Проверка выполняется перед запуском задачи.
2. Если `count >= 42`, задача блокируется, показывается предложение Premium.
3. Инкремент делается только после успешной выдачи:
   - файл отправлен в Telegram
   - или успешно отправлено сообщение со ссылкой.
4. Инкремент строго один раз на `job_id` (dedup).
5. Ошибки/таймауты/отмена не увеличивают счетчик.
6. Если задача завершилась в новом месяце, учитывается месяц завершения (UTC).

## 3. Как работает оплата Premium

1. Пользователь вызывает `/premium` или кнопку `Купить Premium`.
2. Если настроены оба канала, бот показывает выбор: Stars или ЮKassa.
3. Stars:
   - `currency = XTR`
   - `amount = PREMIUM_MONTHLY_STARS`
   - `subscription_period = PREMIUM_PERIOD_SECONDS`.
4. ЮKassa:
   - создается платеж в API ЮKassa,
   - пользователь платит по `confirmation_url`,
   - затем нажимает кнопку `Проверить оплату`.
5. Любой успешный платеж обрабатывается идемпотентно через PostgreSQL (`orders` + `payments`).
6. Продление:
   - `new_expire = max(now_utc, current_expires_at) + 30 дней`.
7. При истечении monthly-плана пользователь автоматически становится `free`.

## 4. Metadata-flow (Premium, audio-only)

1. После успешной отправки mp3 (если включено в настройках) показывается:
   - `Изменить данные`
   - `Оставить`
2. В меню редактирования:
   - `Изменить название`
   - `Изменить автора`
   - `Отмена`
3. После изменения хотя бы одного поля появляется `Получить файл`.
4. По `Получить файл`:
   - изменения применяются к рабочему файлу
   - edited-файл отправляется
   - сессия закрывается.
5. TTL сессии: 1 час с последнего действия.
6. По TTL удаляются файл и состояние; пользователю отправляется:
   - `Сессия редактирования истекла (1 час). Отправьте ссылку заново.`

## 5. Безопасность (операционный минимум)

- dedup Telegram updates по `update_id`.
- dedup платежей по уникальным ключам в PostgreSQL:
  - `payments(provider, provider_payment_id)`
  - `payments(idempotency_key)`
  - `orders(idempotency_key)`
- RBAC-проверка на каждой защищенной админ-операции.
- критичные операции только в приватном чате с ботом.
- админ-операции изменения ролей/планов идут через двухшаговое подтверждение (nonce + TTL).
- запреты:
  - self-escalation
  - снятие последнего `superadmin`.
- audit events пишутся для изменений ролей/планов.

## 6. Запуск/перезапуск

Работать из корня проекта:

```powershell
cd c:\Users\zimov\soundcloud_bot
```

Первый запуск / после изменений:

```powershell
docker compose up -d --build
```

Перезапуск только бота:

```powershell
docker compose up -d --build bot
```

Если меняли только `.env`:

```powershell
docker compose up -d --force-recreate bot
```

Остановить:

```powershell
docker compose down
```

## 7. Логи и health-check

Логи бота:

```powershell
docker compose logs -f bot
```

Локальный лог-файл:

```powershell
Get-Content .\bot.log -Tail 200
```

Проверка контейнеров:

```powershell
docker compose ps
```

Проверка Redis:

```powershell
docker compose ps redis
docker compose logs --tail 200 redis
```

Проверка PostgreSQL:

```powershell
docker compose ps postgres
docker compose logs --tail 200 postgres
```

## 8. Что делать если...

### 8.1 Не проходит оплата / дубли платежей
- Смотреть события `payment.*`, `subscription.*`, `payments.db.*`.
- Проверить, что нет всплеска `payment.duplicate_ignored`.
- Для Stars: проверить currency `XTR` и корректность invoice.
- Для ЮKassa: проверить `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`, `YOOKASSA_RETURN_URL`.
- Убедиться, что PostgreSQL доступен и созданы таблицы:
  - `users`
  - `products`
  - `orders`
  - `payments`
  - `refunds`
  - `audit_log`

### 8.2 Пользователь жалуется на блокировку Free
- Проверить `limit.free.blocked`.
- Проверить текущий UTC-месяц и `usage:{user_id}:{YYYYMM}`.
- Подтвердить, что инкремент идет только на успешные выдачи.

### 8.3 RBAC-отказы
- Проверить `rbac.denied`.
- Убедиться, что операция выполняется в private-чате.
- Проверить роль пользователя (`/admin_profile <user_id>` у superadmin).

### 8.4 Metadata session expired
- Проверить `metadata.edit.expired`.
- Это штатное поведение TTL=1h.
- Пользователь должен отправить ссылку заново.

### 8.5 Redis недоступен
- Проверить `E_REDIS_UNAVAILABLE`, контейнер и пароль.
- Бот может работать с локальным fallback, но для прод это нежелательно.

### 8.6 YouTube: `Requested format is not available` / только `mhtml` (storyboard)
- Причина: `yt-dlp` не может решить YouTube JS challenge (`n challenge`), форматы видео/аудио не извлекаются.
- Проверить в контейнере:
  - `node -v` (должен быть установлен runtime)
  - наличие `cookies.txt` и что файл не пустой
- Для решения challenge включены настройки:
  - `YTDLP_JS_RUNTIMES=node`
  - `YTDLP_REMOTE_COMPONENTS=ejs:github`
- После обновления `.env` или кода перезапустить:
  - `docker compose up -d --build bot`
  - `docker compose logs -f bot`

## 9. Инциденты (минимальный playbook)

1. Экстренный отзыв админ-прав:
   - superadmin выполняет `admin_setrole <user_id> user <reason>` с подтверждением.
2. Временная заморозка ручных назначений:
   - отключить доступ к админ-командам на уровне бота/роутинга (hotfix).
3. Ротация секретов:
   - BOT_TOKEN
   - REDIS_PASSWORD
   - другие секреты в env/secret-store.

## 10. Проверка воспроизводимости “с нуля”

Функционал считается завершенным только если:
- обновлены `RUNBOOK.md`, `DEVELOPER_GUIDE.md`, `ERROR.md`;
- новый инженер может поднять проект по шагам из этого runbook;
- canary-проверки проходят:
  1. Free-limit 42/месяц.
  2. Premium-платеж + продление.
  3. Metadata-flow + TTL.
  4. RBAC и двухшаговые admin-операции.

## 11. Ручной чеклист приемки

Пошаговые сценарии AC-1..AC-12 вынесены в:

- `ACCEPTANCE_CHECKLIST.md`
- `ADMIN_COMMANDS_GUIDE.md` (практический гайд по админ-командам и примерам)

## 12. Ежедневный Чеклист (Прод, 3-5 минут)

1. Проверка контейнеров:
```powershell
docker compose ps
```
Ожидаемо: `bot`, `postgres`, `redis` в `Up`/`healthy`.

2. Проверка ошибок в логах бота:
```powershell
docker compose logs --tail 200 bot
```
Проверь, что нет `payments.db.unavailable`, `authentication failed`, частых `payment.flow.failed`.

3. Быстрый контроль таблиц:
```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "\dt"
```
Ожидаемо: `users`, `products`, `orders`, `payments`, `refunds`, `audit_log`.

4. Проверка зависших платежей:
```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "SELECT id, provider, provider_payment_id, status, is_processed, created_at FROM payments WHERE status IN ('pending','waiting_for_capture') ORDER BY created_at ASC LIMIT 20;"
```
Если есть очень старые записи - проверь вручную источник платежа.

5. Проверка дублей (идемпотентность):
```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "SELECT provider, provider_payment_id, COUNT(*) FROM payments GROUP BY provider, provider_payment_id HAVING COUNT(*) > 1;"
```
Ожидаемо: 0 строк.

6. Проверка неучтенных успешных платежей:
```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "SELECT id, provider, provider_payment_id, status, is_processed, updated_at FROM payments WHERE status='succeeded' AND is_processed=false ORDER BY updated_at DESC LIMIT 20;"
```
Ожидаемо: пусто.

7. Проверка всплеска ошибок по статусам (24 часа):
```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "SELECT status, COUNT(*) FROM payments WHERE created_at > NOW() - INTERVAL '24 hours' GROUP BY status ORDER BY COUNT(*) DESC;"
```
Если `failed/blocked/canceled` резко растут - инцидент, смотри логи `payment.*`.

8. Проверка refunds:
```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "SELECT id, payment_id, status, amount_minor, currency, created_at FROM refunds ORDER BY created_at DESC LIMIT 20;"
```
Проверь корректность статусов и сумм.

9. Проверка audit-log:
```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "SELECT id, event_type, severity, provider, created_at FROM audit_log ORDER BY id DESC LIMIT 30;"
```
Проверь, что нет аномально большого количества `WARNING`.

10. Контроль секретов (раз в неделю):
- убедиться, что `.env` не попал в git/логи;
- проверить актуальность:
  - `POSTGRES_PASSWORD`
  - `PAYMENTS_DATABASE_URL`
  - `YOOKASSA_SECRET_KEY`
  - `BOT_TOKEN`
