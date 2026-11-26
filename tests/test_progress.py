"""Exercise public progress accounting with opaque synthetic receipts only."""
from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest

from media_automation.progress import (
    DEFAULT_STALL_SECONDS, Disposition, Handoff, Lane, Lease, Monitor, TerminalReceipt, Work,
)


def key(label):
    return sha256(label.encode()).hexdigest()


class Progress(unittest.TestCase):
    scope = key("synthetic-monitor")

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.database = Path(scratch.name)/"monitor.sqlite"

    def observe(self, now, *, first_pass=Lane(), review=Lane(), **facts):
        # A fresh monitor and SQLite connection simulate each scheduled invocation.
        with closing(sqlite3.connect(self.database)) as db:
            return Monitor(db).evaluate(self.scope, now, first_pass=first_pass, review=review, **facts)

    def due(self, now):
        with closing(sqlite3.connect(self.database)) as db:
            return Monitor(db).reserve_due(now)

    def ack(self, report):
        with closing(sqlite3.connect(self.database)) as db:
            Monitor(db).acknowledge(report["id"], key("synthetic-accepted-delivery"))

    def test_repeat_observations_and_same_evidence_do_not_reset_stall(self):
        item = Work(key("synthetic-work"))
        progress = key("unchanged-commit")
        first = Lane(work=(item,), dispositions=(Disposition(progress, 8),))
        self.assertEqual(self.observe(10, first_pass=first)["first_pass"]["last_committed_progress"], 8)
        for now in (20, 200, 489):
            replay = Lane(work=(item,), dispositions=(Disposition(progress, now-1),))
            self.assertEqual(self.observe(now, first_pass=replay)["first_pass"]["state"],
                             "ACTIONABLE_PROGRESS_WINDOW")
        stalled = self.observe(490, first_pass=first)["first_pass"]
        self.assertEqual(stalled["state"], "P1_ACTIONABLE_NO_PROGRESS")
        self.assertEqual(stalled["oldest_serviceable_age"], DEFAULT_STALL_SECONDS)
        alert, = self.due(490)
        self.assertEqual(alert["severity"], "P1")
        self.assertEqual(alert["fingerprint"], stalled["fingerprint"])
        self.assertEqual(self.due(491), [])
        self.assertEqual(self.due(790), [alert])
        self.ack(alert)
        self.ack(alert)
        actual = Lane(work=(item,), dispositions=(Disposition(key("new-verified-commit"), 810),))
        recovered = self.observe(811, first_pass=actual)["first_pass"]
        self.assertFalse(recovered["incident_open"])
        resolution, = self.due(811)
        self.assertEqual((resolution["kind"], resolution["fingerprint"]), ("recovered", alert["fingerprint"]))
        self.assertTrue(resolution["evidence"])
        self.ack(resolution)
        again = self.observe(1290, first_pass=actual)["first_pass"]
        second, = self.due(1290)
        self.assertEqual(again["state"], "P1_ACTIONABLE_NO_PROGRESS")
        self.assertEqual(second["fingerprint"], alert["fingerprint"])
        self.assertEqual(second["incident"], alert["incident"] + 1)
        self.assertNotEqual(second["id"], alert["id"])

    def test_first_pass_motion_cannot_mask_stranded_review(self):
        review = Lane(work=(Work(key("synthetic-review")),))
        initial = Lane(work=(Work(key("stock-1"), Lease(10, 400)),))
        self.observe(20, first_pass=initial, review=review, stall_seconds=100)
        bounded = self.observe(121, first_pass=initial, review=review, stall_seconds=100)
        self.assertEqual(bounded["first_pass"]["state"], "BOUNDED_ACTIVE_WORK")
        self.assertEqual(bounded["review"]["state"], "DEFERRED_BEHIND_FIRST_PASS")
        self.assertEqual(self.due(121), [])
        stock = Lane(work=(Work(key("stock-2"), Lease(580, 900)),),
                     dispositions=(Disposition(key("separate-stock-receipt"), 585),))
        late = self.observe(610, first_pass=stock, review=review, stall_seconds=100)
        self.assertEqual(late["first_pass"]["state"], "BOUNDED_ACTIVE_WORK")
        self.assertEqual(late["first_pass"]["last_committed_progress"], 585)
        self.assertEqual(late["review"]["state"], "P1_ACTIONABLE_NO_PROGRESS")
        self.assertEqual(late["review"]["oldest_serviceable_age"], 590)
        alert, = self.due(610)
        self.assertEqual(alert["lane"], "review")

    def test_expired_or_extended_lease_is_not_progress(self):
        long = Lane(work=(Work(key("stock"), Lease(5, 200)),))
        self.observe(10, first_pass=long, stall_seconds=100)
        self.assertEqual(self.observe(111, first_pass=long, stall_seconds=100)["first_pass"]["state"],
                         "BOUNDED_ACTIVE_WORK")
        self.assertEqual(self.observe(201, first_pass=long, stall_seconds=100)["first_pass"]["state"],
                         "P1_ACTIONABLE_NO_PROGRESS")
        extension = Lane(work=(Work(key("stock"), Lease(5, 1000)),))
        self.assertEqual(self.observe(211, first_pass=extension, stall_seconds=100)["first_pass"]["state"],
                         "P1_ACTIONABLE_NO_PROGRESS")

    def test_handoff_is_neither_progress_nor_resolved_by_coverage_delivery_or_other_work(self):
        pending = Lane(work=(Work(key("synthetic-proposal")),))
        self.observe(10, review=pending, stall_seconds=100)
        self.observe(110, review=pending, stall_seconds=100)
        alert, = self.due(110)
        self.ack(alert)
        a, b = Handoff(key("case-a"), 112), Handoff(key("case-b"), 113)
        complete = Lane(coverage_complete=True, dispositions=(Disposition(key("unrelated-completion"), 115),))
        post = self.observe(120, review=complete, handoffs=(a, b), stall_seconds=100)["review"]
        self.assertTrue(post["incident_open"])
        self.assertEqual(post["pending_handoffs"], 2)
        self.assertEqual(self.due(120), [])
        # Omit both issued items from the next observation.
        stayed = self.observe(220, review=complete, stall_seconds=100)["review"]
        self.assertEqual(stayed["state"], "P1_HANDOFF_OWNERSHIP_UNPROVED")
        self.assertEqual(stayed["oldest_handoff_age"], 108)
        self.assertEqual(stayed["fingerprint"], alert["fingerprint"])
        reviewed = TerminalReceipt(a.key, key("verified-review"), 150, "reviewed", "saved_evidence_reviewed")
        partial = self.observe(221, review=complete, receipts=(reviewed,), stall_seconds=100)["review"]
        self.assertTrue(partial["incident_open"])
        self.assertEqual(partial["pending_handoffs"], 1)
        self.assertEqual(partial["terminal_outcomes"], {"reviewed": 1})
        blocked = TerminalReceipt(b.key, key("verified-hold"), 222, "hard_blocked", "explicit_hold")
        finished = self.observe(223, review=complete, receipts=(reviewed, blocked), stall_seconds=100)["review"]
        self.assertEqual(finished["state"], "NO_ACTIONABLE_DOCUMENTED_BLOCKERS")
        self.assertFalse(finished["incident_open"])
        self.assertEqual(finished["terminal_outcomes"], {"hard_blocked": 1, "reviewed": 1})
        resolution, = self.due(223)
        self.assertEqual(resolution["kind"], "recovered")
        self.assertTrue(resolution["evidence"])

    def test_exact_review_can_resolve_without_assuming_physical_coverage(self):
        target = Handoff(key("issued-review"), 10)
        self.observe(10, handoffs=(target,), stall_seconds=100)
        stalled = self.observe(110, stall_seconds=100)["review"]
        self.assertEqual(stalled["state"], "P1_HANDOFF_OWNERSHIP_UNPROVED")
        done = TerminalReceipt(target.key, key("sealed-proof"), 90, "reviewed", "saved_evidence_reviewed")
        reviewed = self.observe(111, receipts=(done,), stall_seconds=100)["review"]
        self.assertFalse(reviewed["incident_open"])
        self.assertEqual(reviewed["state"], "NO_ACTIONABLE_REVIEW_COMPLETE")
        event, = self.due(111)
        self.assertEqual(event["kind"], "recovered")  # A stale undelivered stall is retired.
        self.assertFalse(event["coverage_complete"])

    def test_held_only_unknown_coverage_and_exhaustion_are_distinct(self):
        result = self.observe(10, first_pass=Lane(coverage_complete=True),
                              review=Lane(blockers=("explicit_hold",), coverage_complete=True))
        self.assertEqual(result["first_pass"]["state"], "COVERED_EXHAUSTED")
        self.assertEqual(result["review"]["state"], "NO_ACTIONABLE_DOCUMENTED_BLOCKERS")
        self.assertEqual(self.observe(1000)["first_pass"]["state"], "NO_ACTIONABLE_COVERAGE_UNPROVED")
        self.assertEqual(self.due(1001), [])
        unknown = Lane(unresolved=2)
        self.assertEqual(self.observe(2000, review=unknown, stall_seconds=100)["review"]["state"],
                         "UNRESOLVED_EVIDENCE")
        self.assertEqual(self.observe(2101, review=unknown, stall_seconds=100)["review"]["state"],
                         "P1_UNRESOLVED_REVIEW")

    def test_wrong_unknown_or_conflicting_receipt_cannot_retire_a_handoff(self):
        item = Handoff(key("exact-handoff"), 20)
        self.observe(30, handoffs=(item,))
        bad = TerminalReceipt(key("different-handoff"), key("proof"), 31, "reviewed", "saved_evidence_reviewed")
        with self.assertRaisesRegex(ValueError, "no matching issued"):
            self.observe(31, receipts=(bad,))
        with self.assertRaisesRegex(ValueError, "terminal facts"):
            self.observe(31, receipts=(TerminalReceipt(item.key, key("read"), 31, "read", "seen"),))
        self.assertEqual(self.observe(32)["review"]["pending_handoffs"], 1)
        good = TerminalReceipt(item.key, key("proof"), 33, "reviewed", "saved_evidence_reviewed")
        self.observe(33, receipts=(good,))
        self.observe(34, receipts=(good,))
        with self.assertRaisesRegex(ValueError, "cannot replace"):
            self.observe(35, receipts=(TerminalReceipt(item.key, good.proof, 35, good.outcome, good.reason),))
        self.assertEqual(self.observe(36)["review"]["terminal_outcomes"], {"reviewed": 1})

    def test_monitor_rejects_native_journal_and_clock_regression(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE unrelated_journal(id)")
            with self.assertRaisesRegex(ValueError, "Not an isolated progress journal"):
                Monitor(db)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                             [("unrelated_journal",)])
        self.observe(10)
        with self.assertRaisesRegex(ValueError, "clock moved backwards"):
            self.observe(9, handoffs=(Handoff(key("rollback"), 6),))
        self.assertEqual(self.observe(11)["review"]["pending_handoffs"], 0)
