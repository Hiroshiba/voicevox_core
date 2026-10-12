#!/usr/bin/env python3
"""Portable deterministic C/source-manifest generation. Never compiles or runs C/JS.

Only stdlib file/text/integer operations are used. A process/network audit hook
is installed before generation, including --check. No dependency discovery,
toolchain execution, imported runtime artifacts, or operator model is permitted.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
M, N, K, KS = 61568, 128, 128, 11
FOCUS_BASE = 30752
OFFSETS = {1: [8, 21, 34, 47], 3: [48, 17, 22, 15], 5: [28, 29, 30, 31]}
ENDPOINTS = [(0, 0), (0, 127), (10, 0), (10, 127)]
NAMES = ["finite_mixed_cancellation", "signed_zero", "subnormal_normal_boundary",
         "infinity_overflow_invalid", "padded_zero_special_weights",
         "competing_nan_ks0_k0", "competing_nan_ks0_k127",
         "competing_nan_ks10_k0", "competing_nan_ks10_k127",
         "bias_vs_input_nan", "bias_vs_weight_nan"]
MIXED = [0x3e800001, 0xbe000003, 0x3d800005, 0xbe800007, 0x3e000009, 0xbd80000b]
SUBNORMAL = [1, 0x80000001, 0x007fffff, 0x807fffff, 0x00800000, 0x80800000]
BOUNDARY_WEIGHTS = [0x3f800000, 0xbf800000, 0x3f000000, 0x40000000]
PADDING = [0x7f800000, 0xff800000, 0x7fc00001, 0xffc54321,
           0x7f800001, 0xff800123, 0x7fc12345, 0xff812345]
AUDIT = {"subprocess_invocations": 0, "network_invocations": 0}


def install_audit():
    def audit(event, args):
        if event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.posix_spawnp", "os.fork", "os.exec"):
            AUDIT["subprocess_invocations"] += 1
            raise RuntimeError("Source-only generation forbids " + event)
        if event.startswith("socket."):
            AUDIT["network_invocations"] += 1
            raise RuntimeError("Source-only generation forbids " + event)
    sys.addaudithook(audit)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def nan_word(role, index, phase):
    sign = (index + phase) & 1
    quiet = ((index + phase) >> 1) & 1
    return ((sign << 31) | 0x7f800000 | (quiet << 22) |
            ((role + 1) << 16) | 0x123 | (index << 2))


def injections(fid, d):
    """Exact sparse overwrite recipe; words are unsigned integers, never NaNs."""
    rows = [FOCUS_BASE + offset for offset in OFFSETS[d]]
    phase = [1, 3, 5].index(d)
    ins, ws, bs = [], [], []

    def inp(r, p, k, word):
        x = rows[r] - 5 * d + p * d
        assert 0 <= x < M
        ins.append({"row": x, "k": k, "word": word})

    def weight(n, p, k, word):
        ws.append({"channel": n, "ks": p, "k": k, "word": word})

    if fid == 1:
        for r in range(4):
            for k, word in enumerate([0x4b800000, 0x3f800000, 0xcb800000]):
                inp(r, 0, k, word)
        for n in range(N):
            for k in range(3):
                weight(n, 0, k, 0x3f800000)
    elif fid == 4:
        for r in range(4):
            for p, k, word in [(0, 0, 0x7f7fffff), (0, 1, 0xff7fffff),
                               (10, 126, 0x7f800000), (10, 127, 0xff800000)]:
                inp(r, p, k, word)
        for n in range(N):
            for p, k, word in [(0, 0, 0x40000000), (0, 1, 0x40000000),
                               (10, 126, 0x80000000 if n & 1 else 0),
                               (10, 127, 0xbf800000 if n & 1 else 0x3f800000)]:
                weight(n, p, k, word)
    elif fid == 5:
        for n in range(N):
            weight(n, 0 if n < 64 else 10, 127 if (n // 8) & 1 else 0, PADDING[n % 8])
    elif 6 <= fid <= 9:
        p, k = ENDPOINTS[fid - 6]
        for r in range(4):
            inp(r, p, k, nan_word(1, r, phase))
        for n in range(N):
            weight(n, p, k, nan_word(2, n, phase))
    elif fid in (10, 11):
        for n in range(N):
            bs.append({"channel": n, "word": nan_word(0, n, phase)})
        if fid == 10:
            for r in range(4):
                inp(r, 0, 0, nan_word(1, r, phase))
        else:
            for n in range(N):
                weight(n, 0, 0, nan_word(2, n, phase))
    return ins, ws, bs


def fixture_manifest():
    cases = []
    for fid, name in enumerate(NAMES, 1):
        for d in (1, 3, 5):
            ins, ws, bs = injections(fid, d)
            if fid == 5:
                witnesses = [{"rows": [0, 1, 2, 3], "channels": [0, 63], "ks": 0},
                             {"rows": [M-4, M-3, M-2, M-1], "channels": [64, 127], "ks": 10}]
            else:
                witnesses = [{"rows": [FOCUS_BASE + x for x in OFFSETS[d]], "channels": [0, 127]}]
            cases.append({"fixture_id": fid, "dilation": d, "name": name,
                          "seed": {1: 17, 3: 29, 5: 43}[d],
                          "input_overwrites": ins, "weight_overwrites": ws,
                          "bias_overwrites": bs, "witnesses": witnesses,
                          "designated_output_words": 512,
                          "signed_zero_designated_words": {"even_channel": 0x80000000, "odd_channel": 0, "negative_count": 256, "positive_count": 256} if fid == 2 else None,
                          "endpoint": list(ENDPOINTS[fid-6]) if 6 <= fid <= 9 else None,
                          "expectation": "all_finite" if fid == 1 else "all_zero" if fid == 2 else "all_finite_boundary" if fid == 3 else "designated_nan"})
    return {
        "schema": "chrome-panel-qualification-fixtures-v1", "status": "SOURCE_ONLY_NOT_EXECUTED",
        "full_shape": {"M": M, "N": N, "K": K, "KS": KS, "MR": 4, "NR": 8, "MC": 32},
        "fixture_ids": dict(enumerate(NAMES, 1)), "timing_fixture_id": 0,
        "fixture_families": 11, "full_shape_cases": 33,
        "calls_per_case": 10, "calls": 330, "references": 33,
        "comparisons": 297, "comparison_words": 2340569088,
        "order_per_case": [[arm, coverage] for arm in "RABQP" for coverage in [1, 0]],
        "construction": "Every qualification input, raw weight, and bias word is stored with uint32_t + memcpy. No numeric NaN conversions.",
        "baseline": {
            "id1": {"input_palette": MIXED, "input_index": "(17*i+13*(i/128)+19*seed)%6", "weight_palette": MIXED, "weight_index": "(29*n+43*p+11*k+7*seed)%6", "bias_word": 0},
            "id2": {"input_words": [0, 0x80000000], "input_index": "i&1", "weight_words": [0xbf800000, 0x3f800000], "weight_index": "(k+n)&1", "bias_words": [0x80000000, 0], "bias_index": "n&1"},
            "id3": {"input_palette": SUBNORMAL, "input_index": "(i+dilation_index)%6", "weight_palette": BOUNDARY_WEIGHTS, "weight_index": "(n+k+p)%4", "bias_palette": SUBNORMAL, "bias_index": "n%6"},
            "ids4_to11": {"input_word": 0x3d800000, "weight_word": 0x3d800000, "bias_word": 0},
        },
        "focus_base": FOCUS_BASE, "focus_offsets_by_dilation": OFFSETS,
        "nan_word_formula": "((index+phase)&1)<<31 | 0x7f800000 | (((index+phase)>>1)&1)<<22 | (role+1)<<16 | 0x123 | index<<2",
        "nan_roles": {"bias": 0, "input": 1, "weight": 2},
        "phase": "dilation_index: D1=0,D3=1,D5=2",
        "padding_special_words_by_lane": PADDING,
        "padding_mapping": "channel<64:KS0,left rows0..3; channel>=64:KS10,right rowsM-4..M-1; K=(channel//8)%2 ?127:0; exactly one special weight per channel",
        "output_poison": {"timing": 0x7fc12345, "first_qualification_R": 0x7fc12345, "later": "bitwise complement of each live reference word; controller writes outside clock", "first_special_write_proof": "complete source stores plus full disjoint coverage, never universal sentinel absence"},
        "cases": cases,
    }


def fixtures_c():
    text = '''/* Generated qualification fixtures. All special data stays uint32 until memcpy.
 * This file has no operator invocation, allocation, or floating-point arithmetic.
 */
static unsigned active_fixture;
static const unsigned fixture_focus_offsets[3][4]={{8,21,34,47},{48,17,22,15},{28,29,30,31}};
static void fixture_store(float* p,uint32_t word) { memcpy(p,&word,4); }
static void fixture_fill(unsigned id,unsigned d) {
 const unsigned di=d==1?0:d==3?1:2,seed=d==1?17:d==3?29:43;
 const uint32_t mixed[]={MIXED_WORDS};
 const uint32_t subnormal[]={SUBNORMAL_WORDS};
 const uint32_t boundary_weights[]={BOUNDARY_WORDS};
 for(size_t i=0;i<WORDS;i++) {
  uint32_t word=0x3d800000u;
  if(id==1)word=mixed[(17*i+13*(i/128)+19*seed)%6];
  else if(id==2)word=(i&1)?0x80000000u:0u;
  else if(id==3)word=subnormal[(i+di)%6];
  fixture_store(input+i,word);
 }
 for(size_t n=0;n<128;n++) {
  fixture_store(bias+n,id==2?((n&1)?0u:0x80000000u):id==3?subnormal[n%6]:0u);
  for(size_t p=0;p<11;p++)for(size_t k=0;k<128;k++) {
   uint32_t word=0x3d800000u;
   if(id==1)word=mixed[(29*n+43*p+11*k+7*seed)%6];
   else if(id==2)word=((k+n)&1)?0x3f800000u:0xbf800000u;
   else if(id==3)word=boundary_weights[(n+k+p)%4];
   fixture_store(weights+(n*11+p)*128+k,word);
  }
 }
 /* Exact sparse overwrites mechanically generated from the public manifest. */
 switch(id*10+d) {
'''
    for key, words in [("MIXED_WORDS", MIXED), ("SUBNORMAL_WORDS", SUBNORMAL), ("BOUNDARY_WORDS", BOUNDARY_WEIGHTS)]:
        text = text.replace(key, ",".join(f"0x{x:08x}u" for x in words))
    for fid in range(1, 12):
        for d in (1, 3, 5):
            ins, ws, bs = injections(fid, d)
            text += f" case {fid*10+d}: /* fixture {fid}, D{d} */\n"
            for x in ins:
                text += f"  fixture_store(input+{x['row']*N+x['k']}u,0x{x['word']:08x}u);\n"
            for x in ws:
                text += f"  fixture_store(weights+{(x['channel']*KS+x['ks'])*K+x['k']}u,0x{x['word']:08x}u);\n"
            for x in bs:
                text += f"  fixture_store(bias+{x['channel']}u,0x{x['word']:08x}u);\n"
            text += "  break;\n"
    return text + ''' default: abort(); /* Caller validation makes this unreachable. */
 }
}
uint32_t screen_fixture_meta(unsigned i) {
 const unsigned di=dilation==1?0:dilation==3?1:2;
 if(i==0)return 1;
 if(i==1)return active_fixture;
 if(i==2)return active_fixture?512:0;
 if(i==3)return 30752;
 if(i>=4 && i<=7)return 30752+fixture_focus_offsets[di][i-4];
 if(i==8)return active_fixture>=6 && active_fixture<=9?(active_fixture>=8?10:0):UINT32_MAX;
 if(i==9)return active_fixture>=6 && active_fixture<=9?((active_fixture&1)?127:0):UINT32_MAX;
 if(i==10)return active_fixture==1?1:active_fixture==2?3:active_fixture==3?4:active_fixture>=4?2:0;
 if(i==11)return 11;
 return UINT32_MAX;
}
'''


def callbacks_c():
    prefix = (ROOT / "src/callback-prefix.c.in").read_text().replace("tools/generate_sources.py", "tools/generate_native.py")
    prologue = (ROOT / "src/common-prologue.c.in").read_text()
    body = (ROOT / "src/common-body.c.in").read_text()
    args = "const struct igemm_context context[restrict XNN_MIN_ELEMENTS(1)],\n    size_t m_start, size_t n_start, size_t m_size, size_t n_size"
    panel = "const float* panel = (const float*) ((uintptr_t) context->packed_w +\n        (n_start + n) * context->w_stride);"
    result = prefix
    for arm in ("P", "Q"):
        result += f"void vvx_ksb_20261011_{arm}({args})\n{{\n" + prologue
        if arm == "P":
            result += "  for (size_t n = 0; n < 128; n += 8) {\n    " + panel + "\n    for (size_t s = 0; s < 32; s += 4) {\n"
        else:
            result += "  for (size_t s = 0; s < 32; s += 4) {\n    for (size_t n = 0; n < 128; n += 8) {\n      " + panel.replace("\n        ", "\n          ") + "\n"
        result += body + "    }\n  }\n}\n\n"
    return result + (ROOT / "src/selector.c.in").read_text()


def operator_c():
    text = (ROOT / "harness/operator.c.in").read_text()
    text = text.replace("Fixed finite fixtures;", "Fixed timing and eleven qualification fixture families;")
    text = text.replace("vvx_ksb_20261011_C", "vvx_ksb_20261011_Q")
    text = text.replace("static unsigned dilation,diagnostic;", '''static unsigned dilation,diagnostic;
/* Dedicated quiescent alias challenge: no allocation or operator work.
 * Compare every prior heap at this SAME offset before/after a changed write,
 * restore the word, then reject any cross-heap change. Raw offset stays in page. */
static uint32_t alias_probe;
uintptr_t screen_alias_probe_ptr(void) { return (uintptr_t)&alias_probe; }
#include "fixtures.inc"''')
    text = text.replace("int screen_prepare(unsigned d,unsigned warmup) {", "static int screen_prepare_common(unsigned d,unsigned warmup,unsigned fixture_id) {")
    text = text.replace("|| warmup>1)return prepare_return(30);", "|| warmup>1 || fixture_id>11)return prepare_return(30);")
    text = text.replace("dilation=d;diagnostic=warmup;", "dilation=d;diagnostic=warmup;active_fixture=fixture_id;")
    start = text.index(" for(size_t i=0;i<WORDS;i++) input[i]=")
    end = text.index(" for(size_t i=0;i<WORDS;i++){const uint32_t poison", start)
    finite = text[start:end].replace(" memset((unsigned char*)input+IO_BYTES,0,64);\n", "")
    text = text[:start] + " if(!fixture_id){\n" + finite + " }else fixture_fill(fixture_id,d);\n memset((unsigned char*)input+IO_BYTES,0,64);\n" + text[end:]
    wrappers = '''int screen_prepare(unsigned d,unsigned coverage) { return screen_prepare_common(d,coverage,0); }
int screen_prepare_fixture(unsigned d,unsigned coverage,unsigned fixture_id) {
 if(fixture_id<1 || fixture_id>11)return 30;
 return screen_prepare_common(d,coverage,fixture_id);
}
'''
    return text.replace("/* This is the sole actual operator invocation.", wrappers + "/* This is the sole actual operator invocation.")


def source_proofs():
    endpoints = padding = 0
    for d in (1, 3, 5):
        rows = [FOCUS_BASE+x for x in OFFSETS[d]]
        footprints = [{row-5*d+p*d for p in range(KS)} for row in rows]
        for r, footprint in enumerate(footprints):
            assert rows[r] % 4 == r
            assert all(0 <= x < M for x in footprint)
            assert all(footprint.isdisjoint(other) for other in footprints[r+1:])
        for fid in range(6, 10):
            ins, ws, bs = injections(fid, d)
            p, k = ENDPOINTS[fid-6]
            assert not bs
            injection = {(x["row"], x["k"]): x["word"] for x in ins}
            assert len(injection) == 4
            for r, row in enumerate(rows):
                hits = [(q, x["k"]) for q in range(KS) for x in ins if x["row"] == row-5*d+q*d]
                assert hits == [(p, k)]
                for n in range(N):
                    own = [x for x in ws if x["channel"] == n]
                    assert len(own) == 1 and (own[0]["ks"], own[0]["k"]) == (p, k)
                    assert all((x & 0x7f800000) == 0x7f800000 and x & 0x7fffff for x in (injection[(row-5*d+p*d,k)], own[0]["word"]))
                    # Every earlier term is exactly 2^-8; j additions give
                    # j/256, representable because 0<=j<=1407<2^24.
                    assert 0 <= p*K+k <= 1407 < 2**24
                    endpoints += 1
        _, ws, _ = injections(5, d)
        for n in range(N):
            own = [x for x in ws if x["channel"] == n]
            assert len(own) == 1
            w = own[0]
            for row in (range(4) if n < 64 else range(M-4, M)):
                x = row-5*d+w["ks"]*d
                assert x < 0 or x >= M
                assert w["ks"] == (0 if n < 64 else 10)
                assert w["k"] == (127 if (n//8)&1 else 0)
                assert w["word"] == PADDING[n%8]
                padding += 1
    assert endpoints == 6144 and padding == 1536
    return {
        "status": "PASS_SOURCE_PROOF_ONLY", "operator_execution": False,
        "unmasked_endpoint_witnesses": endpoints, "padded_special_witnesses": padding,
        "endpoint_finite_prefix": {"input_word": 0x3d800000, "weight_word": 0x3d800000, "bias_word": 0, "input_and_weight_exact": "1/16", "product_exact": "1/256", "prefix_accumulator_exact": "j/256 for 0<=j<=1407", "maximum_prefix_numerator": 1407, "overflow_or_invalid_before_endpoint": False, "argument": "All designated rows are interior; disjoint eleven-row footprints exclude every other injection. Only the designated input and channel weight at the endpoint are NaN. Prior products and accumulator values are exactly representable dyadics bounded by 1407/256, so no earlier overflow/invalid/NaN can mask the endpoint."},
        "padding_finite_prefix": "One special weight per channel only. All prior products are 0 or1/256 and all prefix accumulators are exact j/256,0<=j<=1407. Left KS0 and right KS10 occupy separate channels. Every designated target pointer is the original zero sentinel; zero times the sole infinity/NaN produces NaN at the selected K.",
        "finite_rounding_witness": "At each focus row and all128 channels, bias0 and first KS0 K0..2 products are exactly2^24,1,-2^24. Round-to-nearest-even loses the+1 before cancellation, yielding0; reordered (2^24 + -2^24)+1 yields1. All other ID1 operands are finite with abs(input)<=2^24,abs(weight)<=1; even the conservative1408*2^24 bound is finite.",
        "signed_zero_proof": "At every designated interior row,input sign is k%2 (row*128 is even); weight sign is1-((k+n)%2),so every product sign is1-(n%2). Bias has exactly that sign. Every update adds two same-sign zeros,thus designated even-channel results are exactly0x80000000 and odd-channel results0x00000000. All16 panels contain four of each sign:256 negative and256 positive witnesses perD,768 of each overD1/3/5. Actual full outputs are still compared to R raw words.",
        "boundary_finite_proof": "Magnitude of every baseline operand is at most2^-126 for inputs/bias and2 for weights. Absolute sum bounded by(1+2816)*2^-126, far below overflow. Underflow and rounding are intentional.",
        "infinity_invalid_proof": "Designated rows start with maxfinite*2=+inf then -maxfinite*2=-inf; +inf+-inf is invalid/NaN. Later injections also exercise infinity times signed zero. All128 channel lanes and four MR slots are covered.",
        "bias_competition_proof": "Bias is a tagged NaN in every channel. At first operation KS0,K0, ID10 has tagged input NaN and finite weight, ID11 finite input and tagged weight NaN. No prior update can mask the specified addition competition.",
        "nan_payload_scope": "Signed quiet/signaling inputs span four MR slots; weights/bias span all16 panels and eight lanes. Exact output payload behavior is an empirical Chrome gate, not implied by these source proofs.",
        "task_store_proof": {"baseline_callbacks": 15392, "candidate_callbacks": 1924, "subtiles_per_candidate": 8, "panels_per_subtile": 16, "stores_per_panel": 8, "words_per_store": 4, "output_words": M*N, "argument": "Each candidate callback owns32 adjacent rows, each subtile four, each panel eight channels; eight four-word stores cover32 disjoint words.1924*8*16*8*4=7880704. Original R/A/B path uses the pinned complete MR4 kernel."},
        "subprocess_invocations": AUDIT["subprocess_invocations"], "network_invocations": AUDIT["network_invocations"],
    }


def abi_manifest():
    base = ["dilation", "M", "N", "MR", "NR", "KS", "kc_bytes", "ks_scaled_bytes", "mc", "nc", "pool_threads", "range_m", "range_n", "a_offset_u32", "operator_address_private", "pool_address_private", "selected_callback_address_private", "kernel_address_private", "participant_a_private", "participant_b_private", "coverage_callbacks", "coverage_enabled"]
    import re
    template = (ROOT/"harness/operator.c.in").read_text()
    diagnostic = {22+int(index): name.strip() for index, name in re.findall(r"case (\d+):return.*?/\* ([^*]+) \*/", template)}
    assert len(diagnostic) == 105
    return {
        "schema": "chrome-panel-native-abi-v1", "word_and_pointer_bits": 32,
        "exports": {"screen_init": [], "screen_prepare": ["dilation", "coverage_0_or_1"], "screen_prepare_fixture": ["dilation", "coverage_0_or_1", "fixture_id_1_to_11"], "screen_run": [], "screen_after": [], "screen_canaries": [], "screen_ptr": ["region_0_to_6"], "screen_bytes": ["region_0_to_6"], "screen_meta": ["index_0_to_126"], "screen_fixture_meta": ["index_0_to_11"], "screen_alias_probe_ptr": [], "screen_delete": [], "screen_shutdown": []},
        "sole_operator_invocation": "screen_run: exactly one synchronous xnn_run_operator(op,pool)",
        "lifecycle": "init once/module; each call prepare fresh complete create/reshape/setup, run once, after once, delete once; shutdown once/module",
        "calibration": "screen_meta(0) is harmless and returns0 with no live operator; no qualification-root calibration",
        "distinct_heap_probe": "Dedicated staticuint32 (4bytes/module, no allocation). Controller quiescent-checks every earlier heap at the NEW heap's SAME byte offset before/after writing complement(oldword) to new heap, restores the reserved word, and rejects any cross-heap change. Own-probe-only checks at differing offsets are insufficient. No raw address leaves page; no operator calls.",
        "regions": [{"id": i, "name": name, "bytes": count, "minimum_alignment": align, "readonly": i != 3, "caller_canary_bytes_each_side": 256 if i < 4 else 0} for i, (name, count, align) in enumerate(zip(["input", "raw_weights", "bias", "output", "packed_weights", "indirection", "zero"], [31522880, 720896, 512, 31522816, 721408, 2708992, 528], [64,64,64,64,16,8,16]))],
        "screen_meta": {**dict(enumerate(base)), **diagnostic},
        "screen_fixture_meta": {0: "schema=1", 1: "active_fixture_id_0_to_11", 2: "designated_witness_count_512_or_timing0", 3: "focus_base30752", 4: "focus_row_MR0", 5: "focus_row_MR1", 6: "focus_row_MR2", 7: "focus_row_MR3", 8: "endpoint_KS_or_UINT32_MAX", 9: "endpoint_K_or_UINT32_MAX", 10: "expectation:timing0,allfinite1,designatedNaN2,allzero3,boundaryfinite4", 11: "family_count11"},
        "private_pointer_contract": "Region addresses,a_offset,operator/pool/function/participant addresses stay inside quiescent page memory. Export only bounded checks, stable arm-local participant labels and hashes to public receipt.",
        "zero_contract": {"allocation_bytes": 528, "reshape_context_zero_size": 0, "setup_context_zero_size": 528, "zero_words": 132},
        "failure_diagnostics": "screen_meta22..126 retains the donor map exactly. These reads are outside the clock. Failed/unknown calls are terminal; no retry.",
        "coverage": "Callback observer has no barrier. Require two distinct actual participants, each task seen once, no errors; R/A15392 callbacks, B/Q/P1924.",
        "region_validation": "Controller verifies in-bounds disjoint regions,minimum alignment,every indirection entry and modulo-2^32 a_offset/sentinel semantics,readonly bytes,packed bytes,caller canaries,stack cookies,mod64 and mod4096 phases. Internal allocator metadata/padding are not claimed canary-covered.",
    }


def selector_proof():
    text = (ROOT/"src/selector.c.in").read_text()
    before_assignment = text.split("#if VVX_KSB_ARM == 1",1)[0]
    return {
        "schema": "chrome-panel-selector-source-binding-v1",
        "status": "SOURCE_ONLY_NO_FRESH_NEGATIVE_PROBES",
        "current_selector_before_assignment_sha256": sha(before_assignment.encode()),
        "current_selector_sha256": sha(text.encode()),
        "guard_relation": "Every precondition and branch before the final task assignment is byte-identical to the accepted v3 selector. Only the historical third-arm C callback target becomes Q; B=1 and P=2 remain unchanged. Q=3 has no alternative selector.",
        "historical_source_evidence": [
            {"path": "ks-blocking-qualification/harness/guard-cases.json", "sha256": "b0f742f850768f33a4982b94f864ba5cc6373ab6ec9014e9492433dbb116632a", "scope": "90 historical mutable negative descriptor cases"},
            {"path": "ks-blocking-qualification/evidence/harness-source-tests.json", "sha256": "d387ec107049d0827cf7724090e9b967c6745f80b7a26e43e24114a0cd2e69df", "scope": "Historical integer and source checks only"},
            {"path": "ks-blocking-qualification/launch/attempt/synthetic-receipt.json", "sha256": "548de264bd7959ff2f9c0da13e06865d36c6c3a00db9274aa81c7d8958a38d8d", "scope": "Historical Node qualification recorded90 guard cases,zero failures. It exercised R/A/B/P/C,not new Q or Chrome bundles. Referenced only; no runtime files copied or results inherited."},
        ],
        "pre_setup_lifecycle": "Production reshape calls this selector after persistent indirection creation while ctx.c=NULL,ctx.a_offset=0,ctx.zero_size=0. Every new prepare verifies actual chosen callback and descriptor before setup. Setup then establishes output,actual modulo32 offset,and context zero_size528; every run/after validates live geometry. Operator allocation zero_size remains528 throughout.",
        "new_runtime_scope": "Only the330 prescribed full-shape qualification calls. No new negative-descriptor probes,out-of-shape arithmetic,general-fallback qualification or local execution.",
        "emitted_review_pending": True,
    }


def outputs():
    manifest = fixture_manifest()
    files = {"src/vvx_ksb_20261011.c": callbacks_c(),
             "harness/operator.c": operator_c(), "harness/fixtures.inc": fixtures_c(),
             "protocol/qualification-fixtures.json": json.dumps(manifest, indent=2)+"\n",
             "harness/abi.json": json.dumps(abi_manifest(), indent=2)+"\n",
             "evidence/native-selector-proof.json": json.dumps(selector_proof(), indent=2)+"\n",
             "evidence/native-fixture-proof.json": json.dumps(source_proofs(), indent=2)+"\n"}
    receipt = {
        "schema": "chrome-panel-native-source-generation-v1", "status": "SOURCE_ONLY_NOT_COMPILED_OR_EXECUTED",
        "plan_sha256": "07456a44febb29f820920fb68fe4f62071676b05c0f933495db076ce06ace702",
        "donor": {"name": "ks-full-operator-screen-v3", "callback_sha256": "9a1bd19af2e09b9b8ccc6937074a3abb0be76c34b1529dd714db10e4d960c1e6", "operator_sha256": "925e53fa26da358708dd28a4e13709ca4b489f96e739e9b29a346548811834f8", "convolution_sha256": "421a162ba5b004938832f72bf86f72f9f506cb2e81566a9851ccc0be02a15ff6"},
        "common_body_sha256": sha((ROOT/"src/common-body.c.in").read_bytes()),
        "q_p_change": "Only MR4-subtile/NR8-panel loop nesting and panel-address placement differ. Identical arithmetic/bias/indirection/zero/offset/store body emitted twice.",
        "arm_definitions": {"R": None, "A": None, "B": "VVX_KSB_ARM=1", "Q": "VVX_KSB_ARM=3", "P": "VVX_KSB_ARM=2"},
        "generated": {name: {"bytes": len(text.encode()), "sha256": sha(text.encode())} for name, text in files.items()},
        "compiler_invocations": 0, "node_invocations": 0, "browser_invocations": 0,
        **AUDIT,
    }
    files["evidence/native-generation.json"] = json.dumps(receipt, indent=2)+"\n"
    return files


def main():
    install_audit()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify source bytes without writing")
    args = parser.parse_args()
    for name, text in outputs().items():
        path = ROOT/name
        if args.check:
            if not path.exists() or path.read_text() != text:
                raise SystemExit("Generated source mismatch: " + name)
        else:
            path.write_text(text)
    print(json.dumps({"status": "PASS_SOURCE_ONLY", "check_only": args.check, **AUDIT}))


if __name__ == "__main__":
    main()
