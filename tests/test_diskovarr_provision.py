import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch


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


if not _module_available("utils.global_logger"):
    global_logger = types.ModuleType("utils.global_logger")
    global_logger.logger = _Logger()
    sys.modules["utils.global_logger"] = global_logger

from utils import diskovarr_provision as dp
from utils import diskovarr_settings as ds


class _FakeConfigManager:
    def __init__(self, config):
        self.config = config
        self.save_config = Mock()

    def get(self, key, default=None):
        return self.config.get(key, default)


def _config():
    return {
        "dumb": {"plex_address": "", "plex_token": ""},
        "diskovarr": {
            "enabled": False,
            "url": "",
            "api_key": "",
            "admin_password": "",
            "request_mode": "pull",
            "riven_url": "",
            "configure_riven": True,
            "configure_connections": True,
            "riven_bridge_key": "",
        },
        "riven_backend": {
            "enabled": False,
            "process_name": "Riven Backend",
            "port": 8080,
            "symlink_library_path": "/mnt/debrid/riven_symlinks",
            "symlink_backup_roots": ["/mnt/debrid/riven_symlinks"],
            "env": {"RIVEN_DOWNLOADERS_REAL_DEBRID_API_KEY": "old"},
            "config_file": "/riven/backend/data/settings.json",
        },
        "riven_frontend": {"enabled": False, "process_name": "Riven Frontend", "port": 3000},
        "postgres": {"enabled": False, "process_name": "PostgreSQL", "port": 5432},
        "decypharr": {"enabled": False, "process_name": "Decypharr", "port": 8282, "api_keys": {}, "mount_type": "dfs", "mount_path": "/mnt/debrid/decypharr"},
        "zilean": {"enabled": False, "process_name": "Zilean", "port": 8182},
        "tautulli": {"enabled": False, "process_name": "Tautulli", "port": 8181},
        "sonarr": {"instances": {"Default": {"enabled": False, "process_name": "Sonarr", "port": 8989}}},
        "radarr": {"instances": {"Default": {"enabled": False, "process_name": "Radarr", "port": 7878}}},
        "prowlarr": {"instances": {"Default": {"enabled": False, "process_name": "Prowlarr", "port": 9696}}},
        "seerr": {"instances": {"Default": {"enabled": False, "process_name": "Seerr", "port": 5055}}},
        "zurg": {"instances": {}},
        "rclone": {"instances": {}},
    }


def _plan(**overrides):
    plan = {
        "debrid": {"provider": "alldebrid", "api_key": "ad-key"},
        "diskovarr": {"url": "http://127.0.0.1:3232/", "api_key": "dk"},
        "services": ["zilean", "sonarr"],
        "tmdb_api_key": "0123456789abcdef0123456789abcdef",
        "plex": {"url": "http://plex:32400", "token": "ptok"},
        "library_path": "/mnt/debrid/library/",
    }
    plan.update(overrides)
    return plan


class NormalizePlanTests(unittest.TestCase):
    def test_fills_defaults_and_always_includes_riven(self):
        plan = dp.normalize_plan(_plan(services=["sonarr"], library_path="", plex=None))
        self.assertEqual("alldebrid", plan["debrid"]["provider"])
        self.assertEqual(["riven_backend", "sonarr"], plan["services"])
        self.assertEqual(dp.DEFAULT_LIBRARY_PATH, plan["library_path"])
        self.assertIsNone(plan["plex"])
        self.assertEqual("http://127.0.0.1:3232", plan["diskovarr"]["url"])

    def test_accepts_provider_spelling_variants(self):
        plan = dp.normalize_plan(_plan(debrid={"provider": "Real-Debrid", "api_key": "rd"}))
        self.assertEqual("realdebrid", plan["debrid"]["provider"])

    def test_rejects_unknown_provider_missing_key_and_unknown_service(self):
        with self.assertRaises(dp.ProvisionError):
            dp.normalize_plan(_plan(debrid={"provider": "torbox", "api_key": "x"}))
        with self.assertRaises(dp.ProvisionError):
            dp.normalize_plan(_plan(debrid={"provider": "alldebrid", "api_key": ""}))
        with self.assertRaises(dp.ProvisionError):
            dp.normalize_plan(_plan(services=["plex"]))
        with self.assertRaises(dp.ProvisionError):
            dp.normalize_plan(_plan(diskovarr={"url": "", "api_key": "k"}))
        with self.assertRaises(dp.ProvisionError):
            dp.normalize_plan(_plan(library_path="relative/path"))


class RivenEnvTests(unittest.TestCase):
    def test_alldebrid_plan_enables_only_alldebrid_and_extras(self):
        env = dp.riven_env_overrides(dp.normalize_plan(_plan()))
        self.assertEqual("true", env["RIVEN_DOWNLOADERS_ALL_DEBRID_ENABLED"])
        self.assertEqual("false", env["RIVEN_DOWNLOADERS_REAL_DEBRID_ENABLED"])
        self.assertEqual("ad-key", env["RIVEN_DOWNLOADERS_ALL_DEBRID_API_KEY"])
        self.assertNotIn("RIVEN_DOWNLOADERS_REAL_DEBRID_API_KEY", env)
        self.assertEqual("true", env["RIVEN_SCRAPING_ZILEAN_ENABLED"])
        self.assertEqual("true", env["RIVEN_UPDATERS_PLEX_ENABLED"])
        self.assertEqual(
            "0123456789abcdef0123456789abcdef",
            env["RIVEN_INDEXER_TMDB_READ_ACCESS_TOKEN"],
        )

    def test_realdebrid_plan_without_extras(self):
        plan = dp.normalize_plan(
            _plan(debrid={"provider": "realdebrid", "api_key": "rd"}, services=[], plex=None, tmdb_api_key="")
        )
        env = dp.riven_env_overrides(plan)
        self.assertEqual("true", env["RIVEN_DOWNLOADERS_REAL_DEBRID_ENABLED"])
        self.assertEqual("rd", env["RIVEN_DOWNLOADERS_REAL_DEBRID_API_KEY"])
        self.assertNotIn("RIVEN_SCRAPING_ZILEAN_ENABLED", env)
        self.assertNotIn("RIVEN_UPDATERS_PLEX_ENABLED", env)
        self.assertNotIn("RIVEN_INDEXER_TMDB_READ_ACCESS_TOKEN", env)


class ConfigStepTests(unittest.TestCase):
    def setUp(self):
        self.cm = _FakeConfigManager(_config())
        self.patches = [
            patch.object(dp, "CONFIG_MANAGER", self.cm),
            patch.object(ds, "CONFIG_MANAGER", self.cm),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_configure_writes_diskovarr_plex_and_riven_blocks(self):
        plan = dp.normalize_plan(_plan())
        dp._configure_diskovarr_block(plan)
        self.assertTrue(dp._configure_plex(plan))
        dp._configure_riven(plan)

        disk = self.cm.config["diskovarr"]
        self.assertTrue(disk["enabled"])
        self.assertEqual("http://127.0.0.1:3232", disk["url"])
        self.assertEqual("dk", disk["api_key"])
        self.assertTrue(disk["configure_riven"] and disk["configure_connections"])
        self.assertEqual("http://plex:32400", self.cm.config["dumb"]["plex_address"])
        self.assertEqual("ptok", self.cm.config["dumb"]["plex_token"])

        riven = self.cm.config["riven_backend"]
        self.assertEqual("/mnt/debrid/library", riven["symlink_library_path"])
        self.assertEqual(["/mnt/debrid/library"], riven["symlink_backup_roots"])
        self.assertNotIn("RIVEN_DOWNLOADERS_REAL_DEBRID_API_KEY", riven["env"])
        self.assertEqual("ad-key", riven["env"]["RIVEN_DOWNLOADERS_ALL_DEBRID_API_KEY"])

    def test_alldebrid_stack_uses_decypharr_then_singletons(self):
        plan = dp.normalize_plan(_plan())
        started = []
        with patch.object(dp, "_unified_start", side_effect=lambda core, optional=None: started.append(("unified", core, optional)) or {}), patch.object(
            dp, "_start_singleton", side_effect=lambda key: started.append(("singleton", key))
        ):
            dp._start_debrid_stack(plan)
        self.assertEqual("rclone", self.cm.config["decypharr"]["mount_type"])
        self.assertEqual("ad-key", self.cm.config["decypharr"]["api_keys"]["alldebrid"])
        self.assertEqual(
            [
                ("unified", [{"name": "decypharr", "debrid_service": "AllDebrid", "debrid_key": "ad-key"}], None),
                ("singleton", "postgres"),
                ("singleton", "riven_backend"),
            ],
            started,
        )

    def test_realdebrid_stack_goes_through_riven_onboarding(self):
        plan = dp.normalize_plan(_plan(debrid={"provider": "realdebrid", "api_key": "rd"}))
        with patch.object(dp, "_unified_start", return_value={}) as unified, patch.object(dp, "_start_singleton") as single:
            dp._start_debrid_stack(plan)
        unified.assert_called_once_with(
            [{"name": "riven_backend", "debrid_service": "RealDebrid", "debrid_key": "rd"}]
        )
        single.assert_not_called()

    def test_extra_services_split_core_optional_and_frontend(self):
        plan = dp.normalize_plan(_plan(services=["zilean", "sonarr", "prowlarr", "tautulli", "riven_frontend"]))
        with patch.object(dp, "_unified_start", return_value={"errors": []}) as unified, patch.object(dp, "_start_singleton") as single:
            dp._start_extra_services(plan)
        unified.assert_called_once_with([{"name": "sonarr"}, {"name": "prowlarr"}], ["zilean", "tautulli"])
        single.assert_called_once_with("riven_frontend")

    def test_capabilities_reports_catalog_and_current_provider(self):
        self.cm.config["decypharr"]["enabled"] = True
        self.cm.config["decypharr"]["api_keys"] = {"alldebrid": "k"}
        with patch.object(dp, "_is_running", return_value=False):
            caps = dp.capabilities()
        self.assertTrue(caps["provision"])
        self.assertEqual("alldebrid", caps["current"]["provider"])
        self.assertEqual({"alldebrid", "realdebrid"}, {p["key"] for p in caps["providers"]})
        keys = [s["key"] for s in caps["services"]]
        self.assertEqual([e["key"] for e in dp.SERVICE_CATALOG], keys)
        self.assertEqual("/mnt/debrid/riven_symlinks/movies", caps["current"]["paths"]["movies"])

    def test_run_plan_records_steps_and_result(self):
        plan = dp.normalize_plan(_plan(services=["sonarr"]))
        with patch.object(dp, "_start_debrid_stack") as debrid, patch.object(
            dp, "_start_extra_services", return_value={"errors": []}
        ), patch.object(dp, "_wait_for_riven_api_key", return_value="riven-key"), patch.object(
            ds, "run_sync", return_value={"ok": True, "steps": {}, "errors": []}
        ) as sync, patch.object(dp, "_is_running", return_value=True):
            dp._JOB.clear()
            result = dp.run_plan(plan)
        debrid.assert_called_once()
        sync.assert_called_once_with("provision", wait_for_riven_s=30)
        self.assertEqual("alldebrid", result["provider"])
        self.assertTrue(result["riven"]["has_api_key"])
        self.assertEqual("http://127.0.0.1:8080", result["riven"]["url"])
        self.assertEqual("/mnt/debrid/library/shows", result["paths"]["shows"])
        statuses = {s["key"]: s["status"] for s in dp.job_status()["steps"]}
        self.assertEqual({"config": "done", "debrid": "done", "services": "done", "riven_key": "done", "wire": "done"}, statuses)

    def test_start_plan_rejects_concurrent_runs(self):
        with patch.object(dp, "run_plan", side_effect=lambda plan: (_ for _ in ()).throw(dp.ProvisionError("boom"))):
            state = dp.start_plan(_plan())
            dp._JOB_THREAD.join(timeout=5)
        self.assertIn("started_at", state)
        final = dp.job_status()
        self.assertFalse(final["running"])
        self.assertFalse(final["ok"])
        self.assertEqual(["boom"], final["errors"])


class RivenSettingsDecypharrFallbackTests(unittest.TestCase):
    def test_parse_config_keys_falls_back_to_decypharr(self):
        stub_cm = _FakeConfigManager(
            {
                "riven_backend": {},
                "rclone": {"instances": {}},
                "zurg": {"instances": {}},
                "decypharr": {
                    "enabled": True,
                    "mount_type": "rclone",
                    "mount_path": "/mnt/debrid/decypharr",
                    "api_keys": {"alldebrid": "ad", "torbox": ""},
                },
                "postgres": {},
                "zilean": {},
                "dumb": {},
            }
        )
        with patch.dict(sys.modules, {"utils.config_loader": types.SimpleNamespace(CONFIG_MANAGER=stub_cm)}):
            rs = importlib.reload(importlib.import_module("utils.riven_settings"))
            keys = rs.parse_config_keys(stub_cm.config)
        self.assertEqual("/mnt/debrid/decypharr/__all__", keys["SYMLINK_RCLONE_PATH"])
        self.assertEqual("ad", keys["DOWNLOADERS_ALL_DEBRID_API_KEY"])
        self.assertIsNone(keys["DOWNLOADERS_TORBOX_API_KEY"])
        self.assertEqual("/mnt/debrid/decypharr/__all__", stub_cm.config["riven_backend"]["wait_for_dir"])


if __name__ == "__main__":
    unittest.main()
