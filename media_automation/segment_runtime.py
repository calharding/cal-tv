from .core import ReviewRequired


def reviewed_segment(path, request, episode, duration, width, height):
    if request.get("segment_runtime_reviews") is not None:
        raise ReviewRequired("Installation-specific segment review not supported")
    return None
