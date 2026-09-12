"""
Diskovarr integration - native wiring between DUMB, Riven and Diskovarr.

Diskovarr (https://github.com/Lebbitheplow/diskovarr) is a request and
recommendation front end that runs outside of DUMB. This module talks to its
native admin API so an operator no longer has to copy keys around by hand:

* Riven bridge - Diskovarr keeps a dedicated 68-character "DUMB" bridge key
  that Riven uses to poll approved requests through Diskovarr's
  Overseerr-compatible request endpoint. DUMB fetches (and, if needed, creates)
  that key and feeds it to Riven's ``content.overseerr`` settings.
* Connections - DUMB pushes the URLs and API keys of the services it manages
  (Riven, Plex, Tautulli, Radarr, Sonarr, Jellyfin) into Diskovarr's
  Admin -> Connections page. Riven is always kept current because DUMB owns it;
  every other service is only filled in when Diskovarr has nothing configured.

Authentication uses the Diskovarr API key from Admin -> General. Diskovarr
releases that only accept an admin session for ``/admin`` routes are supported
through the optional ``admin_password`` fallback.
"""

from __future__ import annotations

import configparser
import copy
import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

import defusedxml.ElementTree as ET

from utils.config_loader import CONFIG_MANAGER
from utils.global_logger import logger
from utils.url_security import safe_request, validate_url_scheme

RIVEN_SETTINGS_FILE = "/riven/backend/data/settings.json"
RIVEN_BRIDGE_KEY_LENGTH = 68  # Riven's Overseerr client rejects any other length
DEFAULT_TIMEOUT = 15
REQUEST_MODES = ("pull", "push")

_SYNC_LOCK = threading.Lock()
_LAST_SYNC: dict[str, Any] = {}
_WIRING_THREAD: Optional[threading.Thread] = None


class DiskovarrError(Exception):
    """Raised when Diskovarr cannot be reached or rejects a request."""


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


class DiskovarrClient:
    """Minimal client for Diskovarr's native HTTP API."""

    def __init__(
        self,
        url: str,
        api_key: str = "",
        admin_password: str = "",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = validate_url_scheme(str(url or "").strip()).rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.admin_password = str(admin_password or "")
        self.timeout = timeout
        self._cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies)
        )
        self._admin_session = False

    # -- transport ----------------------------------------------------------
    def _open(self, request: urllib.request.Request):
        validate_url_scheme(request.full_url)
        return self._opener.open(request, timeout=self.timeout)

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[dict] = None,
        admin: bool = False,
        api_key: Optional[str] = None,
        _retry_login: bool = True,
    ) -> Any:
        headers = {"Accept": "application/json"}
        key = self.api_key if api_key is None else api_key
        if key:
            headers["X-Api-Key"] = key
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        request = safe_request(
            _join_url(self.base_url, path), data=body, headers=headers, method=method
        )
        try:
            with self._open(request) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if (
                error.code == 401
                and admin
                and _retry_login
                and self.admin_password
                and self._login()
            ):
                return self._request(
                    method,
                    path,
                    data=data,
                    admin=admin,
                    api_key=api_key,
                    _retry_login=False,
                )
            detail = ""
            try:
                payload = json.loads(error.read().decode("utf-8") or "{}")
                detail = payload.get("error") or payload.get("message") or ""
            except Exception:
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise DiskovarrError(f"HTTP {error.code} from {path}{suffix}") from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise DiskovarrError(f"Request to {path} failed: {error}") from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise DiskovarrError(f"Non-JSON response from {path}") from None

    def _login(self) -> bool:
        """Open an admin session with the configured admin password."""
        try:
            result = self._request(
                "POST",
                "/admin/login",
                data={"password": self.admin_password},
                _retry_login=False,
            )
        except DiskovarrError as error:
            logger.warning("Diskovarr admin login failed: %s", error)
            return False
        self._admin_session = bool(result and result.get("ok"))
        return self._admin_session

    # -- public endpoints -----------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health", api_key="") or {}

    # -- admin endpoints ------------------------------------------------------
    def admin_status(self) -> dict:
        return self._request("GET", "/admin/status", admin=True) or {}

    def connection_settings(self) -> dict:
        return self._request("GET", "/admin/connections/settings", admin=True) or {}

    def reveal_keys(self) -> dict:
        return self._request("GET", "/admin/connections/reveal", admin=True) or {}

    def save_connections(self, payload: dict) -> dict:
        return (
            self._request("POST", "/admin/connections/save", data=payload, admin=True)
            or {}
        )

    def riven_config(self) -> dict:
        """Read Diskovarr's Riven settings; also provisions the DUMB bridge app."""
        return self._request("GET", "/admin/riven/config", admin=True) or {}

    def enable_riven_bridge(self, enabled: bool = True) -> dict:
        return (
            self._request(
                "POST",
                "/admin/riven/dumb/enable",
                data={"enabled": bool(enabled)},
                admin=True,
            )
            or {}
        )

    def test_riven(self) -> dict:
        return self._request("POST", "/admin/riven/config/test", admin=True) or {}

    # -- Overseerr-compatible bridge (authenticated with the bridge key) ------
    def bridge_request_count(self, bridge_key: str) -> dict:
        return self._request("GET", "/api/v1/request/count", api_key=bridge_key) or {}


# -- configuration helpers ----------------------------------------------------


def diskovarr_config() -> dict:
    cfg = CONFIG_MANAGER.get("diskovarr", {})
    return cfg if isinstance(cfg, dict) else {}


def is_enabled(cfg: Optional[dict] = None) -> bool:
    cfg = diskovarr_config() if cfg is None else cfg
    return bool(cfg.get("enabled"))


def validate_diskovarr_config(cfg: Optional[dict] = None) -> list[str]:
    """Return human-readable configuration problems (empty when valid)."""
    cfg = diskovarr_config() if cfg is None else cfg
    if not cfg.get("enabled"):
        return []
    errors = []
    url = str(cfg.get("url") or "").strip()
    if not url:
        errors.append("Diskovarr URL is not set.")
    else:
        try:
            validate_url_scheme(url)
        except ValueError as error:
            errors.append(f"Diskovarr URL is invalid: {error}")
    if not (cfg.get("api_key") or cfg.get("admin_password")):
        errors.append(
            "Diskovarr API key (Admin -> General) or admin password is required."
        )
    if cfg.get("request_mode") not in REQUEST_MODES:
        errors.append("Diskovarr request_mode must be 'pull' or 'push'.")
    riven_url = str(cfg.get("riven_url") or "").strip()
    if riven_url:
        try:
            validate_url_scheme(riven_url)
        except ValueError as error:
            errors.append(f"Diskovarr riven_url is invalid: {error}")
    return errors


def build_client(cfg: Optional[dict] = None) -> DiskovarrClient:
    cfg = diskovarr_config() if cfg is None else cfg
    return DiskovarrClient(
        cfg.get("url") or "",
        api_key=cfg.get("api_key") or "",
        admin_password=cfg.get("admin_password") or "",
    )


# -- Riven bridge ------------------------------------------------------------


def riven_bridge_env(cfg: Optional[dict] = None) -> dict[str, str]:
    """Environment overrides that point Riven's Overseerr source at Diskovarr."""
    cfg = diskovarr_config() if cfg is None else cfg
    key = str(cfg.get("riven_bridge_key") or "").strip()
    if not (cfg.get("enabled") and cfg.get("configure_riven") and key):
        return {}
    return {
        "RIVEN_CONTENT_OVERSEERR_ENABLED": "true",
        "RIVEN_CONTENT_OVERSEERR_URL": str(cfg.get("url") or "").rstrip("/"),
        "RIVEN_CONTENT_OVERSEERR_API_KEY": key,
        "RIVEN_CONTENT_OVERSEERR_USE_WEBHOOK": "false",
    }


def ensure_riven_bridge_key(
    client: DiskovarrClient, cfg: dict, persist: bool = True
) -> tuple[str, bool]:
    """Fetch Diskovarr's DUMB bridge key, creating and enabling it if needed.

    Returns ``(key, changed)``. The key is cached in ``diskovarr.riven_bridge_key``
    so Riven can still be configured when Diskovarr is briefly unreachable.
    """
    client.riven_config()  # creates the DUMB bridge app and fixes its key length
    client.enable_riven_bridge(True)
    revealed = client.reveal_keys()
    key = str(revealed.get("dumbApiKey") or "").strip()
    if len(key) != RIVEN_BRIDGE_KEY_LENGTH:
        raise DiskovarrError(
            "Diskovarr did not return a "
            f"{RIVEN_BRIDGE_KEY_LENGTH}-character Riven bridge key"
        )
    changed = key != str(cfg.get("riven_bridge_key") or "")
    if changed:
        cfg["riven_bridge_key"] = key
        CONFIG_MANAGER.config["diskovarr"] = cfg
        if persist:
            CONFIG_MANAGER.save_config()
    return key, changed


def prepare_riven_bridge() -> bool:
    """Make sure the bridge key is known before Riven starts.

    Returns True when Riven's Overseerr source can be configured (fresh or
    cached key). Safe to call when Diskovarr is disabled.
    """
    cfg = diskovarr_config()
    if not (cfg.get("enabled") and cfg.get("configure_riven")):
        return False
    errors = validate_diskovarr_config(cfg)
    if errors:
        for error in errors:
            logger.error("Diskovarr config error: %s", error)
        return False
    try:
        ensure_riven_bridge_key(build_client(cfg), cfg)
        logger.info("Diskovarr Riven bridge key is ready.")
        return True
    except DiskovarrError as error:
        cached = bool(cfg.get("riven_bridge_key"))
        logger.warning(
            "Diskovarr bridge key refresh failed (%s); %s",
            error,
            "using cached key" if cached else "Riven bridge not configured",
        )
        return cached


def read_riven_overseerr_settings(path: str = RIVEN_SETTINGS_FILE) -> dict:
    """Return Riven's current ``content.overseerr`` block (empty if unavailable)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, ValueError):
        return {}
    block = ((settings.get("content") or {}).get("overseerr")) or {}
    return block if isinstance(block, dict) else {}


def _read_riven_api_key(path: str = RIVEN_SETTINGS_FILE) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return str(json.load(handle).get("api_key") or "")
    except (OSError, ValueError):
        return ""


# -- connection discovery -----------------------------------------------------


def _first_enabled_instance(service_key: str) -> Optional[dict]:
    cfg = CONFIG_MANAGER.get(service_key, {}) or {}
    instances = cfg.get("instances") if isinstance(cfg, dict) else None
    if not isinstance(instances, dict):
        return None
    for instance in instances.values():
        if isinstance(instance, dict) and instance.get("enabled"):
            return instance
    return None


def _parse_arr_api_key(config_xml_path: str) -> str:
    try:
        if not (config_xml_path and os.path.exists(config_xml_path)):
            return ""
        node = ET.parse(config_xml_path).getroot().find(".//ApiKey")
        if node is not None and (node.text or "").strip():
            return node.text.strip()
    except Exception as error:
        logger.warning("Failed reading ApiKey from %s: %s", config_xml_path, error)
    return ""


def _parse_tautulli_api_key(config_ini_path: str) -> str:
    try:
        if not (config_ini_path and os.path.exists(config_ini_path)):
            return ""
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read(config_ini_path, encoding="utf-8")
        value = parser.get("General", "api_key", fallback="") or ""
        return value.strip().strip('"').strip("'")
    except Exception as error:
        logger.warning(
            "Failed reading Tautulli API key from %s: %s", config_ini_path, error
        )
    return ""


def riven_connection(cfg: Optional[dict] = None, wait_s: int = 0) -> Optional[dict]:
    """Where Diskovarr should reach Riven, plus Riven's API key."""
    cfg = diskovarr_config() if cfg is None else cfg
    riven_cfg = CONFIG_MANAGER.get("riven_backend", {}) or {}
    if not riven_cfg.get("enabled"):
        return None
    url = str(cfg.get("riven_url") or "").strip()
    if not url:
        port = riven_cfg.get("port") or 8082
        url = f"http://127.0.0.1:{port}"
    settings_file = riven_cfg.get("config_file") or RIVEN_SETTINGS_FILE
    deadline = time.time() + max(0, wait_s)
    api_key = _read_riven_api_key(settings_file)
    while not api_key and time.time() < deadline:
        time.sleep(2)
        api_key = _read_riven_api_key(settings_file)
    return {"url": url.rstrip("/"), "api_key": api_key}


def plex_connection() -> Optional[dict]:
    from utils.plex_dbrepair import _plex_token, _plex_url

    plex_cfg = CONFIG_MANAGER.get("plex", {}) or {}
    dumb_cfg = CONFIG_MANAGER.get("dumb", {}) or {}
    if not (plex_cfg.get("enabled") or dumb_cfg.get("plex_address")):
        return None
    return {
        "url": _plex_url(plex_cfg, dumb_cfg),
        "token": _plex_token(plex_cfg, dumb_cfg),
    }


def tautulli_connection() -> Optional[dict]:
    cfg = CONFIG_MANAGER.get("tautulli", {}) or {}
    if not cfg.get("enabled") or not cfg.get("port"):
        return None
    return {
        "url": f"http://127.0.0.1:{cfg.get('port')}",
        "api_key": _parse_tautulli_api_key(cfg.get("config_file") or ""),
    }


def arr_connection(service_key: str) -> Optional[dict]:
    instance = _first_enabled_instance(service_key)
    if not instance or not instance.get("port"):
        return None
    return {
        "url": f"http://127.0.0.1:{instance.get('port')}",
        "api_key": _parse_arr_api_key(instance.get("config_file") or ""),
    }


def jellyfin_connection() -> Optional[dict]:
    cfg = CONFIG_MANAGER.get("jellyfin", {}) or {}
    if not cfg.get("enabled") or not cfg.get("port"):
        return None
    return {"url": f"http://127.0.0.1:{cfg.get('port')}"}


def build_connection_payload(
    cfg: dict,
    current: dict,
    revealed: dict,
    riven: Optional[dict],
    plex: Optional[dict] = None,
    tautulli: Optional[dict] = None,
    radarr: Optional[dict] = None,
    sonarr: Optional[dict] = None,
    jellyfin: Optional[dict] = None,
) -> dict:
    """Compose the ``/admin/connections/save`` body.

    Riven fields are authoritative (DUMB owns Riven). Everything else is only
    filled in when Diskovarr has no value yet, so an operator's manual
    configuration is never overwritten.
    """
    payload: dict[str, Any] = {"dumb_request_mode": cfg.get("request_mode") or "pull"}
    if riven and riven.get("url"):
        payload["riven_url"] = riven["url"]
        payload["riven_enabled"] = True
        if riven.get("api_key"):
            payload["riven_api_key"] = riven["api_key"]

    def fill(
        url_field: str,
        url: Optional[str],
        key_field: str = "",
        key: Optional[str] = "",
        revealed_field: str = "",
    ) -> None:
        if url and not str(current.get(url_field) or "").strip():
            payload[url_field] = url
        if key_field and key and not str(revealed.get(revealed_field) or "").strip():
            payload[key_field] = key

    if plex:
        fill("plex_url", plex.get("url"), "plex_token", plex.get("token"), "plexToken")
    if tautulli:
        fill(
            "tautulli_url",
            tautulli.get("url"),
            "tautulli_api_key",
            tautulli.get("api_key"),
            "tautulliApiKey",
        )
    if radarr:
        fill(
            "radarr_url",
            radarr.get("url"),
            "radarr_api_key",
            radarr.get("api_key"),
            "radarrApiKey",
        )
    if sonarr:
        fill(
            "sonarr_url",
            sonarr.get("url"),
            "sonarr_api_key",
            sonarr.get("api_key"),
            "sonarrApiKey",
        )
    if jellyfin:
        fill("jellyfin_url", jellyfin.get("url"))
    return payload


def sync_connections(
    client: DiskovarrClient, cfg: dict, wait_for_riven_s: int = 0
) -> list[str]:
    """Push DUMB-managed connections into Diskovarr. Returns the updated fields."""
    current = client.connection_settings()
    revealed = client.reveal_keys()
    payload = build_connection_payload(
        cfg,
        current,
        revealed,
        riven_connection(cfg, wait_s=wait_for_riven_s),
        plex=plex_connection(),
        tautulli=tautulli_connection(),
        radarr=arr_connection("radarr"),
        sonarr=arr_connection("sonarr"),
        jellyfin=jellyfin_connection(),
    )
    client.save_connections(payload)
    return sorted(payload.keys())


# -- orchestration ------------------------------------------------------------


def _refresh_riven_settings_safely() -> None:
    """Re-push Riven settings after the bridge key changed while Riven runs."""
    from utils.riven_settings import load_settings

    try:
        load_settings()
    except Exception as error:
        logger.warning("Riven settings refresh after bridge change failed: %s", error)


def run_sync(reason: str = "manual", wait_for_riven_s: int = 0) -> dict:
    """Run the full Diskovarr wiring pass and record the outcome."""
    global _LAST_SYNC
    with _SYNC_LOCK:
        result: dict[str, Any] = {
            "ok": False,
            "reason": reason,
            "started_at": _timestamp(),
            "steps": {},
            "errors": [],
        }
        cfg = diskovarr_config()
        if not cfg.get("enabled"):
            result["skipped"] = "disabled"
            _LAST_SYNC = result
            return copy.deepcopy(result)
        errors = validate_diskovarr_config(cfg)
        if errors:
            result["errors"] = errors
            _LAST_SYNC = result
            return copy.deepcopy(result)

        bridge_changed = False
        try:
            client = build_client(cfg)
            health = client.health()
            result["version"] = health.get("version")
            if cfg.get("configure_riven"):
                _, bridge_changed = ensure_riven_bridge_key(client, cfg)
                result["steps"]["riven_bridge"] = (
                    "updated" if bridge_changed else "ready"
                )
            if cfg.get("configure_connections"):
                updated = sync_connections(client, cfg, wait_for_riven_s)
                result["steps"]["connections"] = updated
            result["ok"] = True
        except DiskovarrError as error:
            result["errors"].append(str(error))
            logger.warning("Diskovarr sync (%s) failed: %s", reason, error)
        except Exception as error:  # keep background wiring from taking DUMB down
            result["errors"].append(str(error))
            logger.exception("Diskovarr sync (%s) crashed", reason)
        result["finished_at"] = _timestamp()
        _LAST_SYNC = result

    if bridge_changed and reason != "startup":
        riven_cfg = CONFIG_MANAGER.get("riven_backend", {}) or {}
        if riven_cfg.get("enabled"):
            threading.Thread(
                target=_refresh_riven_settings_safely,
                daemon=True,
                name="diskovarr-riven-refresh",
            ).start()
    return copy.deepcopy(result)


def get_last_sync() -> dict:
    return copy.deepcopy(_LAST_SYNC)


def start_diskovarr_wiring(
    reason: str = "startup", delay_s: int = 15, wait_for_riven_s: int = 120
) -> bool:
    """Run ``run_sync`` on a daemon thread. Returns False when nothing was started."""
    global _WIRING_THREAD
    if not is_enabled():
        logger.debug("Diskovarr integration disabled; wiring skipped")
        return False
    if _WIRING_THREAD and _WIRING_THREAD.is_alive():
        logger.debug("Diskovarr wiring already running")
        return False

    def _worker():
        if delay_s > 0:
            time.sleep(delay_s)
        run_sync(reason, wait_for_riven_s=wait_for_riven_s)

    _WIRING_THREAD = threading.Thread(
        target=_worker, daemon=True, name="diskovarr-wiring"
    )
    _WIRING_THREAD.start()
    logger.info("Diskovarr wiring scheduled (%s)", reason)
    return True


# -- status -----------------------------------------------------------------------


def _riven_bridge_status(cfg: dict) -> dict:
    key = str(cfg.get("riven_bridge_key") or "")
    riven_cfg = CONFIG_MANAGER.get("riven_backend", {}) or {}
    riven_block = read_riven_overseerr_settings(
        riven_cfg.get("config_file") or RIVEN_SETTINGS_FILE
    )
    expected_url = str(cfg.get("url") or "").rstrip("/")
    return {
        "configured": bool(key),
        "mode": cfg.get("request_mode") or "pull",
        "riven_enabled": bool(riven_block.get("enabled")),
        "riven_url_matches": (
            str(riven_block.get("url") or "").rstrip("/") == expected_url
        ),
        "riven_key_matches": bool(key) and riven_block.get("api_key") == key,
    }


def collect_status() -> dict:
    """Live view of the integration for the ``/diskovarr/status`` endpoint."""
    cfg = diskovarr_config()
    status: dict[str, Any] = {
        "enabled": bool(cfg.get("enabled")),
        "url": cfg.get("url") or "",
        "request_mode": cfg.get("request_mode") or "pull",
        "configure_riven": bool(cfg.get("configure_riven")),
        "configure_connections": bool(cfg.get("configure_connections")),
        "config_errors": validate_diskovarr_config(cfg),
        "reachable": False,
        "admin_access": False,
        "version": None,
        "riven_bridge": _riven_bridge_status(cfg),
        "requests": None,
        "connections": None,
        "last_sync": get_last_sync(),
    }
    if not cfg.get("enabled") or status["config_errors"]:
        return status
    try:
        client = build_client(cfg)
    except ValueError as error:
        status["config_errors"].append(str(error))
        return status
    try:
        health = client.health()
        status["reachable"] = True
        status["version"] = health.get("version")
    except DiskovarrError as error:
        status["error"] = str(error)
        return status
    try:
        connections = client.connection_settings()
        status["admin_access"] = True
        status["connections"] = {
            field: connections.get(field)
            for field in (
                "riven_enabled",
                "riven_url",
                "dumb_request_mode",
                "default_request_service",
                "plex_url",
                "tautulli_url",
                "jellyfin_url",
                "jellyfin_enabled",
                "radarr_enabled",
                "sonarr_enabled",
            )
        }
    except DiskovarrError as error:
        status["admin_error"] = str(error)
    bridge_key = str(cfg.get("riven_bridge_key") or "")
    if bridge_key:
        try:
            status["requests"] = client.bridge_request_count(bridge_key)
        except DiskovarrError as error:
            status["riven_bridge"]["error"] = str(error)
    return status
