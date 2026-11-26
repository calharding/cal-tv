"""Queue inspection and offline scope validation for the public baseline."""
import argparse
import contextlib
import json
from pathlib import Path
import sqlite3

from .backlog_cli import main as backlog_main

STATES = {"fast_ready", "preparing", "running", "ready", "deferred", "review_running",
          "held", "resolved"}


def inspect_queue(root):
    root = Path(root)
    path = root / "queue.sqlite3"
    if root.is_symlink() or not root.is_dir() or not path.is_file() or path.is_symlink():
        raise ValueError("An existing local queue is required. No state was created.")
    # This queue view has no physical inventory or age proof, even when it is empty.
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        counts = dict(db.execute("SELECT state,count(*) FROM items GROUP BY state"))
    active = sum(counts.get(s, 0) for s in ("fast_ready", "running", "review_running", "preparing"))
    unresolved = sum(n for state, n in counts.items() if state not in {"ready", "resolved"})
    return {"status": "QUEUE_SNAPSHOT", "counts": {s: counts.get(s, 0) for s in sorted(STATES | counts.keys())},
            "active": active, "unresolved": unresolved, "source_coverage_verified": False,
            "stale_work_requiring_reconciliation": [], "queue_age_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fastpassctl", description=__doc__)
    parser.add_argument("--root", type=Path, help="Existing queue-state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Inspect an existing queue read-only")
    validate = commands.add_parser("validate", help="Validate a reviewed scope offline")
    validate.add_argument("--scope", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        return backlog_main(["--scope", args.scope])
    try:
        if args.root is None:
            raise ValueError("An explicit queue root is required")
        print(json.dumps(inspect_queue(args.root), sort_keys=True))
        return 0
    except (ValueError, OSError, sqlite3.Error):
        print(json.dumps({"status": "BLOCKED_QUEUE_UNAVAILABLE", "source_coverage_verified": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
