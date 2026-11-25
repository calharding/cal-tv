from .core import ReviewRequired


def approved_existing_movie_size(path, request):
    if request.get("existing_movie_size_exception") is not None:
        raise ReviewRequired("Installation-specific film size exception not supported")
    return False


def approved_prepared_movie_size(path, request, record):
    raise ReviewRequired("Installation-specific prepared film exception not supported")


def prepared_exception_from_result(request, result):
    raise ReviewRequired("Installation-specific prepared film exception not supported")
