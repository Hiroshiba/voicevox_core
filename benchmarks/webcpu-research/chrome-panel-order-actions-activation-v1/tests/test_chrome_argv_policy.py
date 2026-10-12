"""Pure source-derived launch policy; no browser, Node or JavaScript evaluation."""
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.transition import validate_launch_argv
from browser.browser import validate_launch_argv as browser_validate

class ChromeArgvPolicy(unittest.TestCase):
    def setUp(self):
        self.policy=json.loads((ROOT/'launch/chrome-launch-argv-policy.json').read_text())

    def test_pinned_helper_and_literal_defaults_from_inert_source(self):
        row=next(x for x in json.loads((ROOT/'launch/bootstrap-helper-lock.json').read_text())['runtime_helper_packages']['playwright']['package_files'] if x['name']=='driver/package/lib/coreBundle.js')
        self.assertEqual(self.policy['pinned_core_bundle'],row)
        source=(ROOT/'protocol/chrome-launch-argv-source.txt').read_text()
        feature_source=source.split('    disabledFeatures = [',1)[1].split('    ].filter(Boolean);',1)[0]
        features=[json.loads(m.group(1)) for m in re.finditer(r'^\s*("[^"\n]*")\s*,?\s*$',feature_source,re.M)]
        switch_source=source.split('    chromiumSwitches = (options) => [',1)[1].split('    ].filter(Boolean);',1)[0]
        switches=[]
        for line in switch_source.splitlines():
            line=line.strip()
            if not line or line.startswith('//'):continue
            if 'disabledFeatures.join' in line:switches.append('--disable-features='+','.join(features))
            elif 'PLAYWRIGHT_LEGACY_SCREENSHOT' in line:switches.append('--enable-features=CDPScreenshotNewSurface')
            elif 'options?.android' in line:switches.append('--disable-sync')
            else:
                token=re.fullmatch(r'("[^"\n]*"),?',line);self.assertIsNotNone(token);switches.append(json.loads(token.group(1)))
        tail=['--enable-unsafe-swiftshader','--headless','--hide-scrollbars','--mute-audio','--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4','--no-sandbox','--js-flags=--no-wasm-revectorize','--user-data-dir=<OWNED_PROFILE_DIR>','--remote-debugging-pipe','--no-startup-window']
        self.assertEqual(self.policy['identity_policy']['launch_argv_policy'],['<ADMITTED_CHROME_EXECUTABLE>']+switches+tail)
        self.assertEqual(len(self.policy['identity_policy']['launch_argv_policy']),47)
        self.assertIn('chromeArguments.push(...args)',source)
        self.assertIn('if (options.chromiumSandbox !== true)',source)
        self.assertTrue(self.policy['no_runtime_success_claim'])

    def test_both_validators_enforce_exact_source_policy_and_owned_profile(self):
        policy=copy.deepcopy(self.policy['identity_policy']['launch_argv_policy'])
        policy[0]='/synthetic/owned/installation/chrome-headless-shell-linux64/chrome-headless-shell'
        actual=[a.replace('<OWNED_PROFILE_DIR>','/synthetic/stage/tmp/playwright_chromiumdev_profile-Abc123') for a in policy]
        for validate in (validate_launch_argv,browser_validate):
            self.assertEqual(len(validate(actual,policy,'/synthetic/stage/tmp')),64)
            for mutation in (actual[:-1],actual+['--enable-wasm-revectorize'],[x.replace('--no-wasm-revectorize','--wasm-revectorize') for x in actual],[x.replace('/synthetic/stage/tmp/','/other/') for x in actual],['/other/chrome']+actual[1:]):
                with self.assertRaises(ValueError):validate(mutation,policy,'/synthetic/stage/tmp')

    def test_source_policy_is_bound_before_provision_and_historical_generator_is_disabled(self):
        source=(ROOT/'tools/program.py').read_text()
        self.assertIn("same(template['identity_policy'],load_json(source/'launch/chrome-launch-argv-policy.json')['identity_policy'],'source_launch_argv_policy_binding')",source)
        self.assertLess(source.index('verify_bound_source_facts(template,context)',source.index('def run(')),source.index("_phase('provision'",source.index('def run(')))
        self.assertIn('HISTORICAL_FOUR_PACKAGE_GENERATOR_DISABLED_FOR_ACTIVATION',(ROOT/'tools/lock_helper_sources.py').read_text())
