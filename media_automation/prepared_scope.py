"""Exact prepared-source admission; never expands the normal downloads root.
Only trusted explicit configuration may supply these entries. No discovery,
preparation, policy editing, historical-proof replay or media content reads.
"""
from pathlib import Path
from .core import ReviewRequired
from .importer import identity_seal


def require(ok, message):
    if not ok:
        raise ReviewRequired(message)


def validate_prepared_scope(config, request, source):
    """Return admitted scope or None for an ordinary completed-source directory.

    Call before containment rejection. No request/config values are changed.
    The separately bound plan, approval and cross-journal admission still apply.
    """
    source = Path(source).absolute()
    require(not any(p.is_symlink() for p in [source, *source.parents]), 'Prepared source ancestry is a symlink')
    scopes = config.get('prepared_source_scopes', [])
    # Match a named source directory, not the whole tree beneath a staging root.
    matches = [s for s in scopes if Path(s['prepared_path']).parent == source]
    if not matches:
        return None
    require(config.get('prepared_scope_enabled') is True, 'Prepared source profile is inactive')
    require(len(matches) == 1, 'Prepared source scope is ambiguous')
    scope = matches[0]
    if scope.get('schema_version') == 3:
        return validate_movie_remux_scope(config, request, source, scope)
    if scope.get('schema_version') == 2:
        return validate_remux_scope(config, request, source, scope)
    require(scope.get('schema_version') == 1, 'Unknown prepared scope schema')
    require(request.get('kind') == 'season' and request.get('provider_id') == scope['provider_id']
            and request.get('season') == scope['season'] and request.get('episodes') == [scope['episode']],
            'Prepared source canonical episode differs')
    require(type(scope.get('maximum_height')) is int and scope['maximum_height'] == 480
            and type(request.get('max_height')) is int and 0 < request['max_height'] <= scope['maximum_height'],
            'Prepared source must retain its approved 480-line ceiling')
    expected_exception = dict(schema_version=1, provider_id=scope['provider_id'], season=scope['season'],
                              episode=scope['episode'], maximum_bytes=scope['bytes'], maximum_height=720,
                              approval_reference=scope['approval_reference'])
    require(bool(scope.get('approval_reference')) and request.get('size_exception_approval') == scope['approval_reference']
            and request.get('episode_size_exception') == expected_exception,
            'Prepared size exception must match exact reviewed source and episode')
    require(type(scope.get('bytes')) is int and scope['bytes'] > 0, 'Prepared scope byte limit invalid')
    original = Path(scope['original_path']);prepared = Path(scope['prepared_path'])
    require(original.is_absolute() and prepared.is_absolute() and original != prepared,
            'Prepared and original paths must be distinct absolute paths')
    require(prepared.parent == source, 'Prepared source directory differs')
    for name, path in [('original', original), ('prepared', prepared)]:
        require(not any(p.is_symlink() for p in [path, *path.parents]), 'Prepared/original path is a symlink')
        seal = scope[name + '_identity_seal']
        require(len(seal) == 5 and seal[2] == scope['bytes'], 'Prepared/original saved byte identity differs')
        require(identity_seal(path) == seal, 'Prepared/original identity changed')
    require(scope['original_identity_seal'][:2] != scope['prepared_identity_seal'][:2],
            'Prepared copy aliases the original inode')
    return scope


def validate_remux_scope(config, request, source, scope):
    """Separate v2 stream-copy contract; does not reinterpret historical v1 copies."""
    from .quality_policy import load_policy, requested_episode_cap
    require(scope.get('mode') == 'light_stream_copy', 'Unknown preparation mode')
    require(request.get('kind') == 'season' and request.get('provider_id') == scope.get('provider_id')
            and request.get('season') == scope.get('season') and request.get('episodes') == [scope.get('episode')],
            'Remux canonical episode differs')
    require(type(request.get('max_height')) is int and 0 < request['max_height'] <= 480
            and scope.get('maximum_height') == request['max_height'], 'Remux cannot expand resolution')
    require(scope.get('approval_reference') and scope['approval_reference'] == scope.get('preparation_approval_reference'),
            'Explicit preparation approval binding required')
    require(scope.get('full_av_integrity_proved') is False and scope.get('structural_check_passed') is True,
            'Light structural evidence must be represented truthfully')
    cap = scope.get('output_cap_bytes')
    require(type(cap) is int and cap > 0, 'Exact positive remux output cap required')
    duration = scope.get('duration_seconds'); height = scope.get('actual_height')
    require(type(duration) in (int,float) and duration > 0 and type(height) is int and 0 < height <= scope['maximum_height'],
            'Remux measured duration/resolution required')
    policy, digest = load_policy(config)
    require(digest == scope.get('policy_sha256'), 'Remux policy binding changed')
    allowed = requested_episode_cap(duration, height, policy, request, scope['episode'])
    require(cap <= allowed, 'Remux output cap exceeds existing policy or explicit exception')
    original, prepared = Path(scope['original_path']), Path(scope['prepared_path'])
    require(original.is_absolute() and prepared.is_absolute() and original != prepared and prepared.parent == source,
            'Exact separate remux paths required')
    for role, path in [('original',original),('prepared',prepared)]:
        require(not any(p.is_symlink() for p in [path,*path.parents]), 'Remux source ancestry is a symlink')
        seal = scope.get(role+'_identity_seal')
        require(isinstance(seal,list) and len(seal)==5 and type(seal[2]) is int and 0 < seal[2] <= cap,
                'Remux byte identity exceeds exact cap')
        require(identity_seal(path)==seal, 'Remux original/prepared identity changed')
    require(scope['original_identity_seal'][:2] != scope['prepared_identity_seal'][:2], 'Remux aliases original')
    return scope


def remux_scope_from_result(request, original_path, result, *, output_cap_bytes, approval_reference, policy_sha256):
    """Build an inert exact descriptor from a completed structural preparation receipt.
    Does not enable anything; admission later pins policy and current source/output seals.
    """
    require(request.get('kind')=='season' and len(request.get('episodes',[]))==1, 'One reviewed episode required')
    require(result.get('status')=='PREPARED' and result.get('mode')=='STREAM_COPY'
            and result.get('all_tracks_preserved_structurally') is True and result.get('full_av_integrity_proved') is False,
            'Completed lightweight remux receipt required')
    require(type(output_cap_bytes) is int and output_cap_bytes==result.get('maximum_bytes'), 'Output cap must equal preparation limit')
    p=result['probe']; height=max(s.get('height',0) for s in p['streams'] if s.get('codec_type')=='video')
    return dict(schema_version=2,mode='light_stream_copy',provider_id=request['provider_id'],season=request['season'],
        episode=request['episodes'][0],maximum_height=request['max_height'],actual_height=height,
        output_cap_bytes=output_cap_bytes,duration_seconds=float(p['format']['duration']),policy_sha256=policy_sha256,
        original_path=str(original_path),prepared_path=result['path'],original_identity_seal=result['source_identity_seal'],
        prepared_identity_seal=result['prepared_identity_seal'],approval_reference=approval_reference,
        preparation_approval_reference=approval_reference,structural_check_passed=True,full_av_integrity_proved=False)


def validate_movie_remux_scope(config, request, source, scope):
    """Movie-only structural copy contract; does not alter TV admission."""
    from .quality_policy import load_policy
    require(scope.get('mode') == 'movie_light_stream_copy' and request.get('kind') == 'movie', 'Movie preparation contract required')
    require(bool(request.get('provider_id')) and request['provider_id'] == scope.get('provider_id')
            and bool(request.get('title')) and request['title'] == scope.get('title')
            and type(request.get('year')) is int and 1800 <= request['year'] <= 2200
            and request['year'] == scope.get('year'), 'Prepared movie identity differs')
    approval = scope.get('approval_reference')
    require(isinstance(approval,str) and bool(approval.strip())
            and approval == request.get('preparation_approval_reference'), 'Movie preparation approval differs')
    require(scope.get('structural_check_passed') is True and scope.get('full_av_integrity_proved') is False,
            'Movie structural evidence must be truthful')
    maximum = scope.get('maximum_height');height = scope.get('actual_height');width = scope.get('actual_width')
    require(type(maximum) is int and maximum == request.get('max_height') and 0 < maximum <= 1080
            and type(height) is int and 0 < height <= maximum
            and type(width) is int and 0 < width <= 1920, 'Movie resolution exceeds bound')
    duration = scope.get('duration_seconds')
    require(type(duration) in (int,float) and 30 <= duration < 86400, 'Movie duration invalid')
    policy,digest = load_policy(config);cap = scope.get('output_cap_bytes')
    require(digest == scope.get('policy_sha256'), 'Movie policy binding changed')
    require(request.get('existing_movie_size_exception') is None,
            'Installation-specific film size exception not supported')
    require(type(cap) is int and 0 < cap <= min(4000000000,policy['films']['hard_ceiling_bytes']),
            'Movie byte ceiling exceeded')
    original,prepared = Path(scope['original_path']),Path(scope['prepared_path'])
    require(original.is_absolute() and prepared.is_absolute() and original != prepared and prepared.parent == source,
            'Exact movie preparation paths required')
    for role,path in [('original',original),('prepared',prepared)]:
        require(not any(p.is_symlink() for p in [path,*path.parents]), 'Movie preparation ancestry symlink')
        seal = scope.get(role+'_identity_seal')
        require(isinstance(seal,list) and len(seal)==5 and type(seal[2]) is int and 0 < seal[2] <= cap
                and identity_seal(path)==seal, 'Movie source/output identity changed')
    require(scope['original_identity_seal'][:2] != scope['prepared_identity_seal'][:2], 'Movie copy aliases original')
    return scope


def movie_remux_scope_from_result(request, original_path, result, *, output_cap_bytes, approval_reference, policy_sha256):
    """Create inert movie descriptor. Existing scope approval remains required at admission."""
    require(request.get('kind') == 'movie' and request.get('preparation_approval_reference') == approval_reference,
            'Explicit movie preparation approval required')
    require(result.get('status') == 'PREPARED' and result.get('mode') == 'STREAM_COPY'
            and result.get('all_tracks_preserved_structurally') is True and result.get('full_av_integrity_proved') is False,
            'Completed structural movie preparation required')
    require(request.get('existing_movie_size_exception') is None,
            'Installation-specific film size exception not supported')
    require(type(output_cap_bytes) is int and 0 < output_cap_bytes <= 4000000000
            and output_cap_bytes == result.get('maximum_bytes'), 'Exact movie preparation byte limit required')
    require(request.get('max_height') == result.get('maximum_height'), 'Movie preparation resolution differs')
    probe = result['probe'];videos = [s for s in probe['streams'] if s.get('codec_type') == 'video']
    require(bool(videos), 'Movie video evidence absent')
    return dict(schema_version=3,mode='movie_light_stream_copy',provider_id=request['provider_id'],title=request['title'],year=request['year'],
        maximum_height=request['max_height'],actual_height=max(s.get('height',0) for s in videos),actual_width=max(s.get('width',0) for s in videos),
        output_cap_bytes=output_cap_bytes,duration_seconds=float(probe['format']['duration']),policy_sha256=policy_sha256,
        original_path=str(original_path),prepared_path=result['path'],original_identity_seal=result['source_identity_seal'],
        prepared_identity_seal=result['prepared_identity_seal'],approval_reference=approval_reference,
        structural_check_passed=True,full_av_integrity_proved=False)
