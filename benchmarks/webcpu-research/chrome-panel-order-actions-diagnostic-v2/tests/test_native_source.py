#!/usr/bin/env python3
"""Integer/source-only tests. Never compile, launch, or interpret C/JS/Wasm."""
from pathlib import Path
import ast
import hashlib
import importlib.util
import json
import re
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_source_generator", ROOT/"tools/generate_native.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)
G.install_audit()


def function(text, name):
    start = text.index("void " + name + "(")
    opening = text.index("{", start)
    depth = 1
    cursor = opening+1
    while depth:
        depth += (text[cursor] == "{") - (text[cursor] == "}")
        cursor += 1
    return text[start:cursor]


class NativeSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.callbacks = (ROOT/"src/vvx_ksb_20261011.c").read_text()
        cls.operator = (ROOT/"harness/operator.c").read_text()
        cls.template = (ROOT/"harness/operator.c.in").read_text()
        cls.fixture = (ROOT/"harness/fixtures.inc").read_text()
        cls.manifest = json.loads((ROOT/"protocol/qualification-fixtures.json").read_text())
        cls.abi = json.loads((ROOT/"harness/abi.json").read_text())

    def test_generation_exact_without_runtime(self):
        for name, source in G.outputs().items():
            self.assertEqual((ROOT/name).read_text(), source, name)
        self.assertEqual(G.AUDIT, {"subprocess_invocations": 0, "network_invocations": 0})

    def test_no_runtime_import_or_evaluation(self):
        tree = ast.parse((ROOT/"tools/generate_native.py").read_text())
        modules = {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
        self.assertTrue(modules <= {"argparse", "hashlib", "json", "sys", "re"})
        self.assertNotIn("eval(", (ROOT/"tools/generate_native.py").read_text())
        self.assertNotIn("exec(", (ROOT/"tools/generate_native.py").read_text())

    def test_donor_template_hashes(self):
        self.assertEqual(hashlib.sha256((ROOT/"harness/operator.c.in").read_bytes()).hexdigest(), "925e53fa26da358708dd28a4e13709ca4b489f96e739e9b29a346548811834f8")
        self.assertEqual(hashlib.sha256((ROOT/"src/convolution.ksb.c").read_bytes()).hexdigest(), "421a162ba5b004938832f72bf86f72f9f506cb2e81566a9851ccc0be02a15ff6")

    def test_no_C_arm_or_carry(self):
        self.assertNotIn("vvx_ksb_20261011_C", self.callbacks)
        self.assertNotIn("carry[", self.callbacks)
        self.assertIn("3 (Q)", (ROOT/"src/vvx_ksb_20261011.h").read_text())

    def test_QP_identical_inner_body(self):
        body = (ROOT/"src/common-body.c.in").read_text()
        for arm in "QP":
            self.assertEqual(function(self.callbacks, "vvx_ksb_20261011_"+arm).count(body.rstrip()), 1)
        self.assertEqual(self.callbacks.count(body), 2)
        self.assertEqual(body.count("wasm_f32x4_add(wasm_f32x4_mul("), 8)
        self.assertEqual(body.count("wasm_v128_store("), 8)
        self.assertEqual(body.count("if XNN_UNPREDICTABLE(a"), 4)

    def test_only_prespecified_loop_order(self):
        q = function(self.callbacks, "vvx_ksb_20261011_Q")
        p = function(self.callbacks, "vvx_ksb_20261011_P")
        self.assertLess(q.index("for (size_t s"), q.index("for (size_t n"))
        self.assertLess(p.index("for (size_t n"), p.index("for (size_t s"))
        for text in (q, p):
            self.assertLess(text.index("for (size_t p"), text.index("size_t k = kc"))
            self.assertIn("k -= sizeof(float);", text)
            self.assertNotIn("xnn_compute_igemm(", text)

    def test_P_exact_donor_when_available(self):
        donor = ROOT.parent/"ks-full-operator-screen-v3/src/vvx_ksb_20261011.c"
        if donor.exists():
            old = donor.read_text()
            self.assertEqual(function(old, "vvx_ksb_20261011_P"), function(self.callbacks, "vvx_ksb_20261011_P"))
            self.assertEqual(function(old, "vvx_ksb_20261011_B"), function(self.callbacks, "vvx_ksb_20261011_B"))
            self.assertEqual(function(old, "vvx_ksb_20261011_select").replace("vvx_ksb_20261011_C", "vvx_ksb_20261011_Q"), function(self.callbacks, "vvx_ksb_20261011_select"))
        else:
            self.skipTest("historical donor comparison unavailable in standalone; common-body and pinned-source checks still run")

    def test_distinct_heap_probe_has_no_allocation_or_operator_call(self):
        self.assertIn("static uint32_t alias_probe;", self.operator)
        self.assertIn("uintptr_t screen_alias_probe_ptr(void) { return (uintptr_t)&alias_probe; }", self.operator)
        self.assertIn("screen_alias_probe_ptr", self.abi["exports"])

    def test_B_eight_original_paths(self):
        b = function(self.callbacks, "vvx_ksb_20261011_B")
        self.assertIn("for (size_t s = 0; s < 32; s += 4)", b)
        self.assertIn("xnn_compute_igemm(context, m_start + s, n_start, 4, n_size);", b)

    def test_unchanged_narrow_selector_pre_setup(self):
        selector = function(self.callbacks, "vvx_ksb_20261011_select")
        for condition in ["ctx->zero_size == 0", "ctx->c == NULL", "ctx->a_offset == 0", "num_threads == 2", "nc == 128", "task->tile[0] == 4", "op->output_width == 61568", "ctx->ks == 11"]:
            self.assertIn(condition, selector)
        last_guard = selector.rindex("if (!")
        mutation = selector.index("task->task_2d_tile_2d = (")
        self.assertLess(last_guard, mutation)
        self.assertIn("task->tile[0] = 32;", selector)

    def test_actual_lifecycle_and_only_one_run(self):
        for symbol in ["xnn_create_convolution2d_nhwc_f32(", "xnn_reshape_convolution2d_nhwc_f32(", "xnn_setup_convolution2d_nhwc_f32(", "xnn_run_operator("]:
            self.assertEqual(self.operator.count(symbol), 1)
        self.assertIn("int screen_run(void) { return (int)xnn_run_operator(op,pool); }", self.operator)
        self.assertNotIn("xnn_run_operator", self.fixture)
        self.assertIn("int screen_prepare_fixture(unsigned d,unsigned coverage,unsigned fixture_id)", self.operator)
        self.assertIn("screen_prepare_common(d,coverage,0)", self.operator)

    def test_production_allocator_alignment_canaries_unchanged(self):
        self.assertIn("xnn_initialize(NULL)", self.operator)
        self.assertNotIn("struct xnn_allocator", self.operator)
        self.assertEqual(self.operator.count("guarded_allocate(NULL,64,"), 4)
        self.assertIn("#define RED 256u", self.operator)
        self.assertIn("XNN_EXTRA_BYTES + 128u * sizeof(float)", self.operator)
        self.assertIn("_Static_assert(ZERO_BYTES==528u", self.operator)
        self.assertIn("c->zero_size!=(before_setup?0:op->zero_size)", self.operator)
        self.assertIn("memset((unsigned char*)input+IO_BYTES,0,64);", self.operator)

    def test_real_pool_observer_without_barrier(self):
        self.assertIn("pthreadpool_create(2)", self.operator)
        observed = self.operator[self.operator.index("static void observed"):self.operator.index("int screen_init")]
        for forbidden in ("while", "pthread_barrier", "sched_yield"):
            self.assertNotIn(forbidden, observed)
        self.assertIn("pthread_self()", observed)
        self.assertIn("selected.task_2d_tile_2d(context,m,n,ms,ns);", observed)
        self.assertIn("atomic_load(&participant_a)==atomic_load(&participant_b)", self.operator)

    def test_finite_timing_recipe_unchanged(self):
        for line in self.template.splitlines():
            if "input[i]=" in line or "weights[(o*11+p)*128+k]=" in line or "bias[o]=" in line:
                self.assertIn(line, self.operator)
        self.assertIn("seed=d==1?17:d==3?29:43", self.operator)
        self.assertIn("const uint32_t poison=0x7FC12345u;memcpy(output+i,&poison,4);", self.operator)

    def test_meta_abi_unchanged(self):
        for start, end in [("static uint32_t screen_prepare_meta", "int screen_delete"), ("uintptr_t screen_ptr", "/* screen_meta indices")]:
            self.assertEqual(self.template[self.template.index(start):self.template.index(end)], self.operator[self.operator.index(start):self.operator.index(end)])
        abi = json.loads((ROOT/"harness/abi.json").read_text())
        self.assertEqual(len(abi["screen_meta"]), 127)
        self.assertEqual(abi["screen_meta"]["18"], "participant_a_private")
        self.assertEqual(abi["screen_meta"]["94"], "context_zero_size")
        self.assertEqual(sum(x["bytes"] for x in abi["regions"] if x["readonly"]), 35675216)

    def test_exact_fixture_matrix_counts(self):
        m = self.manifest
        self.assertEqual(len(m["cases"]), 33)
        self.assertEqual({(x["fixture_id"], x["dilation"]) for x in m["cases"]}, {(f,d) for f in range(1,12) for d in (1,3,5)})
        self.assertEqual(m["order_per_case"], [[a,c] for a in "RABQP" for c in [1,0]])
        self.assertEqual(m["comparisons"]*61568*128, m["comparison_words"])
        self.assertEqual(m["calls"], 330)

    def test_special_words_generated_integer_only(self):
        self.assertIn("memcpy(p,&word,4)", self.fixture)
        for bad in ("NAN", "nan(", "strtof", "isnan", "Float32Array", "Math.fround", "wasm_f32"):
            self.assertNotIn(bad, self.fixture)
        self.assertNotIn("malloc(", self.fixture)
        for case in self.manifest["cases"]:
            for key in ("input_overwrites", "weight_overwrites", "bias_overwrites"):
                for x in case[key]:
                    self.assertIs(type(x["word"]), int)
                    self.assertTrue(0 <= x["word"] <= 0xffffffff)

    def test_four_signed_quiet_signaling_classes(self):
        for role, count in [(0,128),(1,4),(2,128)]:
            for phase in range(3):
                words = [G.nan_word(role,i,phase) for i in range(count)]
                self.assertEqual({(w>>31,(w>>22)&1) for w in words}, {(s,q) for s in (0,1) for q in (0,1)})
                self.assertTrue(all(w&0x7f800000 == 0x7f800000 and w&0x7fffff for w in words))

    def test_signed_zero_exact_designated_outputs(self):
        for d in (1,3,5):
            for row in [G.FOCUS_BASE+x for x in G.OFFSETS[d]]:
                self.assertTrue(all(0<=row-5*d+p*d<G.M for p in range(11)))
                for n in range(128):
                    bias_sign = 1-(n&1)
                    for k in range(128):
                        input_sign = ((row*128+k)&1)
                        weight_sign = 1-((k+n)&1)
                        self.assertEqual(input_sign^weight_sign,bias_sign)
        for case in self.manifest["cases"]:
            if case["fixture_id"] == 2:
                self.assertEqual(case["signed_zero_designated_words"], {"even_channel":0x80000000,"odd_channel":0,"negative_count":256,"positive_count":256})

    def test_unmasked_endpoint_and_padding_proofs(self):
        p = G.source_proofs()
        self.assertEqual(p["unmasked_endpoint_witnesses"], 6144)
        self.assertEqual(p["padded_special_witnesses"], 1536)
        self.assertFalse(p["endpoint_finite_prefix"]["overflow_or_invalid_before_endpoint"])
        self.assertEqual(p["endpoint_finite_prefix"]["maximum_prefix_numerator"],1407)
        self.assertEqual(p["task_store_proof"]["output_words"], 1924*8*16*8*4)

    def test_each_manifest_overwrite_binds_emitted_source(self):
        for case in self.manifest["cases"]:
            label = f" case {case['fixture_id']*10+case['dilation']}:"
            part = self.fixture.split(label,1)[1].split("  break;",1)[0]
            expected = 0
            for key, tensor in [("input_overwrites","input"),("weight_overwrites","weights"),("bias_overwrites","bias")]:
                for x in case[key]:
                    index = x["row"]*128+x["k"] if tensor=="input" else (x["channel"]*11+x["ks"])*128+x["k"] if tensor=="weights" else x["channel"]
                    self.assertIn(f"fixture_store({tensor}+{index}u,0x{x['word']:08x}u);",part)
                    expected += 1
            self.assertEqual(part.count("fixture_store("), expected)

    def test_duplicate_injections_absent(self):
        for case in self.manifest["cases"]:
            for key, fields in [("input_overwrites",("row","k")),("weight_overwrites",("channel","ks","k")),("bias_overwrites",("channel",))]:
                self.assertEqual(len(case[key]),len({tuple(x[f] for f in fields) for x in case[key]}))

    def test_full_table_integer_geometry_and_padding(self):
        for d in (1,3,5):
            padded = 0
            indices = 0
            for tile in range(0,61568,4):
                for p in range(11):
                    for lane in range(4):
                        index = tile*11+p*4+lane
                        self.assertEqual(index,indices)
                        indices += 1
                        x = tile+lane-5*d+p*d
                        padded += x < 0 or x >= 61568
            self.assertEqual(indices,677248)
            self.assertEqual(padded,30*d)

    def test_source_mutations_detected(self):
        for before, after in [("p < 11", "p < 10"), ("k -= sizeof(float)","k -= 8"), ("wasm_f32x4_mul(va0, vb0123)","wasm_f32x4_mul(vb0123, va0)"), ("a0 != zero","a0 == zero"), ("ctx->zero_size == 0","ctx->zero_size == 528"), ("task->tile[0] = 32","task->tile[0] = 4")]:
            self.assertIn(before,self.callbacks)
            self.assertNotEqual(self.callbacks.replace(before,after,1),G.callbacks_c())
        for before, after in [("0x4b800000u","0x4b800001u"),("0x7f800001u","0x7fc00001u"),("fixture_store(bias+0u","fixture_store(bias+1u")]:
            self.assertIn(before,self.fixture)
            self.assertNotEqual(self.fixture.replace(before,after,1),G.fixtures_c())

    def test_compiled_artifacts_absent_in_owned_paths(self):
        for subdir in ("src", "harness"):
            for path in (ROOT/subdir).rglob("*"):
                self.assertNotIn(path.suffix, {".wasm", ".o", ".a", ".so", ".bc", ".ll", ".wat"})


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeSourceTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    receipt = {"status": "PASS_SOURCE_ONLY" if result.wasSuccessful() else "FAIL_SOURCE_ONLY", "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "scope": "Pure Python integer proofs and source/mutation checks only; no C/JS/Wasm execution or mathematical operator payload run", **G.AUDIT}
    (ROOT/"evidence/native-source-tests.json").write_text(json.dumps(receipt,indent=2)+"\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)
