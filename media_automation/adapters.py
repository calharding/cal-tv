from __future__ import annotations

import configparser
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from .core import ReviewRequired, combined_episode_end


class AdapterError(Exception):
    """Messages must never contain credentials, request bodies or URLs."""
    def __init__(self,message,status=None):super().__init__(message);self.status=status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def open_no_redirect(req, timeout=30):
    return urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout)


def fetch(req, limit=8*1024*1024, timeout=30, allowed_https_host=None, no_redirect=False):
    try:
        class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,request,fp,code,msg,headers,newurl):
                p=urllib.parse.urlsplit(newurl)
                if p.scheme!='https' or p.hostname!=allowed_https_host or p.port not in [None,443]:
                    raise AdapterError('Indexer redirect outside approved HTTPS endpoint refused')
                return super().redirect_request(request,fp,code,msg,headers,newurl)
        opener=urllib.request.build_opener(RestrictedRedirect()) if allowed_https_host else None
        response=open_no_redirect(req,timeout) if no_redirect else opener.open(req,timeout=timeout) if opener else urllib.request.urlopen(req,timeout=timeout)
        with response:
            data = response.read(limit+1)
        if len(data) > limit:
            raise AdapterError('Response exceeded size limit')
        return data
    except urllib.error.HTTPError as e:
        status=e.code;e.close()
        raise AdapterError('Remote service request failed (HTTP '+str(status)+')',status=status) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AdapterError('Remote service request failed') from None


class Jellyfin:
    def __init__(self, config):self.config=config

    def library_path(self, path):
        root = Path(self.config['media_root'])
        try:
            relative = Path(path).relative_to(root)
        except ValueError:
            raise ReviewRequired('Media path is outside the configured library root') from None
        if not relative.parts or '..' in relative.parts:
            raise ReviewRequired('Invalid library path')
        container = self.config.get('jellyfin_library_root')
        if not isinstance(container, str) or not container.startswith('/') or '..' in container.split('/'):
            raise ReviewRequired('Explicit Jellyfin library root required')
        return container.rstrip('/') + '/' + relative.as_posix()

    def api(self,path,body=None,method=None,extra_headers=None):
        if not self.config.get('jellyfin_enabled',False):raise ReviewRequired('Jellyfin integration awaits credential scope approval')
        key=Path(self.config['jellyfin_key_file']).read_text().strip()
        from .jellyfin_setup import authorization_header
        headers={'Authorization':authorization_header(key),'Content-Type':'application/json'}
        headers.update(extra_headers or {})
        req=urllib.request.Request(self.config['jellyfin_url']+path,data=None if body is None else json.dumps(body).encode(),headers=headers,method=method)
        raw=fetch(req,limit=2*1024*1024,no_redirect=True)
        return json.loads(raw) if raw else {}

    def refresh(self, paths):
        if self.config.get('targeted_refresh_permitted') is not True:
            raise ReviewRequired('Restricted Jellyfin token cannot trigger targeted refresh; no administrator fallback')
        self.api('/Library/Media/Updated', {'Updates':[{'Path':self.library_path(p),'UpdateType':'Created'} for p in paths]},'POST')

    def owned(self,request):
        name='Tmdb' if request['provider_id'].startswith('tmdb:') else 'Imdb'
        value=request['provider_id'].split(':',1)[1]
        items=self.api('/Items?'+urllib.parse.urlencode({'Recursive':'true','IncludeItemTypes':'Movie' if request['kind']=='movie' else 'Series','AnyProviderIdEquals':name+'.'+value,'Fields':'Path,ProviderIds'})).get('Items',[])
        # Some installed Jellyfin versions ignore AnyProviderIdEquals; verify IDs locally.
        items=[x for x in items if str(x.get('ProviderIds',{}).get(name,''))==value]
        if request['kind']=='movie':return any(x.get('Type')=='Movie' and x.get('Path') for x in items)
        for series in [x for x in items if x.get('Type')=='Series']:
            episodes=self.api('/Items?'+urllib.parse.urlencode({'ParentId':series['Id'],'Recursive':'true','IncludeItemTypes':'Episode'})).get('Items',[])
            for ep in episodes:
                if ep.get('ParentIndexNumber')==request['season'] and ep.get('IndexNumber') is not None:
                    covered=set(range(ep['IndexNumber'],(ep.get('IndexNumberEnd') or ep['IndexNumber'])+1))
                    end=combined_episode_end(request)
                    wanted=set(range(request['episodes'][0],end+1)) if end is not None else set(request['episodes'])
                    if covered & wanted:return True
        return False

    def verify(self,request,paths):
        self.metadata_pending=[]
        from .manual_services import ManualServices
        metadata_rows=[];series_metadata=None
        name='Tmdb' if request['provider_id'].startswith('tmdb:') else 'Imdb'
        value=request['provider_id'].split(':',1)[1]
        query=urllib.parse.urlencode({'Recursive':'true','IncludeItemTypes':'Movie' if request['kind']=='movie' else 'Series','AnyProviderIdEquals':name+'.'+value,'Fields':'Path,ProviderIds'})
        found=self.api('/Items?'+query).get('Items',[])
        found=[x for x in found if str(x.get('ProviderIds',{}).get(name,''))==value]
        paths=list(paths)
        expected={self.library_path(p) for p in paths}
        if not expected or len(expected)!=len(paths):return None
        # For seasons the provider ID belongs to the series, so locate each exact imported path separately.
        if request['kind']=='season' and found:
            series_matches=[x for x in found if x.get('Type')=='Series']
            if len(series_matches)!=1:return None
            series=series_matches[0]
            detail=self.api('/Items/'+series['Id'])
            if (not isinstance(detail,dict) or detail.get('Id')!=series['Id'] or detail.get('Type')!='Series'
                or str(detail.get('ProviderIds',{}).get(name,''))!=value):return None
            genres=detail.get('Genres')
            genres=[x for x in genres if isinstance(x,str) and x.strip()] if isinstance(genres,list) else []
            series_metadata={'genres':genres,'needs_metadata_attention':not bool(genres),
                'verification':'series_genre_presence_not_full_factual_audit'}
            if series:
                found=self.api('/Items?'+urllib.parse.urlencode({'ParentId':series['Id'],'Recursive':'true','IncludeItemTypes':'Episode','Fields':'Path,ProviderIds'})).get('Items',[])
        matched=[x for x in found if x.get('Path') in expected]
        if len(matched)!=len(expected) or {x.get('Path') for x in matched}!=expected:return None
        if request['kind']=='season':
            wanted=set(request['episodes'])
            combined=combined_episode_end(request)
            if combined is not None:wanted=set(range(request['episodes'][0],combined+1))
            covered=set()
            for item in matched:
                first=item.get('IndexNumber');last=item.get('IndexNumberEnd') or first
                if item.get('Type')!='Episode' or item.get('SeriesId')!=series['Id']:return None
                if item.get('ParentIndexNumber')!=request['season'] or type(first) is not int or type(last) is not int or last<first:return None
                nums=set(range(first,last+1))
                if covered & nums:return None
                covered.update(nums)
                # Bind episode identity to its canonical destination, not only batch coverage.
                marker=re.search(r'(?i)S(\d+)E(\d+)(?:-?E(\d+))?',Path(item['Path']).name)
                if not marker or (int(marker[1]),int(marker[2]),int(marker[3] or marker[2]))!=(request['season'],first,last):return None
            if covered!=wanted:return None
        elif any(x.get('Type')!='Movie' for x in matched):return None
        end=combined_episode_end(request)
        if end is not None:
            covered=set()
            for item in matched:
                first=item.get('IndexNumber');last=item.get('IndexNumberEnd') or first
                if item.get('ParentIndexNumber')!=request['season'] or type(first) is not int or type(last) is not int:return None
                covered.update(range(first,last+1))
            if covered!=set(range(request['episodes'][0],end+1)):return None
        reviewed_streams=[]
        for item in matched:
            detail=self.api('/Items/'+item['Id'])
            fields=('Id','Type','Path') if request['kind']=='movie' else ('Id','Type','Path','SeriesId','ParentIndexNumber','IndexNumber','IndexNumberEnd')
            if not isinstance(detail,dict) or any(detail.get(k)!=item.get(k) for k in fields):return None
            ids=detail.get('ProviderIds',{})
            if not isinstance(ids,dict):return None
            normalized={str(k).casefold():str(v) for k,v in ids.items()}
            if len(normalized)!=len(ids):return None
            if request['kind']=='movie':
                if normalized.get(name.casefold())!=value:return None
            else:
                expected_ids=item.get('ProviderIds',{})
                if not isinstance(expected_ids,dict) or any(normalized.get(str(k).casefold())!=str(v) for k,v in expected_ids.items()):return None
            status=ManualServices.movie_metadata_status(detail)
            if request['kind']=='movie':
                if not status['complete']:
                    self.metadata_pending.append({'item_id':item['Id'],'reason':'movie_descriptive_metadata_incomplete','missing':status['missing']})
                    return None
                metadata_rows.append({'item_id':item['Id'],'movie_metadata':status})
            else:
                metadata_rows.append({'item_id':item['Id'],'episode_metadata':{'overview_present':status['fields']['overview'],
                    'overview_release_placeholder':status['overview_release_placeholder'],'lock_data':detail.get('LockData'),
                    'needs_metadata_attention':not status['fields']['overview'],'series_metadata':series_metadata,
                    'verification':'episode_overview_presence_only_not_full_factual_audit'}})
            playback=self.api('/Items/'+item['Id']+'/PlaybackInfo')
            sources=playback.get('MediaSources',[])
            if len(sources)!=1 or sources[0].get('Path')!=item['Path']:return None
            source=sources[0]
            if not source.get('Id'):return None
            subtitle=source.get('DefaultSubtitleStreamIndex')
            streams=source.get('MediaStreams',[])
            from .quality_policy import language
            if subtitle not in (None,-1):
                selected=[v for v in streams if v.get('Type')=='Subtitle' and v.get('Index')==subtitle]
                if len(selected)!=1 or language(selected[0].get('Language'))!='eng':return None
            from .speech_evidence import language_evidence
            from .quality_policy import language,expected_audio
            original_path=next(p for p in paths if self.library_path(p)==item['Path'])
            evidence=language_evidence(Path(original_path),request) or {}
            audio_index=evidence.get('audio_stream_index')
            default_audio=[v for v in streams if v.get('Type')=='Audio' and v.get('Index')==source.get('DefaultAudioStreamIndex')]
            if len(default_audio)!=1:return None
            if audio_index is None and language(default_audio[0].get('Language'))!=expected_audio(request):return None
            if audio_index is not None:
                audio=[v for source in playback['MediaSources'] for v in source.get('MediaStreams',[]) if v.get('Type')=='Audio' and v.get('Index')==audio_index]
                if len(audio)!=1 or source.get('DefaultAudioStreamIndex')!=audio_index:return None
                if any(language(v.get('Language')) not in [None,expected_audio(request)] for v in audio):
                    raise ReviewRequired('Jellyfin audio-track language conflicts with the verified stream')
                reviewed_streams.append({'item_id':item['Id'],'audio_stream_index':audio_index,'actual_language_verified':expected_audio(request)})
            # A bounded stream read verifies server delivery without playing on a user's device.
            key=Path(self.config['jellyfin_key_file']).read_text().strip()
            from .jellyfin_setup import authorization_header
            req=urllib.request.Request(self.config['jellyfin_url']+'/Videos/'+item['Id']+'/stream?'+urllib.parse.urlencode({'static':'true','MediaSourceId':source['Id']}),headers={'Authorization':authorization_header(key),'Range':'bytes=0-65535'})
            try:
                with open_no_redirect(req,timeout=15) as r:
                    delivered=r.read(65536)
                    with open(original_path,'rb') as local:prefix=local.read(65536)
                    if not prefix or delivered!=prefix:return None
            except (urllib.error.URLError,OSError):raise AdapterError('Jellyfin stream verification failed') from None
        return {'items':[{'id':x['Id'],'name':x.get('Name')} for x in matched],
                'server_playback_verified':True,'physical_client_playback_verified':False,'verified_audio_streams':reviewed_streams,'metadata':metadata_rows}
