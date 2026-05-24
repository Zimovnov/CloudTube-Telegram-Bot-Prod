from app.config import MAX_MEDIA_DURATION_SECONDS


def max_duration_for_plan(_plan_type=None):
    return MAX_MEDIA_DURATION_SECONDS


async def resolve_user_download_policy(profile):
    return {
        "role": (profile or {}).get("role"),
        "blocked_by_limit": False,
        "max_duration_seconds": MAX_MEDIA_DURATION_SECONDS,
        "unlimited_requests": True,
    }
