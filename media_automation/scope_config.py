"""Derive a nonexecuting scoped config from an inactive profile and live settings.
Reads no credentials and changes neither input. Enabling a particular reviewed
run remains separate; inherited refresh permission is not permission to import.
"""
import copy
from pathlib import Path
from .core import ReviewRequired


def scoped_config(profile, live, run_directory):
    cfg=copy.deepcopy(profile)
    # These production bindings must agree, not silently follow a changed root
    # or relaxed policy. New values require explicit review of the profile.
    pinned=('media_root','complete_root','quality_policy_path','quality_policy_sha256',
            'storage_guard_script','storage_guard_config')
    for key in pinned:
        if not profile.get(key) or live.get(key)!=profile[key]:
            raise ReviewRequired('Existing production binding differs: '+key)
    version=live.get('quality_policy_version')
    if not isinstance(version,str) or not version:
        raise ReviewRequired('Existing policy version is missing')
    if profile.get('quality_policy_version') not in (None,version):
        raise ReviewRequired('Existing policy version differs from profile')
    cfg['quality_policy_version']=version
    for key in ('jellyfin_enabled','targeted_refresh_permitted'):
        value=live.get(key,False)
        if type(value) is not bool:
            raise ReviewRequired('Existing permission is not an explicit boolean: '+key)
        cfg[key]=value
    # References only. Never copy live inline keys, cookies, passwords or tokens.
    if cfg['jellyfin_enabled']:
        keyfile=live.get('jellyfin_key_file');url=live.get('jellyfin_url')
        if not isinstance(keyfile,str) or not Path(keyfile).is_absolute() or not isinstance(url,str) or not url:
            raise ReviewRequired('Existing Jellyfin connection references are missing')
        cfg['jellyfin_key_file']=keyfile;cfg['jellyfin_url']=url
    # Use a separate catalogue intent journal rather than reusing another run's effects.
    directory=Path(run_directory)
    if not directory.is_absolute() or '..' in directory.parts:
        raise ReviewRequired('Absolute scoped review directory required')
    cfg['catalogue_intent_db']=str(directory/'catalogue-intents.sqlite3')
    # Never let a template or previously used profile accidentally activate work.
    cfg.update(backlog_fast_enabled=False,manual_service_writes_enabled=False,
               prepared_scope_enabled=False,acquisition_enabled=False,geek_enabled=False,
               prepared_source_scopes=[],scope_selected=False)
    cfg['notice']='Nonexecuting scoped configuration. Existing Jellyfin permissions inherited unchanged; exact job, scope and enable approval still required.'
    return cfg
