# Cloud acceptance boundary

`cloud-readonly-acceptance.yml` is a branch-scoped, read-only acceptance run. It has no cron and does not replace any production scheduler. It uses the existing private-vault checkout credential; it persists no credential and creates no new credential or model billing dependency.

Two deterministic slices are exercised against the real remote private snapshot:

- Weekly iteration source discovery, incremental audit and in-memory Observatory snapshot construction through the existing `--mode weekly --dry-run` entrypoint. A second audit enumerates the same candidate window and reads every selected file in full. Its count must equal the pipeline count; the public summary contains only a combined evidence digest, file count, exact-body duplicate count, and weak-date count. Exact byte duplicates do not establish semantic deduplication. HTML rendering, artifact generation, LLM judgment, personal-memory promotion, finalization and writes are not executed.
- Cloud evidence health: same-day source-arrival proof, same-day Get specialized sync proof (zero known gaps, failures and duplicates), and promotion report/cursor consistency. The controller, Mac workspace, device state and remote service processes are explicitly unverified. A disabled runtime policy is expected; it never probes or restarts it.

A successful Actions job proves execution of the checks, not healthy business results. Inspect the independent `health` fields. Missing/stale evidence blocks health; before noon Shanghai a missing promotion report is pending. Conflicting or malformed formal report statuses also block health, even if an earlier line says success. The JSON contains only a fixed allowlist of statuses, counts and digests. Private checkout streams are captured, errors are generic, and authentication exists only in the Git subprocess environment. Unlike the standard checkout action, this step never prints the private HEAD or repository/branch metadata. Public source-checkout metadata remains visible. Do not upload private stdout, report bodies or the checkout as artifacts.

## Idempotency and acceptance

The run key is a digest of mode, period and private snapshot commit. Repeating a run never writes upstream or advances a business cursor. A clean checkout is required. Before/after fingerprints cover all regular-file bytes, permission bits, directory presence and symlink targets, including Git-ignored files; links are not followed. Only root `.git` runtime metadata is excluded. Timestamps are excluded because reads may update access time. This proves final tree equality, not detection of transient changes restored between snapshots or writes outside the tree. This is read-only idempotency, not a production write transaction. Production writes still require destination readback and durable single-writer state.

Run `python3 -m unittest discover -s tests -p 'test_cloud*.py' -v`. Unit tests use synthetic fixtures; the Actions acceptance invokes actual private data. No unit fixture is production proof.

## Cutover

Keep the original jobs active until a cloud replacement has passed destination and mobile-result readback. Store the old ID/config and last successful cursor privately. Pause the original, read back paused, then enable exactly one replacement. If verification fails, pause the new job, read back paused, restore the old job and cursor without deleting prior evidence. Never enable both writers. Local workspace fast-forward/cleanup remains a separate local maintenance task and cannot be validated by a cloud clone.

This branch can be closed without affecting production. Removing its workflow prevents further manual acceptance runs; no business state needs rollback.
