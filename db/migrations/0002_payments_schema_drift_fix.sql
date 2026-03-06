ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS invalid_reason TEXT NULL;
