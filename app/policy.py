from app.access import PLAN_FREE, is_premium_plan
from app.config import FREE_MAX_DURATION_SECONDS, FREE_MONTHLY_LIMIT, PREMIUM_MAX_DURATION_SECONDS
from app.usage import get_free_usage_count


def max_duration_for_plan(plan_type):
    return PREMIUM_MAX_DURATION_SECONDS if is_premium_plan(plan_type) else FREE_MAX_DURATION_SECONDS


async def resolve_user_download_policy(profile):
    plan_type = profile.get("plan_type", PLAN_FREE)
    usage_count = await get_free_usage_count(profile["user_id"])
    blocked_by_limit = plan_type == PLAN_FREE and usage_count >= FREE_MONTHLY_LIMIT
    return {
        "plan_type": plan_type,
        "plan_expires_at_utc": profile.get("plan_expires_at_utc"),
        "role": profile.get("role"),
        "usage_count": usage_count,
        "free_limit": FREE_MONTHLY_LIMIT,
        "is_premium": is_premium_plan(plan_type),
        "blocked_by_limit": blocked_by_limit,
        "max_duration_seconds": max_duration_for_plan(plan_type),
    }
