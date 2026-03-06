import asyncio
import os
import uuid

import pytest
from aiohttp import ClientSession, web

os.environ.setdefault("BOT_TOKEN", "test-token")

from app import jobs, state  # noqa: E402
from app.access import PLAN_PREMIUM_LIFETIME, PLAN_PREMIUM_MONTHLY  # noqa: E402
from app.config import YOOKASSA_CURRENCY, YOOKASSA_PREMIUM_MONTHLY_AMOUNT  # noqa: E402
from app.payment_runtime import PTB_APPLICATION_KEY, _handle_yookassa_webhook  # noqa: E402
from app.payment_service import PROVIDER_YOOKASSA, run_yookassa_reconciliation  # noqa: E402
from app.payments_store import (  # noqa: E402
    acquire_payment_session_sync,
    attach_payment_session_sync,
    finalize_verified_payment_sync,
    get_effective_entitlement_sync,
    get_payment_session_sync,
    get_payment_sync,
    init_payments_store_sync,
    list_reconcilable_payments_sync,
    register_pending_payment_sync,
    set_plan_entitlement_sync,
)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_PAYMENTS_INTEGRATION") != "1",
        reason="set RUN_PAYMENTS_INTEGRATION=1 to run live Postgres/Redis smoke tests",
    ),
]


def _unique_user_id():
    return 8_000_000_000 + (uuid.uuid4().int % 1_000_000_000)


def _unique_payment_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class _DummyBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append({"chat_id": int(chat_id), "text": str(text)})


class _DummyApplication:
    def __init__(self):
        self.bot = _DummyBot()
        self.bot_data = {}


@pytest.fixture(scope="module", autouse=True)
def _live_backends_ready():
    assert init_payments_store_sync() is True
    jobs.init_redis_client()
    assert state.REDIS_CLIENT is not None
    assert state.REDIS_CLIENT.ping() is True
    yield
    try:
        state.REDIS_CLIENT.close()
    except Exception:
        pass
    state.REDIS_CLIENT = None


def test_live_finalize_verified_payment_is_atomic_and_idempotent():
    user_id = _unique_user_id()
    provider = "integration_smoke_pg"
    payment_id = _unique_payment_id("payment")
    amount_minor = int(YOOKASSA_PREMIUM_MONTHLY_AMOUNT) * 100

    pending = register_pending_payment_sync(
        provider,
        payment_id,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=amount_minor,
        currency=YOOKASSA_CURRENCY,
        status="pending",
        metadata={"source": "pytest-smoke"},
    )
    assert pending["is_processed"] is False

    processed_now, payment_record, entitlement = finalize_verified_payment_sync(
        provider,
        payment_id,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=amount_minor,
        currency=YOOKASSA_CURRENCY,
        status="succeeded",
        metadata={"source": "pytest-smoke", "stage": "finalized"},
    )
    assert processed_now is True
    assert payment_record["is_processed"] is True
    assert payment_record["status"] == "succeeded"
    assert entitlement["plan_type"] == PLAN_PREMIUM_MONTHLY
    assert entitlement["source_payment_id"] == payment_id
    assert entitlement["version"] == 1

    duplicate_processed, duplicate_payment, duplicate_entitlement = finalize_verified_payment_sync(
        provider,
        payment_id,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=amount_minor,
        currency=YOOKASSA_CURRENCY,
        status="succeeded",
        metadata={"source": "pytest-smoke", "stage": "duplicate"},
    )
    assert duplicate_processed is False
    assert duplicate_payment["is_processed"] is True
    assert duplicate_entitlement["version"] == entitlement["version"]

    stored = get_payment_sync(provider, payment_id)
    effective = get_effective_entitlement_sync(user_id)
    assert stored["is_processed"] is True
    assert effective["plan_type"] == PLAN_PREMIUM_MONTHLY
    assert effective["source_payment_id"] == payment_id


def test_live_redis_throttle_path_works():
    user_id = _unique_user_id()
    redis_key = jobs._redis_key("cooldown", "settings", user_id)
    state.REDIS_CLIENT.delete(redis_key)

    assert jobs.allow_settings_change(user_id) is True
    assert jobs.allow_settings_change(user_id) is False

    state.REDIS_CLIENT.delete(redis_key)


def test_live_payment_session_reuse_and_mock_yookassa_reconcile(monkeypatch):
    user_id = _unique_user_id()
    session_key = f"it-session-{uuid.uuid4().hex[:16]}"
    payment_id = _unique_payment_id("yk")
    amount_minor = int(YOOKASSA_PREMIUM_MONTHLY_AMOUNT) * 100

    created = acquire_payment_session_sync(
        session_key,
        provider=PROVIDER_YOOKASSA,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        ttl_seconds=300,
    )
    assert created["action"] == "create"

    attached = attach_payment_session_sync(
        session_key,
        payment_id=payment_id,
        payment_url="https://example.test/confirm",
        status="pending",
        ttl_seconds=300,
    )
    assert attached["payment_id"] == payment_id

    reused = acquire_payment_session_sync(
        session_key,
        provider=PROVIDER_YOOKASSA,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        ttl_seconds=300,
    )
    assert reused["action"] == "reuse"
    assert reused["payment_id"] == payment_id
    assert get_payment_session_sync(session_key)["payment_id"] == payment_id

    register_pending_payment_sync(
        PROVIDER_YOOKASSA,
        payment_id,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=amount_minor,
        currency=YOOKASSA_CURRENCY,
        status="pending",
        metadata={"source": "pytest-mock-yookassa"},
    )
    pending = list_reconcilable_payments_sync(provider=PROVIDER_YOOKASSA, limit=20)
    assert any(item["payment_id"] == payment_id for item in pending)

    async def _fake_get_payment(external_payment_id):
        assert external_payment_id == payment_id
        return {
            "id": external_payment_id,
            "status": "succeeded",
            "paid": True,
            "amount_minor": amount_minor,
            "currency": YOOKASSA_CURRENCY,
            "raw": {
                "id": external_payment_id,
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "299.00", "currency": YOOKASSA_CURRENCY},
                "metadata": {
                    "user_id": str(user_id),
                    "plan_type": PLAN_PREMIUM_MONTHLY,
                },
            },
        }

    monkeypatch.setattr("app.payment_service.get_yookassa_payment", _fake_get_payment)

    application = _DummyApplication()
    processed = asyncio.run(run_yookassa_reconciliation(application, limit=20))
    assert processed == 1

    payment = get_payment_sync(PROVIDER_YOOKASSA, payment_id)
    entitlement = get_effective_entitlement_sync(user_id)
    assert payment["is_processed"] is True
    assert payment["status"] == "succeeded"
    assert entitlement["plan_type"] == PLAN_PREMIUM_MONTHLY
    assert entitlement["source_payment_id"] == payment_id
    assert application.bot.messages and application.bot.messages[0]["chat_id"] == user_id

    processed_again = asyncio.run(run_yookassa_reconciliation(application, limit=20))
    assert processed_again == 0


def test_live_webhook_http_endpoint_mock_flow(monkeypatch):
    user_id = _unique_user_id()
    payment_id = _unique_payment_id("webhook")
    amount_minor = int(YOOKASSA_PREMIUM_MONTHLY_AMOUNT) * 100

    register_pending_payment_sync(
        PROVIDER_YOOKASSA,
        payment_id,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=amount_minor,
        currency=YOOKASSA_CURRENCY,
        status="pending",
        metadata={"source": "pytest-webhook"},
    )

    async def _fake_get_payment(external_payment_id):
        assert external_payment_id == payment_id
        return {
            "id": external_payment_id,
            "status": "succeeded",
            "paid": True,
            "amount_minor": amount_minor,
            "currency": YOOKASSA_CURRENCY,
            "raw": {
                "id": external_payment_id,
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "299.00", "currency": YOOKASSA_CURRENCY},
                "metadata": {
                    "user_id": str(user_id),
                    "plan_type": PLAN_PREMIUM_MONTHLY,
                },
            },
        }

    monkeypatch.setattr("app.payment_service.get_yookassa_payment", _fake_get_payment)

    async def _exercise_webhook():
        application = _DummyApplication()
        web_app = web.Application()
        web_app[PTB_APPLICATION_KEY] = application
        web_app.router.add_post("/webhooks/yookassa", _handle_yookassa_webhook)
        runner = web.AppRunner(web_app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        socket = site._server.sockets[0]
        host, port = socket.getsockname()[:2]
        try:
            async with ClientSession() as session:
                response = await session.post(
                    f"http://{host}:{port}/webhooks/yookassa",
                    json={"event": "payment.succeeded", "object": {"id": payment_id}},
                )
                payload = await response.json()
                assert response.status == 200
                assert payload["status"] == "ok"
                assert payload["result"] == "processed"
        finally:
            await runner.cleanup()
        return application

    application = asyncio.run(_exercise_webhook())
    payment = get_payment_sync(PROVIDER_YOOKASSA, payment_id)
    entitlement = get_effective_entitlement_sync(user_id)
    assert payment["is_processed"] is True
    assert payment["status"] == "succeeded"
    assert entitlement["plan_type"] == PLAN_PREMIUM_MONTHLY
    assert entitlement["source_payment_id"] == payment_id
    assert application.bot.messages and application.bot.messages[0]["chat_id"] == user_id


def test_live_lifetime_entitlement_is_not_downgraded_by_monthly_payment():
    user_id = _unique_user_id()
    provider = "integration_smoke_lifetime"
    payment_id = _unique_payment_id("lifetime")
    amount_minor = int(YOOKASSA_PREMIUM_MONTHLY_AMOUNT) * 100

    initial = set_plan_entitlement_sync(
        user_id,
        PLAN_PREMIUM_LIFETIME,
        source_provider="integration_smoke",
        source_payment_id="grant-lifetime",
    )
    assert initial["plan_type"] == PLAN_PREMIUM_LIFETIME

    register_pending_payment_sync(
        provider,
        payment_id,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=amount_minor,
        currency=YOOKASSA_CURRENCY,
        status="pending",
        metadata={"source": "pytest-smoke"},
    )
    processed_now, _, entitlement = finalize_verified_payment_sync(
        provider,
        payment_id,
        user_id=user_id,
        plan_type=PLAN_PREMIUM_MONTHLY,
        amount_minor=amount_minor,
        currency=YOOKASSA_CURRENCY,
        status="succeeded",
        metadata={"source": "pytest-smoke"},
    )
    assert processed_now is True
    assert entitlement["plan_type"] == PLAN_PREMIUM_LIFETIME
    assert entitlement["source_payment_id"] == "grant-lifetime"
