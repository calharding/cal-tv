from __future__ import annotations

import errno
import json
import os
import subprocess
import time
import stat
import sys
from pathlib import Path
from .core import ReviewRequired, checked_file, safe_component, sha256, combined_episode_end
from .quality_policy import expected_audio, foreign_subtitles_required, language, episode_cap, requested_episode_cap
from .speech_evidence import offline_language_verified,language_evidence,nonverbal_content_verified

VIDEO = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.mpg', '.mpeg', '.ts'}


def probe(path: Path):
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'format=duration:stream=index,codec_type,codec_name,width,height,sample_aspect_ratio,display_aspect_ratio:stream_tags=language:stream_disposition=attached_pic,default,forced',
                            '-of', 'json', str(path)], capture_output=True, timeout=90)
        if r.returncode:
            raise ReviewRequired('ffprobe could not read media')
        info = json.loads(r.stdout)
        # Explicit container cover art is not a motion-video stream.
        info['attached_pictures'] = [s for s in info.get('streams', [])
                                     if s.get('codec_type') == 'video' and s.get('disposition', {}).get('attached_pic') == 1]
        info['streams'] = [s for s in info.get('streams', [])
                           if not (s.get('codec_type') == 'video' and s.get('disposition', {}).get('attached_pic') == 1)]
        return info
    except (subprocess.TimeoutExpired, ValueError):
        raise ReviewRequired('Media probe failed or timed out')


def validate_video(path, info, request, episode=None, decoder=True,policy=None,selection=None):
    if request.get('combined_episode_staging') is not None:
        raise ReviewRequired('Combined episode awaits source-bound measured story-boundary review')
    streams = info.get('streams', [])
    video = [s for s in streams if s.get('codec_type') == 'video']
    audio = [s for s in streams if s.get('codec_type') == 'audio']
    if not video or not audio:
        raise ReviewRequired('Video and audio streams are required')
    duration = float(info.get('format', {}).get('duration', 0))
    combined_episode_end(request)
    if duration < 30:
        raise ReviewRequired('Sample or truncated media')
    height = max(s.get('height', 0) for s in video)
    width = max(s.get('width', 0) for s in video)
    if height<=0 or width<=0:raise ReviewRequired('Actual video resolution is unknown')
    if height > min(request.get('max_height',1080),1080) or width>1920:
        raise ReviewRequired('Resolution exceeds approved profile')
    from .source_resolution import approved_source_resolution
    source_resolution_approved=approved_source_resolution(path,request,episode,width,height)
    from .source_movie_size import approved_existing_movie_size
    movie_size_approved=approved_existing_movie_size(path,request)
    if policy:
        if request['kind']=='movie' and path.stat().st_size>policy['films']['hard_ceiling_bytes'] and not movie_size_approved:
            raise ReviewRequired('Actual film exceeds the 4 decimal GB hard ceiling')
        if request['kind']=='movie' and selection and width<=1280 and height<=720 and not selection.get('policy_assessment',{}).get('film_720p_fallback'):
            raise ReviewRequired('Actual film is 720p or lower without an approved 1080p-unavailability fallback')
        if request['kind']=='season':
            actual_tier=1080 if height>720 or width>1280 else 720
            selected_caps=(selection or {}).get('policy_assessment',{}).get('episode_caps',[])
            actual_cap=requested_episode_cap(duration,actual_tier,policy,request,episode)
            planned_cap=next((x['cap_bytes'] for x in selected_caps if x['episode']==episode),actual_cap)
            if path.stat().st_size>min(planned_cap,actual_cap):raise ReviewRequired('Actual episode exceeds its runtime/resolution cap')
            if (actual_tier>720 and not source_resolution_approved
                and not (selection or {}).get('policy_assessment',{}).get('tv_1080p_fallback')):
                raise ReviewRequired('Actual 1080p TV has no justified lower-resolution-unavailability record')
            if (policy['television'].get('resolution_preference') and actual_tier<=720
                and (height>480 or width>854) and not source_resolution_approved
                and not any((selection or {}).get('policy_assessment',{}).get(key)
                            for key in ['tv_720p_fallback','tv_1080p_fallback'])):
                raise ReviewRequired('Actual 720p-tier TV has no justified 480p-unavailability record')
    languages = sorted({language(s.get('tags', {}).get('language')) or 'und' for s in audio})
    expected = expected_audio(request)
    offline_verified=offline_language_verified(path,request)
    nonverbal_verified=nonverbal_content_verified(path,request,duration)
    if nonverbal_verified and len(audio)!=1:raise ReviewRequired('Nonverbal review needs a single reviewed audio stream')
    reviewed_audio_index=(language_evidence(path,request) or {}).get('audio_stream_index') if offline_verified else None
    if reviewed_audio_index is not None and not any(x.get('index')==reviewed_audio_index for x in audio):
        raise ReviewRequired('Reviewed English audio stream is absent from the completed file')
    if expected not in languages and not offline_verified and not nonverbal_verified:
        raise ReviewRequired('Requested audio language is not tagged; audible review required')
    # Tags are evidence, not proof of the language being spoken. No heavy model is installed.
    if not request.get('audible_language_confirmed', False) and not offline_verified and not nonverbal_verified:
        raise ReviewRequired('Actual spoken language needs explicit sample review')
    from .segment_runtime import reviewed_segment
    segment_review=reviewed_segment(path,request,episode,duration,width,height)
    if episode is not None:
        expected_seconds = float(request['episode_durations'][str(episode)])
        if segment_review is None and abs(duration - expected_seconds) > max(90, expected_seconds * 0.08):
            raise ReviewRequired('Episode duration disagrees with metadata listing')
    if decoder:
        # Preserve original stream indices after filtering attached artwork.
        motion_index = video[0].get('index')
        motion_selector = '0:'+str(motion_index) if type(motion_index) is int else '0:v:0'
        # AVI cannot be safely fast-seeked on every legacy audio stream. A
        # single strict full decode covers the entire file, avoids repeatedly
        # decoding overlapping prefixes, and retains the same error policy.
        positions = [None] if path.suffix.lower() == '.avi' else [0, duration/2, max(0, duration-20)]
        for position in positions:
            try:
                input_seek = [] if position is None else ['-ss', str(position)]
                duration_limit = [] if position is None else ['-t', '10']
                r = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror'] + input_seek +
                                    ['-i', str(path)] + duration_limit + ['-map', motion_selector, '-map', '0:'+str(reviewed_audio_index) if reviewed_audio_index is not None else '0:a:0',
                                    '-f', 'null', '-'], capture_output=True, timeout=300 if path.suffix.lower() == '.avi' else 120)
            except subprocess.TimeoutExpired:
                raise ReviewRequired('Decode sample timed out')
            if r.returncode:
                raise ReviewRequired('A decode sample failed')
    return {'segment_runtime_review':segment_review,'existing_movie_size_exception_verified': movie_size_approved, 'source_resolution_exception_verified': source_resolution_approved, 'duration_seconds': duration, 'height': height, 'audio_languages': languages,
            'width':width,'aspect_ratios':[{'sample':s.get('sample_aspect_ratio'),'display':s.get('display_aspect_ratio')} for s in video],
            'requested_audio_language':expected,'verified_audio_stream_index':reviewed_audio_index,'english_subtitles_required':foreign_subtitles_required(request),
            'subtitle_tracks': [s for s in streams if s.get('codec_type') == 'subtitle'],
            'language_verification': 'source_bound_nonverbal_content_review' if nonverbal_verified else 'offline_multilingual_asr_sample_review' if offline_verified else 'explicit_audible_review', 'decode_samples_passed': decoder, 'decode_scope': 'whole_file' if decoder and path.suffix.lower()=='.avi' else 'samples' if decoder else 'not_run'}


def destination(request, episode=None):
    title = safe_component(request['title'])
    folder = f"{title} ({request['year']})"
    if request['kind'] == 'movie':
        return Path('Movies') / folder / folder
    end=combined_episode_end(request)
    suffix=f'-E{end:02d}' if end is not None else ''
    return Path('Shows') / folder / f"Season {request['season']:02d}" / f"{title} S{request['season']:02d}E{episode:02d}{suffix}"


def inventory(source: Path, request):
    if request.get('combined_episode_staging') is not None:
        raise ReviewRequired('Combined episode awaits source-bound measured story-boundary review')
    if source.is_symlink() or not source.is_dir():
        raise ReviewRequired('Completed job directory is invalid')
    candidates = []
    for base, dirs, files in os.walk(source, followlinks=False):
        if any((Path(base)/d).is_symlink() for d in dirs):
            raise ReviewRequired('Symlink directory in completed job')
        for name in files:
            p = checked_file(source, str((Path(base)/name).relative_to(source)))
            if p.suffix.lower() in VIDEO and 'sample' not in p.stem.lower():
                candidates.append(p)
    if request['kind'] == 'movie':
        if len(candidates) != 1:
            raise ReviewRequired('Expected exactly one movie file; mapping review required')
        return [(candidates[0], None)]
    explicit=request.get('reviewed_episode_file_mappings')
    end=combined_episode_end(request)
    if end is not None and explicit is None:raise ReviewRequired('Combined story requires reviewed source-bound range mapping')
    if explicit is not None:
        if not isinstance(explicit,list) or len(explicit)!=len(request['episodes']):raise ReviewRequired('Reviewed episode mapping must cover the exact request')
        sources={str(p.relative_to(source)):p for p in candidates};mapped={};seen=set()
        for mapping in explicit:
            relative=mapping.get('source');episode=mapping.get('episode')
            if (relative not in sources or relative in seen or type(episode) is not int or episode not in request['episodes'] or episode in mapped
                or mapping.get('season')!=request['season'] or not mapping.get('review_reference')
                or not mapping.get('release_guid') or mapping['release_guid']!=request.get('episode_mapping_release_guid')):
                raise ReviewRequired('Reviewed episode mapping disagrees with file/release/request')
            if end is not None and mapping.get('episode_end')!=end:raise ReviewRequired('Reviewed source mapping must cover both regular episode parts')
            pair=request.get('combined_episode_range') or {}
            if pair.get('schema_version') in (4,5,6):
                current=sources[relative]
                if (str(current)!=pair.get('source_path') or mapping.get('identity_seal')!=pair.get('source_identity_seal')
                    or current.stat().st_size!=pair.get('source_bytes')):
                    raise ReviewRequired('Reviewed combined programme source binding differs')
                require_identity(current,pair['source_identity_seal'])
                if pair.get('schema_version')==6:
                    if mapping.get('sha256')!=pair.get('source_sha256') or sha256(current)!=pair['source_sha256']:
                        raise ReviewRequired('Reviewed programme content hash differs')
                    require_identity(current,pair['source_identity_seal'])
            elif mapping.get('sha256')!=sha256(sources[relative]):raise ReviewRequired('Reviewed episode mapping source changed')
            if pair.get('schema_version') in (2,3) and (mapping.get('sha256')!=pair.get('source_sha256') or sources[relative].stat().st_size!=pair.get('source_bytes')):
                raise ReviewRequired('Measured pair source binding disagrees with the reviewed file')
            mapped[episode]=sources[relative];seen.add(relative)
        if seen!=set(sources) or sorted(mapped)!=request['episodes']:raise ReviewRequired('Reviewed mapping must not omit or add video files')
        return [(mapped[e],e) for e in sorted(mapped)]
    import re
    mapped = {}
    for p in candidates:
        m = re.search(r'(?i)S(\d{1,2})E(\d{1,3})(?!\d)', p.name)
        if (not m or int(m[1]) != request['season']
            or re.match(r'(?i)(?:[-_. ]*(?:S\d{1,2})?E\d+|-\d+)',p.name[m.end():])
            or re.search(r'(?i)(?:^|[._ -])part\d+(?:[._ -]|$)',p.name)):
            raise ReviewRequired('Episode numbering requires manual mapping')
        ep = int(m[2])
        if ep in mapped:
            raise ReviewRequired('Duplicate episode in completed download')
        mapped[ep] = p
    if sorted(mapped) != request['episodes']:
        raise ReviewRequired('Episode gaps, extras or missing files')
    return [(mapped[e], e) for e in sorted(mapped)]


def identity_seal(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise ReviewRequired('Media must be a regular non-symlink file')
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode]


def require_identity(path, expected):
    if identity_seal(path) != expected:
        raise ReviewRequired('Source identity or metadata changed during import')


def _publication_filesystem(path, *, directory):
    """Observe a device and (on Linux) the mount of the same open object."""
    is_expected = stat.S_ISDIR if directory else stat.S_ISREG
    try:
        if not sys.platform.startswith("linux"):
            info = path.lstat()
            if not is_expected(info.st_mode):
                raise ReviewRequired("Unexpected publication filesystem path type")
            return info.st_dev, None

        if not hasattr(os, "O_PATH"):
            raise ReviewRequired("Linux publication mount identity is unavailable")
        fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0))
        try:
            info = os.fstat(fd)
            if not is_expected(info.st_mode):
                raise ReviewRequired("Unexpected publication filesystem path type")
            with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as status:
                ids = [line.partition(":")[2].strip() for line in status if line.startswith("mnt_id:")]
            if len(ids) != 1 or not ids[0].isdecimal():
                raise ReviewRequired("Linux publication mount identity is unavailable")
            return info.st_dev, int(ids[0])
        finally:
            os.close(fd)
    except (OSError, UnicodeError):
        raise ReviewRequired("Could not verify publication filesystem") from None


def require_same_publication_filesystem(source, destination):
    """Check a source against the nearest existing destination directory, without writes."""
    source, destination = Path(source), Path(destination)
    try:
        for parent in destination.parents:
            try:
                parent.lstat()  # A symlink or non-directory is not a missing directory.
            except FileNotFoundError:
                continue
            break
        else:
            raise ReviewRequired("Publication destination has no existing ancestor")
    except OSError:
        raise ReviewRequired("Could not verify publication destination ancestry") from None
    original = _publication_filesystem(source, directory=False)
    target = _publication_filesystem(parent, directory=True)
    if original != target:
        raise ReviewRequired("EXDEV: hardlinks require the same mount; no copy fallback")


def import_job(store, job, source: Path, media: Path, *, probe_fn=probe, decoder=True, link_fn=os.link,policy=None,selection=None,screening="legacy"):
    if screening not in ('legacy', 'light'):
        raise ReviewRequired('Unknown screening mode')
    if screening == 'light':
        decoder = False
    # Separate table: no stat seal is mislabelled as a content hash.
    with store.db:
        store.db.execute('CREATE TABLE IF NOT EXISTS import_identity (job_id TEXT NOT NULL, dst TEXT NOT NULL, seal TEXT NOT NULL, PRIMARY KEY(job_id,dst))')
    request = job['request']; job_id = job['id']
    if media.is_symlink() or not media.is_dir():
        raise ReviewRequired('Library root is invalid')
    rows = []
    sidecars=[p for p in source.rglob('*') if p.is_file() and p.suffix.lower() in {'.srt','.ass','.ssa','.vtt','.sub','.idx'}]
        # Validate every file before publishing the first one.
    for src, ep in inventory(source, request):
        initial_seal = identity_seal(src)
        raw_probe=probe_fn(src)
        info = validate_video(src, raw_probe, request, ep, decoder=decoder,policy=policy,selection=selection)
        require_identity(src, initial_seal)
        info['screening'] = screening
        bound_evidence = any(request.get(k) for k in ('reviewed_episode_file_mappings', 'offline_language_verification', 'offline_language_verification_by_sha256', 'nonverbal_content_review'))
        info['content_hash_performed'] = True if screening != 'light' else None if bound_evidence else False
        info['content_hash_policy'] = 'legacy_full_hash' if screening != 'light' else 'source_bound_exception_checks_retained' if bound_evidence else 'no_media_hash'
        if screening == 'light':
            audio = [x for x in raw_probe.get('streams', []) if x.get('codec_type') == 'audio']
            defaults = [x for x in audio if x.get('disposition', {}).get('default') == 1]
            chosen = defaults[0] if len(defaults) == 1 else audio[0] if not defaults and len(audio) == 1 else None
            if chosen is None:
                raise ReviewRequired('Ambiguous default audio needs review')
            verified_index = info.get('verified_audio_stream_index')
            if not (language(chosen.get('tags', {}).get('language')) == info['requested_audio_language']
                    or (verified_index is not None and chosen.get('index') == verified_index)
                    or info.get('language_verification') == 'source_bound_nonverbal_content_review'):
                raise ReviewRequired('Default audio is not the reviewed requested language')
            for stream in raw_probe.get('streams', []):
                if (stream.get('codec_type') == 'subtitle'
                    and language(stream.get('tags', {}).get('language')) != 'eng'
                    and any(stream.get('disposition', {}).get(k) == 1 for k in ('default', 'forced'))):
                    raise ReviewRequired('Non-English subtitle enabled by default/forced')
        relative=str(src.relative_to(source))
        embedded=[]
        for review in request.get('subtitle_content_reviews',[]):
            if review.get('video')!=relative or language(review.get('language'))!='eng' or not review.get('review_approval') or review.get('usable') is not True or review.get('matching') is not True:continue
            if any(s.get('codec_type')=='subtitle' and s.get('index')==review.get('stream_index') for s in raw_probe.get('streams',[])):embedded.append(review)
        info['verified_english_subtitle_sources']=['embedded:'+str(r['stream_index']) for r in embedded]
        if sidecars and not request.get('subtitle_mappings'):info['omitted_unreviewed_sidecars']=[str(p.relative_to(source)) for p in sidecars]
        dst = media / (str(destination(request, ep)) + src.suffix.lower())
        current=media
        for part in dst.relative_to(media).parts:
            current=current/part
            if current.is_symlink():raise ReviewRequired('Symlink in destination path')
        if not dst.resolve().is_relative_to(media.resolve()):
            raise ReviewRequired('Destination escapes library')
        digest = sha256(src) if screening == 'legacy' else ''
        require_identity(src, initial_seal)
        prior = store.db.execute('SELECT * FROM imports WHERE dst=?', (str(dst),)).fetchone()
        saved_seal = store.db.execute('SELECT seal FROM import_identity WHERE job_id=? AND dst=?', (job_id, str(dst))).fetchone()
        if prior and screening == 'light':
            if not (prior['job_id'] == job_id and prior['src'] == str(src)
                    and prior['state'] in ('INTENT', 'LINKED')
                    and (prior['device'], prior['inode']) == tuple(initial_seal[:2])
                    and saved_seal and json.loads(saved_seal['seal']) == initial_seal):
                raise ReviewRequired('Prior import intent differs; reconcile before retry')
            if not dst.exists() and prior['state'] != 'INTENT':
                raise ReviewRequired('Previously linked destination is missing; reconcile')
        if dst.exists() or dst.is_symlink():
            if (not dst.is_symlink() and prior and prior['job_id'] == job_id
                and prior['src'] == str(src) and prior['state'] in ('INTENT', 'LINKED')
                and (prior['device'], prior['inode']) == tuple(initial_seal[:2])
                and os.path.samefile(src, dst) and digest == prior['sha256']
                and (screening == 'legacy' or (saved_seal and json.loads(saved_seal['seal']) == initial_seal))):
                rows.append({'source': str(src), 'destination': str(dst), 'sha256': digest, 'identity_seal': initial_seal, 'probe': info, 'recovered': True})
                continue
            raise ReviewRequired('Library destination already exists; never overwrite')
        require_same_publication_filesystem(src, dst)
        rows.append({'source': str(src), 'destination': str(dst), 'sha256': digest, 'identity_seal': initial_seal, 'probe': info, 'recovered': False})
    if sidecars and request.get('subtitle_mappings'):
        import re
        mapped_sources=set(); mapped_videos=set()
        video_rows={str(Path(r['source']).relative_to(source)):r for r in rows}
        for mapping in request.get('subtitle_mappings',[]):
            src=checked_file(source,mapping['source'])
            subtitle_seal=identity_seal(src)
            if src.suffix.lower()!='.srt' or not mapping.get('review_approval'):
                raise ReviewRequired('Only content-reviewed SRT subtitles can be imported')
            if src.stat().st_size>5*1024*1024:raise ReviewRequired('Subtitle exceeds review size limit')
            try:text=src.read_text(encoding='utf-8-sig')
            except UnicodeError:raise ReviewRequired('Subtitle encoding needs review')
            if '-->' not in text:raise ReviewRequired('Subtitle content is not recognizable SRT')
            subtitle_language=mapping['language'];subtitle_language='hin' if subtitle_language=='hi' else subtitle_language
            if not re.fullmatch(r'[a-z]{2,3}(?:-[A-Z]{2})?',subtitle_language):raise ReviewRequired('Invalid subtitle language code')
            video=video_rows.get(mapping['video'])
            if not video:raise ReviewRequired('Subtitle is not mapped to a planned video')
            dst=Path(video['destination']).with_suffix('.'+subtitle_language+'.srt')
            digest=sha256(src);require_identity(src, subtitle_seal);prior=store.db.execute('SELECT * FROM imports WHERE dst=?',(str(dst),)).fetchone()
            recovered=dst.exists() and prior is not None and prior['job_id']==job_id and os.path.samefile(src,dst) and prior['sha256']==digest
            if dst.is_symlink() or (dst.exists() and not recovered):raise ReviewRequired('Subtitle destination already exists')
            if not recovered:
                require_same_publication_filesystem(src, dst)
            if any(r['destination']==str(dst) for r in rows):raise ReviewRequired('Duplicate subtitle destination')
            rows.append({'source':str(src),'destination':str(dst),'sha256':digest,'identity_seal':subtitle_seal,'probe':{'subtitle_language':subtitle_language,'content_review':mapping['review_approval']},'recovered':recovered})
            if subtitle_language in ['en','eng'] and mapping.get('usable') is True and mapping.get('matching') is True:
                video['probe']['verified_english_subtitle_sources'].append('sidecar:'+str(src.relative_to(source)))
            mapped_sources.add(src)
            mapped_videos.add(mapping['video'])
        if {p for p in sidecars if p.suffix.lower()=='.srt'}-mapped_sources:
            raise ReviewRequired('Some SRT subtitles lack a reviewed mapping')
        # Alternative formats are omitted only when a reviewed SRT exists for that video.
        for p in sidecars:
            if p.suffix.lower()!='.srt' and not any(str(p.relative_to(source)).startswith(v.rsplit('.',1)[0]) for v in mapped_videos):
                raise ReviewRequired('Unmatched alternative subtitle format needs review')
    for row in rows:
        if row['probe'].get('english_subtitles_required') and not row['probe'].get('verified_english_subtitle_sources'):
            raise ReviewRequired('Foreign-language film lacks verified usable matching English subtitles; tags/filenames alone are insufficient')
    for row in rows:
        src, dst = Path(row['source']), Path(row['destination'])
        if row['recovered']:
            require_identity(src, row['identity_seal'])
            if dst.is_symlink() or not dst.exists() or not os.path.samefile(src, dst):
                raise ReviewRequired('Recovered destination identity changed')
            with store.db:
                store.db.execute('UPDATE imports SET state=? WHERE job_id=? AND dst=?',('LINKED',job_id,str(dst)))
            continue
        seal = row['identity_seal']
        require_identity(src, seal)
        require_same_publication_filesystem(src, dst)
        dst.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        st = src.stat()
        # Journal intent before link(). A crash after link is reconciled by inode/hash.
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO import_identity VALUES(?,?,?)', (job_id, str(dst), json.dumps(seal)))
            store.db.execute('INSERT INTO imports VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id,dst) DO UPDATE SET state=excluded.state',
                             (job_id, str(src), str(dst), row['sha256'], st.st_dev, st.st_ino, 'INTENT'))
        try:
            # The callback can publish metadata as well as the link. Detect a
            # changed mount before entering either publication path.
            require_same_publication_filesystem(src, dst)
            link_fn(src, dst)
        except OSError as e:
            if e.errno == errno.EXDEV:
                raise ReviewRequired('EXDEV: hardlink failed; no copy fallback')
            raise
        # Never chmod a hardlink: it changes the original inode too.
        require_identity(src, seal)
        if not os.path.samefile(src, dst):
            raise ReviewRequired('Hardlink verification failed')
        with store.db:
            store.db.execute('UPDATE imports SET state=? WHERE job_id=? AND dst=?', ('LINKED', job_id, str(dst)))
    logs = media / '.tidy-logs'
    logs.mkdir(mode=0o755, exist_ok=True)
    record = logs / ('automation-' + job_id)
    record.mkdir(mode=0o755, exist_ok=True)
    temp = record/'provenance.json.tmp'
    temp.write_text(json.dumps({'job_id': job_id, 'provider_id': request['provider_id'],
                                'metadata_reference': request.get('metadata_reference'), 'files': rows}, indent=2))
    os.replace(temp, record/'provenance.json')
    marker = f'job={job_id}'
    manifest = logs/'manifest.tsv'
    old = manifest.read_text() if manifest.exists() else ''
    if marker not in old:
        with manifest.open('a') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M')+'\t'+str(source)+'\t'+str(Path(rows[0]['destination']).parent)+'\t'+marker+'\n')
            f.flush(); os.fsync(f.fileno())
    return rows
