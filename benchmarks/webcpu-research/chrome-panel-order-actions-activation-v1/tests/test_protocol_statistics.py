"""Pure Python fixtures exercise source arithmetic, never performance evidence."""
import importlib.util
import math
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
def module(name):
    spec = importlib.util.spec_from_file_location("panel_" + name, ROOT / "tools" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

p = module("protocol")
s = module("statistics")

def samples(q=0.90, panel=0.88, control=1.0):
    return [dict(root=r, dilation=d, arm=a, duration_ms=100 *
        ({"R": 1, "A": control, "B": 0.98, "Q": q, "P": panel}[a]))
        for r in range(1, 11) for d in p.DILATIONS for a in p.ARMS]


class ProtocolTests(unittest.TestCase):
    def test_counts_balance_buffers(self):
        proof = p.static_proofs()
        self.assertEqual(proof["operator_invocations"], 0)
        self.assertEqual(proof["explicit_large_buffer_bytes"], 739008080)
        self.assertFalse(proof["fully_balanced_dilation_order"])

    def test_static_schedule_equals_checked_in(self):
        self.assertEqual(p.load_json(ROOT / "protocol/schedule.json"), p.schedule_document())

    def test_one_run_full_retention(self):
        calls = p.expected_schedule()
        self.assertEqual(sum(c["operator_runs"] for c in calls), 930)
        self.assertEqual(sum(c["retained"] for c in calls), 150)
        for stage, roots in (("qualification", {0}), ("timing", set(range(1, 11)))):
            self.assertEqual({c["root"] for c in calls if c["stage"] == stage}, roots)

    def test_qualification_every_arm_coverage_then_plain(self):
        calls = p.expected_schedule()[:330]
        for first, second in zip(calls[::2], calls[1::2]):
            self.assertTrue(first["coverage"])
            self.assertFalse(second["coverage"])
            self.assertEqual((first["arm"], first["dilation"], first["fixture_id"]),
                (second["arm"], second["dilation"], second["fixture_id"]))


class StatisticsTests(unittest.TestCase):
    def test_both_panel_qualifies(self):
        result = s.analyze_primary_samples(samples())
        self.assertEqual(result["conditional_selection"], "P")
        self.assertFalse(result["performance_claim_authorized"])
        self.assertFalse(result["advancement_authorized"])

    def test_both_without_panel_select_simpler_q(self):
        result = s.analyze_primary_samples(samples(q=.90, panel=.91))
        self.assertTrue(all(result["conditional_candidate_eligible"].values()))
        self.assertFalse(result["conditional_panel_order_pass"])
        self.assertEqual(result["conditional_selection"], "Q")

    def test_only_q(self):
        result = s.analyze_primary_samples(samples(panel=.98))
        self.assertEqual(result["conditional_selection"], "Q")

    def test_only_p(self):
        result = s.analyze_primary_samples(samples(q=.98))
        self.assertEqual(result["conditional_selection"], "P")

    def test_neither(self):
        self.assertIsNone(s.analyze_primary_samples(samples(q=.98, panel=.97))["conditional_selection"])

    def test_control_drift_terminal_for_both(self):
        result = s.analyze_primary_samples(samples(control=1.04))
        self.assertEqual(result["classification"], "inconclusive_control_drift")
        self.assertIsNone(result["conditional_selection"])

    def test_candidate_noise_not_other_candidate(self):
        data = samples()
        for item in data:
            if item["arm"] == "P":
                item["duration_ms"] = 80 if item["root"] % 2 else 100
        result = s.analyze_primary_samples(data)
        self.assertTrue(result["conditional_candidate_eligible"]["Q"])
        self.assertFalse(result["conditional_candidate_eligible"]["P"])
        self.assertEqual(result["conditional_selection"], "Q")

    def test_exact_even_median_unscaled_mad(self):
        self.assertEqual(s.median([1, 2, 3, 8]), 2.5)
        self.assertEqual(s.relative_mad([1, 2, 3, 8]), .4)

    def test_complementary_pairs_need_not_be_wider(self):
        ratios = [math.exp(x) for x in [-.03, .03] * 5]
        result = s.summarize_ratio(ratios, [1] * 10, True)
        low, high = result["pair_sensitivity_95"]
        self.assertLess(high - low, 1e-12)
        self.assertGreater(result["family_adjusted_upper"], 1)

    def test_half_and_loo_are_geometric(self):
        ratios = [.8, .9, .85, .95, .91, .92, .87, .93, .89, .94]
        result = s.summarize_ratio(ratios, [1] * 10)
        self.assertAlmostEqual(result["first_five"], math.prod(ratios[:5]) ** .2)
        self.assertAlmostEqual(result["leave_one_root_out"][0], math.prod(ratios[1:]) ** (1/9))

    def test_bad_samples_fail(self):
        for value in (True, 0, -1, float("nan"), float("inf"), 30001):
            data = samples()
            data[0]["duration_ms"] = value
            with self.assertRaises(ValueError):
                s.analyze_primary_samples(data)
        data = samples()
        data[-1] = data[0]
        with self.assertRaises(ValueError):
            s.analyze_primary_samples(data)

    def test_ties_do_not_win(self):
        result = s.summarize_ratio([1] * 10, [1] * 10, True)
        self.assertEqual(result["strict_wins"], 0)
        self.assertEqual(result["family_adjusted_upper"], 1)


if __name__ == "__main__":
    unittest.main()
