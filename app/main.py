from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.mail263_client import Mail263Client
from app.state import StateStore
from app.sync_service import SyncService
from app.xrxs_client import XrxsClient

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger(__name__)

state_store = StateStore(settings.state_db_path)
source_client = XrxsClient(settings)
target_client = Mail263Client(settings)
sync_service = SyncService(settings, source_client, target_client, state_store)
scheduler = BackgroundScheduler(timezone=settings.timezone)


def _is_placeholder(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return normalized in {"", "replace_me", "https://openapi.example.com", "example.com"}


def get_config_issues() -> list[str]:
    issues: list[str] = []
    if _is_placeholder(settings.xrxs_base_url):
        issues.append("XRXS_BASE_URL is not configured")
    if not settings.xrxs_access_token and (
        _is_placeholder(settings.xrxs_client_id) or _is_placeholder(settings.xrxs_client_secret)
    ):
        issues.append("XRXS access credentials are incomplete")
    if _is_placeholder(settings.mail263_domain):
        issues.append("MAIL263_DOMAIN is not configured")
    if _is_placeholder(settings.mail263_account):
        issues.append("MAIL263_ACCOUNT is not configured")
    if _is_placeholder(settings.mail263_key):
        issues.append("MAIL263_KEY is not configured")
    return issues


def scheduled_sync() -> None:
    try:
        result = sync_service.run()
        LOGGER.info("scheduled sync finished: %s", result)
    except Exception:
        LOGGER.exception("scheduled sync failed")


def startup_sync() -> None:
    issues = get_config_issues()
    if issues:
        LOGGER.warning("startup sync skipped because configuration is incomplete: %s", issues)
        return
    try:
        result = sync_service.run()
        LOGGER.info("startup sync finished: %s", result)
    except Exception:
        LOGGER.exception("startup sync failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler.add_job(
        scheduled_sync,
        "interval",
        minutes=settings.sync_interval_minutes,
        id="xrxs-263-sync",
        replace_existing=True,
    )
    scheduler.start()
    startup_sync()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/config")
def read_config() -> dict[str, object]:
    return {
        "app_name": settings.app_name,
        "timezone": settings.timezone,
        "sync_interval_minutes": settings.sync_interval_minutes,
        "dry_run": settings.dry_run,
        "xrxs_adapter": settings.xrxs_adapter,
        "mail263_domain": settings.mail263_domain,
        "issues": get_config_issues(),
    }


@app.get("/diagnose")
def diagnose() -> dict[str, object]:
    issues = get_config_issues()
    return {
        "ok": not issues,
        "issues": issues,
        "checks": {
            "xrxs_base_url_configured": not _is_placeholder(settings.xrxs_base_url),
            "xrxs_has_token_or_client_credentials": bool(
                settings.xrxs_access_token
                or (settings.xrxs_client_id and settings.xrxs_client_secret)
            ),
            "mail263_domain_configured": not _is_placeholder(settings.mail263_domain),
            "mail263_account_configured": not _is_placeholder(settings.mail263_account),
            "mail263_key_configured": not _is_placeholder(settings.mail263_key),
            "mail263_sso_key_configured": bool(settings.mail263_sso_key),
        },
    }


@app.post("/sync")
def trigger_sync() -> dict[str, object]:
    try:
        issues = get_config_issues()
        if issues:
            raise HTTPException(status_code=400, detail={"issues": issues})
        return sync_service.run()
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("manual sync failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/sso-url")
def get_sso_url(email: str, login_platform: str = "windows") -> dict[str, str]:
    if not settings.mail263_sso_enabled:
        raise HTTPException(status_code=400, detail="MAIL263_SSO_ENABLED is false")
    try:
        return {"url": target_client.build_sso_url(email, login_platform)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
