"""Portable progress accounting. Callers provide verified facts, not heartbeats.

The monitor owns a separate SQLite journal. It cannot discover files, read
services, authorize work, or deliver an alert.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re


DEFAULT_STALL_SECONDS = 480
MAX_LEASE_SECONDS = 480
MAX_BATCH = 4096
_TABLES = {"progress_lanes", "progress_seen", "progress_work", "progress_handoffs", "progress_reports"}


@dataclass(frozen=True)
class Lease:
    started_at: float
    expires_at: float


@dataclass(frozen=True)
class Work:
    key: str
    lease: Lease = None


@dataclass(frozen=True)
class Disposition:
    key: str
    committed_at: float


@dataclass(frozen=True)
class Lane:
    work: tuple = ()
    dispositions: tuple = ()
    blockers: tuple = ()
    unresolved: int = 0
    coverage_complete: bool = False


@dataclass(frozen=True)
class Handoff:
    key: str
    issued_at: float


@dataclass(frozen=True)
class TerminalReceipt:
    handoff: str
    proof: str
    committed_at: float
    outcome: str
    reason: str


def _json(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _reason(code):
    return isinstance(code, str) and re.fullmatch("[a-z][a-z0-9_]{0,63}", code) is not None


def _check_time(value, now):
    return _time(value) and value <= now


@contextmanager
def _transaction(db):
    if db.in_transaction:
        raise ValueError("Use a dedicated progress journal outside an existing transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _unique(rows, kind, key):
    if type(rows) is not tuple or len(rows) > MAX_BATCH or any(type(x) is not kind for x in rows):
        raise ValueError("Expected bounded typed observations")
    values = [key(x) for x in rows]
    if not all(_hash(x) for x in values) or len(values) != len(set(values)):
        raise ValueError("Expected unique opaque evidence keys")


def _validate_lane(lane, now):
    if type(lane) is not Lane or type(lane.coverage_complete) is not bool:
        raise ValueError("Expected a lane observation with explicit coverage")
    _unique(lane.work, Work, lambda item: item.key)
    _unique(lane.dispositions, Disposition, lambda item: item.key)
    if (type(lane.unresolved) is not int or not 0 <= lane.unresolved <= MAX_BATCH
            or type(lane.blockers) is not tuple or len(lane.blockers) > MAX_BATCH
            or any(not _reason(code) for code in lane.blockers)):
        raise ValueError("Expected bounded blocker codes and unresolved count")
    if any(not _check_time(item.committed_at, now) for item in lane.dispositions):
        raise ValueError("Invalid disposition time")
    for item in lane.work:
        lease = item.lease
        if lease is not None and (type(lease) is not Lease or not _check_time(lease.started_at, now)
                                  or not _time(lease.expires_at) or lease.expires_at <= lease.started_at):
            raise ValueError("Invalid lease window")


class Monitor:
    """Progress and delivery journal. Use an isolated SQLite database."""

    def __init__(self, db):
        self.db = db
        with _transaction(db):
            tables = {name for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables <= _TABLES:
                raise ValueError("Not an isolated progress journal")
            for schema in (
                """CREATE TABLE IF NOT EXISTS progress_lanes(
                    scope TEXT, lane TEXT, since REAL, last_progress REAL, observed REAL NOT NULL,
                    episode INTEGER NOT NULL, open INTEGER NOT NULL, PRIMARY KEY(scope,lane))""",
                """CREATE TABLE IF NOT EXISTS progress_seen(
                    scope TEXT, lane TEXT, evidence TEXT, PRIMARY KEY(scope,lane,evidence))""",
                """CREATE TABLE IF NOT EXISTS progress_work(
                    scope TEXT, lane TEXT, work TEXT, first_seen REAL NOT NULL, PRIMARY KEY(scope,lane,work))""",
                """CREATE TABLE IF NOT EXISTS progress_handoffs(
                    scope TEXT, handoff TEXT, issued REAL NOT NULL, terminal TEXT, outcome TEXT,
                    PRIMARY KEY(scope,handoff))""",
                """CREATE TABLE IF NOT EXISTS progress_reports(
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, next_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, receipt TEXT, superseded INTEGER NOT NULL DEFAULT 0)""",
            ):
                db.execute(schema)

    def evaluate(self, scope, now, *, first_pass, review, handoffs=(), receipts=(),
                 stall_seconds=DEFAULT_STALL_SECONDS):
        """Atomically evaluate both lanes from one caller-verified observation.

        Terminal receipts must be authenticated upstream. Calling this method
        neither validates that external authority nor releases a hold.
        """
        if not _hash(scope) or not _time(now) or type(stall_seconds) is not int or not 1 <= stall_seconds <= 86400:
            raise ValueError("Expected a bounded observation and opaque scope")
        _validate_lane(first_pass, now)
        _validate_lane(review, now)
        _unique(handoffs, Handoff, lambda item: item.key)
        _unique(receipts, TerminalReceipt, lambda item: item.handoff)
        if (any(not _check_time(item.issued_at, now) for item in handoffs)
                or any(not _hash(r.proof) or not _check_time(r.committed_at, now)
                       or r.outcome not in ("reviewed", "hard_blocked") or not _reason(r.reason) for r in receipts)):
            raise ValueError("Expected exact handoff and terminal facts")

        with _transaction(self.db):
            pending, terminal_atoms, outcomes = self._handoffs(scope, handoffs, receipts)
            first = self._lane(scope, "first_pass", now, first_pass, (), (), {}, stall_seconds, False)
            downstream = self._lane(scope, "review", now, review, pending, terminal_atoms, outcomes,
                                    stall_seconds, first["state"] == "BOUNDED_ACTIVE_WORK")
        return {"first_pass": first, "review": downstream}

    def _handoffs(self, scope, handoffs, receipts):
        db = self.db
        for item in handoffs:
            db.execute("INSERT OR IGNORE INTO progress_handoffs VALUES(?,?,?,NULL,NULL)",
                       (scope, item.key, item.issued_at))
            if db.execute("SELECT issued FROM progress_handoffs WHERE scope=? AND handoff=?",
                          (scope, item.key)).fetchone()[0] != item.issued_at:
                raise ValueError("Handoff issue time differs")

        atoms = []
        for item in receipts:
            row = db.execute("SELECT issued,terminal,outcome FROM progress_handoffs WHERE scope=? AND handoff=?",
                             (scope, item.handoff)).fetchone()
            if row is None or item.committed_at < row[0]:
                raise ValueError("Terminal receipt has no matching issued handoff")
            terminal = _digest(["terminal", item.proof, item.outcome, item.reason, item.committed_at])
            if row[1] is None:
                db.execute("UPDATE progress_handoffs SET terminal=?,outcome=? WHERE scope=? AND handoff=?",
                           (terminal, item.outcome, scope, item.handoff))
                atoms.append((_digest([item.handoff, terminal]), item.committed_at))
            elif row[1:] != (terminal, item.outcome):
                raise ValueError("Terminal receipt cannot replace prior proof")

        # Omitted handoffs survive incomplete snapshots. Only an exact terminal
        # receipt can retire them, even when the caller proves intake coverage.
        pending = db.execute("SELECT handoff,issued FROM progress_handoffs WHERE scope=? AND terminal IS NULL "
                             "LIMIT ?", (scope, MAX_BATCH+1)).fetchall()
        if len(pending) > MAX_BATCH:
            raise ValueError("Outstanding handoff limit exceeded")
        outcomes = dict(db.execute("SELECT outcome,count(*) FROM progress_handoffs "
                                   "WHERE scope=? AND terminal IS NOT NULL GROUP BY outcome", (scope,)))
        return pending, atoms, outcomes

    def _lane(self, scope, name, now, lane, pending, receipts, outcomes, stall, deferred):
        db = self.db
        fingerprint = _digest(["cal-tv-progress-v1", scope, name])
        old = db.execute("SELECT since,last_progress,observed,episode,open FROM progress_lanes "
                         "WHERE scope=? AND lane=?", (scope, name)).fetchone()
        since, last, observed, episode, opened = old or (None, None, now, 0, False)
        if now < observed:
            raise ValueError("Monitor clock moved backwards")
        committed = []
        atoms = [(_digest(["disposition", x.key]), x.committed_at) for x in lane.dispositions]
        terminal_keys = {key for key, _ in receipts}
        for key, at in [*atoms, *receipts]:
            fresh = db.execute("INSERT OR IGNORE INTO progress_seen VALUES(?,?,?)",
                               (scope, name, key)).rowcount
            if fresh and (old is None or at >= observed or key in terminal_keys):
                committed.append((at, key))
        if committed:
            last = max(last or 0, max(at for at, _ in committed))

        work_times = []
        for item in lane.work:
            db.execute("INSERT OR IGNORE INTO progress_work VALUES(?,?,?,?)", (scope, name, item.key, now))
            work_times.append(db.execute("SELECT first_seen FROM progress_work WHERE scope=? AND lane=? AND work=?",
                                        (scope, name, item.key)).fetchone()[0])
        count, unresolved = len(lane.work), lane.unresolved
        pressure = bool(count or unresolved or pending)
        since = (since if since is not None else now) if pressure else None
        elapsed = now-max(since, last or 0) if since is not None else 0
        leases = [item.lease for item in lane.work if item.lease is not None]
        bounded = [lease for lease in leases if now < lease.expires_at <= lease.started_at+MAX_LEASE_SECONDS]
        all_leases_bounded = bool(bounded) and len(bounded) == len(leases)
        active = all_leases_bounded and elapsed < stall + MAX_LEASE_SECONDS
        behind = deferred and pressure and len(bounded) == len(leases) and elapsed < stall + MAX_LEASE_SECONDS
        overdue = sum(now-issued >= stall for _, issued in pending)
        action = None
        if overdue or (pressure and elapsed >= stall and not (active or behind)):
            state = ("P1_HANDOFF_OWNERSHIP_UNPROVED" if overdue or (pending and not count and not unresolved)
                     else "P1_ACTIONABLE_NO_PROGRESS" if count
                     else "P1_UNRESOLVED_REVIEW")
            if not opened:
                episode += 1
                opened, action = True, "stall"
        elif pressure:
            state = ("BOUNDED_ACTIVE_WORK" if active else "DEFERRED_BEHIND_FIRST_PASS" if behind
                     else "AWAITING_DOWNSTREAM_REVIEW" if pending else "ACTIONABLE_PROGRESS_WINDOW" if count
                     else "UNRESOLVED_EVIDENCE")
            if committed and opened and not pending:
                opened, action = False, "recovered"
        else:
            state = ("NO_ACTIONABLE_DOCUMENTED_BLOCKERS" if lane.blockers or outcomes.get("hard_blocked")
                     else "NO_ACTIONABLE_REVIEW_COMPLETE" if outcomes.get("reviewed")
                     else "COVERED_EXHAUSTED" if lane.coverage_complete
                     else "NO_ACTIONABLE_COVERAGE_UNPROVED")
            if opened and (committed or lane.coverage_complete):
                opened, action = False, "recovered"

        age = max((now-issued for _, issued in pending), default=None)
        if action:
            event_id = _digest([fingerprint, episode, action])
            report = dict(id=event_id, kind=action, lane=name, fingerprint=fingerprint,
                          incident=episode, severity="P1", state=state,
                          evidence=[key for _, key in sorted(committed)[-8:]],
                          coverage_complete=lane.coverage_complete, pending_handoffs=len(pending),
                          oldest_handoff_age=age)
            db.execute("INSERT OR IGNORE INTO progress_reports(id,body,next_at) VALUES(?,?,?)",
                       (event_id, _json(report), now))
            if action == "recovered":
                db.execute("UPDATE progress_reports SET superseded=1 WHERE id=?",
                           (_digest([fingerprint, episode, "stall"]),))
        db.execute("INSERT OR REPLACE INTO progress_lanes VALUES(?,?,?,?,?,?,?)",
                   (scope, name, since, last, now, episode, int(opened)))
        return dict(state=state, actionable=count, unresolved=unresolved, blockers=lane.blockers,
                    incident_open=bool(opened), incident=episode, fingerprint=fingerprint,
                    last_committed_progress=last, pending_handoffs=len(pending),
                    oldest_handoff_age=age, oldest_serviceable_age=now-min(work_times) if work_times else None,
                    terminal_outcomes=outcomes)

    def reserve_due(self, now, *, limit=2):
        """Reserve report envelopes with bounded backoff, without sending."""
        if not _time(now) or type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("Invalid delivery bounds")
        with _transaction(self.db):
            rows = self.db.execute("""SELECT id,body,attempts FROM progress_reports
                WHERE receipt IS NULL AND superseded=0 AND next_at<=? ORDER BY rowid LIMIT ?""",
                (now, limit)).fetchall()
            for key, _, attempts in rows:
                self.db.execute("UPDATE progress_reports SET attempts=attempts+1,next_at=? WHERE id=?",
                                (now+min(3600,300*2**min(attempts,4)), key))
        return [json.loads(body) for _, body, _ in rows]

    def acknowledge(self, event_id, accepted_receipt):
        """A transport acknowledgement closes delivery, never a handoff."""
        if not _hash(event_id) or not _hash(accepted_receipt):
            raise ValueError("Expected opaque event and accepted receipt digests")
        with _transaction(self.db):
            row = self.db.execute("SELECT receipt FROM progress_reports WHERE id=?", (event_id,)).fetchone()
            if row is None or row[0] not in (None, accepted_receipt):
                raise ValueError("Unknown delivery or conflicting receipt")
            self.db.execute("UPDATE progress_reports SET receipt=? WHERE id=?", (accepted_receipt, event_id))
