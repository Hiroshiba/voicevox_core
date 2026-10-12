#!/usr/bin/env python3
"""Frozen scalar arithmetic only. A result here is never runtime admission.

No subprocess, network, native library, Wasm or browser import is performed.
Complete runtime qualification, resources and provenance are separate mandatory
verifier obligations; a synthetic timing table can exercise this arithmetic but
cannot establish a performance result or authorize advancement.
"""
from __future__ import annotations

import math
from collections import Counter

ARMS = ("R", "A", "B", "Q", "P")
DILATIONS = (1, 3, 5)
T9_FAMILY = 3.184113720001939
T4_SENSITIVITY = 2.7764451051977854
MAD_MAX = 0.02


def _finite_positive(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 30_000:
        raise ValueError("duration_ms must be finite, positive and within the 30-second RPC cap")
    return value


def median(values):
    if not values:
        raise ValueError("empty series")
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def mean(values):
    if not values:
        raise ValueError("empty series")
    return math.fsum(values) / len(values)


def sample_sd(values):
    if len(values) < 2:
        raise ValueError("sample standard deviation needs two observations")
    average = mean(values)
    return math.sqrt(math.fsum((x - average) ** 2 for x in values) / (len(values) - 1))


def relative_mad(values):
    middle = median(values)
    if not middle > 0:
        raise ValueError("relative MAD needs positive median")
    return median([abs(x - middle) for x in values]) / middle


def point_from_logs(values):
    return math.exp(mean(values))


def summarize_ratio(numerator, denominator, family_bound=False):
    if len(numerator) != 10 or len(denominator) != 10:
        raise ValueError("exactly ten paired roots are required")
    ratios = [_finite_positive(x) / _finite_positive(y) for x, y in zip(numerator, denominator)]
    logs = [math.log(ratio) for ratio in ratios]
    paired_logs = [(logs[j] + logs[j + 1]) / 2 for j in range(0, 10, 2)]
    pair_mean = mean(paired_logs)
    pair_margin = T4_SENSITIVITY * sample_sd(paired_logs) / math.sqrt(5)
    result = dict(ratios=ratios, log_ratios=logs, geometric_mean=point_from_logs(logs),
        median=median(ratios), relative_mad=relative_mad(ratios),
        strict_wins=sum(x < 1 for x in ratios),
        first_five=point_from_logs(logs[:5]), last_five=point_from_logs(logs[5:]),
        leave_one_root_out=[point_from_logs(logs[:j] + logs[j + 1:]) for j in range(10)],
        pair_log_means=paired_logs,
        pair_sensitivity_95=[math.exp(pair_mean - pair_margin), math.exp(pair_mean + pair_margin)],
        pair_sensitivity_scope="descriptive marginal df4; not necessarily wider; not a gate",
        family_adjusted_upper=None)
    if family_bound:
        result["family_adjusted_upper"] = math.exp(mean(logs) + T9_FAMILY * sample_sd(logs) / math.sqrt(10))
    return result


def _primary_table(samples):
    if not isinstance(samples, list) or len(samples) != 150:
        raise ValueError("exactly all 150 retained samples are required")
    table = {}
    expected_keys = {"root", "dilation", "arm", "duration_ms"}
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != expected_keys:
            raise ValueError("retained scalar sample has missing/extra fields")
        root, d, arm = sample["root"], sample["dilation"], sample["arm"]
        if type(root) is not int or not 1 <= root <= 10 or type(d) is not int or d not in DILATIONS or arm not in ARMS:
            raise ValueError("invalid sample identity")
        key = (root, d, arm)
        if key in table:
            raise ValueError("duplicate root/dilation/arm")
        table[key] = _finite_positive(sample["duration_ms"])
    if set(table) != {(r, d, a) for r in range(1, 11) for d in DILATIONS for a in ARMS}:
        raise ValueError("incomplete sample matrix")
    return table


def analyze_primary_samples(samples):
    """Return a conditional arithmetic decision, never an admitted experiment.

    This interface intentionally cannot accept an authority/PASS boolean. The
    separately reviewed program/receipt verifier must establish all gates and
    independent terminal review before anyone adopts these conditional results.
    """
    table = _primary_table(samples)
    per_d = {}
    control_pass = True
    candidate_pass = {"Q": True, "P": True}
    panel_pass = True
    for d in DILATIONS:
        series = {a: [table[r, d, a] for r in range(1, 11)] for a in ARMS}
        duration_stats = {a: dict(values=xs, median=median(xs), relative_mad=relative_mad(xs))
                          for a, xs in series.items()}
        contrast_names = (("A", "R"), ("B", "A"), ("Q", "B"),
                          ("Q", "A"), ("P", "A"), ("P", "Q"),
                          ("Q", "R"), ("P", "R"))
        contrasts = {f"{x}/{y}": summarize_ratio(series[x], series[y], (x, y) in (("Q", "A"), ("P", "A"), ("P", "Q")))
                     for x, y in contrast_names}
        control = contrasts["A/R"]
        control_checks = dict(
            geometric_agreement=0.98 <= control["geometric_mean"] <= 1.02,
            median_agreement=0.98 <= control["median"] <= 1.02,
            ratio_dispersion=control["relative_mad"] <= MAD_MAX,
            R_dispersion=duration_stats["R"]["relative_mad"] <= MAD_MAX,
            A_dispersion=duration_stats["A"]["relative_mad"] <= MAD_MAX)
        control_pass &= all(control_checks.values())
        candidates = {}
        for candidate in ("Q", "P"):
            contrast = contrasts[f"{candidate}/A"]
            checks = dict(
                duration_dispersion=duration_stats[candidate]["relative_mad"] <= MAD_MAX,
                ratio_dispersion=contrast["relative_mad"] <= MAD_MAX,
                practical_point=contrast["geometric_mean"] <= 0.95,
                adjusted_upper=contrast["family_adjusted_upper"] < 1,
                strict_wins=contrast["strict_wins"] >= 8,
                both_halves=contrast["first_five"] < 1 and contrast["last_five"] < 1,
                all_leave_one_out=all(x < 1 for x in contrast["leave_one_root_out"]))
            candidates[candidate] = dict(checks=checks, per_dilation_pass=all(checks.values()))
            candidate_pass[candidate] &= all(checks.values())
        pq = contrasts["P/Q"]
        panel_checks = dict(adjusted_upper=pq["family_adjusted_upper"] < 1,
            strict_wins=pq["strict_wins"] >= 8, ratio_dispersion=pq["relative_mad"] <= MAD_MAX,
            both_halves=pq["first_five"] < 1 and pq["last_five"] < 1)
        panel_pass &= all(panel_checks.values())
        per_d[str(d)] = dict(durations=duration_stats, contrasts=contrasts,
            control_checks=control_checks, candidates=candidates, panel_checks=panel_checks)
    eligible = {a: bool(control_pass and passed) for a, passed in candidate_pass.items()}
    panel_pass = bool(control_pass and panel_pass)
    selected = ("P" if panel_pass else "Q") if all(eligible.values()) else (
        "Q" if eligible["Q"] else "P" if eligible["P"] else None)
    return dict(schema="chrome-panel-conditional-statistics-v1",
        evidence_basis="scalar arithmetic only; runtime and independent audit gates not established here",
        samples=samples, per_dilation=per_d, global_control_pass=bool(control_pass),
        conditional_candidate_eligible=eligible, conditional_panel_order_pass=panel_pass,
        conditional_selection=selected, performance_claim_authorized=False,
        advancement_authorized=False, inferred_cache_miss_causality=False,
        classification="conditional_candidate" if selected else
            "inconclusive_control_drift" if not control_pass else "no_candidate_meets_frozen_rules",
        limitations=["conditional t-model assumptions", "ten potentially dependent roots on one host",
            "dilation order not fully balanced", "fixed warmups do not prove settled JIT tier",
            "no equivalence, power or precision guarantee"])


def extract_primary_samples(call_records, expected_calls):
    """Strict identity/order extraction after detailed stage validation.

    It repeats the complete 600-call matrix identity check, but cannot replace
    call/resource/cleanup/qualification validation by the transition verifier.
    """
    if len(call_records) != 600 or len(expected_calls) != 600:
        raise ValueError("complete timing matrix required")
    samples = []
    for record, expected in zip(call_records, expected_calls):
        if not isinstance(record, dict) or record.get("call") != expected:
            raise ValueError("call identity/order mismatch")
        if any(type(record["call"][key]) is not type(value) for key, value in expected.items()):
            raise ValueError("call identity type mismatch")
        if expected["retained"]:
            duration = record.get("duration_ms")
            samples.append(dict(root=expected["root"], dilation=expected["dilation"],
                arm=expected["arm"], duration_ms=_finite_positive(duration)))
        elif record.get("duration_ms") is not None:
            raise ValueError("warmup cannot enter retained timing estimators")
    _primary_table(samples)
    return samples
