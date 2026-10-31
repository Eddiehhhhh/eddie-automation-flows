# 艾迪宇宙公开 flow 仓库

This repository hosts the public automation layer for 艾迪宇宙.

It does not contain the full knowledge base. The private vault lives in
`Eddiehhhhh/eddie-llm-wiki`, and the workflows here check that repository out
at runtime, then write back to the private vault through a GitHub token.

## What lives here

- GitHub Actions workflows for sync and automation flows
- Python helpers used by those workflows
- Minimal state files needed for scheduling and idempotency

## What does not live here

- `Raw/` content
- `Wiki/` content
- private chat archives or raw attachments
- Cloud Inbox drafts

## Required GitHub Actions secrets

- `PRIVATE_VAULT_TOKEN`
- `GETNOTE_API_KEY`
- `GETNOTE_CLIENT_ID`
- `FLOMO_TOKEN`
- `NOTION_API_KEY`
- `NOTION_WIKI_MIRROR_PARENT_PAGE_ID` or `NOTION_WIKI_MIRROR_PARENT_PAGE_TITLE`
- `NOTION_WIKI_MIRROR_BUNDLE_PASSPHRASE`
- `XINZHI_CLI_ACCESS_TOKEN`

## Privacy boundary

- This repo should only carry logic, scheduling, and minimal state.
- All content changes go into the private vault checkout.
- Workflow logs should stay metadata-only.
- The private vault token must never be printed into logs or committed into the repo.

## Trigger modes

- schedule
- manual `workflow_dispatch`
- external `repository_dispatch`

## Notion saved-article source

`sync_notion_articles_to_raw.py` reads one privately configured article database
into Raw/16. It keeps every existing plugin article, AI summary and daily note
unchanged. Notion page IDs remain separate from plugin IDs; shared original URLs
are recorded as relationships, including ambiguous matches.

The private `.github/state/notion-articles-config.json` uses `version: 1`, an
`enabled` boolean and a `database_id`. Missing configuration defaults off in the
existing personal-data workflow. The `raw16_only` manual mode verifies that the
configuration is enabled; the existing two daily schedules can run the same
collector. No new schedule or Notion write is introduced.

Current readable Markdown embeds localized images. Complete source JSON,
attachments and immutable history remain private. Per-object receipts survive a
partial source failure; edited current/history files or unknown files in the
managed namespace stop capture rather than being overwritten. Only current
articles enter incremental iteration; historical snapshots and generated indexes
are excluded. Empty source pages retain their metadata receipt.

After a run, verify the exact private content/state readback and the local
Obsidian copy. Code deployment alone does not authorize disabling the local
plugin: daily message input not represented in the article database remains a
separate migration gap. A failed/interrupted publication can leave an unowned
managed file; preserve it and reconcile against the private receipt before retry.
