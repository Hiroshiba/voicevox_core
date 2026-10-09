import contextlib,io,json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ci_prepare import relay
class RelayTest(unittest.TestCase):
 def test_only_fixed_stage_record_exposed(self):
  row={'schema':'inplace-source-prep-stage-v1','phase':'link_original_core','status':'pass'};out=io.StringIO()
  with contextlib.redirect_stdout(out):relay('PRIVATE UNKNOWN TEXT\nSOURCE_PREP_STAGE '+json.dumps(row)+'\nSOURCE_VARIANT_READY private-path\n')
  self.assertEqual(out.getvalue(),'SOURCE_PREP_STAGE '+json.dumps(row,sort_keys=True)+'\n')
 def test_unknown_payload_rejected_without_output(self):
  for change in [{'private':[1,2,3]},{'phase':'unrecognized'},{'exception_class':'unrecognized'},{'schema':'wrong'}]:
   row={'schema':'inplace-source-prep-stage-v1','phase':'configure','status':'fail',**change};out=io.StringIO()
   with contextlib.redirect_stdout(out),self.assertRaises(ValueError):relay('SOURCE_PREP_STAGE '+json.dumps(row))
   self.assertEqual(out.getvalue(),'')
if __name__=='__main__':unittest.main()
