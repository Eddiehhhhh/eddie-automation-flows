# WeRead canonical paths

Notion shelf pages now use page UUID as identity. The first sync still chooses a
start-date month (or current month when absent) and a title filename. Subsequent
runs update the same path even when month, title, reading status, or date changes.
Different UUIDs with the same title receive an ID suffix only on collision.

## Bootstrap and recovery

The script scans existing Notion/wechat_read frontmatter once per run, accepting
`page_id` and legacy `source_id` UUIDs. It bootstraps each UUID to its lexically
first path and pins it in `Raw/15 微信读书/.notion-page-paths.json`. This choice is
path routing only: it does not establish which duplicate has authoritative text.
The current source response updates only the selected path; other copies remain.
All existing paths and links stay intact. The Markdown index uses canonical paths.

The JSON index lives in the private vault and is included by the existing scoped
Raw commit step; no public state or workflow/authentication changes are needed.
Writes use atomic replacement. A failed note write leaves the old note available;
a crash after note write but before index save is recovered by the next scan.
Missing canonical notes are rebuilt at the reserved path. Bad JSON, conflicting
IDs, path traversal, symlink escapes and ownership collisions stop the run rather
than guessing. Limited runs preserve the complete Markdown index. The existing
workflow serializes private-vault writers; simultaneous manual local syncs are
not supported.

## Read-only audit

With `VAULT_ROOT` set to a private vault checkout:

```
python scripts/sync_notion_weread_to_raw.py --audit-report /private/path/candidates.json
```

This needs no Notion credential and writes only the specified report outside the
source directory. It includes UUIDs, candidate canonical paths, every duplicate
path and SHA-256. Keep this report private; do not attach real titles, UUIDs,
source URLs or reports to this public repository. Resolve references separately
before any later cleanup. This change never removes, renames, merges, or rewrites
noncanonical duplicates.

## Verification and release

```
python3 -m unittest discover -s tests -v
```

Tests use synthetic offline fixtures, including month rollover, rename/status,
date addition/removal, same-title IDs, legacy duplicate conflicts, failed writes,
retry recovery, corrupt state and partial runs. No production sync is required
for these tests.

A draft PR is not a deployment. After review, verify a private read-only dry-run,
merge the approved code, then observe the existing scheduled sync and read back
canonical paths and index from the private vault. Never dispatch a workflow to
this branch blindly: its `checkout_ref` also influences the private-vault commit
ref. No production run is started by this PR.

For rollback, revert the code commit, retain the private path index and all Raw
files as evidence. The old script ignores the JSON index but can recreate monthly
duplicates; rollback does not remedy the original bug. Do not delete old duplicates
or advance knowledge-promotion cursors as part of this release.
