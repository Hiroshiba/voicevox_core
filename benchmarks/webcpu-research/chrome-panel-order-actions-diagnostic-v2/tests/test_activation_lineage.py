"""Scalar/source-only activation lineage checks; no emitted code is loaded."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActivationLineageTests(unittest.TestCase):
    def test_original_plan_bytes_are_unchanged(self):
        self.assertEqual(hashlib.sha256((ROOT / "protocol/sealed-plan-revision3.md").read_bytes()).hexdigest(),
                         "07456a44febb29f820920fb68fe4f62071676b05c0f933495db076ce06ace702")

    def test_native_scientific_and_statistical_sources_stay_frozen(self):
        manifest = json.loads((ROOT / "protocol/admissions/frozen-source-manifest.json").read_text())
        names = [n for n in manifest["files"] if n.startswith(("src/", "harness/"))]
        names += ["tools/protocol.py", "tools/statistics.py", "protocol/schedule.json",
                  "protocol/decision-rules.json", "protocol/resource-budget.json",
                  "protocol/qualification-fixtures.json"]
        for name in names:
            self.assertIn(name, manifest["files"])
            data = (ROOT / name).read_bytes()
            self.assertEqual(len(data), manifest["files"][name]["bytes"], name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest["files"][name]["sha256"], name)

    def test_actual_emitted_admission_is_scalar_and_has_no_runtime_claim(self):
        path = ROOT / "protocol/admissions/local-emitted-admission.json"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                         "34d559ce9e136ce58d274423b3de11202213e0b9d5a4c5a95a58faca029a574e")
        receipt = json.loads(path.read_text())
        self.assertEqual(receipt["status"], "PASS_COMPILE_CLOSURE_AND_STATIC_EMITTED_ADMISSION")
        self.assertFalse(receipt["Chrome_330call_qualification_completed"])
        self.assertFalse(receipt["cross_host_CI_rebuild_equality_demonstrated"])
        self.assertFalse(receipt["runtime_browser_model_ci_publication_authority"])
        self.assertEqual(receipt["new_operator_calls"], 0)

    def test_ten_compiled_assets_have_separate_three_host_provenance(self):
        path = ROOT / "protocol/admissions/local-emitted-assets.json"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                         "613cd86fbbe063ade683a01cf44eb2a3a8b09273664594efd61655ea0b7bf3bb")
        assets = json.loads(path.read_text())["assets"]
        compiled = [a for a in assets if a["arm"] is not None]
        host = [a for a in assets if a["arm"] is None]
        self.assertEqual({a["name"] for a in compiled},
                         {arm + "/screen." + suffix for arm in "RABQP" for suffix in ("js", "wasm")})
        self.assertEqual({a["name"] for a in host}, {"controller.js", "worker.js", "index.html"})
        self.assertEqual(len({a["sha256"] for a in compiled if a["name"].endswith(".js")}), 1)
        self.assertFalse(any(".worker.js" in a["name"] for a in assets))


if __name__ == "__main__":
    unittest.main()
