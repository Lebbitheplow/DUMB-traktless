import tempfile
import unittest
from pathlib import Path

from utils.riven_patches.apply import (
    FILES,
    PATCHES,
    _apply_patches,
    apply_riven_patches,
)


def build_fake_riven_src(root: Path) -> None:
    """Write a synthetic Riven src tree carrying every anchor the patches need.

    Derived from PATCHES itself, so adding a patch does not require touching
    this helper - a new anchor is picked up automatically.
    """
    by_file: dict[str, list[str]] = {}
    for _group, relative, _marker, old, _new in PATCHES:
        by_file.setdefault(relative, []).append(old)

    for relative, snippets in by_file.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n\n".join(snippets) + "\n", encoding="utf-8")


class RivenPatchTests(unittest.TestCase):
    def test_applies_every_patch_to_a_clean_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            build_fake_riven_src(config_dir / "src")

            success, error = apply_riven_patches(str(config_dir))

            self.assertTrue(success, error)
            self.assertIsNone(error)

            for _group, relative, marker, _old, _new in PATCHES:
                text = (config_dir / "src" / relative).read_text(encoding="utf-8")
                self.assertIn(marker, text, f"{relative} missing marker {marker!r}")

    def test_copies_the_backported_modules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            src = config_dir / "src"
            build_fake_riven_src(src)

            success, _error = apply_riven_patches(str(config_dir))
            self.assertTrue(success)

            expected = sorted(p.relative_to(FILES) for p in FILES.rglob("*.py"))
            self.assertTrue(expected, "no payload modules found to copy")
            for relative in expected:
                target = src / relative
                self.assertTrue(target.is_file(), f"{relative} was not copied")
                self.assertEqual(
                    target.read_text(encoding="utf-8"),
                    (FILES / relative).read_text(encoding="utf-8"),
                )

    def test_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            src = config_dir / "src"
            build_fake_riven_src(src)

            applied, skipped, failures = _apply_patches(src)
            self.assertEqual(failures, [])
            self.assertEqual(len(applied), len(PATCHES))
            self.assertEqual(skipped, [])

            first_pass = {
                relative: (src / relative).read_text(encoding="utf-8")
                for _g, relative, _m, _o, _n in PATCHES
            }

            applied, skipped, failures = _apply_patches(src)
            self.assertEqual(failures, [])
            self.assertEqual(applied, [], "second pass rewrote already-patched code")
            self.assertEqual(len(skipped), len(PATCHES))

            for relative, text in first_pass.items():
                self.assertEqual((src / relative).read_text(encoding="utf-8"), text)

    def test_reports_failure_when_an_anchor_drifts(self):
        target = "program/services/downloaders/alldebrid.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            src = config_dir / "src"
            build_fake_riven_src(src)

            drifted = src / target
            drifted.write_text(
                "# upstream rewrote this module\n", encoding="utf-8"
            )

            success, error = apply_riven_patches(str(config_dir))

            self.assertFalse(success)
            self.assertIn("failed to apply", error)

            _applied, _skipped, failures = _apply_patches(src)
            self.assertTrue(
                any(target in failure for failure in failures),
                f"failures did not name {target}: {failures}",
            )
            self.assertTrue(
                all("anchor not found" in failure for failure in failures),
                failures,
            )

    def test_reports_failure_when_source_tree_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            success, error = apply_riven_patches(str(Path(temp_dir) / "absent"))

        self.assertFalse(success)
        self.assertIn("Riven source not found", error)


if __name__ == "__main__":
    unittest.main()
