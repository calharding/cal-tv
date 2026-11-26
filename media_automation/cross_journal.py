"""Read-only admission reconciliation; never creates Store or writes live services.
Run again under all writer locks before mutation. Receipt alone is not authority.
"""
import hashlib,json,re,sqlite3
from pathlib import Path
from .core import ReviewRequired
from .importer import identity_seal
ROLES={'root','manual','batch','films'}

def _overlaps(request,wanted,aliases=None):
    aliases=set(aliases or [wanted['provider_id']])
    if request.get('provider_id') not in aliases:
        # Same title and year are a conflict to resolve, not proof two IDs are equivalent.
        if str(request.get('title','')).casefold()==str(wanted.get('title','')).casefold() and request.get('year')==wanted.get('year'):
            raise ReviewRequired('Same-title provider alias is unresolved')
        return False
    if request.get('kind')!=wanted['kind']:return True
    if wanted['kind']=='movie':return True
    if type(request.get('season')) is not int:raise ReviewRequired('Journal season identity is unknown')
    if request['season']!=wanted['season']:return False
    episodes=request.get('episodes')
    if not isinstance(episodes,list):raise ReviewRequired('Journal episode identity is unknown')
    end=request.get('combined_episode_range') or request.get('combined_episode_staging')
    if end:
        if not isinstance(end,dict) or type(end.get('start')) is not int or type(end.get('end')) is not int or not 1<=end['start']<=end['end']<=999:
            raise ReviewRequired('Journal combined episode identity is unknown')
        episodes=list(range(end['start'],end['end']+1))
    desired=wanted['episodes']
    pair=wanted.get('combined_episode_range') or wanted.get('combined_episode_staging')
    if pair:
        if not isinstance(pair,dict) or type(pair.get('start')) is not int or type(pair.get('end')) is not int or not 1<=pair['start']<=pair['end']<=999:
            raise ReviewRequired('Wanted combined episode identity is unknown')
        desired=list(range(pair['start'],pair['end']+1))
    return bool(set(episodes)&set(desired))

def reconcile(stores,scope,*,services=None,jellyfin=None,recovery_snapshot=None):
    if set(stores)!=ROLES:raise ReviewRequired('Exactly four known journals required')
    request=scope['request'];files=scope['files'];blockers=[];horizons={};matches=[]
    if (request.get('combined_episode_range') or {}).get('schema_version') in (4,5,6):
        from .core import combined_episode_end
        combined_episode_end(request)  # Validate new programme scope before any journal read.
    if not files:raise ReviewRequired('Empty reconciliation scope')
    if request['kind']=='season' and sorted(f['episode'] for f in files)!=request['episodes']:
        raise ReviewRequired('File scope differs from exact requested episodes')
    aliases=set(scope.get('provider_aliases',[]))|{request['provider_id']}
    source_paths=set();target_paths=set();inodes=set()
    for f in files:
        source=Path(f['source']);prepared=Path(f['prepared_root'])
        if not source.is_absolute() or not prepared.is_absolute() or not source.is_relative_to(prepared):raise ReviewRequired('Exact prepared root binding required')
        if any(p.is_symlink() for p in [source,*source.parents]):raise ReviewRequired('Source symlink rejected')
        seal=identity_seal(source)
        if seal!=f['identity_seal']:raise ReviewRequired('Prepared source identity changed')
        source_paths.add(str(source));inodes.add(tuple(seal[:2]))
        paths=[f['destination'],*f.get('alternate_destinations',[])]
        if len(paths)!=len(set(paths)):raise ReviewRequired('Duplicate alternate path')
        target_paths.update(paths)
        for raw in paths:
            p=Path(raw)
            if not p.is_absolute():raise ReviewRequired('Absolute destination required')
            if p.exists() or p.is_symlink():blockers.append({'kind':'existing_target','path':str(p)})
        destination=Path(f['destination'])
        if destination.parent.exists():
            for p in destination.parent.iterdir():
                if p.stem==destination.stem or (request['kind']=='season' and any(re.search(r'(?i)S0*%sE0*%s(?:\D|$)'%(request['season'],ep),p.name) for ep in range(f['episode'],(request.get('combined_episode_range') or {}).get('end',f['episode'])+1))):
                    blockers.append({'kind':'alternate_target','path':str(p)})
    allowed={(x['role'],x['id']):x['state'] for x in scope.get('admission_jobs',[])}
    for role,raw in sorted(stores.items()):
        p=Path(raw)
        if not p.is_absolute() or p.is_symlink() or not p.is_file():raise ReviewRequired('Journal path unavailable')
        db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        try:
            # Read a consistent live snapshot, including WAL state, without touching the schema.
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            jobs=[dict(x) for x in db.execute('SELECT id,state,request,report,approval,updated FROM jobs ORDER BY id')]
            imports=[dict(x) for x in db.execute('SELECT * FROM imports ORDER BY job_id,dst')]
            seq=db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            digest=hashlib.sha256(json.dumps([jobs,imports,seq],sort_keys=True).encode()).hexdigest()
            horizons[role]={'path':str(p),'events':seq,'jobs':len(jobs),'imports':len(imports),'snapshot_sha256':digest}
            touched=set()
            known_ids={j['id'] for j in jobs}
            if any(row['job_id'] not in known_ids for row in imports):
                blockers.append({'kind':'orphan_import_uncertainty','role':role})
            for row in imports:
                if row['src'] in source_paths or row['dst'] in target_paths or (row['device'],row['inode']) in inodes:
                    blockers.append({'kind':'import_overlap','role':role,'row':row});touched.add(row['job_id'])
            for job in jobs:
                req=json.loads(job['request'])
                if req.get('kind') not in ('movie','season') or not req.get('provider_id'):
                    raise ReviewRequired('Journal canonical identity is unknown')
                report=json.loads(job['report']) if job['report'] else {}
                flat=json.dumps(report,sort_keys=True)
                overlap=_overlaps(req,request,aliases) or job['id'] in touched or any(path in flat for path in source_paths|target_paths)
                if not overlap:continue
                matches.append({'role':role,'id':job['id'],'state':job['state']})
                own_imports=[x for x in imports if x['job_id']==job['id']]
                recovering = (recovery_snapshot is not None and recovery_snapshot.get('role')==role and isinstance(recovery_snapshot.get('job'),dict) and {k:recovery_snapshot['job'].get(k) for k in job}==job and job['state']=='MANUAL_BLOCKED')
                if allowed.get((role,job['id']))!=job['state'] or (job['state']!='AUTHORIZED' and not recovering) or own_imports or not job['approval']:
                    blockers.append({'kind':'job_overlap_or_uncertain','role':role,'id':job['id'],'state':job['state']})
        except (sqlite3.Error,ValueError,KeyError,TypeError) as e:
            raise ReviewRequired('Journal schema or identity is uncertain; no admission') from e
        finally:db.close()
    if set(allowed)!={(x['role'],x['id']) for x in matches}:blockers.append({'kind':'admission_identity_not_exact'})
    if services is None or jellyfin is None:blockers.append({'kind':'live_catalogue_preflight_unavailable'})
    else:
        try:
            actual=services.read_arr(scope['plan'])
            desired=scope['plan']['files']
            for row in actual['files']:
                if request['kind']=='movie' or any(set(row['episode_ids'])&set(f['episode_ids']) or row['path']==f['path'] for f in desired):
                    blockers.append({'kind':'arr_overlap','row':row})
            if jellyfin.owned(request):blockers.append({'kind':'jellyfin_overlap'})
        except Exception as e:
            blockers.append({'kind':'live_catalogue_preflight_uncertain','error_type':type(e).__name__})
    return {'safe':not blockers,'blockers':blockers,'horizons':horizons,'matches':matches,
            'scope_sha256':hashlib.sha256(json.dumps(scope,sort_keys=True).encode()).hexdigest(),
            'read_only':True,'requires_fresh_check_under_writer_locks':True}
