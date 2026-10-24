# Get runtime privacy boundary

Public code contains generic algorithms, fixed schema/scope limits and irreversible authorization commitments. Note contents, source-to-page maps, historical receipts, private object coordinates and real-run replay fixtures belong in the existing private vault, not public tests, logs, summaries or artifacts.

The reviewed runtime uses a code-pinned data-only capsule. A credential-free selection gate recognizes only reviewed opaque scope commitments. It precedes private data materialization; the original complete registration and source checks follow before any business write. Missing or changed private data fails closed.

Private object loading checks repository visibility/identity, immutable commit/tree/blob/size and the reviewed content digest, and creates a new local owner-only file. The loader never executes configuration as code or derives a destination from note content.

The production CLI emits versioned opaque proof digests instead of full private records. Recovery retains the independently authenticated public run/job/controller gates, reads only the fixed private state/receipt paths through a bounded single-parent history, reconstructs the full records and checks the public digest before the original semantic verifier. State updates and unique-writer/CAS ownership remain in the original transaction path; there is no second evidence writer. Acknowledged operations are not replayed.

Old exact recovery records and templates remain separately pinned. Public summaries use a fixed allowlist and do not claim full-library completeness or business health from step success.

A local test pass is not deployment or production acceptance. Publication still requires the actual original preflight, current lease, single writer, exact approved paths, backup/readback and conditional head comparison. The reviewed private capsule must be retained and read back before the matching runtime/controller is activated. Only synthetic fixtures may run in public CI; real-history replay evidence remains private.

The public proof may include one opaque state digest used only to reconstruct the authenticated full record. It cannot authorize the state independently. Historical checkpoints use immutable tree/blob/size metadata; final recovery still reads and verifies the actual state bytes. Receipt references from current state are untrusted search hints, not authority. The bounded fallback may fail closed when no authenticated origin can be located.

The separate legacy manual shadow workflow and its saved-inspection modes retain their exact previously published code revisions. This prevents dangling entry points while keeping historical private-data diagnostics out of the new public tree. Their existing runtime output and old public history are not claimed privacy-remediated. They must be migrated before use for new capture cohorts; the new recurring controller does not use this compatibility path.
