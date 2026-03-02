import os
import unittest

os.environ.setdefault("BOT_TOKEN", "test-token")

from app import payments_store, state  # noqa: E402


class PaymentsStoreTests(unittest.TestCase):
    def setUp(self):
        self._orig_users = dict(state.LOCAL_PAYMENT_USERS)
        self._orig_products = dict(state.LOCAL_PAYMENT_PRODUCTS)
        self._orig_orders = dict(state.LOCAL_PAYMENT_ORDERS)
        self._orig_records = dict(state.LOCAL_PAYMENT_RECORDS)
        self._orig_refunds = dict(state.LOCAL_PAYMENT_REFUNDS)
        self._orig_audit = list(state.LOCAL_PAYMENT_AUDIT)
        self._orig_sequences = dict(state.LOCAL_PAYMENT_SEQUENCES)
        self._orig_alert_counters = dict(state.LOCAL_PAYMENT_ALERT_COUNTERS)
        self._orig_db_active = getattr(payments_store, "_PAYMENTS_DB_ACTIVE", False)
        state.LOCAL_PAYMENT_USERS.clear()
        state.LOCAL_PAYMENT_PRODUCTS.clear()
        state.LOCAL_PAYMENT_ORDERS.clear()
        state.LOCAL_PAYMENT_RECORDS.clear()
        state.LOCAL_PAYMENT_REFUNDS.clear()
        state.LOCAL_PAYMENT_AUDIT.clear()
        state.LOCAL_PAYMENT_SEQUENCES.update({"users": 0, "products": 0, "orders": 0, "payments": 0, "refunds": 0, "audit": 0})
        state.LOCAL_PAYMENT_ALERT_COUNTERS.clear()
        payments_store._PAYMENTS_DB_ACTIVE = False

    def tearDown(self):
        state.LOCAL_PAYMENT_USERS.clear()
        state.LOCAL_PAYMENT_USERS.update(self._orig_users)
        state.LOCAL_PAYMENT_PRODUCTS.clear()
        state.LOCAL_PAYMENT_PRODUCTS.update(self._orig_products)
        state.LOCAL_PAYMENT_ORDERS.clear()
        state.LOCAL_PAYMENT_ORDERS.update(self._orig_orders)
        state.LOCAL_PAYMENT_RECORDS.clear()
        state.LOCAL_PAYMENT_RECORDS.update(self._orig_records)
        state.LOCAL_PAYMENT_REFUNDS.clear()
        state.LOCAL_PAYMENT_REFUNDS.update(self._orig_refunds)
        state.LOCAL_PAYMENT_AUDIT.clear()
        state.LOCAL_PAYMENT_AUDIT.extend(self._orig_audit)
        state.LOCAL_PAYMENT_SEQUENCES.clear()
        state.LOCAL_PAYMENT_SEQUENCES.update(self._orig_sequences)
        state.LOCAL_PAYMENT_ALERT_COUNTERS.clear()
        state.LOCAL_PAYMENT_ALERT_COUNTERS.update(self._orig_alert_counters)
        payments_store._PAYMENTS_DB_ACTIVE = self._orig_db_active

    def test_register_pending_and_complete_once_local(self):
        pending = payments_store.register_pending_payment_sync(
            "yookassa",
            "pay-100",
            user_id=123,
            plan_type="premium_monthly",
            amount_minor=29900,
            currency="RUB",
            status="pending",
            metadata={"stage": "created"},
        )
        self.assertEqual(pending["status"], "pending")
        self.assertFalse(pending["is_processed"])

        ok, completed = payments_store.complete_payment_once_sync(
            "yookassa",
            "pay-100",
            user_id=123,
            plan_type="premium_monthly",
            amount_minor=29900,
            currency="RUB",
            status="succeeded",
            metadata={"stage": "paid"},
        )
        self.assertTrue(ok)
        self.assertTrue(completed["is_processed"])
        self.assertEqual(completed["status"], "succeeded")

        ok_again, _ = payments_store.complete_payment_once_sync(
            "yookassa",
            "pay-100",
            user_id=123,
            plan_type="premium_monthly",
            amount_minor=29900,
            currency="RUB",
            status="succeeded",
            metadata={"stage": "paid-again"},
        )
        self.assertFalse(ok_again)

    def test_get_payment_local(self):
        payments_store.register_pending_payment_sync(
            "telegram_stars",
            "charge-1",
            user_id=999,
            plan_type="premium_monthly",
            amount_minor=75,
            currency="XTR",
            status="pending",
            metadata={"source": "telegram"},
        )
        stored = payments_store.get_payment_sync("telegram_stars", "charge-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["user_id"], 999)
        self.assertEqual(stored["currency"], "XTR")

    def test_refund_local_flow(self):
        payments_store.register_pending_payment_sync(
            "telegram_stars",
            "charge-2",
            user_id=1000,
            plan_type="premium_monthly",
            amount_minor=75,
            currency="XTR",
            status="pending",
            metadata={"source": "telegram"},
        )
        payments_store.complete_payment_once_sync(
            "telegram_stars",
            "charge-2",
            user_id=1000,
            plan_type="premium_monthly",
            amount_minor=75,
            currency="XTR",
            status="succeeded",
            metadata={"source": "telegram"},
        )
        refund = payments_store.register_refund_pending_sync(
            "telegram_stars",
            "refund-1",
            payment_provider="telegram_stars",
            payment_external_id="charge-2",
            amount_minor=75,
            currency="XTR",
            status="pending",
            reason="manual",
            metadata={"test": True},
        )
        self.assertEqual(refund["status"], "pending")
        updated = payments_store.update_refund_status_sync(refund["idempotency_key"], "succeeded")
        self.assertEqual(updated["status"], "succeeded")
        loaded = payments_store.get_refund_sync(refund["idempotency_key"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["status"], "succeeded")

    def test_refund_rejects_invalid_amount_and_currency(self):
        payments_store.register_pending_payment_sync(
            "telegram_stars",
            "charge-3",
            user_id=2000,
            plan_type="premium_monthly",
            amount_minor=75,
            currency="XTR",
            status="succeeded",
            metadata={"source": "telegram"},
        )
        payments_store.complete_payment_once_sync(
            "telegram_stars",
            "charge-3",
            user_id=2000,
            plan_type="premium_monthly",
            amount_minor=75,
            currency="XTR",
            status="succeeded",
            metadata={"source": "telegram"},
        )
        with self.assertRaises(ValueError):
            payments_store.register_refund_pending_sync(
                "telegram_stars",
                "refund-bad-amount",
                payment_provider="telegram_stars",
                payment_external_id="charge-3",
                amount_minor=0,
                currency="XTR",
                status="pending",
            )
        with self.assertRaises(ValueError):
            payments_store.register_refund_pending_sync(
                "telegram_stars",
                "refund-bad-currency",
                payment_provider="telegram_stars",
                payment_external_id="charge-3",
                amount_minor=75,
                currency="RUB",
                status="pending",
            )


if __name__ == "__main__":
    unittest.main()
