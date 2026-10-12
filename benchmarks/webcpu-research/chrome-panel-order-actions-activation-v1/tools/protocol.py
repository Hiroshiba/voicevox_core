#!/usr/bin/env python3
"""Pure-data, deterministic specification. No runtime imports or invocation."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

PLAN_SHA256 = "07456a44febb29f820920fb68fe4f62071676b05c0f933495db076ce06ace702"
ARMS = ("R", "A", "B", "Q", "P")
DILATIONS = (1, 3, 5)
WORDS = 7_880_704
MEMORY_BYTES = 134_217_728
ROOT_ORDERS = (
    ("R A P B Q", (1, 3, 5)), ("Q B P A R", (5, 3, 1)),
    ("A B R Q P", (3, 5, 1)), ("P Q R B A", (1, 5, 3)),
    ("B Q A P R", (5, 1, 3)), ("R P A Q B", (3, 1, 5)),
    ("Q P B R A", (1, 3, 5)), ("A R B P Q", (5, 3, 1)),
    ("P R Q A B", (3, 5, 1)), ("B A Q R P", (1, 5, 3)),
)
REGION_BYTES = (31_522_880, 720_896, 512, 31_522_816, 721_408, 2_708_992, 528)
REGION_ALIGNMENT = (64, 64, 64, 64, 16, 8, 16)
READONLY_REGIONS = (0, 1, 2, 4, 5, 6)


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def reject(_):
        raise ValueError("nonfinite JSON constant")
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs, parse_constant=reject)


def expected_schedule():
    calls = []
    stage_indices = Counter()
    def append(stage, root, dilation, arm, coverage, retained, fixture_id,
               phase, pass_index, dilation_position, arm_position, reference):
        calls.append(dict(call_index=len(calls), stage_call_index=stage_indices[stage],
            stage=stage, root=root, dilation=dilation, arm=arm, coverage=coverage,
            retained=retained, fixture_id=fixture_id, phase=phase, pass_index=pass_index,
            dilation_position=dilation_position, arm_position=arm_position,
            reference=reference, reference_words=WORDS if reference else 0,
            comparison_words=0 if reference else WORDS, operator_runs=1))
        stage_indices[stage] += 1
    for di, dilation in enumerate(DILATIONS):
        for fixture_id in range(1, 12):
            for ai, arm in enumerate(ARMS):
                for repeat in range(2):
                    append("qualification", 0, dilation, arm, repeat == 0, False,
                        fixture_id, "qualification", repeat, di, ai,
                        arm == "R" and repeat == 0)
    for root, (order, dilations) in enumerate(ROOT_ORDERS, 1):
        order = tuple(order.split())
        for di, dilation in enumerate(dilations):
            for pass_index in range(4):
                arms = ARMS if pass_index == 0 else order
                for ai, arm in enumerate(arms):
                    append("timing", root, dilation, arm, pass_index == 0,
                        pass_index == 3, 0,
                        "coverage_warmup" if pass_index == 0 else
                        "retained" if pass_index == 3 else "warmup",
                        pass_index, di, ai, pass_index == 0 and arm == "R")
    return calls


def schedule_document():
    calls = expected_schedule()
    return dict(schema="chrome-panel-schedule-v1", status="source_only_not_runtime_evidence",
        plan_sha256=PLAN_SHA256, operator_runs_per_call=1, total_calls=930,
        qualification_calls=330, timing_calls=600, timing_warmup_calls=450,
        retained_calls=150, total_reference_captures=63,
        total_comparisons=867, total_comparison_words=6_832_570_368,
        calls=calls)


def static_proofs():
    calls = expected_schedule()
    assert len(calls) == 930
    proofs = {}
    for stage, expected in (("qualification", (330, 33, 297, 2_340_569_088)),
                            ("timing", (600, 30, 570, 4_492_001_280))):
        selected = [c for c in calls if c["stage"] == stage]
        actual = (len(selected), sum(c["reference"] for c in selected),
            sum(not c["reference"] for c in selected),
            sum(c["comparison_words"] for c in selected))
        assert actual == expected
        proofs[stage] = dict(zip(("calls", "references", "comparisons", "comparison_words"), actual))
    primary = [c for c in calls if c["retained"]]
    assert len(primary) == 150
    positions = Counter((c["dilation"], c["arm"], c["arm_position"]) for c in primary)
    assert len(positions) == 75 and set(positions.values()) == {2}
    predecessors = Counter()
    for d in DILATIONS:
        for order, _ in ROOT_ORDERS:
            arms = order.split()
            predecessors.update((d, a, b) for a, b in zip(arms, arms[1:]))
    assert len(predecessors) == 60 and set(predecessors.values()) == {2}
    d_positions = {str(d): [sum(ds[pos] == d for _, ds in ROOT_ORDERS)
                           for pos in range(3)] for d in DILATIONS}
    assert d_positions == {"1": [4, 2, 4], "3": [3, 4, 3], "5": [3, 4, 3]}
    readonly = sum(REGION_BYTES[i] for i in READONLY_REGIONS)
    assert readonly == 35_675_216
    buffers = 5 * MEMORY_BYTES + REGION_BYTES[3] + readonly + REGION_BYTES[4]
    assert buffers == 739_008_080
    proofs.update(schema="chrome-panel-static-proofs-v1", evidence="pure_python_source_arithmetic_only",
        schedule_canonical_sha256=digest(schedule_document()),
        each_arm_position_count=2, each_directed_predecessor_count=2,
        dilation_position_counts=d_positions, fully_balanced_dilation_order=False,
        readonly_workspace_bytes=readonly, explicit_large_buffer_bytes=buffers,
        engine_overhead_excluded=True, operator_invocations=0)
    return proofs


def write_protocol(root):
    root = Path(root)
    (root / "protocol").mkdir(parents=True, exist_ok=True)
    (root / "evidence").mkdir(parents=True, exist_ok=True)
    for path, document in ((root / "protocol/schedule.json", schedule_document()),
                           (root / "evidence/protocol-static-proofs.json", static_proofs())):
        path.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    write_protocol(Path(__file__).resolve().parents[1])
