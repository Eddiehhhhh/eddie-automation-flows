# Readable flomo cloud current

The existing personal-data workflow can materialize captured flomo evidence into
readable Raw/01 Markdown. This is a local transformation inside the existing
verified private checkout and flomo writer lock. It adds no API reader, schedule,
MCP, Notion writer or credential. Enable `current_export_enabled: true` in the
existing small private flomo configuration only after reviewing captured source
coverage. Default remains disabled.

## Current selection and reading

- Human entry: `Raw/01 Flomo/云端/index.md`, one readable title link per memo.
- Immutable note: `Raw/01 Flomo/云端/版本/<memo>/<version>/<readable-title>.md`.
  The complete title remains in frontmatter, alias and heading; UTF-8 filenames
  are bounded to 160 bytes. The version hash is a parent directory.
- Resource bytes: `Raw/01 Flomo/云端/_assets/`, content-addressed local files.
- Preserved legacy snapshots: `Raw/01 Flomo/云端/legacy-evidence/`.
- Pointer/state: `.github/state/flomo-cloud-current-state.json`.

Each memo has exactly one selected current path in state. Old immutable notes
remain on disk as history. A source edit or later original-source enrichment
creates a new version and moves the pointer; it never overwrites a readable note.
Human edits to owned notes, assets or captured evidence hold the affected export.
Unowned existing index content is preserved. Existing Raw/01 source files and
links remain untouched; source snapshots retain all captured legacy text and
metadata. Different legacy text is visibly labeled as unmerged local evidence.

When original flomo JSON is available, the current body uses that actual memo's
HTML converted to readable Markdown, preserving paragraphs, emphasis, quotes,
lists, code, links and attachments. It keeps second-precision original times with
an explicit timezone and local references to verified saved resource bytes.
Original JSON/HTML and media-status evidence remain linked. An upstream deletion
flag is retained and explained; no local or historical note is deleted.

Every Notion source variant remains linked to its immutable captured snapshot.
When the original source is unavailable, all Notion variant bodies remain
readable and visibly unmerged, with their source titles/tags and dates as received.
Missing or conflicting original timestamps stay unknown; no midnight or Notion
page-creation timestamp is invented as the original memo time. Original-only
memos are materialized even without a Notion page. Missing source IDs do not
mean deletion. Equal original update times with different bodies remain parallel
evidence instead of silently selecting a winner.

Readable capture completeness, original-source availability, media completeness
and unresolved differences are separate facts. A refused/missing resource remains
explicitly degraded. These notes can be read and considered as evidence without
claiming complete original-source or attachment fidelity. A 50 MiB legacy-resource
read limit and 200 MiB per-object resource budget are explicit holds, not type
exclusions. Outside-root and symlink resource references are held. Legacy resource reads stay
inside the existing Flomo source tree; links never grant permission to ingest
another subsystem's configuration or local credential stores. Outside-source
embeds remain held for their owning source rather than being copied implicitly. Source and
rendered material pass the existing high-confidence credential guard before new
objects are written.

## Daily/weekly audit and duplicate protection

The daily/weekly audit selects only the current state pointer in the registered
version area; version history, preserved legacy copies, resource files and the
cloud index remain physical inventory but are not separate fact candidates.
Unrelated Raw/01 notes, including ordinary notes elsewhere under `云端`, remain
eligible. Nothing broadly excludes Raw/01.

A legacy path is suppressed only when its stable memo ID maps to a selected,
complete current note, all current/source/resource hashes validate, and its exact
legacy bytes still match the captured mapping. Human changes or incomplete media
release that suppression. Human-edited selected current notes remain discoverable
with a weak edit-time evidence label; they are not silently overwritten or hidden.
The audit loads/verifies this selection once per run, not once per file. Daily/weekly
closeout counts use the same selected paths while retaining their original-source
date semantics, so importing history is not counted as new personal memos. The
filename auto-fixer also respects the exact immutable namespace owner.

Original source dates stay separate from `current_exported_at`, the actual local
time of a new materialization. Unchanged inputs do not create new versions or
refresh that time. Output-changing renderer upgrades must bump the explicit
renderer version in the input fingerprint. Both source updates and newly materialized evidence can enter
the normal review window; neither timestamp is automatic confirmation of a
personal fact. Existing Raw-to-Wiki confirmation rules still apply.

## Publication and integration gate

The source stage, its state, the new Raw/01 cloud area and its pointer state share
one scoped private commit in the existing workflow. Remote readback checks both
state blob identities and every recorded Markdown, resource, history, original
JSON/HTML and source-evidence blob. A successful local render is not production
acceptance. Existing source receipts/watermarks are read, not rewritten by this
exporter. Existing staging material remains excluded from fact promotion.

The vault owner must wire the new human entry into the existing Raw/01 README,
source registry and Wiki/retrieval entry during actual activation, preserving old
links. Then verify the real private publication, local Obsidian-readable files,
local asset bytes, current/legacy audit behavior and all unresolved source/media
coverage. Do not disable the local plugin or claim Mac-off completeness merely
because this implementation and tests pass.


## Local plugin cutover boundary

Cloud generation does not require the Mac. Receiving these new files in a live
local vault does require coordination: the existing Obsidian flomo plugin scans
Raw/01 recursively and otherwise treats every memo-ID note as its own writable
source. Before the first local receive, its normalizer, ID lookup, backlink
index/repair, cleanup, clear-data action and counts must exclude exactly the
cloud-owned `版本/`, `legacy-evidence/`, `_assets/` subtrees and cloud `index.md`.
Other notes remain under their original owner. Validate the loaded plugin runtime,
not only its disk bundle. Until this is verified, hold local reception of the new
namespace; do not silently disable the plugin or trigger a sync as a test.
