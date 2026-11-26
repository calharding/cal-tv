"""Synthetic publication fixtures. No live transport or actual media."""
import errno
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

from media_automation import backlog_cli as cli, backlog_once, importer
from media_automation.core import ReviewRequired, Store
from tests.test_fast_import import probe


class Services:
    def __init__(self, config):
        self.config = config
        self.associations = []
        self.writes = 0

    def approved_plan(self, plan):
        return True

    def _check_plan(self, plan):
        pass

    def _item(self, plan):
        return {"tmdbId": 101}

    def owned(self, request):
        return False

    def jellyfin(self, route, body=None, method=None):
        if method not in (None, "GET"):
            raise AssertionError("unexpected service write")
        return {"Items": []}

    def read_arr(self, plan):
        return {"path": plan["new_path"], "files": self.associations}

    def update_arr(self, plan):
        self.writes += 1
        self.associations = [{"path": f["path"], "episode_ids": f["episode_ids"]} for f in plan["files"]]

    def check_jellyfin(self, request, plan):
        return [{"item": "fixture"}]


class PublicationMount(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.source = self.root / "complete"
        self.source.mkdir()
        self.original = self.source / "fixture.mkv"
        self.original.write_bytes(b"synthetic publication fixture")
        self.original.chmod(0o640)
        self.before = self.original_stat()
        stores = {role: Store(self.root / role / "state") for role in ("root", "manual", "batch", "films")}
        for store in stores.values():
            self.addCleanup(store.db.close)
        self.store = stores["batch"]
        locks = {role: store.root / "worker.lock" for role, store in stores.items()}
        locks["films"] = stores["films"].root.parent / "import.lock"
        for lock in locks.values():
            lock.touch()
        self.request = {"kind": "movie", "title": "Fixture", "year": 2000, "provider_id": "tmdb:101",
                        "audible_language_confirmed": True}
        job = self.store.create(self.request)
        self.job = job["id"]
        self.store.change(self.job, "AUTHORIZED", approval="synthetic")
        self.config = dict(backlog_fast_enabled=True, manual_service_writes_enabled=True,
                           backlog_shared_store_root=str(self.store.root),
                           backlog_journals={r: str(s.root / "jobs.sqlite3") for r, s in stores.items()},
                           backlog_writer_locks={r: str(p) for r, p in locks.items()},
                           storage_guard_script="fixture", storage_guard_config="fixture", floor_bytes=0,
                           complete_root=str(self.source))
        self.services = Services(self.config)
        self.media = self.root / "library"
        self.media.mkdir()
        self.select_media(self.media)

    def original_stat(self):
        st = self.original.stat()
        return (self.original.read_bytes(), st.st_mode, st.st_ino, st.st_mtime_ns, st.st_nlink)

    def select_media(self, media):
        self.media = media
        self.config["media_root"] = str(media)
        self.target = media / (str(importer.destination(self.request)) + self.original.suffix)
        self.nfo = self.target.with_suffix(".nfo")
        self.plan = {"provider_id": "tmdb:101", "app": "radarr", "new_path": str(self.target.parent),
                     "files": [{"host_path": str(self.target), "path": str(self.target),
                                "canonical_episode": None, "episode_ids": []}]}
        self.doc = dict(config=self.config, request=self.request, source=str(self.source), job_id=self.job,
                        approval_reference="synthetic", plan=self.plan)

    def preview(self):
        with patch.object(cli, "load_policy", return_value=({}, "synthetic-policy")):
            return cli.execute_scope(self.doc, "check", services_factory=lambda *_: (self.services, self.services))

    def execute(self):
        with patch.object(backlog_once, "load_policy", return_value=({}, "synthetic-policy")), \
             patch.object(backlog_once.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            return backlog_once.run(self.store, self.job, self.source, self.config, self.plan, self.services,
                                    expected_request=self.request, expected_approval="synthetic", probe_fn=probe)

    def assert_no_publication(self):
        self.assertEqual(self.original_stat(), self.before)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.nfo.exists())
        self.assertEqual(self.services.writes, 0)

    def assert_unreserved(self):
        self.assert_no_publication()
        self.assertEqual(self.store.get(self.job)["state"], "AUTHORIZED")
        self.assertFalse(self.target.parent.exists())
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM imports").fetchone())
        self.assertIsNone(self.store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('backlog_runs','backlog_admission','import_identity','backlog_metadata')"
        ).fetchone())

    def test_same_mount_hardlinks_through_ordinary_coordinator(self):
        self.assertTrue(self.preview()["reconciliation"]["safe"])
        self.assert_unreserved()
        result = self.execute()
        self.assertTrue(os.path.samefile(self.original, self.target))
        self.assertTrue(self.nfo.is_file())
        self.assertEqual(self.original_stat()[:-1], self.before[:-1])
        self.assertEqual(self.original.stat().st_nlink, self.before[-1] + 1)
        self.assertEqual(self.store.get(self.job)["state"], "READY")
        with patch.object(importer, "require_same_publication_filesystem", side_effect=AssertionError("new-link guard")):
            self.assertEqual(self.execute(), result)  # A verified prior run does not link again.
        self.assertEqual(self.services.writes, 1)

    def test_same_device_different_linux_mount_stops_before_preview_or_reservation(self):
        read = mock_open()
        read.side_effect = [mock_open(read_data=f"mnt_id:\t{identity}\n")()
                            for identity in (101, 102, 101, 102)]
        with patch.object(importer.sys, "platform", "linux"), \
             patch.object(importer.os, "O_PATH", getattr(os, "O_PATH", 0), create=True), \
             patch.object(importer, "open", read, create=True):
            with self.assertRaisesRegex(ReviewRequired, "EXDEV"):
                self.preview()
            with self.assertRaisesRegex(ReviewRequired, "EXDEV"):
                self.execute()
        self.assert_unreserved()

    def test_missing_linux_mount_identity_never_falls_back_to_device_only(self):
        for fdinfo in ("ino:\t12\n", "mnt_id:\tbogus\n", "mnt_id:\t1\nmnt_id:\t2\n"):
            with self.subTest(fdinfo=fdinfo), patch.object(importer.sys, "platform", "linux"), \
                 patch.object(importer.os, "O_PATH", getattr(os, "O_PATH", 0), create=True), \
                 patch.object(importer, "open", mock_open(read_data=fdinfo), create=True):
                with self.assertRaisesRegex(ReviewRequired, "mount identity is unavailable"):
                    importer.require_same_publication_filesystem(self.original, self.target)
        self.assert_unreserved()

    def test_mount_changed_after_intent_blocks_before_nfo_or_media_publication(self):
        # Beginning on one mount, then presenting the same device with a new
        # mount ID after the durable per-file intent models a late bind change.
        def filesystem(path, *, directory):
            reserved = self.store.db.execute("SELECT 1 FROM imports").fetchone() is not None
            return path.stat().st_dev, 202 if directory and reserved else 201

        with patch.object(importer, "_publication_filesystem", side_effect=filesystem):
            with self.assertRaisesRegex(ReviewRequired, "EXDEV"):
                self.execute()
        self.assert_no_publication()
        self.assertEqual(self.store.db.execute("SELECT state FROM imports").fetchone()[0], "INTENT")
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='backlog_metadata'").fetchone())
        self.assertEqual(self.store.db.execute("SELECT phase FROM backlog_runs").fetchone()[0], "BLOCKED")

    def test_source_changed_during_mkdir_stops_before_intent_or_publication(self):
        mkdir = Path.mkdir
        replacement = b"synthetic source changed during destination mkdir"
        changed = False

        def mutate_after_mkdir(path, *args, **kwargs):
            nonlocal changed
            result = mkdir(path, *args, **kwargs)
            if path == self.target.parent and not changed:
                self.original.write_bytes(replacement)
                changed = True
            return result

        with patch.object(Path, "mkdir", mutate_after_mkdir):
            with self.assertRaisesRegex(ReviewRequired, "Source identity or metadata changed"):
                self.execute()

        self.assertTrue(changed)
        self.assertEqual(self.original.read_bytes(), replacement)
        self.assertTrue(self.target.parent.is_dir())
        self.assertFalse(self.target.exists())
        self.assertFalse(self.nfo.exists())
        self.assertEqual(self.services.writes, 0)
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM imports").fetchone())
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM import_identity").fetchone())
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='backlog_metadata'").fetchone())
        self.assertEqual(self.store.db.execute("SELECT phase FROM backlog_runs").fetchone()[0], "BLOCKED")

    def test_source_replaced_during_final_mount_check_preserves_only_old_intent(self):
        before = importer.identity_seal(self.original)
        earlier = self.root / "earlier-source.mkv"
        replacement = b"synthetic new inode after intent"
        filesystem = importer._publication_filesystem

        def replace_at_final_check(path, *, directory):
            observed = filesystem(path, directory=directory)
            if (directory and not earlier.exists()
                    and self.store.db.execute("SELECT 1 FROM imports").fetchone() is not None):
                self.original.rename(earlier)
                self.original.write_bytes(replacement)
            return observed

        with patch.object(importer, "_publication_filesystem", side_effect=replace_at_final_check):
            with self.assertRaisesRegex(ReviewRequired, "Source identity or metadata changed"):
                self.execute()

        self.assertEqual(importer.identity_seal(earlier), before)
        self.assertEqual(earlier.read_bytes(), self.before[0])
        self.assertEqual(self.original.read_bytes(), replacement)
        self.assertTrue(self.target.parent.is_dir())
        self.assertFalse(self.target.exists())
        self.assertFalse(self.nfo.exists())
        self.assertEqual(self.services.writes, 0)
        row = self.store.db.execute("SELECT src,dst,device,inode,state FROM imports").fetchone()
        self.assertEqual(tuple(row), (str(self.original), str(self.target), *before[:2], "INTENT"))
        saved = self.store.db.execute("SELECT seal FROM import_identity").fetchone()[0]
        self.assertEqual(json.loads(saved), before)
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='backlog_metadata'").fetchone())
        self.assertEqual(self.store.db.execute("SELECT phase FROM backlog_runs").fetchone()[0], "BLOCKED")

    def test_reviewed_sidecar_on_another_mount_blocks_before_first_link(self):
        sidecar = self.source / "fixture.srt"
        sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nSynthetic caption\n")
        self.request["subtitle_mappings"] = [{"source": sidecar.name, "video": self.original.name,
                                              "language": "eng", "review_approval": "synthetic"}]
        def filesystem(path, *, directory):
            return path.stat().st_dev, 302 if path == sidecar else 301

        with patch.object(importer, "_publication_filesystem", side_effect=filesystem):
            with self.assertRaisesRegex(ReviewRequired, "EXDEV"):
                importer.import_job(self.store, dict(id=self.job, request=self.request),
                                    self.source, self.media, probe_fn=probe, screening="light")
        self.assert_no_publication()
        self.assertFalse(self.target.parent.exists())
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM imports").fetchone())

    def test_kernel_exdev_after_check_does_not_copy_or_remove_intent(self):
        def refused_link(src, dst):
            raise OSError(errno.EXDEV, "synthetic kernel refusal")
        with self.assertRaisesRegex(ReviewRequired, "hardlink failed; no copy fallback"):
            importer.import_job(self.store, self.store.get(self.job), self.source, self.media, probe_fn=probe,
                                screening="light", link_fn=refused_link)
        self.assert_no_publication()
        self.assertEqual(self.store.db.execute("SELECT state FROM imports").fetchone()[0], "INTENT")

    @unittest.skipUnless(os.environ.get("CAL_TV_TEST_OTHER_MOUNT"), "separate writable scratch filesystem not supplied")
    def test_real_distinct_devices_refuse_before_reservation(self):
        with tempfile.TemporaryDirectory(dir=os.environ["CAL_TV_TEST_OTHER_MOUNT"]) as other:
            self.select_media(Path(other).resolve())
            self.assertNotEqual(self.original.stat().st_dev, self.media.stat().st_dev)
            target = self.media / "kernel-test"
            with self.assertRaises(OSError) as refused:
                os.link(self.original, target)
            self.assertEqual(refused.exception.errno, errno.EXDEV)
            self.assertFalse(target.exists())
            with self.assertRaisesRegex(ReviewRequired, "EXDEV"):
                self.preview()
            with self.assertRaisesRegex(ReviewRequired, "EXDEV"):
                self.execute()
            self.assert_unreserved()


if __name__ == "__main__":
    unittest.main()
