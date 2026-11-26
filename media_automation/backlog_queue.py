"""Inactive supervised foreground queue of exact already-reviewed scopes.
No acquisition, discovery, job creation, daemon or automatic retry of BLOCKED work.
Stop requests take effect after the current coordinator call, before the next item.
"""
import argparse,contextlib,fcntl,hashlib,json,os,sqlite3
from pathlib import Path
from .core import ReviewRequired
from .backlog_cli import load_scope,execute_scope

def _scope(item):
    path=Path(item['scope_path'])
    if not path.is_absolute() or path.is_symlink():raise ReviewRequired('Absolute reviewed scope path required')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ReviewRequired('Queued scope changed; review required')
    doc=json.loads(raw)
    if load_scope(str(path))!=doc:raise ReviewRequired('Queued scope changed during validation')
    if doc['job_id']!=item['job_id']:raise ReviewRequired('Queued job identity differs')
    return doc

def validate(manifest):
    if manifest.get('schema_version')!=1 or not manifest.get('items'):raise ReviewRequired('Explicit nonempty queue manifest required')
    root=Path(manifest['queue_root'])
    if not root.is_absolute() or any(p.is_symlink() for p in [root,*root.parents]):raise ReviewRequired('Absolute non-symlink queue root required')
    seen=set()
    for item in manifest['items']:
        doc=_scope(item)
        if doc['config'].get('backlog_horizon_policy')!='fresh-under-locks' or doc['config'].get('backlog_expected_horizons') is not None:
            raise ReviewRequired('Queue scopes must explicitly use fresh-under-locks reconciliation, not stale pinned horizons')
        identity=(doc['config']['backlog_shared_store_root'],doc['job_id'])
        if identity in seen:raise ReviewRequired('Duplicate job in queue')
        seen.add(identity)
    return hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def _execute(doc):
    result=execute_scope(doc,'execute')
    if result.get('status')=='WAITING_JELLYFIN':return {'state':'WAITING','result':result}
    path=Path(doc['config']['backlog_shared_store_root'])/'jobs.sqlite3'
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT j.state,b.phase FROM jobs j JOIN backlog_runs b ON b.job=j.id WHERE j.id=?',(doc['job_id'],)).fetchone()
    if row!=('READY','READY') or not result.get('jellyfin'):raise ReviewRequired('Coordinator completion not independently recorded')
    return {'state':'READY','result':result}

def request_stop(manifest):
    root=Path(manifest['queue_root'])
    if not root.is_absolute() or any(p.is_symlink() for p in [root,*root.parents]):raise ReviewRequired('Invalid queue stop target')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    try:
        fd=os.open(root/'stop-after-current',os.O_CREAT|os.O_WRONLY|os.O_EXCL,0o600);os.close(fd)
    except FileExistsError:pass
    return {'status':'STOP_REQUESTED','current_item_not_interrupted':True}

def run(manifest,*,executor=_execute,checkpoint=None):
    digest=validate(manifest)
    if manifest.get('queue_execution_enabled') is not True:raise ReviewRequired('Supervised queue is disabled')
    root=Path(manifest['queue_root']);root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if any((root/name).is_symlink() for name in ('queue.lock','queue.sqlite3','stop-after-current')):
        raise ReviewRequired('Queue state path is a symlink')
    with (root/'queue.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ReviewRequired('Existing supervised queue already owns this manifest') from None
        # This lock serializes one manifest, not the native jobs in other journals.
        with contextlib.closing(sqlite3.connect(root/'queue.sqlite3')) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS manifest (id INTEGER PRIMARY KEY CHECK(id=1),digest TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS items (position INTEGER PRIMARY KEY,job_id TEXT NOT NULL,scope_sha256 TEXT NOT NULL,state TEXT NOT NULL,result TEXT)')
            old=db.execute('SELECT digest FROM manifest WHERE id=1').fetchone()
            if old and old['digest']!=digest:raise ReviewRequired('Queue root already belongs to another immutable manifest')
            with db:
                if not old:
                    db.execute('INSERT INTO manifest VALUES (1,?)',(digest,))
                    db.executemany('INSERT INTO items VALUES (?,?,?,?,NULL)',[(i,item['job_id'],item['sha256'],'PENDING') for i,item in enumerate(manifest['items'])])
            completed=0
            for index,item in enumerate(manifest['items']):
                row=db.execute('SELECT * FROM items WHERE position=?',(index,)).fetchone()
                if not row or row['state'] not in ('PENDING','RUNNING','WAITING','READY','BLOCKED') or row['job_id']!=item['job_id'] or row['scope_sha256']!=item['sha256']:
                    raise ReviewRequired('Persisted queue state or scope binding is unknown')
                if row['state']=='READY':completed+=1;continue
                if row['state']=='BLOCKED':return {'status':'BLOCKED','job_id':item['job_id'],'completed':completed,'reconciliation_required':True}
                if (root/'stop-after-current').exists():return {'status':'STOPPED','completed':completed,'next_job_id':item['job_id']}
                # RUNNING can survive a crash. Only the exact coordinator may prove its effects.
                doc=_scope(item)  # Revalidate immutable file immediately before each call.
                if doc['config'].get('backlog_fast_enabled') is not True or doc['config'].get('manual_service_writes_enabled') is not True:
                    raise ReviewRequired('Queued scope execution is disabled')
                with db:db.execute("UPDATE items SET state='RUNNING' WHERE position=?",(index,))
                if checkpoint:checkpoint('RUNNING',index)
                try:
                    outcome=executor(doc)
                    if checkpoint:checkpoint('EXECUTED',index)
                    if outcome.get('state') not in ('READY','WAITING'):raise ReviewRequired('Unknown coordinator outcome')
                    with db:db.execute('UPDATE items SET state=?,result=? WHERE position=?',(outcome['state'],json.dumps(outcome),index))
                    if checkpoint:checkpoint(outcome['state'],index)
                    if outcome['state']=='WAITING':return {'status':'WAITING','job_id':item['job_id'],'completed':completed}
                    completed+=1
                except Exception as error:
                    with db:db.execute("UPDATE items SET state='BLOCKED',result=? WHERE position=?",(json.dumps({'error_type':type(error).__name__}),index))
                    raise
            return {'status':'COMPLETE','completed':completed}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',required=True)
    mode=p.add_mutually_exclusive_group();mode.add_argument('--run',action='store_true');mode.add_argument('--request-stop',action='store_true')
    args=p.parse_args(argv)
    try:
        manifest=json.loads(Path(args.manifest).read_text())
        result=run(manifest) if args.run else request_stop(manifest) if args.request_stop else {'status':'VALIDATED_OFFLINE','manifest_sha256':validate(manifest),'activated':False}
        print(json.dumps(result));return 0
    except (ReviewRequired,OSError,ValueError,KeyError) as error:
        print(json.dumps({'status':'STOPPED','error_type':type(error).__name__,'detail':str(error) if isinstance(error,ReviewRequired) else 'Invalid/unavailable local queue input'}));return 2
if __name__=='__main__':raise SystemExit(main())
