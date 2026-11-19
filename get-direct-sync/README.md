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

The established `get-notion-incremental.yml` workflow runs this runtime directly. The retired direct worker and private dispatcher remain disabled. `Get cloud clock` uses a GitHub server wait timer, then explicitly dispatches Get in sync mode using its short-lived Actions token. Get has no independent cron. The experimental hourly workout tick has been removed, restoring that workflow to its original schedule and jobs. Manual Get runs can still select an explicit mode.
Configuration is one JSON secret, `GET_SYNC_CONFIG_JSON`, with `private_repo`, `data_branch`,
`state_branch`, `namespace`, `raw_prefix`, `notion_database_id`,
`notion_properties`, `allowed_asset_hosts` and `scope`. The runtime requires
`GETNOTE_API_KEY`, `GETNOTE_CLIENT_ID`, `PRIVATE_VAULT_TOKEN`, `NOTION_API_KEY`.
`scope` is `isolated-validation` or `production`; target IDs are configured privately.

Completed scheduled runs of the existing filename audit, workout link and Notion Flomo workflows also wake Get through `workflow_run`, even when their independent business step fails. A dedicated `Get cloud sync wake` maintenance signal exercises this event path without rerunning those business jobs. The trigger guard accepts only reviewed source names and event types from the same repository main branch, before private credentials are loaded. It never consumes upstream code or artifacts, and Get does not listen to its own completion.

`Get cloud clock` adds a GitHub-hosted server-delay timer. Its two environments
must be configured before starting it: `get-clock-hourly` waits 60 minutes and
`get-clock-validation` waits 1 minute, both limited to main with no reviewers.
The wait consumes no runner time. Each released job verifies its timer policy,
dispatches one successor using its short-lived Actions token, reads that successor
back, then separately dispatches Get with a unique clock run key and reads it back. It contains no note credentials. Two bounded
validation ticks transition automatically to the production hourly timer.
`clock.py` reconciles an uncertain dispatch without repeating the POST, prevents
reruns from forking the chain, and fails if the required timer is missing. Every
trusted Get write run also rearms the hourly clock if no future clock remains;
normal scheduled business completions therefore provide recovery. Both the normal timer and recovery paths verify the hourly wait policy; missing protection stops rearming instead of creating an immediate loop. Inspect mode
does not arm a clock. Restart a fully stopped chain with a new manual clock dispatch, not a rerun of an old parent. To stop, disable `get-cloud-clock.yml` and cancel its waiting
run; the existing fallback business flows may still wake Get. Disable the Get
receiver too for a complete stop. This is a server-delay chain, not proof that
native cron delivery has recovered. A GitHub-wide Actions outage can delay both.

The Get workflow uses its own concurrency group; the private state CAS lease remains the authoritative single-writer guard across runs. Unrelated main-branch writes still require conditional writes and verified readback.

The pre-existing private state branch must be dedicated to this namespace and
must differ from the data/default branch. A single conditional Contents update
keeps its lease and full compressed SQLite checkpoint together across runners.
Other writers must not modify this state file. Head and parent checks additionally
freeze publication if unrelated branch changes race the update. No timeout permits lease
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
- Each revision reaches Notion only after its GitHub receipt is checkpointed, including
  verified binary delivery when needed. If Notion fails, a retry uses the complete
  same-revision GitHub receipt unless that stage was subsequently held. Same-ID
  versions remain ordered.
  Unknown ownership, duplicate Notion IDs, human edits and uncertain writes hold.
- GitHub successful writes are read at the exact returned commit and checked against
  the actual Git blob hash and all bytes, avoiding mutable-branch readback ambiguity.
  A lost response gets exact branch reconciliation, never an automatic repeat PUT. State publication tolerates only the original pre-write ref for three bounded read-only checks (1/2/4 seconds); a competing ref, altered content or wrong parent still stops immediately.
- Media are content-addressed in the private repository and uploaded to
  Notion-managed storage. Completion requires exact downloaded bytes at both ends.
  Assets of 20 MiB or more use Git transport in a temporary bare checkout.
  The private branch is rechecked before a normal fast-forward push, and the asset
  is read back at the resulting commit. Signed source URLs alone never satisfy binary delivery.
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

Current limits: 100 MiB per binary asset and 8 MiB compressed/128 MiB raw SQLite.
Recording exports with standalone Markdown image lines archive each image alongside the audio.
Unsupported image syntax and unknown media types remain pending. Notion manual edits still have
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

### Optional reviewed history on an idle hourly tick

The normal source scan and pending deliveries run first. A first-attempt main
sync dispatched by the verified GitHub hourly clock may recover up to five
approved plain-text revisions when its normal queue was empty and still is.
The optional input is the private default branch file
get-direct/<namespace>/reviewed-backfill.json. Its exact fields are
schema (get-reviewed-hourly-history-v1), namespace,
excluded_source_ids (include the known unresolved legacy writes),
and reviewed (an ordered list of rows with source_id, source_version,
path, local_blob, and SHA-256 reviewed_revision of source_id:source_version).
Approving the file requires verifying unique local and GitHub identity,
zero pages in the official Notion database, and the same current Get revision
against the old GitHub Markdown. Do not place note bodies or credentials in it.

Each selected row is rechecked against the exact Get version, approved legacy
blob and zero Notion pages. Paused IDs, legacy exclusions, source holds and
already completed versions retain their restrictions. Deterministically stale
rows get a private disposition tied to the plan blob; a fresh reviewed plan can
reconsider them. If the plan is removed or replaced during execution, no further
row from the previous plan is admitted. After admission, each note uses the
normal durable outbox and readback. An unfinished write remains pending for
the next normal run. No plan creates no historical work. There is no added
schedule. An admission budget of 30 minutes from runtime start preserves a
three-minute reserve for the last plain-text transaction and state release.
A new row also requires at least 1,500 remaining GitHub core requests. An
unavailable quota or a lower balance defers only the optional history stage.

### Notion native presentation

The Notion page shows a readable Get body heading linked to the immutable
private GitHub source version. The display uses native headings, lists,
quotations, code blocks and basic inline styling. Recorder notes use two
toggleable level-one sections for the summary and original transcription,
with level-three and level-four headings preserved inside. Attachments and recordings
use their already verified uploaded bytes, with native media blocks where
supported. Source-only JSON and source media signatures remain in the private
archive. The full original source digest and the digest of the displayed
blocks are verified independently.

Existing plain-text version receipts are still read with their original
contract. An older verified Get version may have its heading relabeled
“历史正文” when an updated native version is appended. Its blocks are retained.
Approved targeted historical labels for existing attachment toggles keep
the original media block and verified bytes. A same-version retry reuses
the existing blocks; it does not retrofit pages in bulk. Missing or unexpectedly edited managed content still stops the item.
