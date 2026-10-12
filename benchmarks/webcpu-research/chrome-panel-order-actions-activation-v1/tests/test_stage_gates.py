"""Pure synthetic scalar receipts only; no browser, process, network or tensors."""
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from launch.source_only import SourceOnlyBlocked
from tools import build_driver as build
from tools import transition as gate
from tools import program
from tools.protocol import expected_schedule, REGION_BYTES, REGION_ALIGNMENT, PLAN_SHA256


def h(value):return hashlib.sha256(str(value).encode()).hexdigest()


def assets():
    rows=[]
    for arm in ('R','A','B','Q','P'):
        for extension,role in (('js','module_js'),('wasm','module_wasm')):
            name=f'{arm}/screen.{extension}'; rows.append({'name':name,'bytes':1,'sha256':h(name),'role':role,'arm':arm})
    for name,role in (('controller.js','controller'),('worker.js','operator_worker'),('index.html','html')):
        rows.append({'name':name,'bytes':1,'sha256':h(name),'role':role,'arm':None})
    rows.sort(key=lambda x:x['name'])
    return {'schema':'panel-assets-v1','admission':'independently_reviewed_complete_assets','logical_names':[r['name'] for r in rows],'assets':rows}


def expected():
    bindings={key:h(key) for key in gate.BINDING_HASHES};bindings.update(plan_sha256=PLAN_SHA256,source_commit='a'*40,job_id='synthetic-job',host_id='synthetic-host')
    identity={'collection_method':'proc_cpuinfo_chrome_bytes_cdp_v1','chrome_product':'HeadlessChrome/153.0.8010.12','v8_version':'15.3.76.4','chrome_distribution_sha256':h('distribution'),'chrome_executable_sha256':h('exe'),'chrome_support_tree_sha256':h('supporttree'),'browser_installation_receipt_sha256':h('installreceipt'),'accepted_flags':['--js-flags=--no-wasm-revectorize'],'launch_argv_policy':['chrome','--js-flags=--no-wasm-revectorize','--user-data-dir=<OWNED_PROFILE_DIR>','--remote-debugging-port=<LOOPBACK_PORT>'],'os':'Linux','architecture':'x86_64','cpu_vendor':'GenuineIntel','cpu_family':'6','cpu_model':'1','cpu_stepping':'1','isa_sha256':h('isa'),'boot_id':'synthetic-boot','job_id':'synthetic-job'}
    return {'schema':'panel-stage-expectations-v1','bindings':bindings,'identity':identity,'asset_manifest':assets(),'owned_temp_roots':{'qualification':'/tmp/synthetic-panel/qualification/tmp','timing':'/tmp/synthetic-panel/timing/tmp'}}


def browser_policy():
    return {'schema':'panel-browser-acquisition-policy-v1','chrome_product':'HeadlessChrome/153.0.8010.12','v8_version':'15.3.76.4','platform':'linux64','source_url':build.BROWSER_URL,'allowed_redirect_urls':[],'method':'HEAD_THEN_ONE_GET','helper_sha256':h('helper'),'source_metadata_sha256':h('metadata'),'playwright_core_bundle_sha256':h('corebundle'),'bootstrap_helper_lock_sha256':h('bootstrap_helper_lock_sha256'),'executable_name':'chrome-headless-shell-linux64/chrome-headless-shell','limits':copy.deepcopy(build.BROWSER_LIMITS),'installation_scope':'complete_official_distribution_regular_files_no_symlinks','identity_scope':'acquired_once_and_pinned_within_ci_job'}


def browser_receipt(policy=None):
    policy=browser_policy() if policy is None else policy
    header={'url':policy['source_url'],'status':200,'content_length':3,'content_type':'application/zip','etag':'"synthetic-etag"'}
    manifest={'schema':'panel-browser-installation-v1','executable_name':policy['executable_name'],'files':[{'name':policy['executable_name'],'bytes':64,'sha256':h('synthetic ELF'),'mode':0o755},{'name':'chrome-headless-shell-linux64/resources.pak','bytes':5,'sha256':h('pak'),'mode':0o644}]}
    return {'schema':'panel-browser-provisioning-receipt-v1','status':'complete','identity_scope':'acquired_once_and_pinned_within_ci_job','source_policy_sha256':gate.digest(policy),'helper_sha256':policy['helper_sha256'],'acquisitions':1,'head_requests':1,'get_requests':1,'redirects':[],'head':header,'get':copy.deepcopy(header),'archive':{'name':'browser-distribution.zip','bytes':3,'sha256':h('zip')},'installation':manifest,'installation_sha256':gate.digest(manifest),'executable_format':{'format':'ELF','bits':64,'endianness':'little','machine':62},'browser_launches':0,'operator_calls':0,'first_failure':None}


def cleanup():return {'confirmed':True,'remaining_processes':0,'unresolved_owners':[],'unknown_generations':[],'wall_seconds':1.0}


def snapshot():
    token=h('supervisor');return {'generations':{token:token},'processes':{token:{'cpu_seconds':1.0,'rss_bytes':1000,'major_faults':0,'swap_bytes':0}},'host':{'available_bytes':2*1024**3,'swap_in_bytes':0,'swap_out_bytes':0},'runtime_artifact_bytes':1,'public_receipt_bytes':1}


def synthetic_receipt(stage='qualification',exp=None,carried=None):
    exp=expected() if exp is None else exp
    phases={str(d):[{'region':i,'mod64':0,'mod4096':i*64} for i in range(7)] for d in (1,3,5)}
    fixtures=copy.deepcopy(carried or {})
    records=[];roots=[];schedule=[c for c in expected_schedule() if c['stage']==stage]
    for c in schedule:
        commands=[{'command':name,'attempted':True,'acknowledged':True,'dispatch_ms':i*3.0,'receipt_ms':i*3.0+1,**({'native_result':0} if name in ('run','delete') else {})} for i,name in enumerate(('prepare','run','verify','delete'))]
        duration=10.0 if c['retained'] else None
        fixture_hash=[h((c['dilation'],c['fixture_id'],x)) for x in range(3)];output_hash=h((c['dilation'],c['fixture_id'],'output'));packed_hash=h((c['dilation'],c['fixture_id'],'packed'))
        fixtures[f"{c['dilation']}:{c['fixture_id']}"]={'fixture_sha256':fixture_hash,'output_sha256':output_hash,'packed_sha256':packed_hash}
        code={1:1,2:3,3:4,4:2,5:2,6:2,7:2,8:2,9:2,10:2,11:2}.get(c['fixture_id'])
        witness={'designated':512,'checked':512,'passed':True,'fixture_id':c['fixture_id'],'expectation_code':code,'full_class_words':7880704 if c['fixture_id'] <= 3 else 0} if c['fixture_id'] else {'designated':0,'checked':0,'passed':True}
        evidence={'geometry':{'descriptor':[c['dilation'],61568,128,4,8,11,512,176,4 if c['arm'] in ('R','A') else 32,128,2,61568,128],'regions':[{'region':i,'bytes':REGION_BYTES[i],'minimum_alignment':REGION_ALIGNMENT[i]} for i in range(7)],'phases':phases[str(c['dilation'])],'indirection_words':677248,'actual_zero_sentinel':True,'modulo_uint32_offset':True,'actual_selector':True,'live_descriptor':True},'fixture_sha256':fixture_hash,'run_ack':{'completed':True,'native_result':0,'elapsed_ms':duration,'heap_before_bytes':134217728,'heap_after_bytes':134217728,'sequence':3},'output':{'words':7880704,'reference_capture':c['reference'],'compared_words':c['comparison_words'],'finite_required':c['fixture_id']==0,'exact_uint32':not c['reference'],'output_sha256':output_hash},'readonly':{'regions':6,'bytes':35675216,'differences':0},'packed':{'bytes':721408,'differences':0,'packed_sha256':packed_hash},'witnesses':witness,'coverage':{'enabled':c['coverage'],'participants':[{'label':0,'identity_sha256':h(c['arm']+'caller')},{'label':1,'identity_sha256':h(c['arm']+'backing')}] if c['coverage'] else [],'callbacks':(15392 if c['arm'] in ('R','A') else 1924) if c['coverage'] else 0,'exact_disjoint':c['coverage']},'canaries':{'caller_regions':4,'prefix_bytes':256,'suffix_bytes':256,'before_bytes':2048,'after_bytes':2048,'compared_bytes':4096,'differences':0,'hidden_allocator_padding_covered':False},'stack':{'before':True,'after':True}}
        records.append({'call':c,'commands':commands,'checks':dict.fromkeys(gate.CHECKS,True),'evidence':evidence,'duration_ms':duration,'worker_failure':None,'resource_windows':[{'command':name,'retained':c['retained'] and name=='run','before':snapshot(),'after':snapshot(),'attempted':True,'acknowledged':True} for name in ('prepare','run','verify','delete')]})
    actual_argv=['chrome','--js-flags=--no-wasm-revectorize','--user-data-dir='+exp['owned_temp_roots'][stage]+'/playwright_chromiumdev_profile-synthetic','--remote-debugging-port=0']
    calibration={'quantum_ms':.001,'positive_samples':1000,'reads':1001,'empty_export_samples':1000,'empty_export_p99_ms':0,'p99_resolution_censored':True,'minimum_primary_ms':.2,'subtracted':False}
    for root in ([0] if stage=='qualification' else range(1,11)):
        roots.append({'root':root,'initializations':[{'ready':True,'arm':arm,'stage':stage,'sequence':1,'heap_bytes':134217728,'backing_workers':1,'pool_participants':2,'hardware_concurrency':2,'calibration':None if stage=='qualification' else copy.deepcopy(calibration),'distinct_backing_verified':True} for arm in ('R','A','B','Q','P')],'shutdown':{'shutdown':True,'calls':330 if stage=='qualification' else 60,'phase_records':phases,'fixture_identities':copy.deepcopy(fixtures),'modules':[{'arm':arm,'shutdown':True,'pool_destroy_returned':True,'library_deinitialized':True,'backing_termination_requested':True,'process_cleanup_required':True,'sequence':100} for arm in ('R','A','B','Q','P')],'process_cleanup_required':True,'buffers_erased':True},'cleanup':cleanup(),'identity':exp['identity'],'actual_launch_argv':actual_argv,'launch_argv_sha256':gate.digest(actual_argv)})
    labels=['before_probe','after_probe']
    for root in ([0] if stage=='qualification' else range(1,11)):labels += [f'before_root_{root}',f'after_root_{root}']
    labels += ['terminal']
    resources={'wall_seconds':100,'tree_cpu_seconds':50,'peak_rss_bytes':2000000000,'min_available_bytes':2*1024**3,'peak_artifact_bytes':100,'public_receipt_bytes':100,'observation_count':len(schedule)*8+30,'missing_observations':0,'ownership_method':'subreaper_pidfd_all_thread_children','all_thread_children_verified':True,'generation_matching':True,'boundaries':[{'label':label,'host':snapshot()['host'],'at_monotonic':float(i)} for i,label in enumerate(labels)],'sampled_not_kernel_aggregate_quota':True,'outer_controller_excluded':True,'outer_supervision':{'startup_gate_released':True,'samples':[{'monotonic_seconds':1.0,'rss_bytes':2000,'artifact_bytes':100,'host_available_bytes':2*1024**3,'tree_cpu_seconds':50,'generations':3,'target_swap_growth_bytes':0,'host_swap_in_delta_bytes':0,'host_swap_out_delta_bytes':0}],'cleanup':{'confirmed':True,'remaining_processes':0,'returncode':0,'failure':None,'cleanup_failure':None,'timed_out':False}}}
    return {'schema':'panel-'+stage+'-receipt-v1','status':'complete','stage':stage,'bindings':exp['bindings'],'identity':exp['identity'],'asset_manifest':exp['asset_manifest'],'final_asset_manifest':copy.deepcopy(exp['asset_manifest']),'compile_count':1,'phase_records':phases,'fixture_identities':fixtures,'calls':records,'root_receipts':roots,'flag_probe':{'operator_calls':0,'wall_seconds':1.0,'accepted':True,'identity':exp['identity'],'cleanup':cleanup(),'actual_launch_argv':actual_argv,'launch_argv_sha256':gate.digest(actual_argv)},'resources':resources,'first_failure':None,'timing_calls':0 if stage=='qualification' else 600}


class CompleteReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exp=expected();cls.receipt=synthetic_receipt(exp=cls.exp)

    def test_complete_qualification_and_fresh_timing(self):
        transition=gate.verify_qualification(self.receipt,self.exp)
        self.assertFalse(transition['execution_authority']);self.assertEqual(transition['comparison_words'],2340569088)
        timing=synthetic_receipt('timing',self.exp,transition['carried_fixture_identities'])
        validated=gate.validate_timing(timing,self.exp,transition)
        self.assertEqual(validated['comparison_words'],4492001280)

    def test_each_detailed_obligation_rejects_mutation(self):
        mutations=[lambda r:r['calls'].pop(),lambda r:r['calls'][0]['commands'][1].update(acknowledged=False),lambda r:r['calls'][0]['evidence']['readonly'].update(bytes=1),lambda r:r['calls'][0]['evidence']['witnesses'].update(checked=511),lambda r:r['calls'][0]['evidence']['coverage']['participants'].pop(),lambda r:r['calls'][0]['evidence']['geometry'].update(actual_selector=False),lambda r:r['calls'][0]['evidence']['geometry'].update(phases=[{'region':i,'mod64':0,'mod4096':(i+1)*64} for i in range(7)]),lambda r:r['root_receipts'][0]['cleanup'].update(confirmed=False),lambda r:r['resources']['outer_supervision'].update(startup_gate_released=False),lambda r:r.update(compile_count=2),lambda r:r['calls'][0]['evidence']['canaries'].update(after_bytes=0),lambda r:r['calls'][0]['resource_windows'][0]['before']['processes'].clear(),lambda r:r['calls'][0].update(duration_ms=1),lambda r:r.update(unknown='extra'),lambda r:r['identity'].update(cpu_model='different'),lambda r:r['final_asset_manifest']['assets'][0].update(sha256='f'*64)]
        for i,mutation in enumerate(mutations):
            with self.subTest(i=i):
                changed=copy.deepcopy(self.receipt);mutation(changed)
                with self.assertRaises((ValueError,TypeError)):gate.verify_qualification(changed,self.exp)

    def test_missing_field_unknown_nan_and_boolean_counts(self):
        for field in self.receipt:
            changed=copy.deepcopy(self.receipt);del changed[field]
            with self.subTest(field=field),self.assertRaises(ValueError):gate.verify_qualification(changed,self.exp)
        changed=copy.deepcopy(self.receipt);changed['compile_count']=True
        with self.assertRaises(ValueError):gate.verify_qualification(changed,self.exp)
        changed=copy.deepcopy(self.receipt);changed['resources']['tree_cpu_seconds']=float('nan')
        with self.assertRaises(ValueError):gate.verify_qualification(changed,self.exp)

    def test_boolean_and_float_counts_rejected(self):
        for scalar in (True,1.0):
            changed=copy.deepcopy(self.receipt);changed['root_receipts'][0]['initializations'][0]['backing_workers']=scalar
            with self.assertRaises(ValueError):gate.verify_qualification(changed,self.exp)
        transition=gate.verify_qualification(self.receipt,self.exp)
        timing=synthetic_receipt('timing',self.exp,transition['carried_fixture_identities']);timing['compile_count']=True
        with self.assertRaises(ValueError):gate.validate_timing(timing,self.exp,transition)
        for scalar in (False,0.0):
            changed=copy.deepcopy(self.receipt);changed['resources']['outer_supervision']['cleanup']['returncode']=scalar
            with self.assertRaises(ValueError):gate.verify_qualification(changed,self.exp)

    def test_every_outer_swap_sample_is_checked(self):
        changed=copy.deepcopy(self.receipt)
        sample=copy.deepcopy(changed['resources']['outer_supervision']['samples'][0])
        changed['resources']['outer_supervision']['samples'].insert(0,sample)
        sample['target_swap_growth_bytes']=4096
        with self.assertRaises(ValueError):gate.verify_qualification(changed,self.exp)

    def test_source_bytes_and_release_are_checked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tools').mkdir();(root/'tools/program.py').write_text('source only')
            manifest={'schema':'chrome-panel-source-seal-v1','files':{'tools/program.py':{'bytes':11,'sha256':h('source only')}}};manifest['source_tree_canonical_sha256']=gate.digest(manifest['files'])
            bindings={'source_commit':'a'*40,'source_tree_sha256':gate.digest(manifest['files'])}
            program.verify_source_release(root,manifest,bindings)
            (root/'tools/program.py').write_text('changed')
            with self.assertRaises(SourceOnlyBlocked):program.verify_source_release(root,manifest,bindings)
            (root/'tools/program.py').write_text('source only');(root/'tools/extra.py').write_text('extra')
            with self.assertRaises(SourceOnlyBlocked):program.verify_source_release(root,manifest,bindings)

    def test_runtime_entrypoints_require_explicit_admission(self):
        import ast
        # Parse only. Successful runtime entrypoints are never invoked by tests.
        for path,function in (('tools/program.py','run'),('launch/phase_process.py','main'),('launch/compiler_process.py','main'),('launch/browser_acquisition.py','main')):
            tree=ast.parse((ROOT/path).read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==function)
            self.assertIn('require_execution_admission',ast.unparse(node.body[0]))
        from launch import source_only
        self.assertIsNone(source_only._STATE)
        with self.assertRaises(SourceOnlyBlocked):source_only.require_execution_admission('pure_negative_gate')

    def test_portable_recipe_is_source_only(self):
        recipe=json.loads((ROOT/'launch/build-recipe.json').read_text())
        self.assertFalse(build.validate_recipe(recipe)['execution_admitted'])
        self.assertEqual(sum(c['id'].startswith('link_') for c in recipe['commands']),5)
        self.assertTrue(all('_screen_alias_probe_ptr' in ' '.join(c['argv']) for c in recipe['commands'] if c['id'].startswith('link_')))
        build.validate_asset_manifest(json.loads((ROOT/'launch/expected-assets.json').read_text()))

    def test_exact_asset_mismatch_never_normalizes(self):
        base=assets();build.compare_asset_manifests(base,copy.deepcopy(base))
        for key,value in (('bytes',2),('sha256','a'*64),('name','renamed.js')):
            changed=copy.deepcopy(base);changed['assets'][0][key]=value
            with self.assertRaises(ValueError):build.compare_asset_manifests(base,changed)

    def test_no_budget_borrowing_or_repeat(self):
        ledger=program.DeadlineLedger(10);self.assertEqual(ledger.begin('provision',10),2710)
        with self.assertRaises(ValueError):ledger.end('provision',2711)
        with self.assertRaises(ValueError):ledger.begin('compile',2711)
        ledger=program.DeadlineLedger(0);ledger.begin('provision',0);ledger.end('provision',1)
        with self.assertRaises(ValueError):ledger.begin('timing',1)

    def test_host_is_observed_once_not_preselected(self):
        e=expected();template={'schema':'panel-request-expectations-v1','bindings':{k:v for k,v in e['bindings'].items() if k not in ('request_sha256','job_id','host_id')},'identity_policy':{k:v for k,v in e['identity'].items() if k in program.IDENTITY_POLICY_FIELDS},'asset_manifest':e['asset_manifest'],'browser_acquisition_policy':browser_policy()}
        template['identity_policy']['launch_argv_policy'][0]='<ADMITTED_CHROME_EXECUTABLE>'
        host={k:v for k,v in e['identity'].items() if k in program.HOST_FIELDS}
        bound=program.bind_allocated_host(template,host,h('request'),'/tmp/new-work',browser_receipt(),'/tmp/new-work/provisioned/browser/installation')
        self.assertEqual(bound['identity']['cpu_model'],host['cpu_model'])
        del host['boot_id']
        with self.assertRaises(ValueError):program.bind_allocated_host(template,host,h('request'),'/tmp/new-work',browser_receipt(),'/tmp/new-work/provisioned/browser/installation')
        request={'schema':'panel-conditional-request-v1','issuer':'root','authorization':'separately_reviewed_hash_bound_actions_conditional_GO','expected':template,'source_release':'a'*40,'budgets':dict(program.PHASES),'scope':{'qualification_calls':330,'conditional_timing_calls':600,'same_job_host':True,'build_attempts':1,'local_browser':False,'retry':False},'provisioning_contract_sha256':h('p'),'build_input_lock_sha256':h('i')}
        program.validate_request(request);request['expected']['identity_policy']['cpu_model']='arbitrary'
        with self.assertRaises(ValueError):program.validate_request(request)

    def test_late_bound_browser_receipt_is_closed_and_once_only(self):
        policy=browser_policy();receipt=browser_receipt(policy)
        identity=build.validate_browser_provisioning_receipt(receipt,policy)
        self.assertEqual(identity['chrome_distribution_sha256'],receipt['archive']['sha256'])
        self.assertEqual(identity['browser_installation_receipt_sha256'],gate.digest(receipt))
        mutations=[lambda r:r.update(acquisitions=2),lambda r:r['get'].update(etag='changed'),lambda r:r['head'].update(content_type='text/html'),lambda r:r['archive'].update(bytes=2),lambda r:r.update(browser_launches=1),lambda r:r['installation']['files'].pop(),lambda r:r['installation']['files'][0].update(name='../escape'),lambda r:r.update(helper_sha256=h('different'))]
        for mutation in mutations:
            changed=copy.deepcopy(receipt);mutation(changed)
            with self.assertRaises(ValueError):build.validate_browser_provisioning_receipt(changed,policy)

    def test_prespecified_redirect_chains_are_separate_per_method(self):
        policy=browser_policy();target='https://storage.googleapis.com/chrome-for-testing-public/153.0.8010.12/linux64/chrome-headless-shell-linux64.zip';policy['allowed_redirect_urls']=[target]
        receipt=browser_receipt(policy)
        receipt['head']['url']=receipt['get']['url']=target
        receipt['redirects']=[{'from_url':policy['source_url'],'to_url':target,'status':307,'method':method} for method in ('HEAD','GET')]
        build.validate_browser_provisioning_receipt(receipt,policy)
        receipt['redirects'][1]['to_url']='https://example.com/substitute'
        with self.assertRaises(ValueError):build.validate_browser_provisioning_receipt(receipt,policy)

    def test_retained_browser_tree_rehash_rejects_mutation_and_extra(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);manifest=browser_receipt()['installation']
            for row in manifest['files']:
                path=root/row['name'];path.parent.mkdir(parents=True,exist_ok=True);data=b'x'*row['bytes'];path.write_bytes(data);os.chmod(path,row['mode']);row['sha256']=hashlib.sha256(data).hexdigest()
            build.verify_browser_installation(root,manifest)
            target=root/manifest['files'][0]['name'];target.write_bytes(b'z'*64)
            with self.assertRaises(ValueError):build.verify_browser_installation(root,manifest)
            target.write_bytes(b'x'*64);(root/'extra').write_text('extra')
            with self.assertRaises(ValueError):build.verify_browser_installation(root,manifest)

    def test_provisioning_checkout_time_cannot_borrow(self):
        ledger=program.DeadlineLedger(100)
        self.assertEqual(ledger.begin('provision',200),2800)
        self.assertEqual(ledger.active['started'],100)
        late=program.DeadlineLedger(100)
        with self.assertRaises(ValueError):late.begin('provision',2801)

    def test_http_metadata_and_zip_methods_are_bounded(self):
        from email.message import Message
        from launch.browser_acquisition import response_metadata,validate_zip_method
        class Response:
            status=200
            def geturl(self):return build.BROWSER_URL
        response=Response();response.headers=Message()
        for name,value in (('Content-Length','3'),('Content-Type','application/zip'),('ETag','synthetic')):response.headers[name]=value
        self.assertEqual(response_metadata(response,browser_policy())['content_length'],3)
        response.headers['Content-Length']='3'
        with self.assertRaises(ValueError):response_metadata(response,browser_policy())
        for method in (0,8):validate_zip_method(method)
        for method in (True,8.0,12,14,93):
            with self.assertRaises(ValueError):validate_zip_method(method)

    def test_import_routes_are_verified_without_importing_packages(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);package=root/'psutil';package.mkdir()
            (package/'__init__.py').write_text("raise AssertionError('must never be imported')")
            old=sys.path[:];existing=sys.modules.pop('psutil',None)
            try:
                sys.path.insert(0,str(root))
                build.verify_helper_import_routes({'psutil':str(package)},ownership_only=True)
                self.assertNotIn('psutil',sys.modules)
                with self.assertRaises(ValueError):build.verify_helper_import_routes({'psutil':str(root/'elsewhere')},ownership_only=True)
            finally:
                sys.path[:]=old
                if existing is not None:sys.modules['psutil']=existing

    def test_context_requires_fresh_owned_provision_and_build_paths(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory)/'attempt';work.mkdir();provision=work/'provision'/'provisioned'
            context={'provision_root':str(provision),'roots':{'SOURCE':str(ROOT.resolve()),'PYTHON':str(Path(sys.executable).absolute()),'SDK':str(provision/'sdk'),'DEPS':str(provision/'deps'),'BASELINE':str(provision/'baseline'),'OUT':str(work/'compile'/'built')}}
            context['runtime_helper_package_roots']={name:str(provision/'deps'/'runtime'/name) for name in ('playwright','pyee','greenlet','psutil','typing_extensions')}
            program.validate_context_paths(context,work,ROOT,sys.executable)
            for token in ('SDK','DEPS','BASELINE','OUT'):
                changed=copy.deepcopy(context);changed['roots'][token]=str(Path(directory)/'historical-cache')
                with self.assertRaises(ValueError):program.validate_context_paths(changed,work,ROOT,sys.executable)
            work.rmdir()
            with self.assertRaises(ValueError):program.validate_context_paths(context,work,ROOT,sys.executable)

    def test_shipped_browser_policy_binds_real_source_hashes(self):
        policy=json.loads((ROOT/'launch/browser-acquisition-policy.json').read_text())
        build.validate_browser_policy(policy)
        self.assertEqual(policy['helper_sha256'],hashlib.sha256((ROOT/'launch/browser_acquisition.py').read_bytes()).hexdigest())
        self.assertEqual(policy['bootstrap_helper_lock_sha256'],hashlib.sha256((ROOT/'launch/bootstrap-helper-lock.json').read_bytes()).hexdigest())
        self.assertEqual(len(policy['allowed_redirect_urls']),1)

    def test_isolated_helper_paths_register_only_after_complete_byte_checks(self):
        import tempfile
        names=('playwright','pyee','greenlet','psutil','typing_extensions')
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);roots={};packages={}
            for name in names:
                data=b"raise AssertionError('must never be imported')\n"
                if name=='typing_extensions':
                    root=parent/(name+'.py');root.write_bytes(data);filename=root.name
                else:
                    root=parent/name;root.mkdir();(root/'__init__.py').write_bytes(data);filename='__init__.py'
                roots[name]=str(root)
                packages[name]={'package_files':[{'name':filename,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}
            lock={'runtime_helper_packages':packages};old=sys.path[:];saved={name:sys.modules.pop(name) for name in names if name in sys.modules}
            try:
                sys.path[:]=[]
                build.configure_verified_helper_imports(lock,roots)
                self.assertTrue(all(name not in sys.modules for name in names))
                sys.path[:]=[];(parent/'unadmitted.py').write_text('unexpected')
                with self.assertRaises(ValueError):build.configure_verified_helper_imports(lock,roots)
            finally:
                sys.path[:]=old;sys.modules.update(saved)

    def test_ephemeral_argv_only_declared_fields(self):
        e=expected();r=self.receipt['root_receipts'][0]
        self.assertEqual(gate.validate_launch_argv(r['actual_launch_argv'],e['identity']['launch_argv_policy'],e['owned_temp_roots']['qualification']),r['launch_argv_sha256'])
        for bad in ('--js-flags=--no-wasm-revectorize --no-liftoff','--user-data-dir=/tmp/unowned'):
            argv=r['actual_launch_argv'].copy();argv[1 if bad.startswith('--js') else 2]=bad
            with self.assertRaises(ValueError):gate.validate_launch_argv(argv,e['identity']['launch_argv_policy'],e['owned_temp_roots']['qualification'])

if __name__=='__main__':unittest.main()
