"""Portable continuity journal. The caller supplies the read-only inventory audit.

A successful audit must account for every discovered source. Runnable work and
unresolved sources are different counts. No state here can publish or delete media.
"""
import json
import sqlite3
import time


class IntakeCoverageError(RuntimeError):
    pass


VERIFIED = {"VERIFIED_IMPORT_RECEIPT", "EXCLUDED_OR_SAMPLE"}
UNRESOLVED = {
    "WAIT_STABILITY", "EXPLICIT_HOLD", "DOCUMENTED_DEFERRED",
    "LIBRARY_ALIAS_NOT_IMPORT_RECEIPT",
}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            k TEXT PRIMARY KEY, payload TEXT NOT NULL, phase INTEGER NOT NULL,
            state TEXT NOT NULL, owner TEXT, until REAL NOT NULL DEFAULT 0,
            tries INTEGER NOT NULL DEFAULT 0, result TEXT, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS todo ON jobs(state, phase, updated);
    """)


def refresh(db, audit, *, marker, now=None):
    """Record one complete scan atomically. Partial audits advance nothing."""
    now = time.time() if now is None else now
    if not isinstance(audit, dict) or not isinstance(audit.get("rows"), list):
        raise IntakeCoverageError("Physical audit result is invalid")
    if audit.get("errors") != [] or audit.get("coverage_complete") is not True:
        raise IntakeCoverageError("Physical audit coverage is incomplete")

    # Validate the whole inventory before touching the journal or its checkpoint.
    rows = {}
    for row in audit["rows"]:
        if not isinstance(row, dict):
            raise IntakeCoverageError("Physical inventory row is invalid")
        seal = row.get("seal")
        status = row.get("status")
        if (not isinstance(seal, list) or len(seal) != 5
                or any(type(x) is not int for x in seal)
                or not isinstance(row.get("source"), str) or not row["source"]
                or not isinstance(status, str)
                or status not in {"PENDING_FIRST_PASS", *VERIFIED, *UNRESOLVED}):
            raise IntakeCoverageError("Physical inventory row is invalid or unclassified")
        key = json.dumps(seal, separators=(",", ":"))
        if key in rows:
            raise IntakeCoverageError("Physical audit must coalesce duplicate source aliases")
        if status in VERIFIED and not row.get("receipt"):
            raise IntakeCoverageError("Verified disposition requires a receipt")
        rows[key] = row

    with db:
        db.execute("BEGIN IMMEDIATE")
        for key, row in rows.items():
            status = row["status"]
            state = "pending" if status == "PENDING_FIRST_PASS" else status.lower()
            # Existing hints are the quicker evidence pass, not authority to import.
            phase = (0 if row.get("hints") else 1) if state == "pending" else 2
            payload = json.dumps(row)
            prior = db.execute("SELECT state FROM jobs WHERE k=?", (key,)).fetchone()
            if prior is None:
                db.execute("INSERT INTO jobs(k,payload,phase,state,updated) VALUES(?,?,?,?,?)",
                           (key, payload, phase, state, now))
            elif prior[0] not in {"leased", *[x.lower() for x in VERIFIED]} and (
                    status in VERIFIED
                    or (prior[0] == "wait_stability" and state == "pending")
                    or (prior[0] == "pending" and status in UNRESOLVED)):
                db.execute("UPDATE jobs SET payload=?,phase=?,state=?,updated=? WHERE k=?",
                           (payload, phase, state, now, key))
        active = db.execute("SELECT k FROM jobs WHERE state IN ('pending','leased')").fetchall()
        for (key,) in active:
            if key not in rows:
                db.execute("UPDATE jobs SET state='blocked_missing_from_complete_audit',"
                           "result=?,owner=NULL,until=0,updated=? WHERE k=?",
                           (json.dumps({"reason": "Missing from complete physical audit; no receipt"}), now, key))
        db.executemany("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                       [("last_refresh", str(now)), ("completed_intake_marker", str(marker)),
                        ("last_scan_count", str(len(rows)))])


def claim(db, now, owner, *, lease_seconds=120):
    """Claim one analysis-only job. Serializes competing SQLite writers."""
    if not owner or lease_seconds <= 0:
        raise ValueError("Explicit owner and positive lease required")
    with db:
        # Expiry makes analysis runnable again; it does not release an import hold.
        db.execute("UPDATE jobs SET state='pending',owner=NULL,updated=0 "
                   "WHERE state='leased' AND until<=?", (now,))
        row = db.execute("SELECT k,payload FROM jobs WHERE state='pending' "
                         "ORDER BY phase, CASE WHEN tries>0 THEN 0 ELSE 1 END,updated,k LIMIT 1").fetchone()
        if row is None:
            return None
        db.execute("UPDATE jobs SET state='leased',owner=?,until=?,tries=tries+1,updated=? WHERE k=?",
                   (owner, now + lease_seconds, now, row[0]))
        return row[0], json.loads(row[1])


def finish(db, key, owner, state, result, *, now=None):
    """Accept an active lease's analysis result, never a publication result."""
    now = time.time() if now is None else now
    if not state.startswith(("blocked_", "awaiting_")) or not result:
        raise ValueError("Analysis must produce a named blocker and evidence")
    with db:
        changed = db.execute("UPDATE jobs SET state=?,result=?,owner=NULL,until=0,updated=? "
                             "WHERE k=? AND state='leased' AND owner=? AND until>?",
                             (state, json.dumps(result), now, key, owner, now)).rowcount
        if changed != 1:
            raise RuntimeError("Analysis lease lost")


def status(db, *, discovery_error=False):
    counts = dict(db.execute("SELECT state,count(*) FROM jobs GROUP BY state"))
    checkpoint = dict(db.execute("SELECT k,v FROM meta"))
    runnable = counts.get("pending", 0) + counts.get("leased", 0)
    unresolved = sum(n for state, n in counts.items() if state not in {x.lower() for x in VERIFIED})
    if discovery_error or "last_refresh" not in checkpoint:
        state = "BLOCKED_INTAKE_AUDIT_INCOMPLETE"
    elif runnable:
        state = "RUNNABLE_RECONCILIATION_REMAINS"
    elif unresolved:
        state = "NO_RUNNABLE_WORK_UNRESOLVED_SOURCES"
    else:
        state = "NO_RUNNABLE_WORK_COMPLETE_LAST_SCAN"
    return {"state": state, "counts": counts, "runnable": runnable, "unresolved": unresolved,
            "last_complete_scan": checkpoint.get("last_refresh")}


def scan(db, intake, audit, *, marker, now=None):
    """Read-only audit and durable checkpoint. A missing root cannot be empty."""
    try:
        if not intake.is_dir() or intake.is_symlink():
            raise IntakeCoverageError("Intake root not available")
        refresh(db, audit(intake), marker=marker, now=now)
    except (IntakeCoverageError, OSError, ValueError, KeyError):
        return status(db, discovery_error=True)
    return status(db)
