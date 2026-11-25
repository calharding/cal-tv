# Development and release boundaries

Use a local Git checkout. Add synthetic tests and record exact verification in
each PR. Never commit actual requests, configured service URLs, tokens, job
records, library receipts, operational logs, media, or site review evidence.

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
