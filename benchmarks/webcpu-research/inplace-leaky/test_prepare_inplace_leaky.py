import unittest,sys
from pathlib import Path
import prepare_inplace_leaky as p
# Immutable original callback bytes, independent of developer workspace/CI path.
BODY=bytes.fromhex('03 02 7d 05 7f 02 7b 02 40 20 02 28 02 00 22 07 20 01 28 02 00 22 09 6b 22 02 41 00 4c 0d 00 20 00 28 02 08 22 01 20 09 41 02 74 22 05 6a 21 06 20 00 28 02 0c 22 08 20 05 6a 21 05 20 00 2a 02 10 21 04 41 00 21 00 20 02 41 04 49 20 08 20 01 6b 41 10 49 72 45 04 40 20 02 41 fc ff ff ff 07 71 21 00 20 04 fd 13 21 0b 41 00 21 01 03 40 20 05 20 01 41 02 74 22 08 6a 20 06 20 08 6a fd 00 02 00 22 0a 20 0b 20 0a fd e6 01 20 0a fd 0c 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 fd 46 fd 52 fd 0b 02 00 20 01 41 04 6a 22 01 20 00 47 0d 00 0b 20 00 20 02 46 0d 01 0b 20 07 20 00 41 7f 73 6a 20 02 41 01 71 04 40 20 05 20 00 41 02 74 22 07 6a 20 06 20 07 6a 2a 02 00 22 03 20 04 20 03 94 20 03 43 00 00 00 00 60 1b 38 02 00 20 00 41 01 72 21 00 0b 20 09 46 0d 00 03 40 20 05 20 00 41 02 74 22 01 6a 20 01 20 06 6a 2a 02 00 22 03 20 04 20 03 94 20 03 43 00 00 00 00 60 1b 38 02 00 20 05 20 01 41 04 6a 22 01 6a 20 01 20 06 6a 2a 02 00 22 03 20 04 20 03 94 20 03 43 00 00 00 00 60 1b 38 02 00 20 00 41 02 6a 22 00 20 02 47 0d 00 0b 0b 0b')
def name(s):b=s.encode();return p.enc(len(b))+b
def fixture(imports=1,slot=17,bodies=None,params=3,elementflags=0):
 bodies=bodies or [BODY,b'\0\x0b'];secs=[p.section(1,b'\1\x60'+bytes([params])+b'\x7f'*params+b'\0')]
 desc=[]
 for i in range(imports):desc.append(name('env')+name('f'+str(i))+b'\0\0')
 desc.append(name('env')+name('memory')+b'\2\0\1');secs.append(p.section(2,p.enc(len(desc))+b''.join(desc)))
 secs.extend([p.section(3,p.enc(len(bodies))+b'\0'*len(bodies)),p.section(4,b'\1\x70\0'+p.enc(slot+1))])
 offset=b'\x41'+bytes([slot])+b'\x0b' # tests keep signed positive values below64
 elem=bytes([elementflags])+(b'\0' if elementflags==2 else b'')+offset+(b'\0' if elementflags==2 else b'')+b'\1'+p.enc(imports)
 secs.extend([p.section(9,b'\1'+elem),p.section(10,p.enc(len(bodies))+b''.join(p.enc(len(b))+b for b in bodies))]);return b'\0asm\1\0\0\0'+b''.join(secs)
class Test(unittest.TestCase):
 def test_pin(self):self.assertEqual(p.sha(BODY),p.BODY_SHA)
 def test_relocation(self):
  for imports in (0,1,9):
   for slot in (0,17,61):
    for flags in (0,2):
     data=fixture(imports,slot,elementflags=flags);out,r=p.transform(data);self.assertEqual(len(out),len(data)+6);self.assertEqual(r['absolute_function_index'],imports);self.assertEqual(r['callback_table'],{'table':0,'slot':slot});self.assertEqual(r['noop_roundtrip_sha256'],p.sha(data))
 def test_duplicate(self):
  with self.assertRaisesRegex(ValueError,'exactly one'):p.transform(fixture(bodies=[BODY,BODY]))
 def test_changed_body(self):
  with self.assertRaisesRegex(ValueError,'exactly one'):p.transform(fixture(bodies=[BODY[:90]+b'\0'+BODY[91:]]))
 def test_wrong_abi(self):
  with self.assertRaisesRegex(ValueError,'ABI'):p.transform(fixture(params=2))
 def test_double_patch(self):
  out,_=p.transform(fixture())
  with self.assertRaisesRegex(ValueError,'exactly one'):p.transform(out)
 def test_debug_failclosed(self):
  with self.assertRaisesRegex(ValueError,'Debug'):p.transform(fixture()+p.section(0,name('.debug_line')+b'\0'))
 def test_leb_boundary(self):
  data=None
  for padding in range(15990,16050):
   trial=fixture(bodies=[BODY,b'\0'+b'\1'*padding+b'\x0b'])
   size=len(next(x['payload'] for x in p.sections(trial) if x['id']==10))
   if 16378<=size<16384:data=trial;break
  self.assertIsNotNone(data)
  with self.assertRaisesRegex(ValueError,'LEB boundary'):p.transform(data)
 def test_truncated(self):
  with self.assertRaises(ValueError):p.transform(fixture()[:-1])
if __name__=='__main__':unittest.main()
