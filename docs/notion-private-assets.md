# Private pinned-Git assets for Notion

This module is a library for the reviewed mirror executor, not a standalone job.
It does not discover secrets, alter source files, create pages/blocks, append
content, configure a service, or schedule work. The executor must verify that the
checkout is the approved **private** repository before preparing or publishing.

## Interface and durable ownership

`prepare(root, fixed_commit, relative_path)` reads an exact 40-character Git commit
and regular blob; it never reads dynamic working-copy file content. It rejects
outside paths, symlinks, untracked paths, non-blob entries and unavailable LFS
payloads and high-confidence credential shapes. Git object identity and size are verified. MIME is detected from actual
bytes using the system `file` utility, not inferred from a private filename.
`GitBlobAsset` hides private fields in its representation. A 64 MiB source-read
budget is an explicit hold, not a type exclusion.

`publish(asset, api_key, checkpoint, prior_receipt=None)` returns a dictionary with
`status` (`ready` or `held`), `block`, `receipt` and `reason`. The executor supplies
`checkpoint(receipt)` and must persist it to the private state **and acknowledge
remote durability before returning**. The module does not persist state itself.
Bind the receipt under `state.assets[asset.sha256]`; `sources[commit][path]` retains
aliases for identical content reused from multiple pinned private paths. Checkpoint copies are private:
they contain an opaque upload ID, source pin and content identity.

Create is checkpointed as `prepared` before dispatch and `created` after response.
Send is checkpointed as `dispatched` before dispatch and `sent` after response.
A fresh metadata GET must match upload ID, filename, MIME, byte count, status and
expiry before `verified` is checkpointed. Checkpoint failures hold the attachment.
No POST is automatically retried, including rate-limit responses. Unknown create
without an ID cannot be replayed. Unknown send with an ID permits GET-only
recovery; `pending`, expired, inaccessible or mismatched results do not trigger a
second send or a new upload. Existing verified uploads are rechecked and reused.
All exceptions exposed by this module use fixed reason codes, never response
bodies, names or URLs.

The caller owns page/block write intents and may attach only a `ready` native
image, PDF or file block. It must then fetch the target block and call
`verify_uploaded_block(asset, block)` or `canonical_asset_identity(asset, block)`.
These read the actual returned file bytes and require SHA-256 equality with the
pinned source. Only then may the executor substitute the stable content identity
for Notion's temporary signed URL when computing its target snapshot.
`canonical_native_file(block)` also provides actual-byte identity for an existing
human-managed native file without claiming that it came from Git; the executor
must retain its caption/name separately and detect future human file changes. A request
payload or a URL-stripped file block is not binary proof.

Target downloads accept only explicit Notion-managed HTTPS hosts and reuse the
existing public-IP-pinned TLS downloader, with no redirects and no API credential
forwarding. An unrecognized CDN, expired link, failed read or byte mismatch holds
the attachment; do not broaden trust or report completion automatically.

## Current platform limits and fallback

Checked against official Notion documentation on 2026-11-01:

- [File/media limits](https://developers.notion.com/guides/data-apis/working-with-files-and-media):
  retrieve `bot.workspace_limits.max_file_upload_size_in_bytes` from the bot user;
  do not infer a workspace plan. Missing limits hold uploads. API permission, rate,
  validation and access/limit denials remain explicit reasons.
- [Create upload](https://developers.notion.com/reference/create-file) and
  [send upload](https://developers.notion.com/reference/upload-file): native direct
  upload uses JSON create followed by multipart binary send. This implementation
  uses the documented `2026-03-11` version without changing the mirror's other API
  client. Single-part supports at most 20 MiB; larger uploads require
  [multipart](https://developers.notion.com/guides/data-apis/sending-larger-files),
  which is deliberately held by this initial single-part adapter.
- [Attach and reuse](https://developers.notion.com/guides/data-apis/uploading-small-files):
  attach an upload before its one-hour deadline. After attachment, upload IDs can
  be reused. [Target retrieval](https://developers.notion.com/guides/data-apis/retrieving-files)
  converts `file_upload` to `file` with a temporary URL; binary readback remains
  required.

No file type is excluded by a local allowlist. Unknown or unsupported MIME,
platform quota, oversized files and unreadable sources remain held. Where the
caller has verified repository privacy and read access, a held result can expose
a clickable `github.com/<repo>/blob/<commit>/<encoded-path>` original-file link.
That fallback requires the reader's GitHub permission/login and is not an inline
Notion attachment or a completed asset migration. Never expose authenticated raw,
signed-source or credential-bearing URLs. Do not buy storage or a service to
remove a limit without separate authorization.

Only synthetic fixtures are committed. Real source names, private URLs, IDs and
binary content must remain in the private executor state and private checkpoints.
No real Notion mutation or cloud publication is exercised by this library's tests.
