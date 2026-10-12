"""Source/data-only admission and command-transport tests. Never opens authority."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from launch import source_only as admission
from tools import build_driver,program

def canonical(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':')).encode()).hexdigest()

class AdmissionTests(unittest.TestCase):
    def test_provision_command_attempt_acknowledgement_receipt(self):
        tree=ast.parse((ROOT/'launch/provisioning.py').read_text())
        command=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_command')
        initial=next(n.value for n in command.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='receipt' for t in n.targets))
        fields={k.value:v for k,v in zip(initial.keys,initial.values)}
        self.assertIs(ast.literal_eval(fields['attempted']),True)
        self.assertIs(ast.literal_eval(fields['acknowledged']),False)
        acknowledgements=[n for n in ast.walk(command) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Subscript) and ast.unparse(t)=="receipt['acknowledged']" for t in n.targets)]
        self.assertEqual(len(acknowledgements),1)
        ack=acknowledgements[0];self.assertIs(ast.literal_eval(ack.value),True)
        self.assertIn(ack,command.body)
        checks=[n for n in command.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and ast.unparse(n.value.func)=='_check']
        self.assertLess(checks[-1].lineno,ack.lineno)
        self.assertIsInstance(command.body[-1],ast.Return)
        self.assertIn("receipt['requires_outer_cleanup_acknowledgement'] = True",ast.unparse(command))

    def test_complete_document_binding_and_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'browser').mkdir();data=b'{}\n';(root/'browser/host-manifest.json').write_bytes(data)
            files={'browser/host-manifest.json':{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}
            source=canonical(files);manifest={'schema':'chrome-panel-source-seal-v1','files':files,'source_tree_canonical_sha256':source}
            review={'schema':'panel-actions-source-review-v1','status':'PASS','independent':True,'source_tree_sha256':source,'compiled_assets_review_sha256':'a'*64,'host_manifest_sha256':hashlib.sha256(data).hexdigest(),'reviewer':'independent','scope':'one_same_job_330_then_conditional_600'}
            request={'schema':'panel-conditional-request-v1','issuer':'root','authorization':'separately_reviewed_hash_bound_actions_conditional_GO','expected':{'bindings':{'source_review_sha256':'b'*64,'source_tree_sha256':source,'emitted_review_sha256':'a'*64}},'source_release':'c'*40,'budgets':dict(program.PHASES),'scope':{'qualification_calls':330,'conditional_timing_calls':600,'same_job_host':True,'build_attempts':1,'local_browser':False,'retry':False},'provisioning_contract_sha256':'d'*64,'build_input_lock_sha256':'e'*64}
            admission.validate_documents(request,review,manifest,root,'b'*64)
            for mutate in (lambda r:r.update(issuer='worker'),lambda r:r.update(authorization='env_override'),lambda r:r['budgets'].update(compile=601),lambda r:r['scope'].update(retry=True)):
                changed=copy.deepcopy(request);mutate(changed)
                with self.assertRaises(admission.SourceOnlyBlocked):admission.validate_documents(changed,review,manifest,root,'b'*64)
            (root/'browser/host-manifest.json').write_bytes(b'changed')
            with self.assertRaises(admission.SourceOnlyBlocked):admission.validate_documents(request,review,manifest,root,'b'*64)
        self.assertIsNone(admission._STATE)

    def test_exact_child_lease_transport_is_pure(self):
        state={'descriptor_sha256':'a'*64};script='/fixed/tools/process_guard.py';args=['--supervise','--control-fd','7','--cpu-limit','480','--','/fixed/python','-B','-I']
        lease={'schema':'panel-child-lease-v1','descriptor_sha256':'a'*64,'script':script,'args':args,'phase':'compile'}
        self.assertEqual(admission.validate_child_lease(state,lease,script,args),'compile')
        for key,value in (('script','/other'),('args',args+['appended']),('phase','transition'),('descriptor_sha256','b'*64)):
            changed=copy.deepcopy(lease);changed[key]=value
            with self.assertRaises(admission.SourceOnlyBlocked):admission.validate_child_lease(state,changed,script,args)
        self.assertIsNone(admission._STATE)

    def test_python_helpers_get_job_facts_compiler_spec_unchanged(self):
        compiler={'PATH':'','EM_FROZEN_CACHE':'1','HOME':'/owned/home'}
        job={k:'value-'+k for k in build_driver.ADMISSION_ENV_FIELDS}
        observed=build_driver.python_helper_environment(compiler,job)
        self.assertEqual({k:observed[k] for k in compiler},compiler)
        self.assertEqual(set(observed)-set(compiler),set(build_driver.ADMISSION_ENV_FIELDS))
        self.assertFalse(set(compiler)&set(build_driver.ADMISSION_ENV_FIELDS))
        del job['GITHUB_SHA']
        with self.assertRaises(ValueError):build_driver.python_helper_environment(compiler,job)

    def test_guards_and_phase_claims_are_source_bound(self):
        text=(ROOT/'launch/source_only.py').read_text()
        for term in ('panel-order-screen-attempt','parent_start_ticks','boot_id','event_commit',"'.consumed'",'GITHUB_RUN_ATTEMPT'):
            self.assertIn(term,text)
        self.assertIn("'--lease'",text)
        self.assertIn('sys.flags.isolated',text)
        self.assertIn('sys.dont_write_bytecode',text)
        child=ast.parse((ROOT/'launch/child_entry.py').read_text())
        node=next(n for n in child.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        source=ast.unparse(node)
        self.assertLess(source.index('consume_child_lease'),source.index('configure_verified_helper_imports'))
        self.assertLess(source.index('consume_child_lease'),source.index('runpy.run_path'))

    def test_child_rejects_preloaded_source_prefix_before_import(self):
        import types
        source=(ROOT/'launch/child_entry.py').read_text();tree=ast.parse(source)
        guard=next(n for n in tree.body if isinstance(n,ast.For) and '_name' in ast.unparse(n.target))
        first_launch=next(n for n in tree.body if isinstance(n,ast.ImportFrom) and n.module=='launch.source_only')
        self.assertLess(guard.lineno,first_launch.lineno)
        code=compile(ast.Module(body=[guard],type_ignores=[]),'pure_child_registry_guard','exec')
        foreign=types.ModuleType('launch.source_only');foreign.__file__='/unowned/launch/source_only.py'
        with self.assertRaisesRegex(RuntimeError,'source_package_already_shadowed'):
            exec(code,{'sys':types.SimpleNamespace(modules={'launch.source_only':foreign}),'Path':Path,'ROOT':ROOT})
        owned=types.ModuleType('launch.source_only');owned.__file__=str(ROOT/'launch/source_only.py')
        exec(code,{'sys':types.SimpleNamespace(modules={'launch.source_only':owned}),'Path':Path,'ROOT':ROOT})

    def test_verified_helpers_precede_unimported_ambient_but_reject_loaded_shadow(self):
        import sys
        import types
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);owned=base/'owned';ambient=base/'ambient';owned.mkdir();ambient.mkdir()
            data=b"raise AssertionError('no imports in source test')\n"
            for parent in (owned,ambient):
                (parent/'psutil').mkdir();(parent/'psutil/__init__.py').write_bytes(data)
            names=('playwright','pyee','greenlet','psutil','typing_extensions')
            roots={n:str(owned/(n+'.py' if n=='typing_extensions' else n)) for n in names}
            package={'package_files':[{'name':'__init__.py','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}
            lock={'runtime_helper_packages':{n:copy.deepcopy(package) for n in names}}
            old=sys.path[:];saved={n:sys.modules.pop(n) for n in tuple(sys.modules) if n=='psutil' or n.startswith('psutil.')}
            try:
                sys.path[:]=[str(ambient),*old]
                build_driver.configure_verified_helper_imports(lock,roots,ownership_only=True)
                self.assertEqual(sys.path[0],str(owned));self.assertNotIn('psutil',sys.modules)
                bad=types.ModuleType('psutil');bad.__file__=str(ambient/'psutil/__init__.py')
                with mock.patch.dict(sys.modules,{'psutil':bad}):
                    with self.assertRaises(ValueError):build_driver.configure_verified_helper_imports(lock,roots,ownership_only=True)
            finally:
                sys.path[:]=old;sys.modules.update(saved)

    def test_late_export_failure_revokes_committed_directory(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);public=work/'public-scalars';public.mkdir()
            (public/'receipt.json').write_text('{"status":"completed_pending_independent_terminal_audit"}')
            (public/'manifest.json').write_text('{"advancement_authorized":false}')
            try:
                with mock.patch('time.monotonic',return_value=11):
                    __import__('tools.transition',fromlist=['need']).need(__import__('time').monotonic()<=10,'finalization_receipt_deadline')
            except ValueError:
                program.revoke_public_export(work)
            self.assertFalse(public.exists());self.assertTrue((work/'.public-scalars.revoked/receipt.json').is_file())
            tree=ast.parse((ROOT/'tools/program.py').read_text())
            run=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run')
            handlers=[n for n in ast.walk(run) if isinstance(n,ast.ExceptHandler)]
            handler=next(n for n in handlers if any(isinstance(x,ast.Expr) and 'revoke_public_export' in ast.unparse(x) for x in n.body))
            self.assertIn('revoke_public_export',ast.unparse(handler.body[0]))

    def test_ten_compiled_assets_are_exact_old_admission(self):
        previous=json.loads((ROOT/'protocol/admissions/local-emitted-assets.json').read_text())
        current=json.loads((ROOT/'launch/expected-assets.json').read_text())
        self.assertEqual([r for r in previous['assets'] if r['arm']],[r for r in current['assets'] if r['arm']])
        self.assertEqual(len([r for r in current['assets'] if r['arm']]),10)
        hosts=json.loads((ROOT/'browser/host-manifest.json').read_text())
        for row in hosts['files']:
            actual=next(r for r in current['assets'] if r['name']==row['name'])
            self.assertEqual({k:actual[k] for k in row},row)

if __name__=='__main__':unittest.main()
