from aiohttp import web

from app.config import (
    ROBOKASSA_WEBHOOK_BIND_HOST,
    ROBOKASSA_WEBHOOK_BIND_PORT,
    ROBOKASSA_WEBHOOK_ENABLED,
    ROBOKASSA_WEBHOOK_PATH,
)
from app.logging_utils import log_event
from app.payment_service import verify_and_finalize_robokassa_payment
from app.robokassa import normalize_result_payload

PTB_APPLICATION_KEY = web.AppKey("ptb_application", object)


async def _extract_robokassa_payload(request):
    raw = {}
    try:
        if request.can_read_body:
            post_data = await request.post()
            raw.update({str(key): str(value) for key, value in post_data.items()})
    except Exception:
        pass
    raw.update({str(key): str(value) for key, value in request.query.items()})
    return normalize_result_payload(raw)


async def _handle_robokassa_webhook(request):
    application = request.app[PTB_APPLICATION_KEY]
    try:
        payload = await _extract_robokassa_payload(request)
    except Exception:
        return web.Response(text="bad request", status=400)
    payment_id = str(payload.get("id") or "").strip()
    if not payment_id:
        return web.Response(text="missing invoice", status=400)
    if not payload.get("signature_valid"):
        log_event(
            "payment.webhook.invalid_signature",
            level="WARNING",
            payment_id=payment_id,
        )
        return web.Response(text="bad signature", status=400)
    try:
        result = await verify_and_finalize_robokassa_payment(
            application,
            payload,
            expected_user_id=None,
            trigger="webhook",
        )
        log_event(
            "payment.webhook.received",
            level="INFO",
            payment_id=payment_id,
            result=result.get("result"),
            status=result.get("status"),
        )
        return web.Response(text=f"OK{payment_id}", status=200)
    except Exception as e:
        log_event(
            "payment.webhook.failed",
            level="ERROR",
            payment_id=payment_id,
            error_class=type(e).__name__,
            error=str(e),
        )
        return web.Response(text="error", status=500)


async def start_payment_runtime(application):
    if not ROBOKASSA_WEBHOOK_ENABLED:
        return
    web_app = web.Application()
    web_app[PTB_APPLICATION_KEY] = application
    web_app.router.add_get(ROBOKASSA_WEBHOOK_PATH, _handle_robokassa_webhook)
    web_app.router.add_post(ROBOKASSA_WEBHOOK_PATH, _handle_robokassa_webhook)
    runner = web.AppRunner(web_app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, ROBOKASSA_WEBHOOK_BIND_HOST, int(ROBOKASSA_WEBHOOK_BIND_PORT))
    await site.start()
    application.bot_data["payment_webhook_runner"] = runner
    log_event(
        "payment.webhook.started",
        level="INFO",
        bind_host=ROBOKASSA_WEBHOOK_BIND_HOST,
        bind_port=int(ROBOKASSA_WEBHOOK_BIND_PORT),
        path=ROBOKASSA_WEBHOOK_PATH,
    )


async def stop_payment_runtime(application):
    runner = application.bot_data.pop("payment_webhook_runner", None)
    if runner:
        await runner.cleanup()
