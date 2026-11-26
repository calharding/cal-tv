# Default to policy limits; the public checkout cannot attest to site resolution exceptions.
from .core import ReviewRequired


def approved_source_resolution(path, request, episode, width, height):
    if request.get("source_resolution_exception") is not None:
        raise ReviewRequired("Installation-specific source resolution exception not supported")
    return False
