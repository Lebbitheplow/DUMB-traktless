"""
Diskovarr API Router - status, connection test and wiring for Diskovarr.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from utils.dependencies import get_optional_current_user
from utils.global_logger import logger
from utils import diskovarr_provision, diskovarr_settings

diskovarr_router = APIRouter()


class DiskovarrTestRequest(BaseModel):
    url: str
    api_key: str = ""
    admin_password: str = ""


def _probe(client: "diskovarr_settings.DiskovarrClient") -> dict:
    """Check reachability and admin access without mutating Diskovarr."""
    health = client.health()
    result = {
        "ok": True,
        "version": health.get("version"),
        "admin_access": False,
    }
    try:
        client.connection_settings()
        result["admin_access"] = True
    except diskovarr_settings.DiskovarrError as error:
        result["admin_error"] = str(error)
    return result


@diskovarr_router.get("/status")
def get_diskovarr_status(
    current_user: str = Depends(get_optional_current_user),
):
    """Live integration status: reachability, Riven bridge, connections."""
    return diskovarr_settings.collect_status()


@diskovarr_router.post("/test")
def test_diskovarr_connection(
    payload: DiskovarrTestRequest,
    current_user: str = Depends(get_optional_current_user),
):
    """Test a Diskovarr URL plus API key or admin password before saving."""
    if not payload.url:
        raise HTTPException(status_code=400, detail="URL is required.")
    if not (payload.api_key or payload.admin_password):
        raise HTTPException(
            status_code=400, detail="API key or admin password is required."
        )
    try:
        client = diskovarr_settings.DiskovarrClient(
            payload.url,
            api_key=payload.api_key,
            admin_password=payload.admin_password,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    try:
        return _probe(client)
    except diskovarr_settings.DiskovarrError as error:
        logger.info("Diskovarr connection test failed: %s", error)
        raise HTTPException(status_code=400, detail=str(error)) from None


@diskovarr_router.post("/sync")
def sync_diskovarr(
    current_user: str = Depends(get_optional_current_user),
):
    """Provision the Riven bridge key and push DUMB connections into Diskovarr."""
    result = diskovarr_settings.run_sync("manual")
    if result.get("skipped"):
        raise HTTPException(
            status_code=400, detail="Diskovarr integration is disabled."
        )
    return result


@diskovarr_router.get("/sync")
def get_diskovarr_sync(
    current_user: str = Depends(get_optional_current_user),
):
    """Outcome of the most recent wiring pass."""
    return diskovarr_settings.get_last_sync()


# -- guided setup (Diskovarr Admin -> Setup wizard) ----------------------------


class ProvisionRequest(BaseModel):
    debrid: dict
    diskovarr: dict
    services: list[str] = []
    tmdb_api_key: str = ""
    plex: Optional[dict] = None
    library_path: str = ""


@diskovarr_router.get("/provision/capabilities")
def get_provision_capabilities(
    current_user: str = Depends(get_optional_current_user),
):
    """Providers, installable services and their live state, plus the last job."""
    return diskovarr_provision.capabilities()


@diskovarr_router.post("/provision")
def start_provision(
    payload: ProvisionRequest,
    current_user: str = Depends(get_optional_current_user),
):
    """Apply a wizard plan in the background; poll /provision/status for progress."""
    try:
        return diskovarr_provision.start_plan(payload.model_dump())
    except diskovarr_provision.ProvisionError as error:
        status = 409 if "already in progress" in str(error) else 400
        raise HTTPException(status_code=status, detail=str(error)) from None


@diskovarr_router.post("/provision/validate")
def validate_provision(
    payload: ProvisionRequest,
    current_user: str = Depends(get_optional_current_user),
):
    """Dry-run validation of a plan without touching DUMB's config."""
    try:
        plan = diskovarr_provision.normalize_plan(payload.model_dump())
    except diskovarr_provision.ProvisionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    return {
        "ok": True,
        "provider": plan["debrid"]["provider"],
        "services": plan["services"],
        "library_path": plan["library_path"],
        "riven_env": sorted(diskovarr_provision.riven_env_overrides(plan).keys()),
    }


@diskovarr_router.get("/provision/status")
def get_provision_status(
    current_user: str = Depends(get_optional_current_user),
):
    """Progress of the current or most recent provisioning run."""
    return diskovarr_provision.job_status()
