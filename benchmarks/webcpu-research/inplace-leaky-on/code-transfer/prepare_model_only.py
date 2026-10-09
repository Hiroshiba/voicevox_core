"""Fetch pinned public CORE archive, package only its sample model privately; no build."""
import argparse,hashlib,io,tarfile,urllib.request,zipfile
from pathlib import Path
CORE='9b539761f3e152b966e08c2de0784129fe8cf68d'
ARCHIVE_SHA='1b02f8d4743c70be2f501e51f47b4c248fa1160a7677c041ddf1e753353f7081'
MODEL_SHA='51425e43e7ad5aa33af06464b77f86c64959ab9317353e8f549c1b7747150fc9'
def package(raw):
 if hashlib.sha256(raw).hexdigest()!=ARCHIVE_SHA:raise ValueError('model_source_hash')
 prefix='voicevox_core-'+CORE+'/model/sample.vvm/'
 output=io.BytesIO()
 with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive,zipfile.ZipFile(output,'w',zipfile.ZIP_STORED) as z:
  files=[x for x in archive.getmembers() if x.name.startswith(prefix) and x.isfile()]
  if not files or any('/' in x.name[len(prefix):] for x in files):raise ValueError('model_layout')
  for x in sorted(files,key=lambda x:x.name):
   name=x.name[len(prefix):]
   if name in {'.','..'} or not name:raise ValueError('model_name')
   info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));info.create_system=3;info.external_attr=0o100644<<16
   z.writestr(info,archive.extractfile(x).read())
 result=output.getvalue()
 if hashlib.sha256(result).hexdigest()!=MODEL_SHA:raise ValueError('model_hash')
 return result
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--archive',type=Path);a=p.parse_args()
 if a.output.exists():raise ValueError('new_model_output_required')
 raw=a.archive.read_bytes() if a.archive else urllib.request.urlopen('https://github.com/yamachu/voicevox_core/archive/'+CORE+'.tar.gz',timeout=120).read(128*1024*1024)
 data=package(raw);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_bytes(data)
 print('SOURCE_ON_MODEL_READY '+MODEL_SHA)
