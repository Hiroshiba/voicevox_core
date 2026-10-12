import hashlib
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class HelperSourceLocks(unittest.TestCase):
    def setUp(self):
        self.lock=json.loads((ROOT/'launch/bootstrap-helper-lock.json').read_text())

    def test_all_donor_references_are_exact_inert_text(self):
        self.assertEqual(len(self.lock['donor_source_files']),13)
        for row in self.lock['donor_source_files']:
            path=ROOT/row['packaged_reference']
            self.assertTrue(path.name.endswith('.txt'))
            raw=path.read_bytes();self.assertEqual(len(raw),row['bytes'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(),row['sha256'])

    def test_runtime_package_closed_rosters_and_versions(self):
        groups=self.lock['runtime_helper_packages']
        self.assertEqual({k:v['version'] for k,v in groups.items()},
                         {'playwright':'1.63.0','pyee':'13.0.1','greenlet':'3.5.6','psutil':'7.2.2','typing_extensions':'4.16.0'})
        for row in groups.values():
            files=row['package_files'];names=[x['name'] for x in files]
            self.assertEqual(names,sorted(set(names)))
            self.assertFalse(any('__pycache__' in n or n.endswith('.pyc') for n in names))
            digest=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            self.assertEqual(digest,row['package_tree_sha256'])

    def test_chrome_not_inferred_from_helper_metadata(self):
        self.assertTrue(self.lock['no_import_install_execution'])
        self.assertIn('no Chrome bytes',self.lock['scope'])
        rows=self.lock['runtime_helper_packages']['playwright']['package_files']
        node=next(x for x in rows if x['name']=='driver/node')
        self.assertEqual(node['sha256'],'7fde7b8afa198da66257f42ee2001d874c7355631e6d1579a5fb5ef1f246df4c')
        self.assertNotIn('chrome_executable_sha256',self.lock)
