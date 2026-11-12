"""Produce a clean source archive, never publish to an external service."""
import argparse,hashlib,json,zipfile
from pathlib import Path
from check_release import check
from release_files import selected_files

def export(root,output):
    result=check(root)
    if result['status']!='passed':raise RuntimeError('Release checks failed: '+repr(result['errors']))
    if output.exists():raise FileExistsError('Use a new output filename; archive already exists')
    output.parent.mkdir(parents=True,exist_ok=True)
    files=list(selected_files(root))
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for path in files:z.write(path,'posthub/'+path.relative_to(root).as_posix())
    with zipfile.ZipFile(output) as z:
        assert z.testzip() is None
        assert len(z.namelist())==len(files)
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.sha256').write_text(digest+'  '+output.name+'\n',encoding='utf-8')
    return {'source_files':len(files),'bytes':output.stat().st_size,'sha256':digest,'excludes':'private data, generated build/dist, local artifacts and historical screenshots','published':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(export(Path(__file__).resolve().parents[1],args.output),ensure_ascii=True))
