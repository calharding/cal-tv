# Development and release boundaries

Use a local Git checkout. Add synthetic tests and record exact verification in
each PR. Never commit actual requests, configured service URLs, tokens, job
records, library receipts, operational logs, media, or site review evidence.

Start at [the launcher](../fastpassctl) and [offline commands](../media_automation/fastpass.py).
For the guarded worker, see [backlog_once.py](../media_automation/backlog_once.py).
The [architecture guide](architecture.md) connects these paths and includes a
synthetic exercise that needs no site configuration.

Test discovery, source custody, journal state, and independent library verification
separately. For an import, success is a durable verified receipt against the
destination and canonical identity. A shadowed alias, incomplete scan, lease,
or unverified publication is not success. Held and deferred work must remain
visible even when no jobs can run.

Live installation is not part of this repo. An eventual site rollout requires
its own explicitly scoped review and bounded rollback plan. Skip automatic
migration, replay or cleanup on ambiguous state. A rollback must retain source
bytes and prior receipts. Park the exact source with the reason and evidence if
any admission test fails. Never let a branch push or merge run an importer.

## Publication mount guard

The portable check compares the source with the deepest existing directory
above the intended destination. It does not create files, copy data, or grant
publication authority. A fresh coordinator run checks again before admission,
and the importer checks each new link at the publication boundary. A mismatch
observed there stops before the media or its publication callback writes an
NFO. Already-linked verification does not require a new hardlink.

Linux compares both the device and the mount ID of the open file or directory.
Separate bind mounts can share a device but still reject hardlinks. Linux
requires readable descriptor mount information in procfs and refuses to use
a device-only guess if that information is unavailable. Other supported Unix
hosts compare device IDs. That fallback cannot distinguish mount aliases that
share a device, and no userspace check can rule out a later mount change.
The actual hardlink syscall remains authoritative. A late refusal never
triggers a copy or an automatic cleanup. If admission, an intent, an empty
directory or another publication effect already exists, retain it for normal
reconciliation.

Run the focused synthetic tests with:

```sh
python3 -B -m unittest tests.test_publication_mount -v
```

To exercise the kernel on a real different device as well, set
`CAL_TV_TEST_OTHER_MOUNT` to a writable scratch directory on a distinct
filesystem from the test temporary directory. The test creates and removes
only its own synthetic subdirectory. It checks actual kernel `EXDEV`, then
checks that preview and locked admission refuse without reserving or
publishing the file. The same-device Linux bind case uses synthetic mount
IDs in the unprivileged test suite.
