"""Versioned user preferences, enforced before selection and acquisition."""
from __future__ import annotations
import hashlib
import json
import math
import time
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from .core import ReviewRequired


def load_policy(config):
    # A version label is for humans; the digest pins the actual rules being applied.
    path=config.get('quality_policy_path')
    if not path:raise ReviewRequired('A versioned quality/size policy is required')
    try:
        raw=Path(path).read_bytes();policy=json.loads(raw)
    except (OSError,ValueError):raise ReviewRequired('Quality/size policy could not be loaded') from None
    digest=hashlib.sha256(raw).hexdigest()
    if policy.get('version')!=config.get('quality_policy_version') or digest!=config.get('quality_policy_sha256'):
        raise ReviewRequired('Quality/size policy version or checksum changed; review before selection')
    return policy,digest


def number(value):
    return type(value) in [int,float] and math.isfinite(value) and value>0


def shape(candidate,policy):
    # A proven failed or otherwise unsuitable source cannot establish availability,
    # even when some of its descriptive metadata is absent.
    if candidate.get('suitable') is False:return 'reject','Release was assessed unsuitable'
    if not number(candidate.get('bytes')) or int(candidate['bytes'])!=candidate['bytes']:
        return 'review','Release byte size is unknown or invalid'
    if not number(candidate.get('height')) or int(candidate['height'])!=candidate['height']:
        return 'review','Release resolution is unknown or invalid'
    if candidate['height']>policy['maximum_resolution']['height']:
        return 'reject','Resolution above 1080p is forbidden'
    if candidate.get('width') is not None:
        if not number(candidate['width']):return 'review','Release width is invalid'
        if candidate['width']>policy['maximum_resolution']['width']:
            return 'reject','Video width exceeds the Full HD ceiling'
        if candidate['width']>1280 and candidate['height']<=720:return 'review','Resolution label conflicts with video width; establish the true source tier'
    if candidate.get('upscaled') is True or (number(candidate.get('source_height')) and candidate['height']>candidate['source_height']):
        return 'reject','Deliberate upscaling conflicts with original source/aspect policy'
    if candidate.get('suitable') is not True:return 'review','Release suitability has not been assessed'
    return 'allow','Known size, resolution and suitability'


def runtimes(request,candidate):
    if request['kind']=='movie':
        value=candidate.get('runtime_seconds')
        if not number(value):raise ReviewRequired('Movie runtime is unknown or invalid')
        return [value]
    expected=len(request.get('episodes',[]));count=candidate.get('episode_count')
    values=candidate.get('episode_runtime_seconds')
    if expected==1 and values is None and number(candidate.get('runtime_seconds')):values=[candidate['runtime_seconds']]
    if type(count) is not int or count!=expected or not isinstance(values,list) or len(values)!=expected or not all(number(x) for x in values):
        raise ReviewRequired('TV episode count and every episode runtime must be known and match the request')
    return values


# Preferred-size compatibility helper. Explicit episode exceptions are checked below.
def episode_targets(seconds,policy):
    return (0,episode_cap(seconds,720,policy))


def episode_cap(seconds,height,policy):
    if not number(seconds) or not number(height):raise ReviewRequired('Known runtime and resolution are required for every episode cap')
    if height>policy['maximum_resolution']['height']:raise ReviewRequired('Resolution above 1080p is forbidden')
    key='720p_or_lower' if height<=720 else '1080p_fallback'
    values=policy['television']['resolution_caps'][key]
    if seconds<=900:return values['up_to_15_minutes_bytes']
    if seconds<=1800:return values['over_15_up_to_30_minutes_bytes']
    if seconds<=3600:return values['over_30_up_to_60_minutes_bytes']
    return int((Decimal(str(seconds))*Decimal(values['over_60_minutes_bytes_per_hour'])/Decimal(3600)).to_integral_value(rounding=ROUND_FLOOR))


def requested_episode_cap(seconds,height,policy,request,episode):
    base=episode_cap(seconds,height,policy)
    exception=request.get('episode_size_exception')
    if exception is None:return base
    if (not isinstance(exception,dict) or exception.get('schema_version')!=1
        or request.get('kind')!='season' or request.get('episodes')!=[episode]
        or not isinstance(request.get('provider_id'),str) or not request['provider_id']
        or type(request.get('season')) is not int or request['season']<1
        or type(episode) is not int or episode<1
        or exception.get('provider_id')!=request.get('provider_id')
        or exception.get('season')!=request.get('season') or exception.get('episode')!=episode
        or not isinstance(exception.get('approval_reference'),str) or not exception['approval_reference'].strip()
        or exception['approval_reference']!=request.get('size_exception_approval')
        or type(exception.get('maximum_bytes')) is not int or exception['maximum_bytes']<=0
        or exception.get('maximum_height') not in [720,1080]):
        raise ReviewRequired('Episode size exception lacks exact identity and approval binding')
    if height>exception['maximum_height']:
        raise ReviewRequired('Resolution exceeds this episode size exception')
    return exception['maximum_bytes']


def tv_resolution_rank(height,policy):
    order=policy['television'].get('resolution_preference',[720,1080])
    for rank,ceiling in enumerate(order):
        if height<=ceiling:return rank
    raise ReviewRequired('TV resolution exceeds the approved preference tiers')


def episode_map(request,candidate):
    times=runtimes(request,candidate)
    mapped=candidate.get('episode_map')
    if mapped is None:
        return [{'episode':e,'runtime_seconds':t,'height':candidate['height']} for e,t in zip(request['episodes'],times)]
    if not isinstance(mapped,list) or len(mapped)!=len(request['episodes']):raise ReviewRequired('Mixed pack needs an exact episode map')
    by_id={x.get('episode'):x for x in mapped}
    if len(by_id)!=len(mapped) or set(by_id)!=set(request['episodes']):raise ReviewRequired('Mixed pack coverage is incomplete or duplicated')
    result=[]
    for ep,t in zip(request['episodes'],times):
        row=by_id[ep]
        if not number(row.get('height')) or not number(row.get('runtime_seconds')) or row['runtime_seconds']!=t:
            raise ReviewRequired('Every mixed-pack episode needs matching runtime and resolution')
        result.append({k:row[k] for k in ['episode','runtime_seconds','height','bytes'] if k in row})
    if max(x['height'] for x in result)!=candidate['height']:raise ReviewRequired('Pack resolution conflicts with episode map')
    return result


def language(value):
    aliases={'en':'eng','english':'eng','zh':'zho','chi':'zho','chinese':'zho','ja':'jpn','ko':'kor','fr':'fra','fre':'fra','de':'deu','ger':'deu','es':'spa','ru':'rus','hi':'hin','italian':'ita','it':'ita','portuguese':'por','pt':'por'}
    if not isinstance(value,str):return None
    v=value.lower().strip()
    if v in ['','und','unknown','original']:return None
    return aliases.get(v,v) if len(v)==3 or v in aliases else None


def expected_audio(request):
    if request.get('original_audio_requested') or request.get('audio_language')=='original':
        desired=language(request.get('original_language'))
        if not desired:raise ReviewRequired('Original audio was requested but the original spoken language is unknown')
        return desired
    desired=language(request.get('audio_language','eng'))
    if not desired:raise ReviewRequired('Requested audio language is unknown')
    return desired


def foreign_subtitles_required(request):
    return request['kind']=='movie' and expected_audio(request)!='eng'


def language_assessment(request,candidate,policy=None):
    desired=expected_audio(request)
    raw=candidate.get('audio_languages') or []
    if not isinstance(raw,list):raise ReviewRequired('Audio language metadata is malformed')
    available={language(x) for x in raw}-{None}
    missing=not raw or all(x is None or str(x).strip().lower() in ['', 'und','unknown'] for x in raw)
    staging_only=desired not in available and missing and bool(request.get('missing_audio_staging_approval')) and (policy or {}).get('language',{}).get('allow_missing_audio_metadata_into_staging') is True
    if desired not in available and not staging_only:raise ReviewRequired('Requested English/original audio is not established; never silently substitute an English dub')
    result={'requested_audio':desired,'english_subtitles_required':foreign_subtitles_required(request),
            'actual_spoken_language_review_required':True,'missing_audio_metadata_staging_only':staging_only}
    if result['english_subtitles_required']:
        subs={language(x) for x in candidate.get('subtitle_languages',[])}
        evidence=candidate.get('subtitle_evidence',{})
        if 'eng' not in subs or evidence.get('kind') not in ['mediainfo','reviewed_stream','verified_sidecar'] or not evidence.get('reference'):
            raise ReviewRequired('Foreign-language film needs established English subtitles; filename/tags alone are insufficient')
        result['final_usable_matching_english_subtitle_verification_required']=True
    return result


def search_reference(search):
    ref=search.get('reference')
    if not isinstance(ref,str) or not ref.strip() or len(ref)>300 or any(x in ref for x in ['?','=','\n','\r']):
        raise ReviewRequired('A safe, noncredential search/comparison reference is required')
    return ref


def assess(request,candidate,candidates,search,policy,review_reference=None):
    """An explicit review can approve soft guidance, never the hard ceilings."""
    decision,reason=shape(candidate,policy)
    result={'decision':decision,'reasons':[reason],'tv_1080p_fallback':False,'policy_version':policy['version']}
    if decision!='allow':return result
    try:
        ref=search_reference(search);times=runtimes(request,candidate)
        result['language']=language_assessment(request,candidate,policy)
    except ReviewRequired as e:return result|{'decision':'review','reasons':[str(e)]}
    result['search_reference']=ref
    result['total_runtime_seconds']=sum(times)
    size=candidate['bytes']
    others=[c for c in candidates if c.get('guid')!=candidate.get('guid')]
    if request['kind']=='movie':
        low,high=policy['films']['target_bytes'];ceiling=policy['films']['hard_ceiling_bytes']
        result['preferred_total_bytes']=[low,high]
        if size>ceiling:return result|{'decision':'reject','reasons':['Film exceeds 4 decimal GB; a new user exception is required']}
        if candidate['height']!=1080:
            if candidate['height']!=720:
                return result|{'decision':'review','reasons':['Native film quality outside 1080p/720p needs explicit source review; never force an upscale']}
            if search.get('complete') is not True:
                return result|{'decision':'review','reasons':['720p film fallback requires a completed suitable-1080p search']}
            found=[];unknown=[]
            for other in others:
                state,_=shape(other,policy)
                if state=='review':unknown.append(other.get('guid'))
                elif state=='allow' and other['height']==1080 and other['bytes']<=ceiling:
                    try:runtimes(request,other);language_assessment(request,other,policy)
                    except ReviewRequired:unknown.append(other.get('guid'));continue
                    found.append(other.get('guid'))
            if found:return result|{'decision':'reject','reasons':['Suitable 1080p film release exists; 720p fallback is not permitted']}
            if unknown:return result|{'decision':'review','reasons':['Unknown search metadata prevents establishing film 1080p unavailability']}
            result['film_720p_fallback']=True
        if size<low:
            if not review_reference:return result|{'decision':'review','reasons':['Below preferred 2–3 GB film size; do not deliberately target compact 1.5 GB releases']}
            result['reasons']=['Explicit reviewed compact-film choice below the target; not a default target']
        elif size<=high:
            result['reasons']=['Within the preferred 2–3 decimal GB film range']
        else:
            if search.get('complete') is not True:
                return result|{'decision':'review','reasons':['3–4 GB film fallback needs a completed suitable-release comparison']}
            ambiguous=[];smaller=[]
            for other in others:
                state,_=shape(other,policy)
                if state=='review':ambiguous.append(other.get('guid'))
                elif state=='allow' and other['height']==candidate['height'] and low<=other['bytes']<size:
                    try:runtimes(request,other);language_assessment(request,other,policy)
                    except ReviewRequired:ambiguous.append(other.get('guid'));continue
                    smaller.append(other.get('guid'))
            if ambiguous:return result|{'decision':'review','reasons':['Unclassified search results prevent asserting no smaller suitable film exists']}
            if smaller:return result|{'decision':'review','reasons':['A smaller suitable film release exists; choose it before using the 4 GB ceiling']}
            result['reasons']=['Occasional 3–4 GB film fallback: no smaller suitable target-or-larger release in completed search']
            result['smaller_suitable_candidates']=[]
    else:
        try:
            mapped=episode_map(request,candidate)
            caps=[requested_episode_cap(x['runtime_seconds'],x['height'],policy,request,x['episode']) for x in mapped]
        except ReviewRequired as e:return result|{'decision':'review','reasons':[str(e)]}
        high=sum(caps)
        result['aggregate_hard_cap_bytes']=high
        result['episode_caps']=[x|{'cap_bytes':cap} for x,cap in zip(mapped,caps)]
        result['episode_count']=len(times)
        if request.get('episode_size_exception'):result['episode_size_exception']=request['episode_size_exception']
        result['reasons']=['Resolution-aware pack cap is the sum of mapped episode caps; smaller files are allowed; no universal 20 GB cap']
        if size>high:return result|{'decision':'reject','reasons':['TV release exceeds its resolution/runtime-derived aggregate hard cap']}
        if any(number(x.get('bytes')) and x['bytes']>cap for x,cap in zip(mapped,caps)):
            return result|{'decision':'reject','reasons':['A mapped episode exceeds its individual runtime/resolution cap']}
        if policy['television'].get('resolution_preference'):
            result['tv_720p_fallback']=False
            ranks={x['episode']:tv_resolution_rank(x['height'],policy) for x in mapped}
            if any(rank>0 for rank in ranks.values()):
                if search.get('complete') is not True:
                    return result|{'decision':'review','reasons':['TV fallback requires a completed comparison for the preferred lower-resolution tiers']}
                lower=[];ambiguous=[]
                for other in others:
                    state,_=shape(other,policy)
                    if state=='reject':continue
                    if (not other.get('episode_map') and number(other.get('height'))
                        and other['height']<=policy['maximum_resolution']['height']
                        and tv_resolution_rank(other['height'],policy)>=max(ranks.values())):continue
                    if state=='review':ambiguous.append(other.get('guid'))
                    elif state=='allow':
                        try:
                            other_map=episode_map(request,other);language_assessment(request,other,policy)
                            other_cap=sum(requested_episode_cap(x['runtime_seconds'],x['height'],policy,request,x['episode']) for x in other_map)
                        except ReviewRequired:ambiguous.append(other.get('guid'));continue
                        if other['bytes']>other_cap:continue
                        if any(tv_resolution_rank(x['height'],policy)<ranks[x['episode']] for x in other_map):lower.append(other.get('guid'))
                if lower:return result|{'decision':'reject','reasons':['A suitable preferred lower-resolution TV option exists']}
                if ambiguous:return result|{'decision':'review','reasons':['Unknown search metadata prevents establishing lower-tier TV unavailability']}
                result['tv_720p_fallback']=any(480<x['height']<=720 for x in mapped)
                result['tv_1080p_fallback']=any(x['height']>720 for x in mapped)
                result['reasons'].append('Completed comparison found no suitable preferred lower tier for each fallback')
            result['tv_resolution_preference']=policy['television']['resolution_preference']
        elif any(x['height']>policy['television']['preferred_maximum_height'] for x in mapped):
            if search.get('complete') is not True:
                return result|{'decision':'review','reasons':['1080p TV fallback requires a completed search for suitable 720p-or-lower releases']}
            lower=[];ambiguous=[]
            for other in others:
                state,_=shape(other,policy)
                if state=='review':ambiguous.append(other.get('guid'))
                elif state=='allow':
                    try:
                        other_map=episode_map(request,other);language_assessment(request,other,policy)
                        other_cap=sum(episode_cap(x['runtime_seconds'],x['height'],policy) for x in other_map)
                    except ReviewRequired:ambiguous.append(other.get('guid'));continue
                    if other['bytes']>other_cap:continue
                    by_ep={x['episode']:x for x in other_map}
                    if any(x['height']>720 and by_ep[x['episode']]['height']<=720 for x in mapped):lower.append(other.get('guid'))
            if lower:return result|{'decision':'reject','reasons':['Suitable 720p-or-lower TV release is available; 1080p fallback is not permitted']}
            if ambiguous:return result|{'decision':'review','reasons':['Unknown search-result metadata prevents proving the 1080p TV fallback is necessary']}
            result['tv_1080p_fallback']=True
            result['reasons'].append('1080p TV fallback: completed search found no suitable 720p-or-lower release')
    if review_reference:result['review_reference']=search_reference({'reference':review_reference})
    result['decision']='allow'
    return result


FIELDS=['guid','name','bytes','height','width','source_height','upscaled','suitable','runtime_seconds','episode_count','episode_runtime_seconds','episode_map','suitability_reason','audio_languages','subtitle_languages','subtitle_evidence']


def choose_release(request,guid,evidence,policy,digest,review_reference=None):
    raw=evidence.get('candidates',[])
    if not isinstance(raw,list) or not raw or len(raw)>200:raise ReviewRequired('Bounded release comparison is required')
    # Retain only fields needed to replay the policy decision, not raw search payloads.
    candidates=[{k:c[k] for k in FIELDS if k in c} for c in raw]
    if len({c.get('guid') for c in candidates})!=len(candidates):raise ReviewRequired('Duplicate candidate GUIDs in comparison')
    matches=[c for c in candidates if c.get('guid')==guid]
    if len(matches)!=1:raise ReviewRequired('Selected release is not in the reviewed comparison')
    selected=matches[0];search=evidence.get('search',{})
    assessment=assess(request,selected,candidates,search,policy,review_reference)
    if assessment['decision']!='allow':raise ReviewRequired('; '.join(assessment['reasons']))
    return selected|{'policy_assessment':assessment,'policy_sha256':digest,
                     'comparison':{'search':{'reference':search_reference(search),'complete':search.get('complete') is True},'candidates':candidates},
                     'selected_at':time.time()}


def choose_next_tv_release(request,evidence,policy,digest,unavailable_guids=()):
    """Try every eligible lower tier before fallback; names are not source identity."""
    if request.get('kind')!='season':raise ReviewRequired('TV candidate recovery requires an episode request')
    unavailable=set(unavailable_guids)
    candidates=[dict(x,suitable=False) if x.get('guid') in unavailable else x for x in evidence.get('candidates',[])]
    options=[x for x in candidates if shape(x,policy)[0]=='allow']
    order=policy['television'].get('resolution_preference',[720,1080])
    options.sort(key=lambda x:(tv_resolution_rank(x['height'],policy),x['height']!=order[tv_resolution_rank(x['height'],policy)],x['bytes']))
    errors=[]
    for candidate in options:
        try:return choose_release(request,candidate['guid'],dict(evidence,candidates=candidates),policy,digest)
        except ReviewRequired as error:errors.append(str(error))
    raise ReviewRequired(errors[-1] if errors else 'No untried eligible TV posting remains')


def enforce_admission(request,release,config):
    policy,digest=load_policy(config)
    if release.get('policy_sha256')!=digest or release.get('policy_assessment',{}).get('policy_version')!=policy['version']:
        raise ReviewRequired('Selected release predates the active policy; select it again under the current version')
    evidence=release.get('comparison',{})
    assessment=assess(request,release,evidence.get('candidates',[]),evidence.get('search',{}),policy,
                      release.get('policy_assessment',{}).get('review_reference'))
    if assessment['decision']!='allow':raise ReviewRequired('; '.join(assessment['reasons']))
    return policy,assessment
