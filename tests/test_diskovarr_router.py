import unittest
from unittest.mock import patch

from fastapi import HTTPException

import importlib
import sys
import types


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = exception = debug


def _module_available(name):
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def _install_runtime_stubs():
    """Stub optional runtime modules so the tests run outside the container."""
    if not _module_available("utils.global_logger"):
        global_logger = types.ModuleType("utils.global_logger")
        global_logger.logger = _Logger()
        sys.modules["utils.global_logger"] = global_logger


_install_runtime_stubs()

# The auth dependency pulls in the full logging stack, which is not importable
# outside the container. Stub it only for this import and restore afterwards so
# later tests see whatever was there before.
_previous_dependencies = sys.modules.get("utils.dependencies")
if not _module_available("utils.dependencies"):
    _dependencies = types.ModuleType("utils.dependencies")
    _dependencies.get_optional_current_user = lambda: None
    sys.modules["utils.dependencies"] = _dependencies
try:
    from api.routers import diskovarr as router
finally:
    if _previous_dependencies is None:
        sys.modules.pop("utils.dependencies", None)
    else:
        sys.modules["utils.dependencies"] = _previous_dependencies
from utils import diskovarr_settings as ds


class _Client:
    def __init__(self, admin_ok=True):
        self.admin_ok = admin_ok

    def health(self):
        return {"status": "ok", "version": "3.2.0"}

    def connection_settings(self):
        if not self.admin_ok:
            raise ds.DiskovarrError("HTTP 401 from /admin/connections/settings")
        return {}


class DiskovarrRouterTests(unittest.TestCase):
    def test_test_requires_url_and_a_credential(self):
        with self.assertRaises(HTTPException) as raised:
            (
                router.test_diskovarr_connection(
                    router.DiskovarrTestRequest(url="", api_key="k"), None
                )
            )
        self.assertEqual(400, raised.exception.status_code)

        with self.assertRaises(HTTPException) as raised:
            (
                router.test_diskovarr_connection(
                    router.DiskovarrTestRequest(url="http://127.0.0.1:3232"), None
                )
            )
        self.assertEqual(400, raised.exception.status_code)

    def test_test_reports_version_and_admin_access(self):
        with patch.object(ds, "DiskovarrClient", return_value=_Client()):
            result = router.test_diskovarr_connection(
                router.DiskovarrTestRequest(url="http://127.0.0.1:3232", api_key="k"),
                None,
            )
        self.assertEqual({"ok": True, "version": "3.2.0", "admin_access": True}, result)

    def test_test_flags_missing_admin_access_without_failing(self):
        with patch.object(ds, "DiskovarrClient", return_value=_Client(admin_ok=False)):
            result = router.test_diskovarr_connection(
                router.DiskovarrTestRequest(url="http://127.0.0.1:3232", api_key="k"),
                None,
            )
        self.assertTrue(result["ok"])
        self.assertFalse(result["admin_access"])
        self.assertIn("401", result["admin_error"])

    def test_test_rejects_invalid_url(self):
        with self.assertRaises(HTTPException) as raised:
            (
                router.test_diskovarr_connection(
                    router.DiskovarrTestRequest(url="ftp://x", api_key="k"), None
                )
            )
        self.assertEqual(400, raised.exception.status_code)

    def test_sync_returns_400_when_disabled(self):
        with patch.object(ds, "run_sync", return_value={"skipped": "disabled"}):
            with self.assertRaises(HTTPException) as raised:
                (router.sync_diskovarr(None))
        self.assertEqual(400, raised.exception.status_code)

    def test_sync_and_status_delegate_to_settings_module(self):
        outcome = {"ok": True, "steps": {}}
        with patch.object(ds, "run_sync", return_value=outcome) as run_sync:
            self.assertEqual(outcome, (router.sync_diskovarr(None)))
        run_sync.assert_called_once_with("manual")

        with patch.object(ds, "collect_status", return_value={"enabled": True}):
            self.assertEqual({"enabled": True}, (router.get_diskovarr_status(None)))

        with patch.object(ds, "get_last_sync", return_value=outcome):
            self.assertEqual(outcome, (router.get_diskovarr_sync(None)))


if __name__ == "__main__":
    unittest.main()
