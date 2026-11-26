# Cal TV

A code-only review baseline for the Cal TV importer and continuity journal. The
default launch path is offline and does not provision a service, enable a scheduler,
download media, or publish to a library. A merge does not deploy.

Start with the [architecture guide](docs/architecture.md) to follow one
fictional file through admission, publication, refusal and restart.

## Local checks

Requires Python 3.9+ on macOS or Linux. Tests that exercise real stream inspection
also require FFmpeg and ffprobe on PATH. Media fixtures are generated in temporary
directories from color, a synthetic tone and authored subtitle text. No episodes,
video files, database snapshots, or service credentials are included.

```sh
python3 -B -m unittest discover -v
./fastpassctl --help
./fastpassctl --root /path/to/local/queue status
./fastpassctl validate --scope /path/to/local/scope.json
```

The status command only reads an existing local SQLite queue. An empty queue
does not assert that physical intake is exhausted. Scope validation parses
a local document and never contacts the library. To try both commands safely,
run `python3 -B -m unittest tests.test_portable_baseline.Entrypoint -v`
from the checkout. It builds and removes its own synthetic workspace.

## Scope

The reviewed single-source importer, writer-lock admission, cross-journal
checks, isolated preparation, and Jellyfin verification logic are included.
The continuity journal accepts a caller-supplied complete inventory audit and
records evidence and blockers without taking a destructive disposition. Site
configuration and policy examples are inert. They do not describe a live site
or grant any approval.

The public launcher omits production dispatch activation, physical audit and
review-index site glue, credential provisioning, acquisition, and installation-
specific exceptions and their binding data. Those exceptions fail closed here.
The included adapter code runs against injected fake services in tests. The
baseline is for development and review, not a drop-in production release.

See [development notes](docs/development.md) for test boundaries and change review.
