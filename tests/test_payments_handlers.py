import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "test-token")

from app.handlers import payments  # noqa: E402


class PaymentsHandlersValidationTests(unittest.TestCase):
    def test_stars_invoice_payload_validation(self):
        self.assertTrue(payments._is_valid_stars_invoice_payload("premium_monthly:42:1700000000", 42))
        self.assertFalse(payments._is_valid_stars_invoice_payload("premium_monthly:99:1700000000", 42))
        self.assertFalse(payments._is_valid_stars_invoice_payload("", 42))

    def test_validate_stars_payment(self):
        expected = int(payments.PREMIUM_MONTHLY_STARS)
        payment_ok = SimpleNamespace(currency="XTR", total_amount=expected, invoice_payload="premium_monthly:101:1")
        ok, reason = payments._validate_stars_payment(payment_ok, 101)
        self.assertTrue(ok)
        self.assertIsNone(reason)

        payment_bad_amount = SimpleNamespace(currency="XTR", total_amount=expected + 1, invoice_payload="premium_monthly:101:1")
        ok, reason = payments._validate_stars_payment(payment_bad_amount, 101)
        self.assertFalse(ok)
        self.assertEqual(reason, "unexpected_amount")

    def test_validate_yookassa_status_payload(self):
        payload_ok = {
            "amount_minor": int(payments.YOOKASSA_PREMIUM_MONTHLY_AMOUNT) * 100,
            "currency": payments.YOOKASSA_CURRENCY,
            "raw": {"metadata": {"user_id": "777", "plan_type": "premium_monthly"}},
        }
        ok, reason = payments._validate_yookassa_status_payload(payload_ok, 777)
        self.assertTrue(ok)
        self.assertIsNone(reason)

        payload_bad_currency = {
            "amount_minor": int(payments.YOOKASSA_PREMIUM_MONTHLY_AMOUNT) * 100,
            "currency": "USD",
            "raw": {"metadata": {"user_id": "777", "plan_type": "premium_monthly"}},
        }
        ok, reason = payments._validate_yookassa_status_payload(payload_bad_currency, 777)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_currency")

        payload_bad_user = {
            "amount_minor": int(payments.YOOKASSA_PREMIUM_MONTHLY_AMOUNT) * 100,
            "currency": payments.YOOKASSA_CURRENCY,
            "raw": {"metadata": {"user_id": "888", "plan_type": "premium_monthly"}},
        }
        ok, reason = payments._validate_yookassa_status_payload(payload_bad_user, 777)
        self.assertFalse(ok)
        self.assertEqual(reason, "metadata_user_mismatch")


if __name__ == "__main__":
    unittest.main()
