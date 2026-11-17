# Direct Get synchronization

Standard-library Python runtime for independent Get API to private GitHub and
Notion delivery. Source text and JSON remain separate from binary completion.
No credentials, private target IDs or runtime state belong in this directory.

## Execution

`python runner.py --mode inspect` is read-only. `baseline` establishes inventory
without migrating existing notes. `sync` scans current source versions and drains
new/changed notes. `backfill --batch-size 5` admits a bounded history batch;
`drain` resumes existing work without scanning the source. Each operation delivers
at most `batch-size` revisions (1–10), rotating across IDs without reordering the
versions of one ID. The authenticated private journal may pause reviewed source
IDs without putting personal IDs into public configuration.

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
In GitHub Actions only, a completed attempt of this exact workflow can be recovered
after GitHub verifies both attempts, their repository identities and current status.
Unknown, running, forked or unreachable attempts remain held. Recovery retains the
checkpoint and records the proof; local CLI owners cannot automatically take over.

## Data and recovery

- Get list pagination is complete each scan. Routine sync reads at most 100 details:
  changed summaries first, with 25 places reserved for rotating detail audits.
  Selected summary hashes and audit progress commit with inventory/outbox only
  after the complete scan. Deferred changes persist. Detail-only edits are eventually
  discovered by rotation, not guaranteed within one hour. Baseline explicitly reads
  every detail. Transport/pagination/identity failures prevent inventory acceptance.
  Incomplete source notes remain private evidence, separate from delivery receipts.
- Content fingerprints ignore only the complete `Expires`, `OSSAccessKeyId`,
  `Signature` trio on verified HTTPS `mediacdn.umiwi.com` attachment URLs. All other
  fields and query parameters count. Original JSON is never normalized or discarded.
- Full source JSON is retained; a Markdown mirror under the configured Raw prefix
  preserves the source body and recording transcript without rewriting them. Its
  first useful title names a stable file inside the ID directory; later titles
  update metadata without moving the file. Immutable JSON versions preserve history.
  `index.md` and `current.json` count verified receipts, not baseline observations.
  New/changed receipts are independently read back; five unchanged targets rotate
  through revalidation per run. Human changes may be detected on a later audit.
- Each destination has independent receipts. Same-ID versions remain ordered.
  Unknown ownership, duplicate Notion IDs, human edits and uncertain writes hold.
- Media are content-addressed in the private repository and uploaded to
  Notion-managed storage. Completion requires exact downloaded bytes at both ends.
  Signed source URLs alone never satisfy binary delivery.
- Expired first-download URLs may refresh only from the same normalized source
  revision. Existing manifests require the exact previously verified asset bytes;
  a new source revision cannot replace old evidence.
- Existing compatible Notion business properties receive verified source values.
  Owned property conflicts hold; ambiguous dates and unsupported fields are not guessed.
- Notion upload creation, sending and attachment have durable intents. Unknown
  creation or partial multipart results remain held; writes are not blindly retried.
- Logs contain counts and bounded reason codes. New source holds or attempted
  delivery failures exit 2. Known historical source holds and work left by the batch
  budget remain explicit in `inventory_status`/counts without failing an otherwise
  healthy operation. An exit 0 does not mean historical synchronization is complete.

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
