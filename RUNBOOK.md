# RUNBOOK

## 1. What changed

- Premium activation for payment flows is now finalized inside PostgreSQL in the same transaction as `payments.is_processed=true`.
- YooKassa is no longer dependent on the user's "Check payment" button. The bot supports:
  - `POST /webhooks/yookassa`
  - background reconciliation of `pending` and `waiting_for_capture`
  - manual "Check payment" as an auxiliary UX path
- Runtime DDL was removed from the app. Schema changes are applied only through SQL migrations.
- In strict production mode the app fails fast on insecure or incomplete payment/Redis configuration.

## 2. Core components

- PostgreSQL:
  - payments, orders, refunds, audit
  - `subscription_entitlements`
  - `payment_sessions`
  - `schema_migrations`
- Redis:
  - settings, RBAC helper state, throttling, nonces, usage counters
- Bot process:
  - Telegram polling
  - embedded `aiohttp` webhook listener
  - YooKassa reconciliation loop

## 3. Required env

Mandatory for the migration step:

```powershell
BOT_TOKEN=...
PAYMENTS_DATABASE_URL=postgresql://...
MIGRATIONS_DATABASE_URL=postgresql://...
REDIS_URL=redis://...   # dev
```

Production-only expectations:

```powershell
APP_ENV=prod
PAYMENTS_STRICT_PROD=1
PAYMENTS_DB_REQUIRED=1
PAYMENTS_ALLOW_INMEMORY_FALLBACK=0
REDIS_REQUIRED=1
PAYMENTS_DATABASE_URL=postgresql://...?...sslmode=require
MIGRATIONS_DATABASE_URL=postgresql://...?...sslmode=require
REDIS_URL=rediss://...
YOOKASSA_WEBHOOK_ENABLED=1
YOOKASSA_WEBHOOK_BIND_HOST=0.0.0.0
YOOKASSA_WEBHOOK_BIND_PORT=8080
YOOKASSA_WEBHOOK_PATH=/webhooks/yookassa
YOOKASSA_RECONCILE_ENABLED=1
YOOKASSA_RECONCILE_INTERVAL_SEC=60
```

Notes:

- In local docker-compose the default `APP_ENV` is `dev`, so strict TLS enforcement is not applied by default.
- In production, insecure PostgreSQL/Redis transport blocks startup.

## 4. Bootstrap and deployment

From project root:

```powershell
cd c:\Users\zimov\soundcloud_bot
```

Build images:

```powershell
docker compose build
```

Apply migrations:

```powershell
docker compose run --rm migrate
```

Start infra and bot:

```powershell
docker compose up -d postgres redis bot
```

Rebuild only the bot:

```powershell
docker compose up -d --build bot
```

Stop everything:

```powershell
docker compose down
```

## 5. Test commands

Project venv unit/regression run:

```powershell
venv\Scripts\python.exe -m pytest -q
```

Live PostgreSQL + Redis smoke inside the compose network:

```powershell
docker compose up -d postgres redis
docker compose run --rm migrate
docker compose run --rm --no-deps -e RUN_PAYMENTS_INTEGRATION=1 bot python -m pytest -q -p no:cacheprovider tests/test_payments_integration_smoke.py
```

YooKassa mock reconciliation smoke only:

```powershell
docker compose run --rm --no-deps -e RUN_PAYMENTS_INTEGRATION=1 bot python -m pytest -q -p no:cacheprovider tests/test_payments_integration_smoke.py -k yookassa
```

YooKassa webhook HTTP endpoint smoke:

```powershell
docker compose run --rm --no-deps -e RUN_PAYMENTS_INTEGRATION=1 bot python -m pytest -q -p no:cacheprovider tests/test_payments_integration_smoke.py -k webhook_http_endpoint
```

What the live smoke verifies:

- real PostgreSQL migrations and payment schema are usable
- real Redis-backed throttling path is usable
- payment finalization is atomic and idempotent
- `payment_sessions` reuse the same pending YooKassa payment
- mocked YooKassa reconciliation finalizes a pending payment without user action
- lifetime entitlement is not downgraded by a monthly payment

## 6. Webhook and payment operations

Internal webhook endpoint:

```text
POST /webhooks/yookassa
```

Webhook behavior:

1. Accept event.
2. Extract `payment_id`.
3. Fetch payment state from YooKassa server-to-server.
4. Validate amount/currency and strict metadata:
   - `metadata.user_id` required
   - `metadata.plan_type` required
   - values must match expected payment data
5. Finalize payment and entitlement in one PostgreSQL transaction.

Reconciliation behavior:

- polls unprocessed `pending` and `waiting_for_capture`
- re-fetches payment status from YooKassa
- finalizes successful payments without user interaction

## 7. YooKassa sandbox and mock

For local development there are two supported provider modes:

- mock mode for CI/local smoke: the test suite monkeypatches `app.payment_service.get_yookassa_payment`, so PostgreSQL finalization and reconciliation are exercised without live YooKassa credentials
- sandbox/real API mode: keep the same server-side flow, but point env to valid YooKassa credentials and callback URL

Minimum env for sandbox/real API:

```powershell
YOOKASSA_SHOP_ID=...
YOOKASSA_SECRET_KEY=...
YOOKASSA_RETURN_URL=https://example.test/payments/return
YOOKASSA_API_BASE=https://api.yookassa.ru/v3
YOOKASSA_WEBHOOK_ENABLED=1
YOOKASSA_WEBHOOK_PATH=/webhooks/yookassa
YOOKASSA_RECONCILE_ENABLED=1
```

Mock mode is the default recommendation before real shop activation, because it validates:

- strict metadata validation
- duplicate-safe finalization
- reconciliation behavior
- notification path after entitlement activation

## 8. Health checks and logs

Containers:

```powershell
docker compose ps
```

Bot logs:

```powershell
docker compose logs -f bot
```

Migration logs:

```powershell
docker compose logs --tail 200 migrate
```

PostgreSQL logs:

```powershell
docker compose logs --tail 200 postgres
```

Redis logs:

```powershell
docker compose logs --tail 200 redis
```

Check schema:

```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "\dt"
```

Expected payment tables include:

- `orders`
- `payments`
- `refunds`
- `audit_log`
- `subscription_entitlements`
- `payment_sessions`
- `schema_migrations`

Check unprocessed YooKassa payments:

```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "SELECT provider_payment_id, status, is_processed, created_at FROM payments WHERE provider='yookassa' AND status IN ('pending','waiting_for_capture') ORDER BY created_at ASC LIMIT 20;"
```

Check entitlements:

```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "SELECT user_id, plan_type, expires_at_utc, source_provider, source_payment_id, version FROM subscription_entitlements ORDER BY updated_at DESC LIMIT 20;"
```

## 9. Incident playbook

If YooKassa payment succeeded but Premium is missing:

1. Check bot logs for `payment.invalid`, `payment.webhook.failed`, `payment.reconcile.failed`.
2. Query the payment row:

```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "SELECT provider_payment_id, status, is_processed, invalid_reason, updated_at FROM payments WHERE provider='yookassa' ORDER BY updated_at DESC LIMIT 20;"
```

3. Query entitlement:

```powershell
docker compose exec postgres psql -U soundbot -d soundbot -c "SELECT * FROM subscription_entitlements WHERE user_id=<telegram_user_id>;"
```

4. If the payment is still `pending` or `waiting_for_capture`, verify webhook delivery and reconciliation settings.

If startup fails in production:

1. Confirm `PAYMENTS_STRICT_PROD=1`.
2. Confirm PostgreSQL DSN includes `sslmode=require` or stronger.
3. Confirm Redis uses `rediss://`.
4. Confirm `MIGRATIONS_DATABASE_URL` is set.
5. Confirm the schema was applied with `docker compose run --rm migrate`.

If webhook is not receiving events:

1. Check reverse proxy routing to `YOOKASSA_WEBHOOK_PATH`.
2. Confirm the container listens on `YOOKASSA_WEBHOOK_BIND_PORT`.
3. Check `payment.webhook.started` in logs.
4. Confirm upstream TLS termination and public callback URL.

## 10. Production checklist

Daily:

1. `docker compose ps`
2. `docker compose logs --tail 200 bot`
3. inspect recent `payment.invalid`, `payment.webhook.failed`, `payment.reconcile.failed`
4. inspect old pending YooKassa payments
5. inspect recent entitlement updates

Before release:

1. apply migrations
2. run test suite
3. confirm strict prod env
4. confirm webhook routing
5. confirm bot runs as non-root container
