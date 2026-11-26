"""Inactive service adapters for explicit reviewed handoff plans.

No acquisition methods or scheduler. Writes require an explicit switch and a
caller-supplied approval callback binding the plan to its import journal.
"""
import copy
import contextlib
import sqlite3
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from .core import ReviewRequired, combined_episode_end


class LocalArr:
    """Transport is intentionally not configured in the portable baseline."""
    def __init__(self, app):
        raise ReviewRequired("Explicit site transport is not bundled")


def provider_matches(item, provider):
    name, value = provider.split(':', 1)
    return str(item.get({'tmdb': 'tmdbId', 'imdb': 'imdbId'}[name], '')) == value


def canonical_episode_ids(file):
    # Extra provider IDs need a reviewed cross-reference to the canonical episode.
    ids=file.get('canonical_provider_ids')
    allowed={'Tvdb','Imdb','Tmdb','TvRage'}
    if not isinstance(ids,dict) or 'Tvdb' not in ids or not set(ids)<=allowed:
        raise ReviewRequired('Explicit canonical episode provider identities required')
    for key,value in ids.items():
        valid=isinstance(value,str) and (bool(re.fullmatch(r'tt[0-9]+',value)) if key=='Imdb' else value.isdigit() and int(value)>0)
        if not valid:raise ReviewRequired('Invalid canonical episode provider identity')
        if key!='Tvdb':
            proof=file.get('canonical_provider_evidence',{}).get(key,{})
            url=urllib.parse.urlsplit(proof.get('source_url',''))
            if (proof.get('tvdb_id')!=ids['Tvdb'] or proof.get('value')!=value or proof.get('title')!=file.get('canonical_title')
                    or not proof.get('evidence_reference') or url.scheme!='https'
                    or url.hostname not in {'thetvdb.com','www.thetvdb.com','imdb.com','www.imdb.com','themoviedb.org','www.themoviedb.org','tvrage.com','www.tvrage.com'}):
                raise ReviewRequired('Additional episode ID lacks reviewed canonical-source evidence')
    return ids


def canonical_plan_episodes(request,file):
    """Exact approved pair only; secondary identity verified in Arr, not invented in JF."""
    # The public validator refuses combined cases; ordinary TV files bind one slot.
    end=combined_episode_end(request)
    if end is None:
        if file.get('canonical_episode_end') is not None or file.get('canonical_episode_parts') is not None:
            raise ReviewRequired('Unrequested combined plan')
        return [dict(episode=file['canonical_episode'],title=file['canonical_title'],
                     episode_id=file['episode_ids'][0],tvdb=canonical_episode_ids(file)['Tvdb'])]
    raise ReviewRequired("Combined episode not supported by the portable baseline")


class ManualServices:
    def __init__(self, config, *, arr, sab, jellyfin, stream,
                 approved_plan=None, sleep=time.sleep):
        self.config = config
        self.arr = arr  # dict app -> (method,path,body) transport
        self.sab = sab
        self.jellyfin = jellyfin  # restricted existing JSON API callback
        self.stream = stream  # bounded authenticated stream callback(item_id,limit)
        self.approved_plan = approved_plan
        self.sleep = sleep

    def read_sab(self, download_id):
        slots = self.sab.history().get('slots', [])
        matches = [x for x in slots if x.get('nzo_id') == download_id]
        if len(matches) != 1: raise ReviewRequired('Exact SAB completion is missing or ambiguous')
        row = matches[0]
        if row.get('cat') and row.get('category') and row['cat'] != row['category']:
            raise ReviewRequired('SAB category fields disagree')
        return {k: row.get(k) for k in ('nzo_id', 'status', 'storage', 'fail_message')} | {
            'cat': row.get('category') or row.get('cat'),
            'repair_warning': any(re.search(r'(?i)\b(error|failed|corrupt|damaged|repairing)\b|\brepaired [1-9][0-9]*',
                ' '.join(str(x) for x in stage.get('actions', []))[:100000])
                for stage in row.get('stage_log', []) if isinstance(stage, dict)
                and 'repair' in str(stage.get('name', '')).lower())}

    def _check_plan(self, plan):
        if plan.get('app') not in self.arr or type(plan.get('item_id')) is not int or plan['item_id'] <= 0:
            raise ReviewRequired('Invalid explicit Arr target')
        if plan.get('move_files') is not False or not plan.get('files'):
            raise ReviewRequired('Only existing-file associations with moveFiles=false are supported')
        root_value = self.config.get('arr_media_root')
        if not isinstance(root_value, str) or not root_value.startswith('/') or '..' in root_value.split('/'):
            raise ReviewRequired('Explicit catalogue media root required')
        root = PurePosixPath(root_value)
        host = Path(self.config['media_root']).resolve()
        parent = PurePosixPath(plan['new_path'])
        if '..' in parent.parts or not parent.is_relative_to(root) or parent == root:
            raise ReviewRequired('Catalogue path is outside the protected media root')
        seen = set(); episode_ids = []
        for f in plan['files']:
            ids = f.get('episode_ids')
            if not isinstance(ids,list) or any(type(x) is not int or x <= 0 for x in ids):
                raise ReviewRequired('Invalid explicit episode IDs')
            if (plan['app']=='sonarr' and not ids) or (plan['app']=='radarr' and ids):
                raise ReviewRequired('Episode identities do not match catalogue kind')
            p = PurePosixPath(f['path'])
            if '..' in p.parts or not p.is_relative_to(parent) or str(p) in seen:
                raise ReviewRequired('Invalid or duplicate existing media path')
            if Path(f['host_path']) != host / p.relative_to(root):
                raise ReviewRequired('Host and container file paths disagree')
            seen.add(str(p)); episode_ids += f['episode_ids']
        if len(episode_ids) != len(set(episode_ids)):
            raise ReviewRequired('Duplicate Arr episode identities in plan')
        return parent

    def _item(self, plan):
        kind = 'movie' if plan['app'] == 'radarr' else 'series'
        item = self.arr[plan['app']]('GET', '/%s/%s' % (kind, plan['item_id']))
        if item.get('id') != plan['item_id'] or not provider_matches(item, plan['provider_id']):
            raise ReviewRequired('Arr canonical identity differs from reviewed plan')
        return item

    def read_arr(self, plan):
        self._check_plan(plan)
        item = self._item(plan); api = self.arr[plan['app']]
        if plan['app'] == 'radarr':
            files = api('GET', '/moviefile?movieId=%s' % plan['item_id'])
            rows = [dict(path=f.get('path'), episode_ids=[]) for f in files]
        else:
            files = api('GET', '/episodefile?seriesId=%s' % plan['item_id'])
            episodes = api('GET', '/episode?seriesId=%s' % plan['item_id'])
            rows = [dict(path=f.get('path'), episode_ids=sorted(e['id'] for e in episodes
                if e.get('episodeFileId') == f['id'])) for f in files]
        return dict(id=item['id'], provider_id=plan['provider_id'], path=item['path'], files=rows)

    def _wait(self, api, command):
        if type(command.get('id')) is not int: raise ReviewRequired('Arr command receipt missing')
        for _ in range(60):
            status = api('GET', '/command/%s' % command['id']).get('status', '').lower()
            if status == 'completed': return
            if status in ('failed', 'aborted', 'cancelled'): break
            self.sleep(0.5)
        raise ReviewRequired('Arr association command did not complete; reconcile before retry')

    def update_arr(self, plan):
        self._check_plan(plan)
        if (self.config.get('manual_service_writes_enabled') is not True
                or self.approved_plan is None or self.approved_plan(copy.deepcopy(plan)) is not True):
            raise ReviewRequired('Live catalogue writes are disabled or lack journal-bound approval')
        api = self.arr[plan['app']]; item = self._item(plan)
        current = self.read_arr(plan)
        for desired in plan['files']:
            for old in current['files']:
                overlap = plan['app'] == 'radarr' or set(old['episode_ids']) & set(desired['episode_ids'])
                if overlap and old['path'] != desired['path']:
                    raise ReviewRequired('Existing media association would be displaced')
        # Arr existing-file branch is selected by parent path, not importMode alone.
        if item['path'] != plan['new_path']:
            if any(not PurePosixPath(f['path']).is_relative_to(PurePosixPath(plan['new_path']))
                   for f in current['files']):
                raise ReviewRequired('Changing catalogue root would strand existing media')
            item = copy.deepcopy(item); item['path'] = plan['new_path']; item['monitored'] = False
            kind = 'movie' if plan['app'] == 'radarr' else 'series'
            api('PUT', '/%s/%s?moveFiles=false' % (kind, item['id']), item)
        checked = self._item(plan)
        if checked['path'] != plan['new_path']: raise ReviewRequired('Arr path update not confirmed')
        payload = []
        for f in plan['files']:
            current = self.read_arr(plan)
            exact = [x for x in current['files'] if x['path'] == f['path']
                     and set(x['episode_ids']) == set(f['episode_ids'])]
            if len(exact) == 1: continue
            # Obtain quality/language model via the supported inspection endpoint;
            # never trust its inferred catalogue episode identities.
            query = urllib.parse.urlencode(dict(folder=f['path'], filterExistingFiles='false'))
            candidates = api('GET', '/manualimport?' + query)
            rows = [x for x in candidates if x.get('path') == f['path']]
            if len(rows) != 1 or not rows[0].get('quality'):
                raise ReviewRequired('Existing-file quality inspection is unavailable')
            row = rows[0]
            p = dict(path=f['path'], quality=f.get('quality', row['quality']),
                     languages=f.get('languages', row.get('languages', [])),
                     releaseGroup=row.get('releaseGroup') or '', indexerFlags=row.get('indexerFlags', 0))
            if plan['app'] == 'sonarr':
                if not f['episode_ids']: raise ReviewRequired('Explicit TV episode IDs are required')
                episodes = api('GET', '/episode?seriesId=%s' % plan['item_id'])
                if not set(f['episode_ids']).issubset({e['id'] for e in episodes}):
                    raise ReviewRequired('Reviewed episode belongs to another series')
                p.update(seriesId=plan['item_id'], episodeIds=f['episode_ids'],
                         releaseType=row.get('releaseType', 'singleEpisode'))
            else: p['movieId'] = plan['item_id']
            # Omit downloadId: linking a tracked download chooses the moving branch
            # and may clean its folder. Source retention is mandatory here.
            payload.append(p)
        if payload:
            # Reconcile an accepted command after interruption before issuing another.
            desired_paths = {x['path'] for x in payload}
            pending = []
            for command in api('GET', '/command'):
                body = command.get('body') or {}
                if str(command.get('name', body.get('name', ''))).lower() != 'manualimport': continue
                old_files = body.get('files') or []
                if not desired_paths.intersection(x.get('path') for x in old_files): continue
                status = command.get('status', '').lower()
                if status in ('queued', 'started'):
                    if old_files != payload:
                        raise ReviewRequired('Overlapping pending catalogue command needs reconciliation')
                    pending.append(command)
            if len(pending) > 1: raise ReviewRequired('Duplicate pending catalogue commands found')
            body = dict(name='ManualImport', files=payload, importMode='copy')
            # Persist BEFORE the network call. A lost reply is not permission to retry.
            journal = self.config.get('catalogue_intent_db')
            if not journal:
                raise ReviewRequired('Durable catalogue intent database is required')
            token = hashlib.sha256(json.dumps([plan['app'], plan['item_id'], sorted(
                (f['path'], sorted(f['episode_ids'])) for f in plan['files'])],
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            with contextlib.closing(sqlite3.connect(journal, timeout=30)) as db:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('CREATE TABLE IF NOT EXISTS catalogue_intent '
                           '(token TEXT PRIMARY KEY, receipt TEXT)')
                db.execute('BEGIN IMMEDIATE')
                prior = db.execute('SELECT receipt FROM catalogue_intent WHERE token=?', (token,)).fetchone()
                if prior is None:
                    db.execute('INSERT INTO catalogue_intent VALUES (?,NULL)', (token,))
                db.commit()
                if pending:
                    command = pending[0]
                elif prior is not None:
                    if prior[0] is None:
                        raise ReviewRequired('Catalogue submission outcome unknown; reconcile without resubmitting')
                    command = json.loads(prior[0])
                else:
                    command = api('POST', '/command', body)
                if type(command.get('id')) is not int:
                    raise ReviewRequired('Catalogue receipt uncertain; do not resubmit')
                db.execute('UPDATE catalogue_intent SET receipt=? WHERE token=?',
                           (json.dumps({'id': command['id']}), token))
                db.commit()
            self._wait(api, command)
        final = self.read_arr(plan)
        for f in plan['files']:
            if len([x for x in final['files'] if x['path'] == f['path']
                    and set(x['episode_ids']) == set(f['episode_ids'])]) != 1:
                raise ReviewRequired('Arr independent association read-back failed')

    @staticmethod
    def movie_metadata_status(item):
        """Cheap descriptive-field presence, not factual certification of every credit."""
        valid_text=lambda value:isinstance(value,str) and bool(value.strip())
        import re
        overview=item.get('Overview')
        # Some MP4 tags contain the release filename, not a plot synopsis.
        # Only reject unmistakable compact release-name patterns, not prose
        # that happens to discuss cameras, resolution or a Blu-ray edition.
        release_placeholder=isinstance(overview,str) and len(overview)<300 and not any(c.isspace() for c in overview) and (
            bool(re.search(r'(?i)\.(?:mkv|mp4|avi|m4v)$',overview)) or
            bool(re.search(r'(?i)(?:^|[._-])(?:480p|576p|720p|1080p|2160p)(?:[._-]|$)',overview)) and
            bool(re.search(r'(?i)(?:^|[._-])(?:bluray|brrip|bdrip|webrip|web-dl|h264|h265|x264|x265|hevc|aac)(?:[._-]|$)',overview)))
        fields={
            'overview':valid_text(overview) and not release_placeholder,
            'genres':isinstance(item.get('Genres'),list) and any(valid_text(x) for x in item['Genres']),
            'poster':isinstance(item.get('ImageTags'),dict) and valid_text(item['ImageTags'].get('Primary')),
            'cast':isinstance(item.get('People'),list) and any(isinstance(x,dict) and x.get('Type')=='Actor' and valid_text(x.get('Name')) for x in item['People']),
        }
        return {'complete':all(fields.values()),'fields':fields,'missing':[k for k,v in fields.items() if not v],
                'overview_release_placeholder':release_placeholder,'lock_data':item.get('LockData'),'verification':'descriptive_fields_present_not_full_factual_audit'}

    def check_jellyfin(self, request, plan):
        self._check_plan(plan)
        key, value = request['provider_id'].split(':', 1)
        key = {'tmdb': 'Tmdb', 'imdb': 'Imdb'}[key]
        query = urllib.parse.urlencode(dict(Recursive='true',
            IncludeItemTypes='Movie' if request['kind'] == 'movie' else 'Series',
            AnyProviderIdEquals=key + '.' + value, Fields='Path,ProviderIds'))
        items = self.jellyfin('/Items?' + query).get('Items', [])
        items = [x for x in items if str(x.get('ProviderIds', {}).get(key)) == value]
        if len(items) != 1: raise ReviewRequired('Jellyfin canonical title missing or ambiguous')
        series_id = items[0]['Id'] if request['kind'] == 'season' else None
        series_metadata=None
        if request['kind'] == 'season':
            series_detail=self.jellyfin('/Items/'+series_id)
            if (not isinstance(series_detail,dict) or series_detail.get('Id')!=series_id
                or series_detail.get('Type')!='Series' or str(series_detail.get('ProviderIds',{}).get(key))!=value):
                raise ReviewRequired('Jellyfin series detail identity differs')
            genres=series_detail.get('Genres')
            genres=[x for x in genres if isinstance(x,str) and x.strip()] if isinstance(genres,list) else []
            series_metadata={'genres':genres,'needs_metadata_attention':not bool(genres),
                'verification':'series_genre_presence_not_full_factual_audit'}
            query = urllib.parse.urlencode(dict(ParentId=items[0]['Id'], Recursive='true',
                IncludeItemTypes='Episode', Fields='Path,ProviderIds'))
            items = self.jellyfin('/Items?' + query).get('Items', [])
        host = Path(self.config['media_root']).resolve()
        jf_root = PurePosixPath(self.config['jellyfin_media_root'])
        rows = []
        for f in plan['files']:
            path = str(jf_root / Path(f['host_path']).relative_to(host))
            exact = [x for x in items if x.get('Path') == path]
            if len(exact) != 1: raise ReviewRequired('Exact Jellyfin file is missing or ambiguous')
            item = exact[0]
            if item.get('Type') != ('Episode' if series_id else 'Movie'):
                raise ReviewRequired('Jellyfin item type differs')
            if series_id and item.get('SeriesId') != series_id:
                raise ReviewRequired('Jellyfin parent series differs')
            expected_end=combined_episode_end(request) if request['kind']=='season' else None
            if expected_end is not None:canonical_plan_episodes(request,f)
            if request['kind'] == 'season' and (item.get('ParentIndexNumber') != request['season']
                    or item.get('IndexNumber') != f['canonical_episode']
                    or (item.get('IndexNumberEnd') != expected_end if expected_end is not None else item.get('IndexNumberEnd') not in (None, f['canonical_episode']))):
                raise ReviewRequired('Jellyfin canonical episode order differs')
            if request['kind']=='season':
                expected_ids=canonical_episode_ids(f)
                actual_ids=item.get('ProviderIds',{})
                if not isinstance(actual_ids,dict):raise ReviewRequired('Jellyfin provider identities are malformed')
                normalized={str(key).casefold():str(value) for key,value in actual_ids.items()}
                if len(normalized)!=len(actual_ids) or any(normalized.get(key.casefold())!=value for key,value in expected_ids.items()):
                    raise ReviewRequired('Jellyfin canonical episode provider identity differs')
                # Extra IDs are not asserted correct and are not grounds for a false hold.
                # A known conflict is rejected above whenever canonical evidence specifies that provider.
                unverified_ids={key:value for key,value in actual_ids.items() if str(key).casefold() not in {k.casefold() for k in expected_ids}}

            episode_metadata=None
            if request['kind']=='season':
                detail=self.jellyfin('/Items/'+item['Id'])
                identity_fields=('Id','Type','Path','SeriesId','ParentIndexNumber','IndexNumber','IndexNumberEnd')
                if not isinstance(detail,dict) or any(detail.get(k)!=item.get(k) for k in identity_fields):
                    raise ReviewRequired('Jellyfin episode detail identity differs')
                detail_ids=detail.get('ProviderIds',{})
                normalized_detail={str(k).casefold():str(v) for k,v in detail_ids.items()} if isinstance(detail_ids,dict) else {}
                if (not isinstance(detail_ids,dict) or len(normalized_detail)!=len(detail_ids)
                    or any(normalized_detail.get(k.casefold())!=v for k,v in expected_ids.items())):
                    raise ReviewRequired('Jellyfin canonical episode provider identity differs')
                status=self.movie_metadata_status(detail)
                episode_metadata={'overview_present':status['fields']['overview'],
                    'overview_release_placeholder':status['overview_release_placeholder'],
                    'lock_data':detail.get('LockData'),
                    'needs_metadata_attention':not status['fields']['overview'],
                    'verification':'episode_overview_presence_only_not_full_factual_audit',
                    'series_metadata':series_metadata}
                # Older episodes may legitimately have sparse descriptions. Report,
                # do not hold playable exact-identity media or impose movie cast/art.

            movie_metadata=None
            if request['kind']=='movie':
                # Provider/path checks above remain mandatory; never equate visibility
                # with a fully presented library entry. Missing fields may still settle.
                detail=self.jellyfin('/Items/'+item['Id'])
                if (not isinstance(detail,dict) or detail.get('Id')!=item['Id'] or detail.get('Type')!='Movie'
                    or detail.get('Path')!=path or str(detail.get('ProviderIds',{}).get(key))!=value):
                    raise ReviewRequired('Jellyfin movie detail identity differs')
                movie_metadata=self.movie_metadata_status(detail)
                if not movie_metadata['complete']:
                    raise ReviewRequired('Jellyfin movie descriptive metadata incomplete')

            playback = self.jellyfin('/Items/%s/PlaybackInfo' % item['Id'])
            sources = playback.get('MediaSources', [])
            if len(sources) != 1 or sources[0].get('Path') != path:
                raise ReviewRequired('Jellyfin playback source differs from exact imported file')
            source = sources[0]
            from .quality_policy import language, expected_audio
            from .speech_evidence import language_evidence
            streams = source.get('MediaStreams', [])
            audio = [x for x in streams if x.get('Type') == 'Audio' and x.get('Index') == source.get('DefaultAudioStreamIndex')]
            evidence = language_evidence(Path(f['host_path']), request) or {}
            verified_index = evidence.get('audio_stream_index')
            if len(audio) != 1 or (verified_index is not None and audio[0].get('Index') != verified_index):
                raise ReviewRequired('Jellyfin default audio differs from verified track')
            if language(audio[0].get('Language')) != expected_audio(request) and not (verified_index is not None and language(audio[0].get('Language')) is None):
                raise ReviewRequired('Jellyfin default audio language differs')
            subtitle_index = source.get('DefaultSubtitleStreamIndex')
            if subtitle_index not in (None, -1):
                subtitles = [x for x in streams if x.get('Type') == 'Subtitle' and x.get('Index') == subtitle_index]
                if len(subtitles) != 1 or language(subtitles[0].get('Language')) != 'eng':
                    raise ReviewRequired('Non-English or unknown subtitle enabled by default')
            # PlaybackInfo identifies the source; a bounded read tests actual server delivery.
            data = self.stream(item['Id'], 65536)
            if not isinstance(data, bytes) or not data or len(data) > 65536:
                raise ReviewRequired('Jellyfin bounded stream delivery failed')
            with open(f['host_path'], 'rb') as local:
                if data != local.read(65536):
                    raise ReviewRequired('Jellyfin served bytes differ from imported file')
            rows.append(dict(path=f['host_path'], item_id=item['Id'], provider_id=request['provider_id'], movie_metadata=movie_metadata, episode_metadata=episode_metadata,
                canonical_season=request.get('season'), canonical_episode=f['canonical_episode'],
                canonical_episode_end=expected_end,
                episode_provider_ids=canonical_episode_ids(f) if request['kind']=='season' else None,
                unverified_episode_provider_ids=unverified_ids if request['kind']=='season' else {},
                stream_bytes=len(data), stream_sha256=hashlib.sha256(data).hexdigest()))
        return rows


class JellyfinStream:
    """Use only the previously approved server and restricted token file."""
    def __init__(self, config): self.config = config

    def __call__(self, item_id, limit):
        if not str(item_id).isalnum() or limit != 65536:
            raise ReviewRequired('Invalid bounded Jellyfin stream request')
        from .jellyfin_setup import authorization_header
        key = Path(self.config['jellyfin_key_file']).read_text().strip()
        request = urllib.request.Request(self.config['jellyfin_url'] + '/Videos/' + item_id + '/stream?static=true',
            headers={'Authorization': authorization_header(key), 'Range': 'bytes=0-65535'})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
                data = response.read(limit)
                if not data: raise ValueError()
                return data
        except Exception:
            raise ReviewRequired('Jellyfin bounded stream failed; private details suppressed') from None
