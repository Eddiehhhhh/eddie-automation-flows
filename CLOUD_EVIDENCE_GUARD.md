# Read-only cloud evidence adapter

## Minimal integration

Add `scripts/cloud_evidence_guard.py` and its unit tests beside the existing
`scripts/cloud_acceptance.py`. The latter is unchanged; the adapter reuses its
Shanghai timezone and strict formal-report status parser.

In the existing cloud inspection reader, collect authorized GitHub responses,
pin destination file reads to one private snapshot, and call:

```python
from cloud_evidence_guard import inspect
cloud_evidence = inspect(observations, now)
```

Treat the old Mac source-arrival proof as a `mac_directory_observation`, not the
cloud source-completeness gate. Keep the local maintenance guard unchanged and
separate. This patch does not alter an existing scheduler, production state,
workflow, rule, reader authorization, or credentials. Wiring and deployment are
separate steps and have not been executed.

## Input contract

- `schema_version: 1`; `snapshot` identifies the pinned private commit and the
  registered paths. Each registered path requires an explicit source entry,
  including entries whose producer or coverage is unknown.
- Each source has `id`, `mode`, `workflow`, an actual `run`, `artifacts`, and
  scoped `coverage`. Run identity is checked against the expected repository,
  workflow path, exact run URL, and full commit SHA.
- Each artifact records repository, path, pinned commit SHA, blob SHA, kind,
  and provenance. `generated_at` must come from the artifact; never substitute
  read time or Git commit time. Preserve `latest_source_at` separately.
- `max_age_hours` is optional and must come from an existing explicit SLA.
  Without one the adapter reports timestamp and age without inventing a failure
  threshold. A recent index does not establish that the source produced recent
  content.
- Paused, archive-only, and manual-only expectations require a cited current
  owner message or repository policy. This is classification, not authorization
  to stop or start anything. Preserve later user corrections in the collector.
- A future `next_due_at` is `pending_future`, not a failed natural-run test.
  Scheduler control unavailable/unknown remains an evidence gap, not stopped.
- Promotion checks reuse the strict status parser and require matching daily
  run, both cursors, scan endpoint, result family, and cursor advancement before
  accepting a reported success. An explicitly paused task is expected absent.

## Output and safety

Execution, artifact arrival, freshness, and reported coverage are separate.
The adapter never asserts a full-system green light or independently verified
full source synchronization. Count-only, preview, and management-state evidence
remain limited to those scopes. Missing evidence is retained, not guessed.

`inspect` is pure and does not mutate its input. It has no network, subprocess,
credential, writeback, scheduler, or API implementation. The optional CLI reads
one evidence file and creates a new private local report with mode 0600. It
refuses overwrites, requires `.private.json`, optionally rejects output under a
specified vault, and prints only aggregate counts. Do not publish private input
or reports as public Actions artifacts. The copied dependency in this review
directory is for local tests only and is not part of the patch.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_cloud_evidence_guard.py' -v
```

Synthetic tests cover pauses, retirement, manual snapshots, future weekly runs,
unknown scheduling, wrong run identity, partial content coverage, stale/future
timestamps, cursor mismatches, conflicting report statuses, immutability,
registered-source omissions, private file permissions, and output safeguards.
Synthetic tests do not prove production source completeness. A private bounded
real-evidence replay is supplied separately and must not enter the public patch.
