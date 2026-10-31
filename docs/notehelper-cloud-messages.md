# Notehelper cloud messages

This is an optional read-only message adapter inside the existing personal-data workflow and private-vault writer. It uses the installed client's Search query against the existing relay. Only the message title prefix is accepted; ordinary saved articles continue through their existing Notion source. The GraphQL response is the relay's root `edges/pageInfo` shape. No delete, burn, login, token refresh or local plugin control is implemented.

## Deployment owner and boundary

The primary operator must configure `NOTEBOOKSYNCER_API_KEY` from the existing authorized notehelper credential, without printing or committing it. The private configuration `.github/state/notehelper-messages-config.json` is `{"version":1,"enabled":true}`; missing configuration defaults off. The shared `PRIVATE_VAULT_TOKEN` guard checks origin fetch/push and private repository API identity before apply writes. No source IDs or credentials belong in the public repository.

The existing 03:20/15:20 Shanghai schedule/concurrency is reused. A mutually exclusive `messages_only` dispatch is available for verification. Other business failures do not prevent this independent collector; cancellation prevents new stages. Successful per-object progress and failed coverage persist in the same private commit as Markdown/JSON/assets, followed by HEAD and freshly fetched remote blob verification. A partial result exits 2 after durable progress is attempted. No private activation is performed by code publication alone.

## Preserved evidence and readable current

`Raw/16 笔记同步助手/云端消息/versions/<hashed-unit>/<version>/<readable-title>.md` contains the actual full source content with source saved/updated timestamps, including available seconds. It is not dated as a new event on import. `source.json` retains all returned message fields; it is a JSON serialization of that source object, not a claim of original HTTP byte identity. Assets are immutable content-addressed files under `_assets`; readable Markdown uses local relative paths. HTTPS downloads use the existing public-IP-pinned, size-bounded, no-redirect downloader with no source API key. File types are not excluded. File/batch budgets hold the affected object explicitly; an inaccessible attachment cannot produce a complete receipt.

The stable human entry `Raw/16 笔记同步助手/云端消息/index.md` lists only the state-selected current versions. It is marked `stage_only: true`, has a persisted `index_sha256`, and is included in exact remote blob verification. A manual index edit holds index publication and remains untouched.

State `.github/state/notehelper-messages-state.json` chooses one current version per source message ID. Older versions and upstream-absent messages remain saved; absence never implies deletion. A selected Markdown edit or any prior owned evidence modification holds that object instead of overwriting it. Source JSON and rendered Markdown pass the existing high-confidence credential shape gate before object writes. Held reasons contain no matched credential/body.

The live local notehelper plugin scans generic `id`, `messages`, `syncedIds` fields and `<!--nh:...-->` markers. New cloud Markdown therefore uses `source_message_ids` and neutral `<!--source-notehelper:...-->` comments. The original source JSON retains the original content. No cloud Markdown advertises the local writer's old identity fields.

## Legacy coexistence and audit

All old daily notes stay untouched and active. A private coverage map records their exact SHA and full existing message-marker ID sets. This first release deliberately reports `unmerged_legacy_preserved`, including for files whose IDs are all captured: manual sections and attachment parity have not been proven. It never suppresses a whole multi-message diary from one matching ID. Deduplication of a legacy daily file requires a later explicit proof that every ID, every manual section and every asset is readable in the new current, while its original SHA remains unchanged.

Daily/weekly fact readers select only state-current cloud versions; old cloud versions, source JSON and assets are excluded. Selected human edits remain discoverable. Legacy notes remain eligible. Filename repair must not rename managed immutable versions. Physical inventory can count all historical files while active facts use only selected versions. New source registration, Wiki navigation and the Raw16 entry must be connected by the primary vault owner before final system acceptance.

## Acceptance

Run source-only once, verify complete terminal pagination and independent unique IDs, compare all expected historical messages with local legacy marker sets, verify original full content and binary attachment SHA, inspect selected readable Markdown, then verify private remote state and every referenced Git blob. A green unit test or published public PR does not prove the cloud chain is deployed. Local plugin retirement remains a separate coordinated action; this adapter does not authorize it.
