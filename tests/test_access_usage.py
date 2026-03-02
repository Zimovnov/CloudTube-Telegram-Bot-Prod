import os
import unittest
from datetime import timedelta
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "test-token")

from app import access, metadata_store, state, usage  # noqa: E402


class AccessAndUsageTests(unittest.TestCase):
    def setUp(self):
        self._orig_redis_client = state.REDIS_CLIENT
        state.REDIS_CLIENT = None

        self._orig_profiles = dict(state.LOCAL_USER_PROFILES)
        self._orig_roles = {k: set(v) for k, v in state.LOCAL_ROLE_INDEX.items()}
        self._orig_audit = list(state.LOCAL_AUDIT_EVENTS)
        self._orig_usage = dict(state.LOCAL_USAGE_COUNTERS)
        self._orig_job_counted = dict(state.LOCAL_JOB_COUNTED)
        self._orig_payment_done = dict(state.LOCAL_PAYMENT_DONE)
        self._orig_updates_done = dict(state.LOCAL_UPDATES_DONE)
        self._orig_nonces = dict(state.LOCAL_PENDING_NONCES)
        self._orig_payment_users = dict(state.LOCAL_PAYMENT_USERS)
        self._orig_payment_products = dict(state.LOCAL_PAYMENT_PRODUCTS)
        self._orig_payment_orders = dict(state.LOCAL_PAYMENT_ORDERS)
        self._orig_payment_records = dict(state.LOCAL_PAYMENT_RECORDS)
        self._orig_payment_refunds = dict(state.LOCAL_PAYMENT_REFUNDS)
        self._orig_payment_audit = list(state.LOCAL_PAYMENT_AUDIT)
        self._orig_payment_sequences = dict(state.LOCAL_PAYMENT_SEQUENCES)
        self._orig_payment_alert_counters = dict(state.LOCAL_PAYMENT_ALERT_COUNTERS)
        self._orig_meta_sessions = dict(state.LOCAL_METADATA_SESSIONS)
        self._orig_meta_input = dict(state.LOCAL_METADATA_INPUT)
        self._orig_meta_active = dict(state.LOCAL_METADATA_USER_ACTIVE)

        state.LOCAL_USER_PROFILES.clear()
        state.LOCAL_ROLE_INDEX["admin"].clear()
        state.LOCAL_ROLE_INDEX["superadmin"].clear()
        state.LOCAL_AUDIT_EVENTS.clear()
        state.LOCAL_USAGE_COUNTERS.clear()
        state.LOCAL_JOB_COUNTED.clear()
        state.LOCAL_PAYMENT_DONE.clear()
        state.LOCAL_UPDATES_DONE.clear()
        state.LOCAL_PENDING_NONCES.clear()
        state.LOCAL_PAYMENT_USERS.clear()
        state.LOCAL_PAYMENT_PRODUCTS.clear()
        state.LOCAL_PAYMENT_ORDERS.clear()
        state.LOCAL_PAYMENT_RECORDS.clear()
        state.LOCAL_PAYMENT_REFUNDS.clear()
        state.LOCAL_PAYMENT_AUDIT.clear()
        state.LOCAL_PAYMENT_SEQUENCES.update({"users": 0, "products": 0, "orders": 0, "payments": 0, "refunds": 0, "audit": 0})
        state.LOCAL_PAYMENT_ALERT_COUNTERS.clear()
        state.LOCAL_METADATA_SESSIONS.clear()
        state.LOCAL_METADATA_INPUT.clear()
        state.LOCAL_METADATA_USER_ACTIVE.clear()

    def tearDown(self):
        state.REDIS_CLIENT = self._orig_redis_client

        state.LOCAL_USER_PROFILES.clear()
        state.LOCAL_USER_PROFILES.update(self._orig_profiles)
        state.LOCAL_ROLE_INDEX["admin"].clear()
        state.LOCAL_ROLE_INDEX["admin"].update(self._orig_roles.get("admin", set()))
        state.LOCAL_ROLE_INDEX["superadmin"].clear()
        state.LOCAL_ROLE_INDEX["superadmin"].update(self._orig_roles.get("superadmin", set()))
        state.LOCAL_AUDIT_EVENTS.clear()
        state.LOCAL_AUDIT_EVENTS.extend(self._orig_audit)
        state.LOCAL_USAGE_COUNTERS.clear()
        state.LOCAL_USAGE_COUNTERS.update(self._orig_usage)
        state.LOCAL_JOB_COUNTED.clear()
        state.LOCAL_JOB_COUNTED.update(self._orig_job_counted)
        state.LOCAL_PAYMENT_DONE.clear()
        state.LOCAL_PAYMENT_DONE.update(self._orig_payment_done)
        state.LOCAL_UPDATES_DONE.clear()
        state.LOCAL_UPDATES_DONE.update(self._orig_updates_done)
        state.LOCAL_PENDING_NONCES.clear()
        state.LOCAL_PENDING_NONCES.update(self._orig_nonces)
        state.LOCAL_PAYMENT_USERS.clear()
        state.LOCAL_PAYMENT_USERS.update(self._orig_payment_users)
        state.LOCAL_PAYMENT_PRODUCTS.clear()
        state.LOCAL_PAYMENT_PRODUCTS.update(self._orig_payment_products)
        state.LOCAL_PAYMENT_ORDERS.clear()
        state.LOCAL_PAYMENT_ORDERS.update(self._orig_payment_orders)
        state.LOCAL_PAYMENT_RECORDS.clear()
        state.LOCAL_PAYMENT_RECORDS.update(self._orig_payment_records)
        state.LOCAL_PAYMENT_REFUNDS.clear()
        state.LOCAL_PAYMENT_REFUNDS.update(self._orig_payment_refunds)
        state.LOCAL_PAYMENT_AUDIT.clear()
        state.LOCAL_PAYMENT_AUDIT.extend(self._orig_payment_audit)
        state.LOCAL_PAYMENT_SEQUENCES.clear()
        state.LOCAL_PAYMENT_SEQUENCES.update(self._orig_payment_sequences)
        state.LOCAL_PAYMENT_ALERT_COUNTERS.clear()
        state.LOCAL_PAYMENT_ALERT_COUNTERS.update(self._orig_payment_alert_counters)
        state.LOCAL_METADATA_SESSIONS.clear()
        state.LOCAL_METADATA_SESSIONS.update(self._orig_meta_sessions)
        state.LOCAL_METADATA_INPUT.clear()
        state.LOCAL_METADATA_INPUT.update(self._orig_meta_input)
        state.LOCAL_METADATA_USER_ACTIVE.clear()
        state.LOCAL_METADATA_USER_ACTIVE.update(self._orig_meta_active)

    def test_bootstrap_superadmin_from_allowed_users(self):
        with mock.patch.object(access, "ALLOWED_USERS", [999001]):
            access.bootstrap_superadmin_sync()
        profile = access.get_user_profile_sync(999001)
        self.assertEqual(profile["role"], access.ROLE_SUPERADMIN)

    def test_monthly_extension_and_auto_expiry(self):
        uid = 12345
        first = access.activate_or_extend_monthly_sync(uid, charge_id="c1")
        second = access.activate_or_extend_monthly_sync(uid, charge_id="c2")
        self.assertEqual(second["plan_type"], access.PLAN_PREMIUM_MONTHLY)
        self.assertNotEqual(first["plan_expires_at_utc"], second["plan_expires_at_utc"])

        expired_dt = access.utc_now() - timedelta(seconds=10)
        access.set_user_profile_sync(
            {
                "user_id": uid,
                "plan_type": access.PLAN_PREMIUM_MONTHLY,
                "plan_expires_at_utc": access.to_utc_iso(expired_dt),
                "role": access.ROLE_USER,
            }
        )
        effective = access.get_user_profile_sync(uid)
        self.assertEqual(effective["plan_type"], access.PLAN_FREE)
        self.assertIsNone(effective["plan_expires_at_utc"])

    def test_last_superadmin_cannot_be_removed(self):
        uid = 70001
        access.set_role_sync(uid, access.ROLE_SUPERADMIN, actor_user_id=None, reason="bootstrap")
        with self.assertRaises(RuntimeError):
            access.set_role_sync(uid, access.ROLE_USER, actor_user_id=uid, reason="self demote")

    def test_usage_increment_is_deduplicated_per_job(self):
        uid = 444
        ok1, count1 = usage.increment_usage_success_once_sync(uid, "job-1")
        ok2, count2 = usage.increment_usage_success_once_sync(uid, "job-1")
        self.assertTrue(ok1)
        self.assertEqual(count1, 1)
        self.assertFalse(ok2)
        self.assertEqual(count2, 1)

    def test_usage_reset_for_month(self):
        uid = 445
        month = "202602"
        state.LOCAL_USAGE_COUNTERS[(uid, month)] = 7
        result = usage.reset_free_usage_sync(uid, month)
        self.assertEqual(result["month_label"], month)
        self.assertEqual(result["previous_count"], 7)
        self.assertEqual(usage.get_free_usage_count_sync(uid, month), 0)
        with self.assertRaises(ValueError):
            usage.reset_free_usage_sync(uid, "202613")

    def test_admin_payload_reset_usage_creates_audit(self):
        actor_id = 9001
        target_id = 9002
        month = usage.utc_month_label()
        state.LOCAL_USAGE_COUNTERS[(target_id, month)] = 5
        result = access.apply_admin_payload_sync(
            {
                "op": "reset_usage",
                "target_user_id": target_id,
                "month_label": month,
                "reason": "manual cleanup",
            },
            actor_id,
        )
        self.assertEqual(result["op"], "reset_usage")
        self.assertEqual(result["usage"]["previous_count"], 5)
        self.assertEqual(usage.get_free_usage_count_sync(target_id, month), 0)
        self.assertTrue(state.LOCAL_AUDIT_EVENTS)
        self.assertEqual(state.LOCAL_AUDIT_EVENTS[0].get("event"), "usage.reset")

    def test_payment_and_update_dedup(self):
        self.assertTrue(usage.register_payment_once_sync("pay-1"))
        self.assertFalse(usage.register_payment_once_sync("pay-1"))
        self.assertTrue(usage.register_update_once_sync(101))
        self.assertFalse(usage.register_update_once_sync(101))

    def test_metadata_validation(self):
        ok, value, key = metadata_store.validate_metadata_value("title", "  Song Name  ")
        self.assertTrue(ok)
        self.assertEqual(value, "Song Name")
        self.assertIsNone(key)

        ok, value, key = metadata_store.validate_metadata_value("artist", "Bad\x01Name")
        self.assertFalse(ok)
        self.assertIsNone(value)
        self.assertEqual(key, "metadata_invalid_control_chars")

    def test_utc_display_formatter(self):
        self.assertEqual(
            access.format_utc_iso_for_display("2026-03-27T17:20:44Z"),
            "2026-03-27 17:20:44 UTC",
        )
        self.assertEqual(access.format_utc_iso_for_display(None), "-")


if __name__ == "__main__":
    unittest.main()
