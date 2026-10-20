# Combined Get migration candidate: unactivated historical/property backfill

This local candidate combines the reviewed canonical read optimization, property mapping v2, and historical placeholder adoption v2 against runtime `6ad627793c23e80ad2ca9568048eb4e595c881a0`. It is not a production activation or a complete-library migration result.

## Combined contract

- The normal verified canonical publication path fills four mapped fields on eligible newly created pages. It requires the exact hash-pinned PyYAML dependency and existing destination schema/options. Missing values or ambiguous timestamps are held; no schema modification is authorized.
- Property backfill, historical body adoption, and native-evidence capture are mutually exclusive runtime modes. Their payload/hash pairs are required; conflicts, unregistered apply payloads, and recovery combinations are rejected before credentials or remote calls.
- The policy scope binds the independent property-manifest, historical-manifest, capture-scope and private-input-anchor sets, plus the canonical new-page property scope. Every activation change requires a newly reviewed runtime/policy/controller anchor.
- Property-only and historical-only transactions share the existing journal and leave the canonical queue, ordering and publication anchors unchanged, including pending entries with the same identity. Immutable historical captures survive normal body and property CAS operations.
- Historical mode cannot use the canonical standing new-page exemption. Its exposed first cohort is unique existing plain-text placeholders. All old blocks and their discussions remain protected. Only newly appended blocks become managed. A subsequent changed source blob with an existing body receipt is held for separate normal managed-update review, avoiding duplicate body adoption.

## Required rollout work

1. Independently review this exact combined patch; run CI against the exact resulting commit. The frozen patch and source manifest are sufficient to reconstruct the candidate; local validation logs are not source files.
2. Confirm the one existing writer is idle with no active body/property journal before changing the runtime. No new general in-flight bridge is included. Preserve the existing concurrency group, old-writer guard, authenticated public policy/publication checks, journal path, lease and checkpoint step.
3. Publish the reviewed runtime only through the authorized release process. Regenerate and review the public policy/scope/controller for that exact commit; the controller must install the hash-pinned dependency before invoking the runtime. The existing controller remains the sole writer.
4. No backfill manifest is activated by this candidate: property, historical-apply and capture allowlists and private input anchors are all empty. Register only separately reviewed exact payload hashes and immutable private Git anchors, then re-freeze and re-review the affected runtime and policy. CLI self-hashes are not activation.
5. Wire the existing workflow's Notion step to the chosen bounded mode. Historical input loading uses the private pinned-input helper; no private source, IDs, titles, page snapshots, or URLs belong in public workflow inputs or logs. Capture writes only immutable evidence to the existing private journal and performs zero Notion writes. Review that exact capture before separately authorizing its derived apply manifest. Capture approval is not apply approval.
6. Property backfill requires its independently reviewed exact source/privacy/schema manifest; do not derive property ownership from a body receipt alone. A scheduled canonical-create scope does not authorize this backfill.
7. For each bounded apply, inspect durable receipts and fresh native readback, then check repeat execution produces no duplicate mutation. Expand cohorts only after resolving held identities, source/privacy review, property mapping, and capacity deliberately. Historical new numeric pages, recordings/media, aliases, duplicates and broad library completion remain outside this first adoption release.

## Local merge fixes beyond the frozen inputs

- Unioned public report source modes and both queue-dequeue exclusions.
- Added complete mode exclusivity to both CLI and direct runtime calls, and bound private historical input anchors in the scope digest.
- Repaired property runtime aggregate reporting: zero initial counters are required by the shared runtime sanitizer; blocked-record reason counts must match blocked action counts.
- Added synthetic CLI-to-runtime and shared-state regression tests and included them in the offline migration check. All tests use synthetic fixtures and mocked external I/O.
