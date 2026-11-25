"""Explicit zero-effect runtime correction. Never called by ordinary retry paths."""
import copy,hashlib,json,os,time,subprocess
from pathlib import Path
from .core import ReviewRequired,validate_request
from .backlog_once import writer_locks
from . import importer
from .quality_policy import load_policy
from .prepared_scope import validate_prepared_scope
from .cross_journal import reconcile
from .adapters import Jellyfin
ERROR='Episode duration disagrees with metadata listing'
def recover(store,doc,replacement,*,expected_job,expected_run,expected_sources,error_receipt,error_receipt_path,recovery_approval,services):
    """All evidence supplied explicitly; a successful return permits a NEW scoped run.
    No media/catalogue effects. Any failure rolls back the entire journal change.
    """
    cfg=doc['config'];jid=doc['job_id'];plan=doc['plan']
    if not recovery_approval:raise ReviewRequired('Explicit recovery approval required')
    old=doc['request'];new=copy.deepcopy(replacement)
    if old.get('kind')!='season' or not new.get('runtime_review_reference'):raise ReviewRequired('Runtime reference required')
    a=copy.deepcopy(old);b=copy.deepcopy(new)
    for x in (a,b):x.pop('episode_durations',None);x.pop('runtime_review_reference',None)
    if a!=b:raise ReviewRequired('Recovery may change only runtime evidence')
    durations=new.get('episode_durations',{})
    if set(durations)!=set(old.get('episode_durations',{})) or any(type(v) not in (int,float) or not 0<v<86400 for v in durations.values()):raise ReviewRequired('Runtime keys or values invalid')
    validate_request(new)
    receipt_path=Path(error_receipt_path)
    if not receipt_path.is_absolute() or any(x.is_symlink() for x in [receipt_path,*receipt_path.parents]) or json.loads(receipt_path.read_text())!=error_receipt:raise ReviewRequired('Original runtime failure receipt changed')
    if error_receipt.get('type')!='ReviewRequired' or error_receipt.get('error')!=ERROR:raise ReviewRequired('Exact runtime failure evidence required')
    if expected_job.get('state')!='MANUAL_BLOCKED' or expected_run.get('phase')!='BLOCKED' or expected_run.get('result') is not None:raise ReviewRequired('Not an untouched blocked runtime run')
    if expected_job.get('request')!=old or expected_job.get('approval')!=doc['approval_reference']:raise ReviewRequired('Prior request/approval mismatch')
    if str(store.root)!=cfg['backlog_shared_store_root']:raise ReviewRequired('Shared store mismatch')
    with writer_locks(store,cfg):
        if not cfg.get('storage_guard_script') or not cfg.get('storage_guard_config'):raise ReviewRequired('Existing storage guard required')
        guard=subprocess.run(['/usr/bin/python3',cfg['storage_guard_script'],cfg['storage_guard_config']],capture_output=True,timeout=10)
        if guard.returncode:raise ReviewRequired('Storage mount guard failed')
        if store.get(jid)!=expected_job:raise ReviewRequired('Prior job changed')
        row=store.db.execute('SELECT * FROM backlog_runs WHERE job=?',(jid,)).fetchone()
        if not row or dict(row)!=expected_run:raise ReviewRequired('Prior run changed')
        admission=store.db.execute('SELECT * FROM backlog_admission WHERE job=?',(jid,)).fetchone()
        if not admission:raise ReviewRequired('Prior admission missing')
        tables={r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table,key in [('imports','job_id'),('import_identity','job_id'),('backlog_metadata','job'),('backlog_refresh','job')]:
            if table in tables and store.db.execute('SELECT 1 FROM '+table+' WHERE '+key+'=?',(jid,)).fetchone():raise ReviewRequired('Prior effect or intent exists')
        intent=Path(cfg['catalogue_intent_db'])
        if os.path.lexists(intent):raise ReviewRequired('Catalogue intent journal exists; separate reconciliation required')
        source=Path(doc['source']).absolute();media=Path(cfg['media_root']).resolve()
        if any(x.is_symlink() for x in [source,*source.parents]):raise ReviewRequired('Source symlink')
        policy,digest=load_policy(cfg);prepared=validate_prepared_scope(cfg,new,source)
        old_prepared=validate_prepared_scope(cfg,old,source)
        old_binding=hashlib.sha256(json.dumps([old,plan,str(source),str(media),digest,old_prepared],sort_keys=True).encode()).hexdigest()
        if old_binding!=expected_run['binding']:raise ReviewRequired('Original run binding differs from supplied scope')
        if not prepared and not source.is_relative_to(Path(cfg['complete_root']).resolve()):raise ReviewRequired('Source outside scope')
        files=[]
        for src,ep in importer.inventory(source,new):
            dst=media/(str(importer.destination(new,ep))+src.suffix.lower())
            if any(x.is_symlink() for x in [dst,*dst.parents]) or os.path.lexists(dst) or os.path.lexists(dst.with_suffix('.nfo')):raise ReviewRequired('Target/NFO already exists or symlink')
            files.append(dict(source=str(src),prepared_root=str(source),identity_seal=importer.identity_seal(src),episode=ep,destination=str(dst),alternate_destinations=cfg.get('backlog_alternate_destinations',{}).get(str(dst),[])))
        actual_sources=[{k:f[k] for k in ('source','episode','identity_seal')} for f in files]
        if actual_sources!=expected_sources:raise ReviewRequired('Reviewed source inventory or seals changed')
        if {f['destination'] for f in files}!={f['host_path'] for f in plan['files']}:raise ReviewRequired('Plan targets differ')
        roles=[k for k,v in cfg['backlog_journals'].items() if Path(v)==store.root/'jobs.sqlite3']
        if len(roles)!=1:raise ReviewRequired('Ambiguous journal role')
        for role,path in cfg['backlog_journals'].items():
            dp=Path(path);lock=dp.parent.parent/'import.lock' if role=='films' else dp.parent/'worker.lock'
            if str(lock)!=cfg['backlog_writer_locks'][role]:raise ReviewRequired('Journal/lock mismatch')
        services._check_plan(plan);canonical=services._item(plan)
        aliases=sorted(set([new['provider_id']]+[k+':'+str(canonical[v]) for k,v in [('tmdb','tmdbId'),('imdb','imdbId')] if canonical.get(v)]))
        scope=dict(request=new,plan=plan,files=files,prepared_scope=prepared,provider_aliases=aliases,admission_jobs=[dict(role=roles[0],id=jid,state='MANUAL_BLOCKED')])
        raw=dict(store.db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        view=Jellyfin(cfg);view.api=services.jellyfin
        preview=reconcile(cfg['backlog_journals'],scope,services=services,jellyfin=view,recovery_snapshot=dict(role=roles[0],job=raw))
        if not preview['safe']:raise ReviewRequired('Fresh recovery preview blocked: '+json.dumps(preview['blockers']))
        for f in files:
            importer.require_identity(Path(f['source']),f['identity_seal'])
            dst=Path(f['destination'])
            if os.path.lexists(dst) or os.path.lexists(dst.with_suffix('.nfo')):raise ReviewRequired('Target appeared during preview')
        binding=hashlib.sha256(json.dumps([new,plan,str(source),str(media),digest,prepared],sort_keys=True).encode()).hexdigest()
        audit=dict(prior_job=expected_job,prior_run=expected_run,expected_sources=expected_sources,prior_admission=dict(admission),error_receipt=error_receipt,error_receipt_path=str(receipt_path),recovery_approval=recovery_approval,replacement=new,preview=preview,new_binding=binding)
        with store.db:
            # CAS again inside the SQLite transaction; all writers remain locked.
            store.db.execute('BEGIN IMMEDIATE')
            if store.get(jid)!=expected_job or dict(store.db.execute('SELECT * FROM backlog_runs WHERE job=?',(jid,)).fetchone())!=expected_run:raise ReviewRequired('Recovery CAS failed')
            store.db.execute('CREATE TABLE IF NOT EXISTS backlog_runtime_recovery (job TEXT PRIMARY KEY, audit TEXT NOT NULL)')
            store.db.execute('INSERT INTO backlog_runtime_recovery VALUES (?,?)',(jid,json.dumps(audit,sort_keys=True)))
            store.db.execute("UPDATE jobs SET request=?,state='MANUAL_BACKLOG',updated=? WHERE id=?",(json.dumps(new),time.time(),jid))
            store.db.execute("UPDATE backlog_runs SET binding=?,phase='ADMITTED',result=NULL WHERE job=?",(binding,jid))
            store.db.execute('UPDATE backlog_admission SET receipt=? WHERE job=?',(json.dumps(preview),jid))
            store.db.execute('INSERT INTO events(job_id,state,at,detail) VALUES(?,?,?,?)',(jid,'MANUAL_BACKLOG',time.time(),'Explicit zero-effect runtime recovery: '+recovery_approval))
        return dict(status='RECOVERED_NOT_EXECUTED',job_id=jid,binding=binding,preview=preview)
