"""Package, wrong-workspace and synthetic-render tests: pure Python only."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackageSourceTests(unittest.TestCase):
    def package_fixture(self, parent):
        source = parent / "source"
        source.mkdir()
        for name in ("tools", "src", "harness", "browser", "protocol", "launch", "workflow", "tests"):
            (source / name).mkdir()
        for name in ("README.md", "runner.py", "tools/launcher_template.py", "tools/render_report.py"):
            (source / name).write_bytes((ROOT / name).read_bytes())
        packager = load(ROOT / "tools/package_launcher.py", "panel_test_packager")
        first = packager.package(source)
        second = packager.package(source)
        self.assertEqual(first, second)
        return source, load(source / "dist/chrome_panel_order_benchmark.py", "panel_test_standalone"), first

    def test_reproducible_package_and_exact_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, launcher, receipt = self.package_fixture(base)
            self.assertTrue(receipt["execution_enabled"])
            self.assertTrue(receipt["runtime_authorization_required"])
            files = launcher.verified_files()
            self.assertEqual(set(files), set(receipt["source_files"]))
            target = base / "extracted"
            self.assertEqual(launcher.extract_only(target), len(files))
            for name, value in files.items():
                self.assertEqual((target / name).read_bytes(), value)
            with self.assertRaises(ValueError):
                launcher.extract_only(target)

    def test_modified_embedded_payload_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            _, launcher, _ = self.package_fixture(Path(directory))
            launcher.PAYLOAD_SHA256 = "0" * 64
            with self.assertRaises(ValueError):
                launcher.verified_files()

    def test_runtime_blocked_before_payload_and_wrong_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            _, launcher, _ = self.package_fixture(base)
            wrong = base / "wrong-workspace"
            wrong.mkdir()
            entry = load(ROOT / "runner.py", "panel_test_runner")
            old = Path.cwd()
            try:
                os.chdir(wrong)
                for inherited in ({}, {"GITHUB_ACTIONS": "true", "CI": "true", "GITHUB_WORKSPACE": str(wrong),
                                      "GITHUB_REPOSITORY": "Hiroshiba/voicevox_core", "GITHUB_RUN_ID": "999", "PANEL_EXECUTION_GO": "true"}):
                    with mock.patch.dict(os.environ, inherited, clear=True):
                        for arguments in ([], ["--prepare"], ["--compile"], ["--run"], ["--actions"],
                                          ["--execute-browser"], ["--execute", "--go", "/missing/approved.json"]):
                            for module in (launcher, entry):
                                with mock.patch.object(launcher, "verified_files", side_effect=AssertionError("payload read before block")):
                                    with self.assertRaises(SystemExit):
                                        module.main(arguments)
                            self.assertEqual(list(wrong.iterdir()), [])
            finally:
                os.chdir(old)

    def test_standalone_wrong_go_cannot_extract_or_delegate(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            _, launcher, _ = self.package_fixture(base)
            for name in ("go", "review", "clock"):
                (base / (name + ".json")).write_text("{}")
            for inherited in ({}, {"GITHUB_ACTIONS": "true", "PANEL_EXECUTION_GO": "true"}):
                with mock.patch.dict(os.environ, inherited, clear=True):
                    with self.assertRaisesRegex(ValueError, "does not bind"):
                        launcher.main(["--execute", "--go", str(base / "go.json"),
                            "--source-review", str(base / "review.json"), "--clock", str(base / "clock.json"),
                            "--source-root", str(base / "fresh-source"), "--work", str(base / "fresh-work")])
                    self.assertFalse((base / "fresh-source").exists())
                    self.assertFalse((base / "fresh-work").exists())

    def test_symlink_extract_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            _, launcher, _ = self.package_fixture(base)
            actual = base / "actual"
            actual.mkdir()
            link = base / "link"
            link.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(ValueError):
                launcher.extract_only(link / "destination")

    def test_root_source_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, _, _ = self.package_fixture(base)
            external = base / "external-readme"
            external.write_text("unadmitted source")
            (source / "README.md").unlink()
            (source / "README.md").symlink_to(external)
            packager = load(ROOT / "tools/package_launcher.py", "panel_symlink_packager")
            with self.assertRaisesRegex(ValueError, "redirected source"):
                packager.inventory(source)

    def test_synthetic_renderer_never_actual(self):
        stats = load(ROOT / "tools/statistics.py", "panel_render_stats")
        renderer = load(ROOT / "tools/render_report.py", "panel_test_renderer")
        samples = [dict(root=r, dilation=d, arm=a, duration_ms=100 * {"R":1,"A":1,"B":.98,"Q":.9,"P":.89}[a])
            for r in range(1, 11) for d in (1, 3, 5) for a in ("R", "A", "B", "Q", "P")]
        document = dict(evidence="synthetic_source_test", actual_actions_run=False,
            statistics=stats.analyze_primary_samples(samples), note="<script>not executable</script>")
        output = renderer.render_synthetic(document)
        self.assertIn("SYNTHETIC SOURCE TEST", output)
        self.assertNotIn("<script>", output)
        self.assertIn("&lt;script&gt;", output)
        document["actual_actions_run"] = True
        with self.assertRaises(ValueError):
            renderer.render_synthetic(document)
        with self.assertRaises(ValueError):
            renderer.render_actual(document)


if __name__ == "__main__":
    unittest.main()
