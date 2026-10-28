"""Read-only production acceptance plus rejected unauthenticated write probes."""
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler

mode, root_arg = sys.argv[1:]
root = Path(root_arg)
base = 'http://127.0.0.1:8001'

def get(path, line='aav', raw=False):
    with urlopen(Request(base+path,headers={'Cookie':'gm_article_line='+line}),timeout=20) as response:
        value=response.read()
        return value if raw else json.loads(value)

def inventory(line):
    data=get('/api/articles?page_size=200',line)
    assert len(data['articles']) == data['total'], 'truncated inventory'
    result=[]
    for article in data['articles']:
        item={k:article.get(k) for k in ('id','job_id','title','publish_blocked')}
        item['distributions']=sorted([{k:d.get(k) for k in ('platform','account','lang','external_id','external_url','wechat_media_id','publish_status')} for d in article.get('distributions',[])],key=lambda d:(str(d['platform']),str(d['account']),str(d['lang'])))
        result.append(item)
    return sorted(result,key=lambda a:(str(a['job_id']),str(a['id'])))

def no_old_workers():
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():continue
        try: args=(entry/'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError,PermissionError,ProcessLookupError):continue
        if any(Path(arg.decode(errors='replace')).name in ('batch_processor.py','workflow_worker.py','auto_blog_worker.py') for arg in args):
            raise RuntimeError('Old publication worker active: '+entry.name)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

if mode=='before':
    no_old_workers()
    values={line:inventory(line) for line in ('aav','solidex')}
    (root/'before-inventory.json').write_text(json.dumps(values,ensure_ascii=False))
    print('BEFORE_COUNTS', {line:len(rows) for line,rows in values.items()})
elif mode=='after':
    for _ in range(30):
        try:
            if get('/api/health')['status']=='ok':break
        except (URLError,OSError):pass
        time.sleep(.5)
    else:raise RuntimeError('service did not become healthy')
    before=json.loads((root/'before-inventory.json').read_text())
    after={line:inventory(line) for line in before}
    assert before==after,'article identities changed during deployment; inspect before taking action'
    no_old_workers()
    expected=(root/'editor-bundle.sha256').read_text().strip()
    assert hashlib.sha256(get('/static/editorial-editor.js',raw=True)).hexdigest()==expected
    for path,target in [('/operator','/workbench'),('/workbench','/auth/start'),
                        ('/aav','/workbench/aav'),('/solidex','/workbench/solidex'),
                        ('/workbench/solidex','next=/workbench/solidex')]:
        try:build_opener(NoRedirect()).open(Request(base+path,headers={'Cookie':'gm_article_line=aav'}),timeout=10)
        except HTTPError as exc:
            assert exc.code==302 and target in exc.headers['Location'], (path,exc.code)
        else:raise AssertionError('Missing real-login boundary: '+path)
    for path,code in [('/api/workflow/start',409),('/api/editorial/1/publish',401)]:
        try:urlopen(Request(base+path,data=b'{}',headers={'Content-Type':'application/json','Cookie':'gm_article_line=aav'}),timeout=10)
        except HTTPError as exc:assert exc.code==code,(path,exc.code)
        else:raise AssertionError('Unauthenticated write not rejected')
    result={'healthy':True,'inventory_unchanged':True,'counts':{line:len(rows) for line,rows in after.items()},'editor_sha256':expected,'legacy_writes_blocked':True,'real_identity_required':True}
    (root/'after-check.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
else:raise ValueError('before or after required')
