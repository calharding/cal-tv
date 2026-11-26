# Drive the shell launcher and portable analysis with no deployment configuration.
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from media_automation import continuity, importer, light_prepare
from media_automation.core import ReviewRequired, Store, sha256


ROOT = Path(__file__).resolve().parents[1]


class Entrypoint(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def cli(self, *args):
        result = subprocess.run([str(ROOT / "fastpassctl"), *map(str, args)], cwd=self.root,
                                capture_output=True, text=True, timeout=12)
        return result.returncode, json.loads(result.stdout)

    def test_offline_scope_through_launcher(self):
        doc = {"schema_version": 1, "config": {"backlog_shared_store_root": str(self.root)},
               "request": {"kind": "movie", "title": "Synthetic Film", "year": 2000,
                           "provider_id": "tmdb:fixture"},
               "plan": {}, "source": str(self.root / "source"), "job_id": "synthetic-job",
               "approval_reference": "synthetic-approval"}
        scope = self.root / "scope.json"
        scope.write_text(json.dumps(doc))
        self.assertEqual(self.cli("validate", "--scope", scope), (0, {
            "status": "VALIDATED_OFFLINE", "network_used": False, "activated": False}))
        doc["request"]["combined_episode_range"] = {"schema_version": 6}
        scope.write_text(json.dumps(doc))
        code, out = self.cli("validate", "--scope", scope)
        self.assertEqual((code, out["status"]), (2, "STOPPED"))
        self.assertEqual({p.name for p in self.root.iterdir()}, {"scope.json"})

    def test_shipped_site_example_cannot_supply_private_settings_or_activate(self):
        config = json.loads((ROOT / "config" / "site.example.json").read_text())
        for key in ("media_root", "complete_root", "arr_media_root",
                    "backlog_shared_store_root", "jellyfin_url",
                    "jellyfin_library_root", "jellyfin_key_file",
                    "quality_policy_path", "quality_policy_version",
                    "quality_policy_sha256"):
            with self.subTest(setting=key):
                self.assertTrue(config[key] is None, "Published location/reference must be unset")
        for key in ("backlog_journals", "backlog_writer_locks"):
            with self.subTest(setting=key):
                self.assertTrue(config[key] == {}, "Published bindings must be empty")
        for key in ("backlog_fast_enabled", "manual_service_writes_enabled",
                    "prepared_scope_enabled", "jellyfin_enabled",
                    "targeted_refresh_permitted", "acquisition_enabled",
                    "geek_enabled"):
            with self.subTest(setting=key):
                self.assertTrue(config[key] is False, "Published permission must be disabled")

        scope = self.root / "fixture-scope.json"
        scope.write_text(json.dumps({
            "schema_version": 1, "config": config, "plan": {},
            "request": {"kind": "movie", "title": "Synthetic Film", "year": 2000,
                        "provider_id": "tmdb:fixture"},
            "source": str(self.root / "synthetic-source"),
            "job_id": "synthetic-job", "approval_reference": "synthetic-approval",
        }))
        self.assertTrue(self.cli("validate", "--scope", scope) == (2, {
            "status": "STOPPED", "error_type": "ReviewRequired",
            "detail": "Explicit shared store required",
        }), "Offline validation must stop with a value-free reason")
        self.assertEqual({p.name for p in self.root.iterdir()}, {"fixture-scope.json"})

    def test_queue_status_reports_holds_and_never_creates_queue(self):
        code, out = self.cli("--root", self.root, "status")
        self.assertEqual((code, out["status"]), (2, "BLOCKED_QUEUE_UNAVAILABLE"))
        self.assertEqual(list(self.root.iterdir()), [])
        db = sqlite3.connect(self.root / "queue.sqlite3")
        with db:
            db.execute("CREATE TABLE items(state TEXT NOT NULL)")
            db.execute("INSERT INTO items VALUES('held')")
        db.close()
        before = (self.root / "queue.sqlite3").read_bytes()
        code, out = self.cli("--root", self.root, "status")
        self.assertEqual((code, out["unresolved"], out["active"]), (0, 1, 0))
        self.assertFalse(out["source_coverage_verified"])
        self.assertEqual(before, (self.root / "queue.sqlite3").read_bytes())

    def test_direct_coordinator_execute_disabled(self):
        command = [os.environ.get("PYTHON", "python3"), "-B", "-m", "media_automation.backlog_cli",
                   "--scope", str(self.root / "missing.json"), "--execute"]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 2)
        self.assertIn("disabled", json.loads(result.stdout)["detail"])
        self.assertEqual(list(self.root.iterdir()), [])


class Discovery(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        continuity.initialize(self.db)
        self.row = {"source": "synthetic.mkv", "seal": [1, 2, 3, 4, 33188],
                    "status": "PENDING_FIRST_PASS"}

    def refresh(self, *rows, marker="fixture-marker"):
        continuity.refresh(self.db, {"rows": list(rows), "errors": [],
                                     "coverage_complete": True}, marker=marker, now=10)

    def test_incomplete_keeps_previous_checkpoint_and_rows(self):
        self.refresh(self.row)
        before = list(self.db.iterdump())
        with self.assertRaises(continuity.IntakeCoverageError):
            continuity.refresh(self.db, {"rows": [], "errors": ["a scan failed"],
                                         "coverage_complete": False}, marker="new", now=20)
        self.assertEqual(list(self.db.iterdump()), before)
        self.assertEqual(continuity.status(self.db, discovery_error=True)["state"],
                         "BLOCKED_INTAKE_AUDIT_INCOMPLETE")

    def test_empty_runnable_is_not_exhaustion(self):
        self.refresh(dict(self.row, status="LIBRARY_ALIAS_NOT_IMPORT_RECEIPT"))
        self.assertIsNone(continuity.claim(self.db, 20, "analyzer"))
        result = continuity.status(self.db)
        self.assertEqual((result["runnable"], result["unresolved"]), (0, 1))
        self.assertEqual(result["state"], "NO_RUNNABLE_WORK_UNRESOLVED_SOURCES")
        with self.assertRaises(continuity.IntakeCoverageError):
            self.refresh(dict(self.row, status="VERIFIED_IMPORT_RECEIPT"))
        self.refresh(dict(self.row, status="VERIFIED_IMPORT_RECEIPT", receipt={"verified": True}))
        self.assertEqual(continuity.status(self.db)["unresolved"], 0)

    def test_complete_scan_cannot_silently_drop_a_missing_source(self):
        self.refresh(self.row)
        self.refresh()
        result = continuity.status(self.db)
        self.assertEqual((result["runnable"], result["unresolved"]), (0, 1))
        self.assertEqual(result["counts"], {"blocked_missing_from_complete_audit": 1})

    def test_lease_cannot_be_finished_after_expiry(self):
        self.refresh(self.row)
        key, _ = continuity.claim(self.db, 20, "owner-one", lease_seconds=2)
        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            continuity.finish(self.db, key, "owner-one", "blocked_test", {"reason": "tested"}, now=23)
        self.assertEqual(continuity.claim(self.db, 23, "owner-two")[0], key)
        continuity.finish(self.db, key, "owner-two", "blocked_test", {"reason": "tested"}, now=24)
        self.refresh(self.row)
        self.assertEqual(continuity.status(self.db)["counts"], {"blocked_test": 1})


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
class GeneratedMedia(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "intake"
        self.library = self.root / "library"
        self.source.mkdir()
        self.library.mkdir()
        sub = self.root / "fabricated.srt"
        sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nSynthetic caption.\n")
        self.video = self.source / "synthetic.mkv"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=96x64:r=2",
             "-f", "lavfi", "-i", "sine=f=480:r=8000", "-i", str(sub), "-t", "33",
             "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "mpeg4", "-c:a", "pcm_s16le",
             "-c:s", "srt", "-metadata:s:a:0", "language=eng", "-metadata:s:s:0", "language=fra",
             "-disposition:a:0", "default", "-disposition:s:0", "default+forced", str(self.video)],
            check=True, capture_output=True, timeout=30)

    def test_default_subtitles_stop_raw_import_and_remux_preserves_original(self):
        original = sha256(self.video)
        seal = importer.identity_seal(self.video)
        request = {"kind": "movie", "title": "Synthetic Film", "year": 2000, "provider_id": "tmdb:fixture",
                   "audible_language_confirmed": True, "max_height": 480}
        store = Store(self.root / "journal")
        self.addCleanup(store.db.close)
        job = store.create(request)
        with self.assertRaisesRegex(ReviewRequired, "(?i)subtitle"):
            importer.import_job(store, job, self.source, self.library, screening="light")
        self.assertEqual(list(self.library.iterdir()), [])
        self.assertEqual(store.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0)
        prepared = self.root / "prepared"
        prepared.mkdir()
        destination = prepared / "synthetic.mkv"
        options = dict(expected_seal=seal, max_bytes=10**7, max_height=480, english_index=1)
        # Synthetic capacity is the only mocked dependency. Stream copy and probes are real.
        with patch.object(light_prepare.os, "statvfs", return_value=SimpleNamespace(f_bavail=1, f_frsize=1)):
            with self.assertRaisesRegex(ReviewRequired, "Insufficient space"):
                light_prepare.prepare(self.video, destination, self.root / "intent", **options)
        self.assertFalse(destination.exists())
        self.assertFalse((self.root / "intent").exists())
        with patch.object(light_prepare.os, "statvfs", return_value=SimpleNamespace(f_bavail=10**8, f_frsize=4096)):
            result = light_prepare.prepare(self.video, destination, self.root / "intent", **options)
        self.assertEqual(result["mode"], "STREAM_COPY")
        self.assertFalse(result["full_av_integrity_proved"])
        self.assertEqual(importer.identity_seal(self.video), seal)
        self.assertEqual(sha256(self.video), original)
        tracks = [s for s in importer.probe(destination)["streams"] if s["codec_type"] == "subtitle"]
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["tags"]["language"], "fra")
        self.assertFalse(tracks[0]["disposition"]["default"] or tracks[0]["disposition"]["forced"])
        rows = importer.import_job(store, job, prepared, self.library, screening="light")
        self.assertTrue(os.path.samefile(destination, rows[0]["destination"]))
        self.assertFalse(os.path.samefile(self.video, rows[0]["destination"]))
        self.assertEqual(sha256(self.video), original)


class PortableMovieScope(unittest.TestCase):
    def test_exact_generic_descriptor_and_installed_exception_denial(self):
        from media_automation.prepared_scope import movie_remux_scope_from_result, validate_movie_remux_scope
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            old = root / "source.mkv"
            old.write_bytes(b"synthetic original")
            prepared = root / "prepared"
            prepared.mkdir()
            new = prepared / "derivative.mkv"
            new.write_bytes(b"synthetic derivative")
            policy = ROOT / "config" / "quality-policy.example.json"
            digest = sha256(policy)
            request = dict(kind="movie", provider_id="tmdb:fixture", title="Synthetic Film",
                           year=2000, max_height=480, preparation_approval_reference="review-fixture")
            result = dict(status="PREPARED", mode="STREAM_COPY", all_tracks_preserved_structurally=True,
                          full_av_integrity_proved=False, maximum_bytes=100, maximum_height=480,
                          path=str(new), source_identity_seal=importer.identity_seal(old),
                          prepared_identity_seal=importer.identity_seal(new),
                          probe={"format":{"duration": "36"}, "streams":[{"codec_type":"video","width":96,"height":64}]})
            binding = {"output_cap_bytes":100, "approval_reference":"review-fixture", "policy_sha256":digest}
            scope = movie_remux_scope_from_result(request, old, result, **binding)
            config = dict(quality_policy_path=str(policy), quality_policy_version="fixture-v1",
                          quality_policy_sha256=digest)
            self.assertEqual(validate_movie_remux_scope(config, request, prepared, scope), scope)
            request["existing_movie_size_exception"] = {"schema_version": 2}
            with self.assertRaisesRegex(ReviewRequired, "Installation-specific"):
                validate_movie_remux_scope(config, request, prepared, scope)
            with self.assertRaisesRegex(ReviewRequired, "Installation-specific"):
                movie_remux_scope_from_result(request, old, result, **binding)
