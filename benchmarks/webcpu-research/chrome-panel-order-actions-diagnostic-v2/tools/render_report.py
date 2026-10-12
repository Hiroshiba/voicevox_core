"""Pure-Python synthetic renderer. It cannot label test data as Actions evidence."""
from __future__ import annotations

import html
import json


def render_synthetic(document):
    if type(document) is not dict or document.get("evidence") != "synthetic_source_test":
        raise ValueError("explicit synthetic_source_test evidence label required")
    if document.get("actual_actions_run") is not False:
        raise ValueError("synthetic rendering cannot claim an Actions run")
    stats = document.get("statistics")
    if type(stats) is not dict or stats.get("schema") != "chrome-panel-conditional-statistics-v1":
        raise ValueError("frozen conditional statistics required")
    if stats.get("performance_claim_authorized") is not False or stats.get("advancement_authorized") is not False:
        raise ValueError("synthetic statistics may not grant result authority")
    rows = []
    for dilation in ("1", "3", "5"):
        item = stats["per_dilation"][dilation]
        for contrast in ("A/R", "B/A", "Q/B", "Q/A", "P/A", "P/Q"):
            value = item["contrasts"][contrast]
            upper = value["family_adjusted_upper"]
            rows.append("<tr>" + "".join("<td>" + html.escape(str(cell)) + "</td>" for cell in (
                dilation, contrast, format(value["geometric_mean"], ".6f"),
                "—" if upper is None else format(upper, ".6f"),
                format(value["relative_mad"] * 100, ".3f") + "%", str(value["strict_wins"]) + "/10")) + "</tr>")
    raw = html.escape(json.dumps(document, sort_keys=True, indent=2, allow_nan=False))
    return """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthetic source test • Chrome panel-order protocol</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:1120px;margin:40px auto;padding:0 24px;color:#182638;background:#f5f7fa}h1{line-height:1.2}.warning{background:#fff1cc;border:2px solid #a36a00;padding:18px;border-radius:8px}table{border-collapse:collapse;background:white;width:100%;margin:24px 0}th,td{padding:9px 14px;border-bottom:1px solid #d7dee8;text-align:right}th:first-child,td:first-child{text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:18px}small{color:#516172}</style>
<h1>Chrome panel-order protocol</h1><p class="warning"><strong>SYNTHETIC SOURCE TEST.</strong>
No Chrome, Wasm, compiler or operator was run. These numbers exercise the frozen
statistics and renderer only. They are not an Actions result or performance evidence.</p>
<p>R / A / B / Q / P · strict FP32 · proposed Chrome 153, revectorization OFF ·
330 qualification calls then conditional 600 timing calls on one Actions host.</p>
<table><thead><tr><th>Dilation</th><th>Contrast</th><th>Geometric ratio</th><th>Adjusted upper</th><th>Relative MAD</th><th>Strict wins</th></tr></thead><tbody>""" + "".join(rows) + """</tbody></table>
<small>Display rounding is presentation only. Decisions retain full returned precision.
Nine primary bounds are conditional t-model estimates; pair sensitivity is descriptive.
No settled JIT tier, cache-miss causality, production layout, adoption or model permission is established.</small>
<details><summary>Complete synthetic scalar input and derived arithmetic</summary><pre>""" + raw + """</pre></details></html>\n"""


def render_actual(*args, **kwargs):
    raise ValueError("No actual Actions artifact is admitted in this source-only release; independent terminal audit required")
