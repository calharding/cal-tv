"""Explicit one-shot entrypoint. Default validates JSON offline; never starts work.
python -m media_automation.backlog_cli --scope /absolute/reviewed-scope.json
Live preview and execution require a separately reviewed site integration.
No job creation, acquisition, daemon, automatic retry, or credential provisioning.
"""
import argparse,json,sqlite3,contextlib
from pathlib import Path
from .core import Store,ReviewRequired,validate_request
from .importer import inventory,destination,identity_seal,require_same_publication_filesystem
from .quality_policy import load_policy
from .prepared_scope import validate_prepared_scope
from .adapters import Jellyfin
from .manual_services import ManualServices,LocalArr,JellyfinStream
from .cross_journal import reconcile
from .backlog_once import run

class ExistingStore(Store):
    """Attach to existing schema without mkdir, migration, chmod, or writes."""
    def __init__(self, root):
        self.root=Path(root).resolve()
        path=self.root/'jobs.sqlite3'
        if not path.is_file() or path.is_symlink():raise ReviewRequired('Existing journal required')
        self.db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=15)
        self.db.row_factory=sqlite3.Row
        try:
            for table,required in {'jobs':{'id','request','approval','state','report','updated'},'imports':{'job_id','src','dst','sha256','device','inode','state'},'events':{'seq','job_id','state','at','detail'}}.items():
                columns={row[1] for row in self.db.execute('PRAGMA table_info('+table+')')}
                if not required<=columns:raise ReviewRequired('Existing journal schema incompatible; no automatic migration')
            self.db.execute('PRAGMA synchronous=FULL')
        except BaseException:
            self.db.close();raise

def load_scope(path):
    p=Path(path)
    if not p.is_absolute() or p.is_symlink():raise ReviewRequired('Absolute reviewed scope file required')
    doc=json.loads(p.read_text())
    if doc.get('schema_version')!=1:raise ReviewRequired('Unknown scope schema')
    for key in ('config','request','plan','source','job_id','approval_reference'):
        if key not in doc:raise ReviewRequired('Incomplete explicit scope')
    validate_request(doc['request'])
    cfg=doc['config']
    if not cfg.get('backlog_shared_store_root') or not Path(cfg['backlog_shared_store_root']).is_absolute():raise ReviewRequired('Explicit shared store required')
    if not doc['approval_reference'] or not doc['job_id']:raise ReviewRequired('Existing approval/job binding required')
    return doc

def existing_job(doc):
    dbpath=Path(doc['config']['backlog_shared_store_root'])/'jobs.sqlite3'
    if not dbpath.is_file() or dbpath.is_symlink():raise ReviewRequired('Existing shared journal required; no automatic creation')
    with contextlib.closing(sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT request,approval FROM jobs WHERE id=?',(doc['job_id'],)).fetchone()
    if not row or json.loads(row[0])!=doc['request'] or row[1]!=doc['approval_reference']:
        raise ReviewRequired('Reviewed scope differs from persisted job/approval')

def real_services(config, plan, approval_reference):
    raise ReviewRequired("Live service transport is disabled in the public baseline")

def execute_scope(doc,mode,*,services_factory=real_services):
    if mode=='validate':return {'status':'VALIDATED_OFFLINE','network_used':False,'activated':False}
    cfg=doc['config'];existing_job(doc)
    if mode=='execute' and (cfg.get('backlog_fast_enabled') is not True or cfg.get('manual_service_writes_enabled') is not True):
        raise ReviewRequired('Execution disabled; both reviewed enable switches must be explicit')
    if mode not in ('check','execute'):raise ReviewRequired('Unknown launch mode')
    services,jelly=services_factory(cfg,doc['plan'],doc['approval_reference'])
    if mode=='check':
        load_policy(cfg)
        req=doc['request'];source=Path(doc['source']).absolute();media=Path(cfg['media_root']).resolve()
        prepared=validate_prepared_scope(cfg,req,source)
        if not prepared and not source.is_relative_to(Path(cfg['complete_root']).resolve()):raise ReviewRequired('Source outside reviewed roots')
        canonical=services._item(doc['plan'])
        roles=[role for role,p in cfg['backlog_journals'].items() if Path(p)==Path(cfg['backlog_shared_store_root'])/'jobs.sqlite3']
        if len(roles)!=1:raise ReviewRequired('Job store role is ambiguous')
        files=[]
        for src,ep in inventory(source,req):
            dst=media/(str(destination(req,ep))+src.suffix.lower())
            require_same_publication_filesystem(src,dst)
            files.append({'source':str(src),'prepared_root':str(source),'identity_seal':identity_seal(src),'episode':ep,
                          'destination':str(dst),'alternate_destinations':[]})
        aliases=[req['provider_id']]+[prefix+':'+str(canonical[key]) for prefix,key in [('tmdb','tmdbId'),('imdb','imdbId')] if canonical.get(key)]
        scope={'request':req,'plan':doc['plan'],'files':files,'prepared_scope':prepared,'provider_aliases':aliases,
               'admission_jobs':[{'role':roles[0],'id':doc['job_id'],'state':'AUTHORIZED'}]}
        result=reconcile(cfg['backlog_journals'],scope,services=services,jellyfin=jelly)
        return {'status':'READ_ONLY_PREVIEW','reconciliation':result,'activated':False}
    store=ExistingStore(Path(cfg['backlog_shared_store_root']))
    try:return run(store,doc['job_id'],doc['source'],cfg,doc['plan'],services,expected_request=doc['request'],expected_approval=doc['approval_reference'])
    finally:store.db.close()

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope',required=True)
    group=parser.add_mutually_exclusive_group();group.add_argument('--check',action='store_true');group.add_argument('--execute',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.check or args.execute:
            raise ReviewRequired("Live preview and execution are disabled in the public baseline")
        doc=load_scope(args.scope)
        result=execute_scope(doc,'execute' if args.execute else 'check' if args.check else 'validate')
        print(json.dumps(result,sort_keys=True));return 0
    except (ReviewRequired,ValueError,KeyError,OSError,sqlite3.Error) as error:
        print(json.dumps({'status':'STOPPED','error_type':type(error).__name__,'detail':str(error) if isinstance(error,ReviewRequired) else 'Invalid scope or unavailable local resource'}));return 2
if __name__=='__main__':raise SystemExit(main())
