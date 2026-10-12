"""Source/data-only checks. Never remove an execution gate or run payloads."""
import ast
import hashlib
import json
from pathlib import Path
import unittest
from launch import bootstrap_fetch, provisioning
from launch.source_only import SourceOnlyBlocked

ROOT=Path(__file__).resolve().parents[1]

class BootstrapSources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs=json.loads((ROOT/'launch/bootstrap-inputs.json').read_text())

    def test_download_roster_exact_pins_and_limits(self):
        rows=self.inputs['downloads'];self.assertEqual(len(rows),29)
        self.assertEqual(len({r['id'] for r in rows}),29)
        for row in rows:
            self.assertTrue(bootstrap_fetch.validate_download(row))
            self.assertEqual(row['maximum_compressed_bytes'],row['bytes'])
            self.assertGreater(row['maximum_expanded_bytes'],0)
            self.assertGreater(row['maximum_members'],0)
        acquired=[r for r in rows if r['sha256'] is None]
        self.assertEqual({r['id'] for r in acquired},{'sdk-node','sdk-tools'})
        self.assertEqual({r['id']:r['bytes'] for r in acquired},{'sdk-node':25738964,'sdk-tools':300508180})
        self.assertTrue(all('exact download byte count' in r['exact_size_provenance'] for r in acquired))

    def test_all_eight_wheels_are_downloadable_preexisting_pins(self):
        wheels=[r for r in self.inputs['downloads'] if r['id'].startswith('wheel-')]
        self.assertEqual(len(wheels),8)
        for row in wheels:
            self.assertTrue(row['url'].startswith('https://files.pythonhosted.org/packages/'))
            self.assertEqual(len(row['sha256']),64)
        psutil=next(r for r in wheels if r['package']=='psutil')
        self.assertEqual(psutil['bytes'],155560)
        self.assertEqual(psutil['sha256'],'076a2d2f923fd4821644f5ba89f059523da90dc9014e85f8e45a5774ca5bc6f9')

    def test_complete_sdk_execution_source_pins_before_tools(self):
        rows=self.inputs['sdk_execution_files']
        self.assertEqual(len(rows),15673)
        self.assertEqual(len({r['name'] for r in rows}),len(rows))
        self.assertTrue(all('__pycache__' not in r['name'] and not r['name'].endswith('.pyc') for r in rows))
        self.assertTrue(all(not r['name'].startswith('upstream/emscripten/cache/') for r in rows))
        for name in ('upstream/bin/clang-21','upstream/bin/lld','upstream/bin/wasm-opt','node/20.18.0_64bit/bin/node','upstream/emscripten/emcc.py'):
            self.assertIn(name,{r['name'] for r in rows})
        module=ast.parse((ROOT/'launch/provisioning.py').read_text())
        run=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='_run')
        source=ast.get_source_segment((ROOT/'launch/provisioning.py').read_text(),run)
        self.assertLess(source.index('_verify_sdk_execution('),source.index("for key in ('configure-offline'"))

    def test_exact_safe_symlink_rosters(self):
        sdk=self.inputs['sdk_symlinks'];self.assertEqual(len(sdk),29)
        self.assertTrue(all(x['within_sdk'] for x in sdk))
        all_links=[link for row in self.inputs['downloads'] for link in row['symlinks']]
        self.assertEqual(len(all_links),40)
        for row in self.inputs['downloads']:
            self.assertEqual(len(bootstrap_fetch.validate_symlink_roster(row['symlinks'])),len(row['symlinks']))
        self.assertEqual(bootstrap_fetch.checked_link('bin/clang++','clang'),'bin/clang')
        for target in ('/tmp/out','../../outside','../a/../../outside'):
            with self.assertRaises(ValueError):bootstrap_fetch.checked_link('bin/link',target)

    def test_link_chains_and_directory_targets_in_temporary_fixture(self):
        import tempfile
        rows=[{'name':'bin/clang','link_text':'clang-21','resolved_suffix':'bin/clang-21','target_kind':'file'},
              {'name':'bin/clang++','link_text':'clang','resolved_suffix':'bin/clang-21','target_kind':'file'},
              {'name':'tests/data','link_text':'../real-data','resolved_suffix':'real-data','target_kind':'directory'}]
        self.assertEqual(bootstrap_fetch.validate_symlink_roster(rows)['bin/clang++'],'bin/clang-21')
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);(root/'bin').mkdir();(root/'tests').mkdir();(root/'real-data').mkdir()
            (root/'bin/clang-21').write_bytes(b'inert fixture, never executed')
            for row in rows:(root/row['name']).symlink_to(row['link_text'])
            for row in rows:
                p=(root/row['name']).resolve(strict=True)
                self.assertTrue(p.is_relative_to(root));self.assertEqual(p.relative_to(root).as_posix(),row['resolved_suffix'])
                self.assertEqual(p.is_dir(),row['target_kind']=='directory')
        cycle=[{'name':'a','link_text':'b','resolved_suffix':'a','target_kind':'file'},
               {'name':'b','link_text':'a','resolved_suffix':'a','target_kind':'file'}]
        with self.assertRaises(ValueError):bootstrap_fetch.validate_symlink_roster(cycle)

    def test_unsafe_archive_names_rejected_without_extraction(self):
        for name in ('/absolute','../parent','a/../parent','a\\b','a//b','./a','a\x00b'):
            with self.assertRaises(ValueError):bootstrap_fetch.safe_name(name)
        self.assertEqual(bootstrap_fetch.safe_name('root/lib/a',1),'lib/a')

    def test_fresh_layout_makes_missing_parent_first(self):
        plan=bootstrap_fetch.initial_layout('/new-work/provision/provisioned')
        self.assertEqual(plan['parent'],'/new-work/provision')
        self.assertTrue(plan['mkdir_parent_first'])
        fake_existing={'/new-work'}
        # Pure mock of the planned parent creation, without filesystem writes.
        fake_existing.add(plan['parent']);self.assertIn(plan['parent'],fake_existing)
        fake_existing.add(plan['root']);self.assertIn(plan['root'],fake_existing)
        src=(ROOT/'launch/bootstrap_fetch.py').read_text()
        body=src[src.index('def _bootstrap_psutil_work'):src.index('\ndef bootstrap_psutil(')]
        self.assertLess(body.index('root.parent.mkdir('),body.index('shutil.disk_usage(root.parent)'))

    def test_patch_exact_context_and_locked_positions(self):
        patch='--- a/file\n+++ b/file\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n'
        self.assertEqual(provisioning.apply_unified({'file':b'a\nb\n'},patch),{'file':b'a\nc\n'})
        with self.assertRaises(ValueError):provisioning.apply_unified({'file':b'a\nwrong\n'},patch)
        shifted=b'prefix\na\nb\n'
        with self.assertRaises(ValueError):provisioning.apply_unified({'file':shifted},patch)
        self.assertEqual(provisioning.apply_unified({'file':shifted},patch,{'file':[1]})['file'],b'prefix\na\nc\n')
        self.assertEqual(len(self.inputs['patches']),8)
        for op in [*self.inputs['patches'],self.inputs['activation_overlay_patch']]:
            self.assertEqual(set(op['hunk_positions']),{r['name'] for r in op['files']})
            self.assertTrue(all(len(r['after_sha256'])==64 for r in op['files']))

    def test_fixed_complete_command_inventory(self):
        commands=self.inputs['commands'];self.assertEqual(len(commands),11)
        expected={'configure-offline','generate-onnx-headers','activation-archived-ir','activation-control','activation-control-ir','activation-candidate','activation-candidate-ir','dispatch-archived-ir','dispatch-assemble','dispatch-roundtrip-ir','baseline-index'}
        self.assertEqual({r['id'] for r in commands},expected)
        configure=next(r for r in commands if r['id']=='configure-offline')
        self.assertIn('-DFETCHCONTENT_FULLY_DISCONNECTED=ON',configure['argv'])
        self.assertIn('-DCMAKE_CROSSCOMPILING_EMULATOR=',configure['argv'])
        self.assertIn('-DCMAKE_DISABLE_FIND_PACKAGE_Git=ON',configure['argv'])
        self.assertEqual(sum(x.startswith('-DFETCHCONTENT_SOURCE_DIR_') for x in configure['argv']),16)
        for row in commands:
            self.assertGreater(row['wall_seconds'],0)
            self.assertNotIn('/workspace/',' '.join(row['argv']))
            self.assertNotIn('make_archive_variants.py',' '.join(row['argv']))

    def test_runtime_entries_block_before_network_process_or_writes(self):
        calls=[(bootstrap_fetch.bootstrap_psutil,({},0)),(bootstrap_fetch.acquire,({},None,0)),
               (bootstrap_fetch.extract,({},None,None,0)),(provisioning.run,({},0)),
               (provisioning._command,({}, {}, {}, 0, []))]
        for function,args in calls:
            with self.assertRaises(SourceOnlyBlocked):function(*args)

    def test_initial_hard_bound_and_journals_are_present(self):
        src=(ROOT/'launch/bootstrap_fetch.py').read_text()
        self.assertIn('end=min(deadline,started+150)',src)
        self.assertIn('work_end=min(deadline-30,started+120)',src)
        self.assertIn('signal.setitimer(signal.ITIMER_REAL',src)
        self.assertIn('bootstrap-transfers.private.jsonl',src)
        self.assertIn("'production_supervised':False",src)
        self.assertNotIn('import subprocess',src)
        self.assertIn('ProxyHandler({})',src)

    def test_helper_lock_and_source_proof_reference_are_current(self):
        self.assertEqual(hashlib.sha256((ROOT/'launch/bootstrap-helper-lock.json').read_bytes()).hexdigest(),self.inputs['helper_lock_sha256'])
        raw=(ROOT/'launch/vendor-reference/vendor/source/source_proof.py.txt').read_bytes()
        self.assertEqual({'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)},self.inputs['source_proof_reference'])

if __name__=='__main__':unittest.main()
