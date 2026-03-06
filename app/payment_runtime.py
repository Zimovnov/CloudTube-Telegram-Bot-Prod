import asyncio

from aiohttp import web

from app.config import (
    YOOKASSA_RECONCILE_BATCH_SIZE,
    YOOKASSA_RECONCILE_ENABLED,
    YOOKASSA_RECONCILE_INTERVAL_SEC,
    YOOKASSA_WEBHOOK_BIND_HOST,
    YOOKASSA_WEBHOOK_BIND_PORT,
    YOOKASSA_WEBHOOK_ENABLED,
    YOOKASSA_WEBHOOK_PATH,
)
from app.logging_utils import log_event
from app.payment_service import verify_and_finalize_yookassa_payment, run_yookassa_reconciliation

PTB_APPLICATION_KEY = web.AppKey("ptb_application", object)


def _extract_yookassa_payment_id(payload):
    if not isinstance(payload, dict):
        return None
    obj = payload.get("object") if isinstance(payload.get("object"), dict) else {}
    payment_id = str(obj.get("id") or payload.get("payment_id") or "").strip()
    return payment_id or None


async def _handle_yookassa_webhook(request):
    application = request.app[PTB_APPLICATION_KEY]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"status": "bad_request"}, status=400)
    payment_id = _extract_yookassa_payment_id(payload)
    if not payment_id:
        return web.json_response({"status": "ignored"}, status=202)
    try:
        result = await verify_and_finalize_yookassa_payment(
            application,
            payment_id,
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
        return web.json_response({"status": "ok", "result": result.get("result")}, status=200)
    except Exception as e:
        log_event(
            "payment.webhook.failed",
            level="ERROR",
            payment_id=payment_id,
            error_class=type(e).__name__,
            error=str(e),
        )
        return web.json_response({"status": "error"}, status=500)


async def start_payment_runtime(application):
    if YOOKASSA_WEBHOOK_ENABLED:
        web_app = web.Application()
        web_app[PTB_APPLICATION_KEY] = application
        web_app.router.add_post(YOOKASSA_WEBHOOK_PATH, _handle_yookassa_webhook)
        runner = web.AppRunner(web_app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, YOOKASSA_WEBHOOK_BIND_HOST, int(YOOKASSA_WEBHOOK_BIND_PORT))
        await site.start()
        application.bot_data["payment_webhook_runner"] = runner
        log_event(
            "payment.webhook.started",
            level="INFO",
            bind_host=YOOKASSA_WEBHOOK_BIND_HOST,
            bind_port=int(YOOKASSA_WEBHOOK_BIND_PORT),
            path=YOOKASSA_WEBHOOK_PATH,
        )
    if YOOKASSA_RECONCILE_ENABLED:
        async def _reconcile_loop():
            while True:
                try:
                    processed = await run_yookassa_reconciliation(
                        application,
                        limit=YOOKASSA_RECONCILE_BATCH_SIZE,
                    )
                    if processed:
                        log_event("payment.reconcile.processed", level="INFO", processed=processed)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log_event(
                        "payment.reconcile.failed",
                        level="ERROR",
                        error_class=type(e).__name__,
                        error=str(e),
                    )
                await asyncio.sleep(int(YOOKASSA_RECONCILE_INTERVAL_SEC))

        task = asyncio.create_task(_reconcile_loop())
        application.bot_data["payment_reconcile_task"] = task


async def stop_payment_runtime(application):
    task = application.bot_data.pop("payment_reconcile_task", None)
    if task:
        task.cancel()
        try:
            await task
        except Exception:
            pass
    runner = application.bot_data.pop("payment_webhook_runner", None)
    if runner:
        await runner.cleanup()
