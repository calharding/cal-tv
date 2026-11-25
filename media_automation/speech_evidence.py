"""Validate source-bound evidence from reviewed, offline multilingual ASR samples."""
import re
from .core import ReviewRequired,sha256
from .quality_policy import expected_audio,language


def language_evidence(path,request):
    evidence=request.get('offline_language_verification')
    by_source=request.get('offline_language_verification_by_sha256')
    if by_source is not None:
        if evidence or not isinstance(by_source,dict):
            raise ReviewRequired('Offline language evidence has ambiguous source bindings')
        evidence=by_source.get(sha256(path))
    return evidence


def select_audio_stream(info,desired='eng'):
    streams=[x for x in info.get('streams',[]) if x.get('codec_type')=='audio']
    matches=[x for x in streams if language(x.get('tags',{}).get('language'))==desired]
    selected=matches[0] if matches else streams[0] if len(streams)==1 else None
    if selected is None or type(selected.get('index')) is not int:
        raise ReviewRequired('Multiple audio tracks require an identified requested-language stream')
    return selected['index']


def offline_language_verified(path,request):
    evidence=language_evidence(path,request)
    if not evidence:return False
    index=evidence.get('audio_stream_index')
    if index is not None and (type(index) is not int or index<0):raise ReviewRequired('Offline language audio-stream binding is invalid')
    if (evidence.get('schema_version')!=1 or evidence.get('method')!='whisper.cpp_multilingual_base'
        or evidence.get('forced_language') is not None or evidence.get('translate') is not False
        or evidence.get('multilingual') is not True or not evidence.get('review_reference')
        or evidence.get('coherent_transcripts_reviewed') is not True
        or not re.fullmatch('[0-9a-f]{64}',evidence.get('model_sha256',''))):
        raise ReviewRequired('Offline spoken-language evidence is incomplete or forced')
    desired=expected_audio(request)
    if language(evidence.get('verified_language'))!=desired:
        raise ReviewRequired('Offline spoken-language result conflicts with requested language')
    samples=evidence.get('samples',[])
    if not isinstance(samples,list):raise ReviewRequired('Offline language samples are malformed')
    accepted=[]
    for sample in samples:
        if index is not None and sample.get('audio_stream_index')!=index:
            raise ReviewRequired('Offline samples do not match the reviewed audio stream')
        if (language(sample.get('detected_language'))!=desired
            or type(sample.get('probability')) not in [int,float] or sample['probability']<0.90
            or len(re.findall(r"[^\W\d_]+",sample.get('transcript',''))) <5
            or type(sample.get('offset_seconds')) not in [int,float] or sample['offset_seconds']<0
            or type(sample.get('duration_seconds')) not in [int,float] or not 10<=sample['duration_seconds']<=30):continue
        accepted.append(sample)
    if len(accepted)<2 or max(s['offset_seconds'] for s in accepted)-min(s['offset_seconds'] for s in accepted)<60:
        raise ReviewRequired('Offline language needs two separated coherent high-confidence speech samples')
    if evidence.get('source_sha256')!=sha256(path):
        raise ReviewRequired('Offline spoken-language evidence does not match the completed file')
    return True


def nonverbal_content_verified(path, request, duration):
    if request.get("nonverbal_content_review") is not None:
        raise ReviewRequired("Installation-specific nonverbal approval not supported")
    return False
