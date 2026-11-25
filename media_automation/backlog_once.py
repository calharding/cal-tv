"""Inactive explicit-scope backlog coordinator. No discovery/acquisition/daemon.
Call only with a reviewed existing job and plan; never converts old journals.
A failure records BLOCKED and requires separate reconciliation, not blind retry.
"""
import hashlib,json,os,tempfile,shutil,subprocess,copy,fcntl,contextlib,stat,time
from pathlib import Path,PurePosixPath
import xml.etree.ElementTree as ET
from .core import ReviewRequired, combined_episode_end
from . import importer
from .quality_policy import load_policy
from .prepared_scope import validate_prepared_scope
from .cross_journal import reconcile
from .adapters import Jellyfin,AdapterError
from .manual_services import canonical_episode_ids, canonical_plan_episodes

def _nfo(request,f):
    root=ET.Element('movie' if request['kind']=='movie' else 'episodedetails')
    title=request['title'] if request['kind']=='movie' else f.get('canonical_title')
    if not title:raise ReviewRequired('Reviewed canonical episode title required')
    ET.SubElement(root,'title').text=title
    ET.SubElement(root,'lockdata').text='false'
    if request['kind']=='season':
        ids=canonical_episode_ids(f)
        for key,value in sorted(ids.items()):
            ET.SubElement(root,'uniqueid',type=key.lower(),default='true' if key=='Tvdb' else 'false').text=value
        ET.SubElement(root,'showtitle').text=request['title']
        ET.SubElement(root,'season').text=str(request['season'])
        ET.SubElement(root,'episode').text=str(f['canonical_episode'])
        end=combined_episode_end(request)
        if end is not None:
            canonical_plan_episodes(request,f)
            ET.SubElement(root,'episodenumberend').text=str(end)
    else:
        key,value=request['provider_id'].split(':',1)
        ET.SubElement(root,'uniqueid',type=key,default='true').text=value
        ET.SubElement(root,'year').text=str(request['year'])
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)

@contextlib.contextmanager
def writer_locks(store,config):
    """Acquire every known writer lock in pathname order, never wait/partially hold."""
    configured=config.get('backlog_writer_locks',{})
    if set(configured)!={'root','manual','batch','films'}:
        raise ReviewRequired('All four existing writer locks must be explicitly configured')
    paths=[];inodes=set()
    for value in configured.values():
        p=Path(value)
        if not p.is_absolute() or any(x.is_symlink() for x in [p,*p.parents]):
            raise ReviewRequired('Writer lock must be an absolute non-symlink path')
        try:info=p.stat()
        except OSError:raise ReviewRequired('Existing writer lock is unavailable') from None
        if not stat.S_ISREG(info.st_mode):raise ReviewRequired('Writer lock is not a regular file')
        inode=(info.st_dev,info.st_ino)
        if inode in inodes:raise ReviewRequired('Writer lock roles alias the same file')
        inodes.add(inode);paths.append(p)
    shared=store.root/'worker.lock'
    if shared not in paths:raise ReviewRequired('Shared Store lock must match one configured writer role')
    with contextlib.ExitStack() as stack:
        for p in sorted(paths,key=str):
            if p==shared:
                if not stack.enter_context(store.lock()):
                    raise ReviewRequired('A media writer already owns a lock')
            else:
                handle=stack.enter_context(p.open('r+'))
                try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ReviewRequired('A media writer already owns a lock') from None
                stack.callback(fcntl.flock,handle,fcntl.LOCK_UN)
        yield

def run(store,job_id,source,config,plan,services,*,expected_request,expected_approval,probe_fn=importer.probe,checkpoint=None,sleep=time.sleep,monotonic=time.monotonic):
    if config.get('backlog_fast_enabled') is not True:raise ReviewRequired('Backlog fast coordinator is inactive')
    if str(store.root)!=config.get('backlog_shared_store_root'):
        raise ReviewRequired('Exact shared production writer/store root must be configured')
    poll_attempts=config.get('jellyfin_poll_attempts',46);poll_interval=config.get('jellyfin_poll_interval_seconds',2)
    poll_timeout=config.get('jellyfin_poll_timeout_seconds',90)
    if type(poll_attempts) is not int or not 1<=poll_attempts<=151 or type(poll_interval) not in (int,float) or not 0<=poll_interval<=5 or type(poll_timeout) not in (int,float) or not 0<poll_timeout<=300:
        raise ReviewRequired('Bounded Jellyfin poll settings invalid')
    horizon_policy=config.get('backlog_horizon_policy')
    if horizon_policy not in (None,'pinned','fresh-under-locks') or (horizon_policy=='fresh-under-locks' and config.get('backlog_expected_horizons') is not None):
        raise ReviewRequired('Contradictory or unknown journal horizon policy')
    with writer_locks(store,config):
        job=store.get(job_id)
        if not job or not expected_approval or job.get('approval')!=expected_approval or job['request']!=expected_request:
            raise ReviewRequired('Exact reviewed request or approval changed before locked admission')
        req=job['request'];raw=Path(source).absolute()
        if any(p.is_symlink() for p in [raw,*raw.parents]):raise ReviewRequired('Symlink in source ancestry')
        source=raw.resolve();media=Path(config['media_root']).resolve()
        policy,policy_digest=load_policy(config)
        if not config.get('storage_guard_script') or not config.get('storage_guard_config'):
            raise ReviewRequired('Existing storage mount guard required')
        guard=subprocess.run(['/usr/bin/python3',config['storage_guard_script'],config['storage_guard_config']],capture_output=True,timeout=10)
        if guard.returncode!=0:raise ReviewRequired('Storage mount guard failed')
        if services.config.get('manual_service_writes_enabled') is not True or not services.approved_plan or services.approved_plan(copy.deepcopy(plan)) is not True:
            raise ReviewRequired('Explicit catalogue plan approval required before linking')
        prepared_scope=validate_prepared_scope(config,req,source)
        if not prepared_scope and not source.is_relative_to(Path(config['complete_root']).resolve()):raise ReviewRequired('Source outside authorized existing backlog root')
        if req.get('subtitle_mappings'):raise ReviewRequired('Mapped sidecars need separately reviewed coordinator support')
        if req.get('combined_episode_staging') or (req.get('combined_episode_range') and req['combined_episode_range'].get('schema_version') not in (3,4,5,6)):raise ReviewRequired('Combined episodes require separate reviewed coordinator')
        combined_episode_end(req)
        if plan['provider_id']!=req['provider_id'] or plan['app']!=('radarr' if req['kind']=='movie' else 'sonarr'):raise ReviewRequired('Plan/request identity differs')
        services._check_plan(plan)
        canonical_item=services._item(plan)  # Confirm canonical catalogue identity before linking anything.
        if shutil.disk_usage(media).free < config.get('floor_bytes',20*1024**3):
            raise ReviewRequired('Media storage reserve is below configured floor')
        if req['kind']=='season':
            episodes=services.arr['sonarr']('GET','/episode?seriesId=%s'%plan['item_id'])
            if req.get('combined_episode_range',{}).get('schema_version')==6:
                existing_files=services.arr['sonarr']('GET','/episodefile?seriesId=%s'%plan['item_id'])
                for member in req['combined_episode_range']['canonical_episode_parts']:
                    live=[e for e in episodes if e.get('id')==member['episode_id']]
                    if len(live)!=1:
                        raise ReviewRequired('Reviewed programme member missing or duplicated')
                    owner=live[0].get('episodeFileId',0)
                    if (live[0].get('hasFile') and not owner) or (owner and sum(f.get('id')==owner for f in existing_files)!=1):
                        raise ReviewRequired('Reviewed programme ownership snapshot inconsistent')
            for f in plan['files']:
                parts=canonical_plan_episodes(req,f)
                exact=[e for e in episodes if e.get('id') in f['episode_ids']]
                if len(exact)!=len(parts) or len(f['episode_ids'])!=len(parts):
                    raise ReviewRequired('Reviewed Arr ID does not match canonical episode')
                for part in parts:
                    matched=[e for e in exact if e.get('id')==part['episode_id']]
                    if len(matched)!=1 or matched[0].get('seasonNumber')!=req['season'] or matched[0].get('episodeNumber')!=part['episode']:
                        raise ReviewRequired('Reviewed Arr ID does not match canonical episode')
                    if req.get('combined_episode_range',{}).get('schema_version') in (5,6) and matched[0].get('title')!=part['title']:
                        raise ReviewRequired('Reviewed programme member title differs from live Sonarr')
                    if str(matched[0].get('tvdbId',''))!=part['tvdb']:
                        raise ReviewRequired('Reviewed episode TVDB identity differs from canonical Sonarr episode')
        if services.config.get('media_root')!=config['media_root']:raise ReviewRequired('Service/library root differs')
        predicted={str(media/(str(importer.destination(req,ep))+src.suffix.lower())):ep for src,ep in importer.inventory(source,req)}
        files=plan['files']
        if len(files)!=len(predicted) or {f['host_path'] for f in files}!=set(predicted):raise ReviewRequired('Reviewed plan differs from exact predicted destinations')
        for f in files:
            if f['canonical_episode']!=predicted[f['host_path']]:raise ReviewRequired('Canonical destination episode differs')
        nfos={str(Path(f['host_path']).with_suffix('.nfo')):_nfo(req,f) for f in files}
        binding=hashlib.sha256(json.dumps([req,plan,str(source),str(media),policy_digest,prepared_scope],sort_keys=True).encode()).hexdigest()
        existing_table=store.db.execute("SELECT 1 FROM sqlite_master WHERE name='backlog_runs'").fetchone()
        existing=store.db.execute('SELECT 1 FROM backlog_runs WHERE job=?',(job_id,)).fetchone() if existing_table else None
        admission=None
        if not existing:
            journals=config.get('backlog_journals',{})
            if set(journals)!={'root','manual','batch','films'}:raise ReviewRequired('All four actual journal paths required')
            for role,path in journals.items():
                dbpath=Path(path)
                expected_lock=(dbpath.parent.parent/'import.lock') if role=='films' else dbpath.parent/'worker.lock'
                if str(expected_lock)!=config['backlog_writer_locks'][role]:raise ReviewRequired('Journal is not bound to its held writer lock')
            roles=[role for role,path in journals.items() if Path(path)==store.root/'jobs.sqlite3']
            if len(roles)!=1:raise ReviewRequired('Current job store is not exactly one reconciled journal')
            scoped_files=[]
            for src,ep in importer.inventory(source,req):
                dst=str(media/(str(importer.destination(req,ep))+src.suffix.lower()))
                scoped_files.append({'source':str(src),'prepared_root':str(source),'identity_seal':importer.identity_seal(src),
                    'destination':dst,'alternate_destinations':config.get('backlog_alternate_destinations',{}).get(dst,[]),'episode':ep})
            aliases=[req['provider_id']]+[prefix+':'+str(canonical_item[key]) for prefix,key in [('tmdb','tmdbId'),('imdb','imdbId')] if canonical_item.get(key)]
            scope={'provider_aliases':sorted(set(aliases)),'request':req,'plan':plan,'files':scoped_files,'prepared_scope':prepared_scope,
                   'admission_jobs':[{'role':roles[0],'id':job_id,'state':'AUTHORIZED'}]}
            view=Jellyfin(config);view.api=services.jellyfin
            admission=reconcile(journals,scope,services=services,jellyfin=view)
            if not admission['safe']:raise ReviewRequired('Fresh cross-journal or live catalogue reconciliation blocked admission: '+json.dumps(admission['blockers']))
            expected=config.get('backlog_expected_horizons')
            if expected is not None and expected!=admission['horizons']:raise ReviewRequired('Journal snapshot changed since scope review')
        with store.db:
            store.db.execute('CREATE TABLE IF NOT EXISTS backlog_runs (job TEXT PRIMARY KEY,binding TEXT NOT NULL,phase TEXT NOT NULL,result TEXT)')
            prior=store.db.execute('SELECT * FROM backlog_runs WHERE job=?',(job_id,)).fetchone()
            if prior and prior['binding']!=binding:raise ReviewRequired('Existing run scope differs')
            if prior and prior['phase']=='BLOCKED':raise ReviewRequired('Prior partial run blocked; reconcile effects before further action')
            if prior and prior['phase']=='READY':return json.loads(prior['result'])
            if not prior:
                if job['state']!='AUTHORIZED' or store.db.execute('SELECT 1 FROM imports WHERE job_id=?',(job_id,)).fetchone():
                    raise ReviewRequired('Existing job effects/state require explicit migration, not fresh admission')
                if not admission or not admission['safe']:raise ReviewRequired('Fresh reconciliation absent')
                store.db.execute('CREATE TABLE IF NOT EXISTS backlog_admission (job TEXT PRIMARY KEY, receipt TEXT NOT NULL)')
                store.db.execute('INSERT INTO backlog_admission VALUES (?,?)',(job_id,json.dumps(admission,sort_keys=True)))
                store.db.execute('INSERT INTO backlog_runs VALUES (?,?,?,NULL)',(job_id,binding,'ADMITTED'))
                store.db.execute("UPDATE jobs SET state='MANUAL_BACKLOG' WHERE id=?",(job_id,))
        if not prior and checkpoint:checkpoint('ADMITTED')
        def phase(value,result=None):
            with store.db:store.db.execute('UPDATE backlog_runs SET phase=?,result=? WHERE job=?',(value,json.dumps(result) if result is not None else None,job_id))
            if checkpoint:checkpoint(value)
        def confirm_catalogue():
            current=services.read_arr(plan)
            if current.get('path')!=plan['new_path']:raise ReviewRequired('Catalogue root changed during scan wait')
            for f in plan['files']:
                exact=[row for row in current['files'] if row['path']==f['path'] and set(row['episode_ids'])==set(f['episode_ids'])]
                if len(exact)!=1:raise ReviewRequired('Catalogue association changed during scan wait')
        def confirm_local(rows):
            if prepared_scope and validate_prepared_scope(config,req,source)!=prepared_scope:
                raise ReviewRequired('Prepared scope changed during publication')
            for row in rows:
                src=Path(row['source']);dst=Path(row['destination'])
                importer.require_identity(src,row['identity_seal'])
                if dst.is_symlink() or not dst.is_file() or not os.path.samefile(src,dst):
                    raise ReviewRequired('Linked destination identity changed')
            for path,data in nfos.items():
                p=Path(path)
                if p.is_symlink() or not p.is_file() or p.read_bytes()!=data:
                    raise ReviewRequired('Published NFO identity changed')
        missing_messages={'Jellyfin canonical title missing or ambiguous','Exact Jellyfin file is missing or ambiguous','Jellyfin movie descriptive metadata incomplete'}
        def refresh_once():
            if config.get('targeted_refresh_permitted') is not True:return
            paths=[str(PurePosixPath(config['jellyfin_media_root'])/Path(f['host_path']).relative_to(media)) for f in plan['files']]
            body={'Updates':[{'Path':p,'UpdateType':'Created'} for p in paths]}
            encoded=json.dumps(body,sort_keys=True)
            with store.db:
                store.db.execute('CREATE TABLE IF NOT EXISTS backlog_refresh (job TEXT PRIMARY KEY,binding TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL)')
                old=store.db.execute('SELECT * FROM backlog_refresh WHERE job=?',(job_id,)).fetchone()
                if old:
                    if old['binding']!=binding or old['body']!=encoded:raise ReviewRequired('Targeted refresh intent changed; reconcile')
                    if old['status']=='DENIED':raise ReviewRequired('Targeted refresh denied; no permission escalation or retry')
                    return  # includes unknown/crash after intent: never resubmit
                store.db.execute('INSERT INTO backlog_refresh VALUES (?,?,?,?)',(job_id,binding,encoded,'INTENT'))
            if checkpoint:checkpoint('REFRESH_INTENT')
            try:
                services.jellyfin('/Library/Media/Updated',body,'POST')
            except Exception as error:
                denied=isinstance(error,AdapterError) and error.status in (401,403)
                with store.db:store.db.execute('UPDATE backlog_refresh SET status=? WHERE job=?',('DENIED' if denied else 'UNKNOWN',job_id))
                if denied:raise ReviewRequired('Targeted refresh denied; no permission escalation or retry') from None
                return  # only read-only verification follows uncertain submission
            with store.db:store.db.execute("UPDATE backlog_refresh SET status='ACCEPTED' WHERE job=?",(job_id,))
            if checkpoint:checkpoint('REFRESH_ACCEPTED')
        transient_metadata_messages={'Jellyfin canonical episode order differs','Jellyfin canonical episode provider identity differs'}
        def verify_visible():
            deadline=monotonic()+poll_timeout
            for attempt in range(poll_attempts):
                try:return services.check_jellyfin(req,plan)
                except ReviewRequired as error:
                    if str(error) not in missing_messages|transient_metadata_messages:raise
                    # Metadata may settle while a scan is in flight. Only observe;
                    # never refresh/reimport again because identity is temporarily wrong.
                    if attempt==0 and str(error) in missing_messages:refresh_once()
                    remaining=deadline-monotonic()
                    if attempt+1==poll_attempts or remaining<=0:raise
                    sleep(min(poll_interval,remaining))
                    # Deadline bounds polling starts, not an in-flight HTTP request.
                    if monotonic()>=deadline:raise
        def publish_nfo(path,data):
            p=Path(path);digest=hashlib.sha256(data).hexdigest()
            if p.is_symlink():raise ReviewRequired('NFO path became a symlink')
            existed=p.exists()
            if existed and p.read_bytes()!=data:raise ReviewRequired('NFO changed before publication')
            with store.db:
                store.db.execute('CREATE TABLE IF NOT EXISTS backlog_metadata (job TEXT NOT NULL,path TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(job,path))')
                old=store.db.execute('SELECT * FROM backlog_metadata WHERE job=? AND path=?',(job_id,path)).fetchone()
                if old and old['digest']!=digest:raise ReviewRequired('NFO intent changed; reconcile without overwrite')
                if not old:store.db.execute('INSERT INTO backlog_metadata VALUES (?,?,?,?)',(job_id,path,digest,'EXISTING' if existed else 'INTENT'))
            if existed:
                if old and old['state']=='INTENT':
                    with store.db:store.db.execute("UPDATE backlog_metadata SET state='PUBLISHED' WHERE job=? AND path=?",(job_id,path))
                return
            fd,tmp=tempfile.mkstemp(prefix='.nfo-',dir=p.parent)
            try:
                os.fchmod(fd,0o644)  # New generated NFO only; never chmod source/media hardlinks.
                with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
                os.link(tmp,p)
            finally:os.unlink(tmp)
            with store.db:store.db.execute("UPDATE backlog_metadata SET state='PUBLISHED' WHERE job=? AND path=?",(job_id,path))
        def link_with_nfo(src,dst):
            # All importer probes/policy checks have passed; its link intent is durable.
            # Expose local metadata before media so realtime scans cannot win this race.
            path=str(Path(dst).with_suffix('.nfo'))
            if path not in nfos:raise ReviewRequired('Unscoped media link refused')
            publish_nfo(path,nfos[path])
            if checkpoint:checkpoint('NFO_PUBLISHED')
            os.link(src,dst)
        try:
            if prior and prior['phase']=='WAITING_JELLYFIN':
                rows=json.loads(prior['result'])
                confirm_local(rows)
                verified=verify_visible()
                confirm_catalogue()
                confirm_local(rows)
                result={'files':rows,'jellyfin':verified,'screening':'light','scope_binding':binding}
                store.change(job_id,'READY','Exact backlog catalogue and Jellyfin verified',report=result)
                phase('READY',result)
                return result
            # Refuse foreign NFOs before any media link; never overwrite metadata.
            for path,data in nfos.items():
                p=Path(path)
                if p.is_symlink() or (p.exists() and p.read_bytes()!=data):raise ReviewRequired('Existing NFO differs; reconcile')
            rows=importer.import_job(store,job,source,media,probe_fn=probe_fn,screening='light',policy=policy,selection=job.get('release'),link_fn=link_with_nfo)
            phase('LINKED',rows)
            for path,data in nfos.items():publish_nfo(path,data)
            phase('CATALOGUE_INTENT')
            services.update_arr(plan)
            phase('WAITING_JELLYFIN',rows)
            # Only existing targeted permission permits one durable refresh; otherwise passive scan.
            confirm_local(rows)
            verified=verify_visible()
            confirm_catalogue()
            confirm_local(rows)
            result={'files':rows,'jellyfin':verified,'screening':'light','scope_binding':binding}
            store.change(job_id,'READY','Exact backlog catalogue and Jellyfin verified',report=result)
            phase('READY',result)
            return result
        except Exception as error:
            if isinstance(error,ReviewRequired) and str(error) in missing_messages:
                current=store.db.execute('SELECT phase FROM backlog_runs WHERE job=?',(job_id,)).fetchone()[0]
                if current=='WAITING_JELLYFIN':return {'status':'WAITING_JELLYFIN','job_id':job_id,'pending_reason':str(error)}
            phase('BLOCKED')
            store.change(job_id,'MANUAL_BLOCKED','Partial manual run requires reconciliation')
            raise
