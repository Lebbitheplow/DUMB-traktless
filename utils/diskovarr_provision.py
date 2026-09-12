"""
Diskovarr guided setup - provision DUMB services from Diskovarr's admin panel.

Diskovarr's Admin -> Setup wizard drives this module through the
``/diskovarr/provision`` endpoints. Given a plan (debrid provider + key, the
services the operator wants, and the keys Diskovarr already holds) it:

* picks the debrid stack for the provider - AllDebrid runs through Decypharr's
  own rclone mount, RealDebrid through Zurg + rclone - and starts Riven on it;
* points Riven's indexer at Diskovarr's TMDB key (TVDB keeps Riven's bundled
  key), its updater at Diskovarr's Plex, and its content source at Diskovarr;
* installs any of the optional request-side apps (Sonarr, Radarr, Prowlarr,
  Seerr, Zilean, Tautulli, Riven UI) through DUMB's normal onboarding path so
  DUMB wires Prowlarr/Decypharr into them exactly as it would from its own UI;
* runs the Diskovarr wiring pass so Riven's URL/key (and any Arr keys Diskovarr
  had no value for) land in Diskovarr's Admin -> Connections.

Everything runs on a background thread; ``job_status()`` reports per-step
progress so the wizard can poll while installs take minutes.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Optional

from utils.config_loader import CONFIG_MANAGER
from utils.global_logger import logger
from utils import diskovarr_settings

DEFAULT_LIBRARY_PATH = "/mnt/debrid/library"
RIVEN_API_KEY_WAIT_S = 180

PROVIDERS = {
    "alldebrid": {
        "key": "alldebrid",
        "label": "AllDebrid",
        "stack": "Decypharr + rclone",
        "core_service": "decypharr",
        "signup_url": "https://alldebrid.com/register/",
        "apikey_url": "https://alldebrid.com/apikeys/",
        "recommended": True,
    },
    "realdebrid": {
        "key": "realdebrid",
        "label": "Real-Debrid",
        "stack": "Zurg + rclone",
        "core_service": "riven_backend",
        "signup_url": "https://real-debrid.com/",
        "apikey_url": "https://real-debrid.com/apitoken",
        "recommended": False,
        "warning": (
            "Real-Debrid returns noticeably fewer cached results than AllDebrid, "
            "so more requests will wait on a fresh download."
        ),
    },
}

# What the wizard can install. ``role`` decides how DUMB starts it:
#   core     -> /process/start-core-service core_services entry (instance-aware)
#   optional -> /process/start-core-service optional_services entry
#   riven    -> handled by the debrid step (always required)
SERVICE_CATALOG = [
    {
        "key": "riven_backend",
        "label": "Riven",
        "role": "riven",
        "required": True,
        "description": "Fulfils Diskovarr requests from your debrid account and builds the symlink library.",
    },
    {
        "key": "riven_frontend",
        "label": "Riven UI",
        "role": "singleton",
        "description": "Riven's own web interface. Optional - Diskovarr is the request front end.",
    },
    {
        "key": "zilean",
        "label": "Zilean",
        "role": "optional",
        "description": "DMM hash cache scraper - noticeably more results for Riven and Prowlarr.",
        "recommended": True,
    },
    {
        "key": "sonarr",
        "label": "Sonarr",
        "role": "core",
        "description": "TV automation. Needed for Diskovarr's YouTube (Tuberr) requests and Sonarr-routed requests.",
    },
    {
        "key": "radarr",
        "label": "Radarr",
        "role": "core",
        "description": "Movie automation for Radarr-routed requests.",
    },
    {
        "key": "prowlarr",
        "label": "Prowlarr",
        "role": "core",
        "description": "Indexer manager - DUMB links it to Sonarr/Radarr (and Zilean) automatically.",
    },
    {
        "key": "seerr",
        "label": "Seerr",
        "role": "core",
        "description": "Classic request app. Not needed with Diskovarr; install only if you still want it.",
    },
    {
        "key": "tautulli",
        "label": "Tautulli",
        "role": "optional",
        "description": "Plex watch history for Diskovarr recommendations - install if you don't run one already.",
    },
]
SERVICE_KEYS = {entry["key"] for entry in SERVICE_CATALOG}

_JOB_LOCK = threading.Lock()
_JOB: dict[str, Any] = {"running": False}
_JOB_THREAD: Optional[threading.Thread] = None


class ProvisionError(Exception):
    """Raised when a plan is invalid or a provisioning step fails."""


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- plan validation --------------------------------------------------------------


def normalize_plan(raw: Optional[dict]) -> dict:
    """Validate the wizard's plan and fill defaults. Raises ProvisionError."""
    raw = raw if isinstance(raw, dict) else {}
    debrid = raw.get("debrid") if isinstance(raw.get("debrid"), dict) else {}
    provider = str(debrid.get("provider") or "").strip().lower().replace("-", "").replace("_", "")
    if provider not in PROVIDERS:
        raise ProvisionError("debrid.provider must be 'alldebrid' or 'realdebrid'.")
    api_key = str(debrid.get("api_key") or "").strip()
    if not api_key:
        raise ProvisionError(f"An API key for {PROVIDERS[provider]['label']} is required.")

    diskovarr = raw.get("diskovarr") if isinstance(raw.get("diskovarr"), dict) else {}
    url = str(diskovarr.get("url") or "").strip()
    if not url:
        raise ProvisionError("diskovarr.url is required.")
    diskovarr_key = str(diskovarr.get("api_key") or "").strip()
    if not diskovarr_key:
        raise ProvisionError("diskovarr.api_key is required.")

    services = raw.get("services")
    if not isinstance(services, list):
        services = []
    selected = []
    for item in services:
        key = str(item or "").strip().lower()
        if key and key not in SERVICE_KEYS:
            raise ProvisionError(f"Unknown service '{key}'.")
        if key and key not in selected:
            selected.append(key)
    if "riven_backend" not in selected:
        selected.insert(0, "riven_backend")

    plex = raw.get("plex") if isinstance(raw.get("plex"), dict) else {}
    plex_url = str(plex.get("url") or "").strip()
    plex_token = str(plex.get("token") or "").strip()

    library_path = str(raw.get("library_path") or "").strip() or DEFAULT_LIBRARY_PATH
    if not library_path.startswith("/"):
        raise ProvisionError("library_path must be an absolute path inside the DUMB container.")

    return {
        "debrid": {"provider": provider, "api_key": api_key},
        "diskovarr": {
            "url": url.rstrip("/"),
            "api_key": diskovarr_key,
            "request_mode": "pull",
        },
        "services": selected,
        "tmdb_api_key": str(raw.get("tmdb_api_key") or "").strip(),
        "plex": {"url": plex_url, "token": plex_token} if plex_url and plex_token else None,
        "library_path": library_path.rstrip("/") or DEFAULT_LIBRARY_PATH,
    }


# -- config helpers ---------------------------------------------------------------


def _service_block(key: str) -> dict:
    block = CONFIG_MANAGER.config.get(key)
    return block if isinstance(block, dict) else {}


def _first_instance(key: str) -> tuple[Optional[str], dict]:
    instances = _service_block(key).get("instances")
    if isinstance(instances, dict):
        for name, cfg in instances.items():
            if isinstance(cfg, dict) and cfg.get("enabled"):
                return name, cfg
        for name, cfg in instances.items():
            if isinstance(cfg, dict):
                return name, cfg
    return None, {}


def _process_names(key: str) -> list[str]:
    block = _service_block(key)
    if isinstance(block.get("instances"), dict):
        return [
            str(cfg.get("process_name") or "")
            for cfg in block["instances"].values()
            if isinstance(cfg, dict) and cfg.get("enabled") and cfg.get("process_name")
        ]
    name = block.get("process_name")
    return [str(name)] if name else []


def _service_enabled(key: str) -> bool:
    block = _service_block(key)
    if isinstance(block.get("instances"), dict):
        return any(
            isinstance(cfg, dict) and cfg.get("enabled")
            for cfg in block["instances"].values()
        )
    return bool(block.get("enabled"))


def _service_port(key: str) -> Optional[int]:
    block = _service_block(key)
    if isinstance(block.get("instances"), dict):
        _, cfg = _first_instance(key)
        return cfg.get("port")
    return block.get("port")


def _api_state():
    from utils.dependencies import get_api_state

    return get_api_state()


def _is_running(process_name: str) -> bool:
    try:
        return _api_state().get_status(process_name) == "running"
    except Exception:
        return False


def current_provider() -> Optional[str]:
    """Which debrid provider the running config is built around, if any."""
    decypharr = _service_block("decypharr")
    if decypharr.get("enabled"):
        for name, key in (decypharr.get("api_keys") or {}).items():
            if str(key or "").strip():
                normalized = str(name).lower().replace(" ", "").replace("_", "")
                if normalized in PROVIDERS:
                    return normalized
    zurg_instances = _service_block("zurg").get("instances") or {}
    for cfg in zurg_instances.values():
        if isinstance(cfg, dict) and cfg.get("enabled") and cfg.get("api_key"):
            return "realdebrid"
    return None


def riven_paths() -> dict:
    """Container-side mount + library paths Riven writes to."""
    from utils.riven_settings import parse_config_keys

    riven_cfg = _service_block("riven_backend")
    try:
        mount_path = parse_config_keys(copy.deepcopy(CONFIG_MANAGER.config)).get(
            "SYMLINK_RCLONE_PATH"
        )
    except Exception:
        mount_path = None
    library_path = (
        str(riven_cfg.get("symlink_library_path") or "").rstrip("/")
        or DEFAULT_LIBRARY_PATH
    )
    return {
        "mount_path": mount_path,
        "library_path": library_path,
        "movies": f"{library_path}/movies",
        "shows": f"{library_path}/shows",
    }


def capabilities() -> dict:
    """What the wizard can do here plus the live state of every catalogued service."""
    dumb_cfg = _service_block("dumb")
    services = []
    for entry in SERVICE_CATALOG:
        key = entry["key"]
        names = _process_names(key)
        services.append(
            {
                **entry,
                "enabled": _service_enabled(key),
                "running": any(_is_running(name) for name in names),
                "port": _service_port(key),
            }
        )
    return {
        "provision": True,
        "providers": list(PROVIDERS.values()),
        "services": services,
        "current": {
            "provider": current_provider(),
            "paths": riven_paths(),
            "plex_configured": bool(dumb_cfg.get("plex_address") and dumb_cfg.get("plex_token")),
            "diskovarr": {
                "enabled": bool(_service_block("diskovarr").get("enabled")),
                "url": _service_block("diskovarr").get("url") or "",
            },
        },
        "job": job_status(),
    }


# -- job state ------------------------------------------------------------------------


def job_status() -> dict:
    with _JOB_LOCK:
        return copy.deepcopy(_JOB)


def _job_step(key: str, label: str, status: str = "running", detail: str = "") -> None:
    with _JOB_LOCK:
        steps = _JOB.setdefault("steps", [])
        for step in steps:
            if step["key"] == key:
                step.update({"status": status, "detail": detail, "updated_at": _timestamp()})
                return
        steps.append(
            {"key": key, "label": label, "status": status, "detail": detail, "updated_at": _timestamp()}
        )


def _job_finish(ok: bool, result: Optional[dict] = None, error: str = "") -> None:
    with _JOB_LOCK:
        _JOB["running"] = False
        _JOB["ok"] = ok
        _JOB["finished_at"] = _timestamp()
        if result is not None:
            _JOB["result"] = result
        if error:
            _JOB.setdefault("errors", []).append(error)


# -- provisioning steps ------------------------------------------------------------


def _configure_diskovarr_block(plan: dict) -> None:
    cfg = dict(diskovarr_settings.diskovarr_config())
    cfg.update(
        {
            "enabled": True,
            "url": plan["diskovarr"]["url"],
            "api_key": plan["diskovarr"]["api_key"],
            "request_mode": plan["diskovarr"]["request_mode"],
            "configure_riven": True,
            "configure_connections": True,
        }
    )
    cfg.setdefault("admin_password", "")
    cfg.setdefault("riven_url", "")
    cfg.setdefault("riven_bridge_key", "")
    CONFIG_MANAGER.config["diskovarr"] = cfg


def _configure_plex(plan: dict) -> bool:
    plex = plan.get("plex")
    if not plex:
        return False
    dumb_cfg = CONFIG_MANAGER.config.setdefault("dumb", {})
    dumb_cfg["plex_address"] = plex["url"]
    dumb_cfg["plex_token"] = plex["token"]
    return True


def riven_env_overrides(plan: dict) -> dict[str, str]:
    """RIVEN_* environment Riven Backend gets on top of DUMB's own derivation."""
    provider = plan["debrid"]["provider"]
    env = {
        "RIVEN_SCRAPING_TORRENTIO_ENABLED": "true",
        "RIVEN_CONTENT_OVERSEERR_UPDATE_INTERVAL": "60",
        "RIVEN_CONTENT_PLEX_WATCHLIST_ENABLED": "false",
        "RIVEN_DOWNLOADERS_ALL_DEBRID_ENABLED": "true" if provider == "alldebrid" else "false",
        "RIVEN_DOWNLOADERS_REAL_DEBRID_ENABLED": "true" if provider == "realdebrid" else "false",
    }
    if provider == "alldebrid":
        env["RIVEN_DOWNLOADERS_ALL_DEBRID_API_KEY"] = plan["debrid"]["api_key"]
    else:
        env["RIVEN_DOWNLOADERS_REAL_DEBRID_API_KEY"] = plan["debrid"]["api_key"]
    if "zilean" in plan["services"]:
        env["RIVEN_SCRAPING_ZILEAN_ENABLED"] = "true"
    if plan.get("tmdb_api_key"):
        # Riven's traktless indexer accepts either a v4 read-access token or a
        # v3 API key here (see riven_patches tmdb_api.py).
        env["RIVEN_INDEXER_TMDB_READ_ACCESS_TOKEN"] = plan["tmdb_api_key"]
    if plan.get("plex"):
        env["RIVEN_UPDATERS_PLEX_ENABLED"] = "true"
    return env


def _configure_riven(plan: dict) -> None:
    riven_cfg = CONFIG_MANAGER.config.setdefault("riven_backend", {})
    riven_cfg["symlink_library_path"] = plan["library_path"]
    roots = riven_cfg.get("symlink_backup_roots")
    if isinstance(roots, list) and plan["library_path"] not in roots:
        riven_cfg["symlink_backup_roots"] = [plan["library_path"]]
    env = dict(riven_cfg.get("env") or {})
    # Drop the other provider's stale key so a provider switch is clean.
    for stale in ("RIVEN_DOWNLOADERS_ALL_DEBRID_API_KEY", "RIVEN_DOWNLOADERS_REAL_DEBRID_API_KEY"):
        env.pop(stale, None)
    env.update(riven_env_overrides(plan))
    riven_cfg["env"] = env


def _unified_start(core: list[dict], optional: Optional[list[str]] = None) -> dict:
    """Run DUMB's onboarding starter exactly as its own UI would."""
    from api.routers.process import CoreServiceConfig, UnifiedStartRequest, _run_startup
    from utils.dependencies import get_api_state, get_updater

    request = UnifiedStartRequest(
        core_services=[CoreServiceConfig(**entry) for entry in core],
        optional_services=list(optional or []),
        optional_service_options={},
    )
    try:
        return _run_startup(request, get_updater(), get_api_state(), logger)
    except Exception as error:  # FastAPI HTTPException or anything from a starter
        detail = getattr(error, "detail", None) or str(error)
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("errors") or str(detail)
        raise ProvisionError(str(detail)) from None


def _start_singleton(key: str) -> None:
    """Enable + start a non-instance service and wait until DUMB reports it running."""
    from api.routers.process import wait_for_process_running
    from utils.dependencies import get_api_state, get_updater

    cfg = CONFIG_MANAGER.config.get(key)
    if not isinstance(cfg, dict):
        raise ProvisionError(f"DUMB has no '{key}' service block.")
    process_name = cfg.get("process_name")
    if not process_name:
        raise ProvisionError(f"'{key}' has no process name.")
    if not cfg.get("enabled"):
        cfg["enabled"] = True
        CONFIG_MANAGER.save_config()
    api_state = get_api_state()
    if api_state.get_status(process_name) == "running":
        return
    process, error = get_updater().auto_update(
        process_name, enable_update=cfg.get("auto_update", False), force_update_check=True
    )
    if not process or not wait_for_process_running(api_state, process_name, timeout=60):
        raise ProvisionError(f"{process_name} failed to start. {error or ''}".strip())


def _start_debrid_stack(plan: dict) -> None:
    provider = plan["debrid"]["provider"]
    key = plan["debrid"]["api_key"]
    if provider == "realdebrid":
        # Riven's own onboarding path: Zurg w/ Riven + rclone w/ Riven + PostgreSQL.
        _unified_start(
            [{"name": "riven_backend", "debrid_service": "RealDebrid", "debrid_key": key}]
        )
        return
    # AllDebrid: Decypharr serves the mount, Riven symlinks out of it.
    decypharr = CONFIG_MANAGER.config.setdefault("decypharr", {})
    decypharr["mount_type"] = "rclone"
    api_keys = decypharr.setdefault("api_keys", {})
    api_keys["alldebrid"] = key
    CONFIG_MANAGER.save_config()
    _unified_start([{"name": "decypharr", "debrid_service": "AllDebrid", "debrid_key": key}])
    _start_singleton("postgres")
    _start_singleton("riven_backend")


def _start_extra_services(plan: dict) -> dict:
    core = [
        {"name": key}
        for key in plan["services"]
        if any(e["key"] == key and e["role"] == "core" for e in SERVICE_CATALOG)
    ]
    optional = [
        key
        for key in plan["services"]
        if any(e["key"] == key and e["role"] == "optional" for e in SERVICE_CATALOG)
    ]
    result = {}
    if core or optional:
        result = _unified_start(core, optional)
    if "riven_frontend" in plan["services"]:
        _start_singleton("riven_frontend")
    return result


def _wait_for_riven_api_key(timeout_s: int = RIVEN_API_KEY_WAIT_S) -> str:
    riven_cfg = _service_block("riven_backend")
    settings_file = riven_cfg.get("config_file") or diskovarr_settings.RIVEN_SETTINGS_FILE
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        key = diskovarr_settings._read_riven_api_key(settings_file)
        if key:
            return key
        time.sleep(3)
    return ""


def run_plan(plan: dict) -> dict:
    """Execute a normalized plan synchronously. Returns the result summary."""
    _job_step("config", "Write DUMB configuration")
    _configure_diskovarr_block(plan)
    plex_set = _configure_plex(plan)
    _configure_riven(plan)
    CONFIG_MANAGER.save_config()
    _job_step("config", "Write DUMB configuration", "done", "Plex linked" if plex_set else "")

    provider = PROVIDERS[plan["debrid"]["provider"]]
    _job_step("debrid", f"Start {provider['stack']} + Riven")
    _start_debrid_stack(plan)
    _job_step("debrid", f"Start {provider['stack']} + Riven", "done")

    extras = [k for k in plan["services"] if k != "riven_backend"]
    if extras:
        _job_step("services", "Install " + ", ".join(extras))
        outcome = _start_extra_services(plan)
        errors = outcome.get("errors") if isinstance(outcome, dict) else None
        _job_step(
            "services",
            "Install " + ", ".join(extras),
            "warning" if errors else "done",
            "; ".join(str(e) for e in errors) if errors else "",
        )

    _job_step("riven_key", "Wait for Riven API key")
    riven_key = _wait_for_riven_api_key()
    _job_step(
        "riven_key",
        "Wait for Riven API key",
        "done" if riven_key else "warning",
        "" if riven_key else "Riven has not written settings.json yet; Diskovarr wiring will retry on the next DUMB start.",
    )

    _job_step("wire", "Wire Diskovarr <-> Riven")
    sync = diskovarr_settings.run_sync("provision", wait_for_riven_s=30)
    sync_errors = sync.get("errors") or []
    _job_step(
        "wire",
        "Wire Diskovarr <-> Riven",
        "done" if sync.get("ok") else "warning",
        "; ".join(sync_errors),
    )

    riven_cfg = _service_block("riven_backend")
    result = {
        "provider": plan["debrid"]["provider"],
        "paths": riven_paths(),
        "riven": {
            "url": f"http://127.0.0.1:{riven_cfg.get('port') or 8080}",
            "has_api_key": bool(riven_key),
        },
        "services": {
            key: {
                "enabled": _service_enabled(key),
                "running": any(_is_running(n) for n in _process_names(key)),
                "port": _service_port(key),
            }
            for key in plan["services"]
        },
        "sync": sync,
    }
    return result


def start_plan(raw_plan: dict) -> dict:
    """Validate and launch a plan on a daemon thread. Returns the initial job state."""
    global _JOB_THREAD
    plan = normalize_plan(raw_plan)
    with _JOB_LOCK:
        if _JOB.get("running") and _JOB_THREAD and _JOB_THREAD.is_alive():
            raise ProvisionError("A provisioning run is already in progress.")
        _JOB.clear()
        _JOB.update(
            {
                "running": True,
                "ok": None,
                "started_at": _timestamp(),
                "finished_at": None,
                "plan": {
                    "provider": plan["debrid"]["provider"],
                    "services": plan["services"],
                    "library_path": plan["library_path"],
                    "plex": bool(plan["plex"]),
                    "tmdb": bool(plan["tmdb_api_key"]),
                },
                "steps": [],
                "errors": [],
                "result": None,
            }
        )

    def _worker():
        try:
            result = run_plan(plan)
            _job_finish(True, result)
        except ProvisionError as error:
            logger.error("Diskovarr provisioning failed: %s", error)
            _job_finish(False, error=str(error))
        except Exception as error:  # never let a wizard run take DUMB down
            logger.exception("Diskovarr provisioning crashed")
            _job_finish(False, error=str(error))

    _JOB_THREAD = threading.Thread(target=_worker, daemon=True, name="diskovarr-provision")
    _JOB_THREAD.start()
    return job_status()

