import tempfile,unittest,shutil
from pathlib import Path
from unittest.mock import patch
import unpack_notices as m
class Tests(unittest.TestCase):
 def test_exact_roundtrip_and_existing_output(self):
  with tempfile.TemporaryDirectory() as d:
   out=Path(d)/'plain';r=m.unpack(out)
   self.assertEqual(r['LICENSES.txt'],'7d6a46636017ab02c58ee49042318b061656e75a2097238e6b0e3e0459584599')
   self.assertEqual(r['NOTICE_INVENTORY.json'],'3b024a1569d461858e5729acd8c90618e3ab75556e7122db0bda84171ac34e7f')
   self.assertEqual({x.name for x in out.iterdir()},{'LICENSES.txt','NOTICE_INVENTORY.json'})
   with self.assertRaises(ValueError):m.unpack(out)
 def test_changed_encoded_bytes_rejected_before_output(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)/'root';root.mkdir();out=Path(d)/'plain'
   for name in ['notice_pins.json','LICENSES.txt.gz.b64','NOTICE_INVENTORY.json.gz.b64']:shutil.copyfile(m.ROOT/name,root/name)
   (root/'LICENSES.txt.gz.b64').write_bytes(b'changed')
   with patch.object(m,'ROOT',root):
    with self.assertRaises(ValueError):m.unpack(out)
   self.assertFalse(out.exists())
if __name__=='__main__':unittest.main()
