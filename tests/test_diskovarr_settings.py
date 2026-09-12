import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

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

from utils import diskovarr_settings as ds

BRIDGE_KEY = "a" * ds.RIVEN_BRIDGE_KEY_LENGTH


class _FakeConfigManager:
    """Stand-in for CONFIG_MANAGER that is immune to stubs left by other tests."""

    def __init__(self, config):
        self.config = config
        self.save_config = Mock()

    def get(self, key, default=None):
        return self.config.get(key, default)


def _cm(config):
    return _FakeConfigManager(config)


def _base_config(**overrides):
    cfg = {
        "enabled": True,
        "url": "http://127.0.0.1:3232",
        "api_key": "diskovarr-key",
        "admin_password": "",
        "request_mode": "pull",
        "riven_url": "",
        "configure_riven": True,
        "configure_connections": True,
        "riven_bridge_key": "",
    }
    cfg.update(overrides)
    return cfg


class FakeClient(ds.DiskovarrClient):
    """Records calls and serves canned responses instead of using the network."""

    def __init__(self, responses=None, **kwargs):
        super().__init__("http://127.0.0.1:3232", **kwargs)
        self.responses = responses or {}
        self.calls = []

    def _request(self, method, path, data=None, admin=False, api_key=None, **_):
        self.calls.append((method, path, data, api_key))
        response = self.responses.get((method, path))
        if isinstance(response, Exception):
            raise response
        return response


class ConfigValidationTests(unittest.TestCase):
    def test_disabled_config_is_always_valid(self):
        self.assertEqual([], ds.validate_diskovarr_config({"enabled": False}))

    def test_enabled_config_requires_url_and_credentials(self):
        errors = ds.validate_diskovarr_config(
            _base_config(url="", api_key="", request_mode="sideways")
        )
        self.assertEqual(3, len(errors))
        self.assertTrue(any("URL" in error for error in errors))
        self.assertTrue(any("API key" in error for error in errors))
        self.assertTrue(any("request_mode" in error for error in errors))

    def test_admin_password_alone_is_acceptable(self):
        errors = ds.validate_diskovarr_config(
            _base_config(api_key="", admin_password="secret")
        )
        self.assertEqual([], errors)

    def test_rejects_non_http_urls(self):
        errors = ds.validate_diskovarr_config(
            _base_config(url="ftp://127.0.0.1", riven_url="file:///tmp")
        )
        self.assertEqual(2, len(errors))


class RivenBridgeEnvTests(unittest.TestCase):
    def test_env_is_empty_without_key_or_when_disabled(self):
        self.assertEqual({}, ds.riven_bridge_env(_base_config()))
        self.assertEqual(
            {},
            ds.riven_bridge_env(
                _base_config(enabled=False, riven_bridge_key=BRIDGE_KEY)
            ),
        )
        self.assertEqual(
            {},
            ds.riven_bridge_env(
                _base_config(configure_riven=False, riven_bridge_key=BRIDGE_KEY)
            ),
        )

    def test_env_points_riven_overseerr_source_at_diskovarr(self):
        env = ds.riven_bridge_env(
            _base_config(url="http://diskovarr.lan:3232/", riven_bridge_key=BRIDGE_KEY)
        )
        self.assertEqual(
            env,
            {
                "RIVEN_CONTENT_OVERSEERR_ENABLED": "true",
                "RIVEN_CONTENT_OVERSEERR_URL": "http://diskovarr.lan:3232",
                "RIVEN_CONTENT_OVERSEERR_API_KEY": BRIDGE_KEY,
                "RIVEN_CONTENT_OVERSEERR_USE_WEBHOOK": "false",
            },
        )


class EnsureBridgeKeyTests(unittest.TestCase):
    def test_provisions_enables_and_persists_the_bridge_key(self):
        cfg = _base_config()
        client = FakeClient(
            {
                ("GET", "/admin/riven/config"): {"dumbHasApiKey": True},
                ("POST", "/admin/riven/dumb/enable"): {"ok": True},
                ("GET", "/admin/connections/reveal"): {"dumbApiKey": BRIDGE_KEY},
            }
        )
        manager = _cm({"diskovarr": cfg})
        with patch.object(ds, "CONFIG_MANAGER", manager):
            key, changed = ds.ensure_riven_bridge_key(client, cfg)

        self.assertEqual(BRIDGE_KEY, key)
        self.assertTrue(changed)
        self.assertEqual(BRIDGE_KEY, cfg["riven_bridge_key"])
        manager.save_config.assert_called_once()
        self.assertEqual(
            [
                ("GET", "/admin/riven/config"),
                ("POST", "/admin/riven/dumb/enable"),
                ("GET", "/admin/connections/reveal"),
            ],
            [(method, path) for method, path, _, _ in client.calls],
        )

    def test_unchanged_key_does_not_rewrite_config(self):
        cfg = _base_config(riven_bridge_key=BRIDGE_KEY)
        client = FakeClient(
            {("GET", "/admin/connections/reveal"): {"dumbApiKey": BRIDGE_KEY}}
        )
        manager = _cm({"diskovarr": cfg})
        with patch.object(ds, "CONFIG_MANAGER", manager):
            _, changed = ds.ensure_riven_bridge_key(client, cfg)
        self.assertFalse(changed)
        manager.save_config.assert_not_called()

    def test_rejects_keys_riven_would_refuse(self):
        client = FakeClient(
            {("GET", "/admin/connections/reveal"): {"dumbApiKey": "short"}}
        )
        with self.assertRaises(ds.DiskovarrError):
            ds.ensure_riven_bridge_key(client, _base_config(), persist=False)


class ConnectionPayloadTests(unittest.TestCase):
    def test_riven_is_authoritative_and_other_services_fill_only_gaps(self):
        payload = ds.build_connection_payload(
            _base_config(request_mode="push"),
            current={
                "riven_url": "http://old:1",
                "plex_url": "http://plex.lan:32400",
                "tautulli_url": "",
                "radarr_url": "",
            },
            revealed={"plexToken": "keep-me", "tautulliApiKey": "", "radarrApiKey": ""},
            riven={"url": "http://127.0.0.1:8082", "api_key": "riven-key"},
            plex={"url": "http://127.0.0.1:32400", "token": "new-token"},
            tautulli={"url": "http://127.0.0.1:8181", "api_key": "taut-key"},
            radarr={"url": "http://127.0.0.1:7878", "api_key": ""},
            jellyfin={"url": "http://127.0.0.1:8096"},
        )

        self.assertEqual(payload["dumb_request_mode"], "push")
        self.assertEqual(payload["riven_url"], "http://127.0.0.1:8082")
        self.assertEqual(payload["riven_api_key"], "riven-key")
        self.assertTrue(payload["riven_enabled"])
        # Diskovarr already has Plex configured: leave it alone.
        self.assertNotIn("plex_url", payload)
        self.assertNotIn("plex_token", payload)
        # Tautulli was empty in Diskovarr: fill both URL and key.
        self.assertEqual(payload["tautulli_url"], "http://127.0.0.1:8181")
        self.assertEqual(payload["tautulli_api_key"], "taut-key")
        # Radarr URL is filled, but no key was available so none is sent and the
        # service is never flipped on.
        self.assertEqual(payload["radarr_url"], "http://127.0.0.1:7878")
        self.assertNotIn("radarr_api_key", payload)
        self.assertNotIn("radarr_enabled", payload)
        self.assertEqual(payload["jellyfin_url"], "http://127.0.0.1:8096")

    def test_without_riven_only_the_mode_is_sent(self):
        payload = ds.build_connection_payload(
            _base_config(), current={}, revealed={}, riven=None
        )
        self.assertEqual({"dumb_request_mode": "pull"}, payload)


class ConnectionDiscoveryTests(unittest.TestCase):
    def test_riven_connection_uses_configured_or_derived_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = os.path.join(tmp, "settings.json")
            with open(settings, "w", encoding="utf-8") as handle:
                json.dump({"api_key": "riven-key"}, handle)
            config = {
                "riven_backend": {
                    "enabled": True,
                    "port": 8082,
                    "config_file": settings,
                }
            }
            with patch.object(ds, "CONFIG_MANAGER", _cm(config)):
                derived = ds.riven_connection(_base_config())
                explicit = ds.riven_connection(
                    _base_config(riven_url="http://dumb.lan:8082/")
                )
        self.assertEqual(
            {"url": "http://127.0.0.1:8082", "api_key": "riven-key"}, derived
        )
        self.assertEqual(
            {"url": "http://dumb.lan:8082", "api_key": "riven-key"}, explicit
        )

    def test_riven_connection_is_none_when_riven_disabled(self):
        with patch.object(
            ds, "CONFIG_MANAGER", _cm({"riven_backend": {"enabled": False}})
        ):
            self.assertIsNone(ds.riven_connection(_base_config()))

    def test_tautulli_api_key_is_read_from_config_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = os.path.join(tmp, "config.ini")
            with open(ini, "w", encoding="utf-8") as handle:
                handle.write('[General]\napi_key = "abc123"\n')
            self.assertEqual("abc123", ds._parse_tautulli_api_key(ini))
        self.assertEqual("", ds._parse_tautulli_api_key("/nonexistent/config.ini"))

    def test_arr_api_key_is_read_from_config_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = os.path.join(tmp, "config.xml")
            with open(xml, "w", encoding="utf-8") as handle:
                handle.write("<Config><ApiKey>arr-key</ApiKey></Config>")
            self.assertEqual("arr-key", ds._parse_arr_api_key(xml))


class RunSyncTests(unittest.TestCase):
    def test_disabled_integration_is_skipped(self):
        with patch.object(ds, "CONFIG_MANAGER", _cm({"diskovarr": {"enabled": False}})):
            result = ds.run_sync("manual")
        self.assertEqual("disabled", result["skipped"])
        self.assertFalse(result["ok"])

    def test_config_errors_are_reported_without_network_access(self):
        with (
            patch.object(
                ds, "CONFIG_MANAGER", _cm({"diskovarr": _base_config(api_key="")})
            ),
            patch.object(ds, "build_client") as build_client,
        ):
            result = ds.run_sync("manual")
        build_client.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_full_pass_provisions_bridge_and_pushes_connections(self):
        cfg = _base_config()
        client = FakeClient(
            {
                ("GET", "/health"): {"status": "ok", "version": "3.2.0"},
                ("GET", "/admin/riven/config"): {},
                ("POST", "/admin/riven/dumb/enable"): {"ok": True},
                ("GET", "/admin/connections/reveal"): {"dumbApiKey": BRIDGE_KEY},
                ("GET", "/admin/connections/settings"): {},
                ("POST", "/admin/connections/save"): {"success": True},
            }
        )
        config = {"diskovarr": cfg, "riven_backend": {"enabled": False}}
        with (
            patch.object(ds, "CONFIG_MANAGER", _cm(config)),
            patch.object(ds, "build_client", return_value=client),
            patch.object(ds, "plex_connection", return_value=None),
            patch.object(ds, "tautulli_connection", return_value=None),
            patch.object(ds, "arr_connection", return_value=None),
            patch.object(ds, "jellyfin_connection", return_value=None),
        ):
            result = ds.run_sync("manual")

        self.assertTrue(result["ok"], result)
        self.assertEqual("3.2.0", result["version"])
        self.assertEqual("updated", result["steps"]["riven_bridge"])
        self.assertEqual(["dumb_request_mode"], result["steps"]["connections"])
        self.assertEqual(BRIDGE_KEY, cfg["riven_bridge_key"])
        self.assertEqual(result, ds.get_last_sync())

    def test_unreachable_diskovarr_is_recorded_not_raised(self):
        client = FakeClient({("GET", "/health"): ds.DiskovarrError("down")})
        with (
            patch.object(ds, "CONFIG_MANAGER", _cm({"diskovarr": _base_config()})),
            patch.object(ds, "build_client", return_value=client),
        ):
            result = ds.run_sync("manual")
        self.assertFalse(result["ok"])
        self.assertEqual(["down"], result["errors"])


class PrepareRivenBridgeTests(unittest.TestCase):
    def test_falls_back_to_cached_key_when_diskovarr_is_down(self):
        cfg = _base_config(riven_bridge_key=BRIDGE_KEY)
        client = FakeClient({("GET", "/admin/riven/config"): ds.DiskovarrError("down")})
        with (
            patch.object(ds, "CONFIG_MANAGER", _cm({"diskovarr": cfg})),
            patch.object(ds, "build_client", return_value=client),
        ):
            self.assertTrue(ds.prepare_riven_bridge())

        cfg = _base_config()
        with (
            patch.object(ds, "CONFIG_MANAGER", _cm({"diskovarr": cfg})),
            patch.object(ds, "build_client", return_value=client),
        ):
            self.assertFalse(ds.prepare_riven_bridge())


class ClientTransportTests(unittest.TestCase):
    def _http_error(self, code, body=b"{}"):
        return urllib.error.HTTPError(
            "http://127.0.0.1:3232/x", code, "err", {}, io.BytesIO(body)
        )

    def test_rejects_unsupported_url_schemes(self):
        with self.assertRaises(ValueError):
            ds.DiskovarrClient("ftp://127.0.0.1:3232")

    def test_admin_401_triggers_password_login_and_retry(self):
        client = ds.DiskovarrClient(
            "http://127.0.0.1:3232", api_key="", admin_password="secret"
        )
        seen = []

        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_open(request):
            seen.append((request.get_method(), request.selector, request.data))
            if request.selector == "/admin/login":
                return _Response(b'{"ok": true}')
            if len(seen) == 1:
                raise self._http_error(401, b'{"error":"Admin session required"}')
            return _Response(b'{"riven_url": "http://127.0.0.1:8082"}')

        with patch.object(client, "_open", side_effect=fake_open):
            result = client.connection_settings()

        self.assertEqual({"riven_url": "http://127.0.0.1:8082"}, result)
        self.assertEqual(
            [
                "/admin/connections/settings",
                "/admin/login",
                "/admin/connections/settings",
            ],
            [selector for _, selector, _ in seen],
        )
        self.assertEqual({"password": "secret"}, json.loads(seen[1][2].decode("utf-8")))

    def test_http_errors_surface_the_server_message(self):
        client = ds.DiskovarrClient("http://127.0.0.1:3232", api_key="k")
        with patch.object(
            client,
            "_open",
            side_effect=self._http_error(401, b'{"message":"Unauthorized"}'),
        ):
            with self.assertRaises(ds.DiskovarrError) as raised:
                client.health()
        self.assertIn("HTTP 401", str(raised.exception))
        self.assertIn("Unauthorized", str(raised.exception))


class StatusTests(unittest.TestCase):
    def test_status_reports_bridge_alignment_with_riven_settings(self):
        cfg = _base_config(riven_bridge_key=BRIDGE_KEY)
        riven_block = {
            "enabled": True,
            "url": "http://127.0.0.1:3232/",
            "api_key": BRIDGE_KEY,
        }
        client = FakeClient(
            {
                ("GET", "/health"): {"version": "3.2.0"},
                ("GET", "/admin/connections/settings"): {
                    "riven_enabled": True,
                    "riven_url": "http://127.0.0.1:8082",
                },
                ("GET", "/api/v1/request/count"): {"pending": 1, "approved": 2},
            }
        )
        with (
            patch.object(ds, "CONFIG_MANAGER", _cm({"diskovarr": cfg})),
            patch.object(ds, "build_client", return_value=client),
            patch.object(ds, "read_riven_overseerr_settings", return_value=riven_block),
        ):
            status = ds.collect_status()

        self.assertTrue(status["reachable"])
        self.assertTrue(status["admin_access"])
        self.assertEqual("3.2.0", status["version"])
        self.assertEqual({"pending": 1, "approved": 2}, status["requests"])
        self.assertTrue(status["riven_bridge"]["riven_url_matches"])
        self.assertTrue(status["riven_bridge"]["riven_key_matches"])
        self.assertTrue(status["connections"]["riven_enabled"])
        count_call = next(
            call for call in client.calls if call[1] == "/api/v1/request/count"
        )
        self.assertEqual(BRIDGE_KEY, count_call[3])

    def test_status_short_circuits_when_disabled(self):
        with patch.object(ds, "CONFIG_MANAGER", _cm({"diskovarr": {"enabled": False}})):
            status = ds.collect_status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["reachable"])


if __name__ == "__main__":
    unittest.main()
