"""Header formatting only. This repository does not provision credentials."""
import re

from .core import ReviewRequired


def authorization_header(token=None):
    value = 'MediaBrowser Client="Cal TV", Device="Integration", DeviceId="cal-tv-integration", Version="1"'
    if token is not None:
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise ReviewRequired("Invalid Jellyfin credential format")
        value += ', Token="' + token + '"'
    return value
