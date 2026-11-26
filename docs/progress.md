# Portable progress monitoring

The progress journal is a standalone library for development and review.
It does not change `fastpassctl`, run a worker, inspect production state,
send an alert, authorize an import, or release a hold.

A caller passes current facts from an independently checked deployment
adapter. A `Work` key must refer to an exact serviceable unit of work.
Unknown eligibility belongs in `Lane.unresolved`. Documented blockers use
non-sensitive reason codes. Coverage is false unless the caller has
independent complete proof. The monitor does not take a heartbeat as
evidence or authenticate a caller's external receipts.

## How it treats progress

- First-pass intake and review keep separate clocks. Bounded active
  leases can justify waiting. First-pass activity cannot reset the
  review clock. A new observation or a repeated commit key is not
  substantive progress.
- An issued `Handoff` remains outstanding even if it is omitted from
  later observations. Only a matching, authenticated `TerminalReceipt`
  retires it. Use an opaque digest of the exact issued scope as the key.
  A reviewed handoff and a proven hard block remain separate outcomes.
  Enqueueing work, reading a task, and acknowledging a report prove
  neither downstream review nor import.
- The stall threshold defaults to eight minutes. Incidents survive
  restarts and use a stable lane fingerprint. A later stall after
  recovery has a new incident number. A no-work observation without
  coverage cannot certify exhaustion.

Keep the monitor in its own SQLite database. The caller supplies
immutable, unique evidence keys and uses a consistent clock. Exact
terminal receipts must already be authenticated against the issued
handoff; do not repurpose a delivery acknowledgement as review proof.
The library stores opaque digests and reason codes. Do not place paths,
credentials or file identifiers in a reason code.

`Monitor.evaluate` records both lanes at once. A scheduler may use
`reserve_due` to fetch report envelopes with retry backoff. The caller
owns delivery and should deduplicate by envelope ID. Only after
confirmed delivery should it call `acknowledge` with an opaque
receipt digest. This acknowledgement changes delivery state only.

## Synthetic exercise

```sh
python3 -B -m unittest tests.test_progress -v
```

The tests drive the monitor across fresh SQLite connections. They include
a repeated heartbeat-like observation with no new disposition, a
bounded long job, an unconsumed handoff, exact recovery and alert retry.
They do not start a worker or exercise a live notification destination.
