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
