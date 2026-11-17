# Direct Get synchronization

Standard-library Python runtime for independent Get API to private GitHub and
Notion delivery. Source text and JSON remain separate from binary completion.
No credentials, private target IDs or runtime state belong in this directory.

## Execution

`python runner.py --mode inspect` is read-only. `baseline` establishes inventory
without migrating existing notes. `sync` scans current source versions and drains
new/changed notes. `backfill --batch-size 5` admits a bounded history batch;
`drain` resumes existing work without scanning the source.

The workflow is manual until a production cutover has been verified. Configuration
is one JSON secret, `GET_SYNC_CONFIG_JSON`, with `private_repo`, `data_branch`,
`state_branch`, `namespace`, `raw_prefix`, `notion_database_id`,
`notion_properties`, `allowed_asset_hosts` and `scope`. The runtime requires
`GETNOTE_API_KEY`, `GETNOTE_CLIENT_ID`, `PRIVATE_VAULT_TOKEN`, `NOTION_API_KEY`.
`scope` is `isolated-validation` or `production`; target IDs are configured privately.

The pre-existing private state branch must be dedicated to this namespace and
must differ from the data/default branch. Its atomically updated lease and full
compressed SQLite checkpoint survive runner replacement. No timeout permits lease
stealing. Before external writes, checkpoint failures freeze the entire runner.
Recover an interrupted lease by verifying its owning run and checkpoint first.

## Data and recovery

- Get list pagination and every detail are checked. Transport/pagination/identity
  failures prevent inventory acceptance. Explicitly held incomplete source notes
  remain private evidence and do not masquerade as complete source records.
- Full source JSON is retained; a Markdown mirror under the configured Raw prefix
  preserves the source body and recording transcript without rewriting them.
- Each destination has independent receipts. Same-ID versions remain ordered.
  Unknown ownership, duplicate Notion IDs, human edits and uncertain writes hold.
- Media are content-addressed in the private repository and uploaded to
  Notion-managed storage. Completion requires exact downloaded bytes at both ends.
  Signed source URLs alone never satisfy binary delivery.
- Notion upload creation, sending and attachment have durable intents. Unknown
  creation or partial multipart results remain held; writes are not blindly retried.
- Logs contain counts and bounded reason codes. Pending/source-held work exits 2;
  a successful process is not inferred from partial target writes.

Current limits: 100 MiB per binary asset; 8 MiB compressed/128 MiB raw SQLite;
unknown MIME and embedded image coverage are held. Notion manual edits still have
an unavoidable check/write race because its API has no conditional update token.
These limits must not be removed merely to make a job green.

## Verification

From this directory:

```
python -m unittest discover -v
```

On macOS the system temporary directory can contain an ancestor alias. Tests and
private state roots must use its resolved real path; retain the alias protection.
The release checks additionally forbid network connections during unit tests.
Real cloud execution and target readback are separate acceptance steps.

API references: [Notion file uploads](https://developers.notion.com/reference/create-file),
[large uploads](https://developers.notion.com/guides/data-apis/sending-larger-files),
[GitHub blobs](https://docs.github.com/en/rest/git/blobs).
