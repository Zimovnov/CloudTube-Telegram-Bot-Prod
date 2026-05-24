import asyncio
import json
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import state
from app.jobs import RedisError, _get_redis_client, _log_redis_issue, _redis_key
from app.logging_utils import log_event


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ads_campaigns_key():
    return _redis_key("ads", "campaigns")


def _ads_stats_key():
    return _redis_key("ads", "stats")


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _validate_url(url):
    parsed = urlparse(str(url or "").strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _normalize_campaign(raw):
    if not isinstance(raw, dict):
        return None
    ad_id = str(raw.get("ad_id") or "").strip()
    text = str(raw.get("text") or "").strip()
    button_text = str(raw.get("button_text") or "").strip()
    url = str(raw.get("url") or "").strip()
    advertiser = str(raw.get("advertiser") or "").strip()
    erid = str(raw.get("erid") or "").strip()
    if not ad_id or not text or not button_text or not _validate_url(url) or not advertiser or not erid:
        return None
    try:
        weight = max(1, int(raw.get("weight", 1)))
    except Exception:
        weight = 1
    return {
        "ad_id": ad_id,
        "text": text,
        "button_text": button_text,
        "url": url,
        "advertiser": advertiser,
        "erid": erid,
        "enabled": bool(raw.get("enabled", True)),
        "weight": weight,
        "created_by": int(raw.get("created_by") or 0),
        "created_at_utc": str(raw.get("created_at_utc") or _utc_now_iso()),
        "updated_at_utc": str(raw.get("updated_at_utc") or _utc_now_iso()),
    }


def _read_campaigns_redis(client):
    try:
        rows = client.hgetall(_ads_campaigns_key()) or {}
        out = {}
        for _, raw in rows.items():
            try:
                item = _normalize_campaign(json.loads(raw))
            except Exception:
                item = None
            if item:
                out[item["ad_id"]] = item
        return out
    except RedisError as e:
        _log_redis_issue(f"Redis ads read failed: {type(e).__name__}: {e}")
        return None


def _write_campaign_redis(client, campaign):
    client.hset(_ads_campaigns_key(), campaign["ad_id"], json.dumps(campaign, ensure_ascii=False))


def _read_campaigns_locked(client):
    if client is not None:
        campaigns = _read_campaigns_redis(client)
        if campaigns is not None:
            return campaigns
    return _copy(state.LOCAL_AD_CAMPAIGNS)


def _write_campaign_locked(client, campaign):
    if client is not None:
        try:
            _write_campaign_redis(client, campaign)
            return
        except RedisError as e:
            _log_redis_issue(f"Redis ads write failed: {type(e).__name__}: {e}")
    state.LOCAL_AD_CAMPAIGNS[campaign["ad_id"]] = _copy(campaign)


def _delete_campaign_locked(client, ad_id):
    if client is not None:
        try:
            client.hdel(_ads_campaigns_key(), ad_id)
            return
        except RedisError as e:
            _log_redis_issue(f"Redis ads delete failed: {type(e).__name__}: {e}")
    state.LOCAL_AD_CAMPAIGNS.pop(ad_id, None)


def _read_stats_locked(client):
    if client is not None:
        try:
            return {str(k): int(v) for k, v in (client.hgetall(_ads_stats_key()) or {}).items()}
        except Exception as e:
            _log_redis_issue(f"Redis ads stats read failed: {type(e).__name__}: {e}")
    return {str(k): int(v) for k, v in state.LOCAL_AD_STATS.items()}


def _increment_stat_locked(client, ad_id):
    if client is not None:
        try:
            client.hincrby(_ads_stats_key(), ad_id, 1)
            return
        except RedisError as e:
            _log_redis_issue(f"Redis ads stats write failed: {type(e).__name__}: {e}")
    state.LOCAL_AD_STATS[ad_id] = int(state.LOCAL_AD_STATS.get(ad_id, 0)) + 1


def create_ad_sync(*, text, button_text, url, advertiser, erid, created_by, weight=1, enabled=True):
    campaign = _normalize_campaign(
        {
            "ad_id": uuid.uuid4().hex[:12],
            "text": text,
            "button_text": button_text,
            "url": url,
            "advertiser": advertiser,
            "erid": erid,
            "enabled": enabled,
            "weight": weight,
            "created_by": created_by,
            "created_at_utc": _utc_now_iso(),
            "updated_at_utc": _utc_now_iso(),
        }
    )
    if campaign is None:
        raise ValueError("invalid_ad")
    client = _get_redis_client()
    with state.ADS_LOCK:
        _write_campaign_locked(client, campaign)
    log_event("ads.created", level="INFO", ad_id=campaign["ad_id"], created_by=created_by)
    return _copy(campaign)


def list_ads_sync():
    client = _get_redis_client()
    with state.ADS_LOCK:
        campaigns = _read_campaigns_locked(client)
        stats = _read_stats_locked(client)
    rows = []
    for item in campaigns.values():
        current = _copy(item)
        current["impressions"] = int(stats.get(current["ad_id"], 0))
        rows.append(current)
    return sorted(rows, key=lambda x: x.get("created_at_utc") or "")


def get_ad_sync(ad_id):
    ad_key = str(ad_id or "").strip()
    if not ad_key:
        return None
    client = _get_redis_client()
    with state.ADS_LOCK:
        campaigns = _read_campaigns_locked(client)
        campaign = campaigns.get(ad_key)
    return _copy(campaign) if campaign else None


def set_ad_enabled_sync(ad_id, enabled):
    ad_key = str(ad_id or "").strip()
    client = _get_redis_client()
    with state.ADS_LOCK:
        campaigns = _read_campaigns_locked(client)
        campaign = campaigns.get(ad_key)
        if not campaign:
            raise KeyError("ad_not_found")
        campaign["enabled"] = bool(enabled)
        campaign["updated_at_utc"] = _utc_now_iso()
        _write_campaign_locked(client, campaign)
    log_event("ads.enabled_changed", level="INFO", ad_id=ad_key, enabled=bool(enabled))
    return _copy(campaign)


def delete_ad_sync(ad_id):
    ad_key = str(ad_id or "").strip()
    client = _get_redis_client()
    with state.ADS_LOCK:
        campaigns = _read_campaigns_locked(client)
        if ad_key not in campaigns:
            raise KeyError("ad_not_found")
        _delete_campaign_locked(client, ad_key)
    log_event("ads.deleted", level="INFO", ad_id=ad_key)
    return True


def record_ad_impression_sync(ad_id):
    ad_key = str(ad_id or "").strip()
    if not ad_key:
        return
    client = _get_redis_client()
    with state.ADS_LOCK:
        _increment_stat_locked(client, ad_key)


def build_ad_message(ad):
    return (
        "Реклама\n\n"
        f"{ad['text']}\n\n"
        f"Рекламодатель: {ad['advertiser']}\n"
        f"erid: {ad['erid']}"
    )


def build_ad_markup(ad):
    return InlineKeyboardMarkup([[InlineKeyboardButton(ad["button_text"], url=ad["url"])]])


async def create_ad(**kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: create_ad_sync(**kwargs))


async def list_ads():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, list_ads_sync)


async def get_ad(ad_id):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_ad_sync, ad_id)


async def set_ad_enabled(ad_id, enabled):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, set_ad_enabled_sync, ad_id, enabled)


async def delete_ad(ad_id):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, delete_ad_sync, ad_id)


async def record_ad_impression(ad_id):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, record_ad_impression_sync, ad_id)
