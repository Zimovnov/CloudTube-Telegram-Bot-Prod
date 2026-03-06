import os
import unittest

os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("BOT_TOKEN", "test-token")

from app import payments_store  # noqa: E402


class PaymentsStoreReadinessTests(unittest.TestCase):
    def test_store_not_ready_without_initialized_db(self):
        self.assertFalse(payments_store.payments_store_is_ready())

    def test_payment_session_constants_exist(self):
        self.assertIn("pending", payments_store.PAYMENT_STATUSES)
        self.assertIn("waiting_for_capture", payments_store.ACTIVE_SESSION_STATUSES)


if __name__ == "__main__":
    unittest.main()
