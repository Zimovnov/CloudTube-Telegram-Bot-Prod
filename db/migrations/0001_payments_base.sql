CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    plan_type TEXT NOT NULL,
    amount_minor BIGINT NULL,
    currency TEXT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata_json TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,
    external_order_id TEXT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    amount_minor BIGINT NULL,
    currency TEXT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NULL,
    completed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_provider_external
    ON orders(provider, external_order_id)
    WHERE external_order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS payments (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_payment_id TEXT NOT NULL,
    external_id TEXT NULL,
    telegram_charge_id TEXT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    amount_minor BIGINT NULL,
    currency TEXT NULL,
    status TEXT NOT NULL,
    is_processed BOOLEAN NOT NULL DEFAULT FALSE,
    processed_at TIMESTAMPTZ NULL,
    invalid_reason TEXT NULL,
    metadata_json TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_provider_payment
    ON payments(provider, provider_payment_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_provider_external
    ON payments(provider, external_id)
    WHERE external_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_telegram_charge
    ON payments(telegram_charge_id)
    WHERE telegram_charge_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payments_provider_status_processed_created
    ON payments(provider, status, is_processed, created_at DESC);

CREATE TABLE IF NOT EXISTS refunds (
    id BIGSERIAL PRIMARY KEY,
    payment_id BIGINT NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_refund_id TEXT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    amount_minor BIGINT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NULL,
    metadata_json TEXT NULL,
    processed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_refunds_provider_id
    ON refunds(provider, provider_refund_id)
    WHERE provider_refund_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'INFO',
    provider TEXT NULL,
    user_id BIGINT NULL,
    order_id BIGINT NULL,
    payment_id BIGINT NULL,
    refund_id BIGINT NULL,
    message TEXT NULL,
    details_json TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_event_created
    ON audit_log(event_type, created_at DESC);

CREATE TABLE IF NOT EXISTS subscription_entitlements (
    user_id BIGINT PRIMARY KEY,
    plan_type TEXT NOT NULL,
    expires_at_utc TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_provider TEXT NULL,
    source_payment_id TEXT NULL,
    version BIGINT NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_entitlements_plan_expires
    ON subscription_entitlements(plan_type, expires_at_utc);

CREATE TABLE IF NOT EXISTS payment_sessions (
    session_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    plan_type TEXT NOT NULL,
    payment_id TEXT NULL,
    payment_url TEXT NULL,
    status TEXT NOT NULL,
    expires_at_utc TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payment_sessions_provider_status_expires
    ON payment_sessions(provider, status, expires_at_utc DESC);
