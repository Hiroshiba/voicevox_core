"""Fail closed on immutable same-fork artifact metadata; never print token/response."""
import json,os,sys,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'timing-release'))
from release_gate import load_contract
REPOSITORY='Hiroshiba/voicevox_core'
REPOSITORY_ID=617625583
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):raise ValueError('metadata_redirect_forbidden')

def validate(meta,binding):
 p=binding['producer'];r=meta.get('workflow_run',{})
 checks=[p['repository']==REPOSITORY,meta.get('id')==binding['artifact_id'],meta.get('name')=='webcpu-pristine-on-immutable-code',meta.get('expired') is False,
         meta.get('digest')=='sha256:'+binding['artifact_zip_sha256'],r.get('id')==p['run_id'],r.get('head_sha')==p['commit'],
         r.get('repository_id')==REPOSITORY_ID,r.get('head_repository_id')==REPOSITORY_ID]
 if not all(checks):raise ValueError('artifact_binding_mismatch')
 return True

def main():
 binding=load_contract()['code_transfer']
 if type(binding['artifact_id']) is not int or binding['artifact_id']<=0:raise ValueError('artifact_id')
 request=urllib.request.Request(f'https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{binding["artifact_id"]}',headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'})
 try:
  with urllib.request.build_opener(NoRedirect()).open(request,timeout=60) as response:
   if response.geturl()!=request.full_url:raise ValueError('unexpected_metadata_redirect')
   data=response.read(1024*1024)
   if len(data)>=1024*1024:raise ValueError('metadata_size')
  validate(json.loads(data),binding)
 except Exception:raise SystemExit('artifact_metadata_verification_failed') from None
 print('SOURCE_ON_ARTIFACT_METADATA_VERIFIED',flush=True)
if __name__=='__main__':main()
