# Resumable new-page creation

`get_notion_create_batches.py` is used only when a newly created Notion page
cannot fit the existing single-request path. Existing-page updates and short
new-page creation retain their previous execution paths.

## Immutable plan and ownership

- Complete root subtrees are packed in source order. No paragraph compaction,
  subtree splitting, extra numbering reset, or format rewrite is introduced.
- Each request contains at most 100 recursive blocks and at most 400 KiB of
  encoded wire JSON, including its page/properties or append/anchor envelope.
- The source-bound plan stores root ranges. A large-page plan selects one
  eligible new page; other fully preflighted records remain deferred.
- The operation records every batch's prepared, in-flight, acknowledged and
  verified phases, the exact known page ID, actual returned IDs, and the
  verified prefix's root IDs and normalized block hashes. An append reconciled
  after a lost response keeps its response null rather than inventing one.
- Every append is preceded by a fresh, complete check of the expected prefix.
  Every resulting prefix must preserve the earlier IDs, content, order and
  properties, and exactly match the full planned prefix.
- The ownership receipt and outbox dequeue happen only after the entire page
  passes a final complete readback. Partial pages have no ownership receipt.

## Interruptions and unknown outcomes

A prepared batch is known to be unsent. A durable in-flight batch is never
blindly repeated. An acknowledged result resumes by reading back its exact
page and returned root IDs. An unknown append can advance only if the entire
expected prefix, including the earlier verified IDs, is observed exactly.
An unknown create without a returned page ID still requires the existing
separately approved exact-page recovery evidence. Matching text alone cannot
adopt a page. Manual additions, deletions, replacements, reordering, edits and
property changes stop execution without deleting anything.

There is one narrower PNG limitation: if the first create response is lost
before its page ID is saved and that first prefix contains the PNG, this
release keeps the in-flight journal held even when separately approved
exact-page recovery evidence is supplied. An already attached or expired
upload cannot satisfy the unattached-upload guard, and the PNG adapter does
not adopt a page without its durable acknowledged page binding. Neither the
page create, append nor upload is repeated. This case needs separately
reviewed PNG-specific exact-page recovery support; it must not be treated as
an automatically recoverable create or cleared to start over. Known-page
acknowledged creates and uncertain later appends retain their native-byte
and complete-prefix readback paths.

Production write authorization adds only the current durable batch's exact
fingerprint. Its newly known page and previous-root anchor are derived from
the same immutable approved plan and validated journal. Arbitrary payloads,
other pages and already-attempted fingerprints are rejected.

## Bounded execution

The new static limits are 32 batches, 3,200 recursive blocks and 1,500 estimated
normal API requests per page. Admission reserves six full durable states for
one batch, rather than multiplying state size by every checkpoint of a page
that may span many invocations. The estimate includes already-saved ownership
receipts, the actual outer outbox envelope, and conservative room for this
page's complete ID/hash history and final receipt. One estimated state must
fit 32 MiB; the six-state reserve must fit 192 MiB before the initial create.
The existing 32 MiB state cap,
192 MiB journal-byte cap, 2,000 Notion request cap, 64 MiB Notion response cap,
remain unchanged. The transport deadline is now 1,500 seconds from the same
constructor-time origin (previously 1,200 seconds). This is a fixed five-minute
allowance increase, not a clock reset or an exemption for proof/setup time.
All modes share the same single session instance and existing counters. The
workflow retains its 55-minute total job cap; preceding Get work and private
Git/proof latency also consume job time, so completion inside that cap is not
guaranteed. Cancellation and uncertain writes still require original resume
verification. Historical metrics conservatively clamp displayed remaining time
to 1,200,000 milliseconds; admission uses the actual unchanged-origin deadline.

Before another batch starts, admission checks reserve complete-prefix reads,
four phase checkpoints plus two rollback/completion checkpoints,
request/response headroom, and existing PNG download
budgets. PNG sizing uses the exact validated image sizes. These time estimates
are headroom, not a guarantee about network or remote Git latency. Hard
transport failures still preserve acknowledged or ambiguous state for resume.
Actual remote-store counters charge every CAS and distinct state blob against
the same invocation's unchanged 192 MiB cap. If the remaining budget cannot
cover the next reserve, execution yields before its external write. A fresh
invocation starts with fresh counters and the same saved plan and receipt
history. Previous pages therefore consume state space, not the next page's
entire hypothetical multi-invocation checkpoint volume. An impossible single
state or unsplittable request still fails before page creation.

When a bounded run should stop, it returns partial with
`batch_boundary_yield`, keeps the live journal on a verified batch boundary,
and records neither a failure nor a completed receipt. A next prepared range
known to be unsent may be removed to return to that verified boundary. The
initial unsent create can also yield. The final ownership readback can yield
after the last verified batch and finish read-only on the next invocation.
Completion-only admission reserves exactly one complete page read (including
fresh native PNG byte verification), no Notion content write, and the two durable
receipt/journal-clear saves. Its PNG, request, response, time and journal-byte
reserves reflect that narrower work; all hard ceilings remain unchanged.
Resumption uses the original plan, not a fresh create plan or broader retry.
The pipeline therefore skips only the unused planning snapshot when a saved
immutable plan exists. The executor still performs every fresh pre-write,
post-write, uncertain-outcome and final full read, with the same source,
writer, duplicate-ID, native-byte and ownership guards. The public
`native_notes_read` count describes planning snapshots and is zero on resume.

## Local validation

All new fixtures are synthetic. No external API calls are made.

```sh
PYTHONPATH=scripts:tests python -m unittest \
  test_notion_create_batches test_notion_executor test_notion_pipeline \
  test_notion_not_dispatched test_notion_prepare_reporting test_notion_outbox \
  test_notion_resume_efficiency test_png_create_batches
```

Coverage includes a 268-root/271-node image/list page, a 2,000-root page,
recursive-count and byte packing, every before/after checkpoint crash,
ambiguous create and append outcomes, manual edits, schema corruption,
NotDispatched fingerprint checks, request and PNG budget yields, actual native
pagination/wire authorization, sequential receipt growth across four large
pages, real byte-counter boundary yields, impossible checkpoint admission,
and resumed ownership without duplicate writes.
PNG coverage also verifies that a lost first create response with an attached,
expired image stays held without duplicate writes, including an explicit
exact-page recovery attempt.

The historical regression modules referenced here are retained in the private replay archive. They must be executed in an isolated private environment, not public CI. Public-tree tests exercise only synthetic or generic inputs.
