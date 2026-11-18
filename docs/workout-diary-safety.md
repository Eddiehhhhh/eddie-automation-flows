# Workout diary linking deployment

Database title search is deliberately disabled. Store both `NOTION_WORKOUT_DATABASE_ID`
and `NOTION_DIARY_DATABASE_ID` as GitHub Actions secrets, after verifying the intended
workspace and database identities privately. Do not commit real IDs, names, pages,
API responses or private configuration in this public repository.

This client uses the legacy Notion database API. Supply database IDs, not page or
newer data-source IDs. The workout relation must target the configured diary database;
an unknown or mismatched target stops processing. Configure ambiguous property selections
using the existing private property secrets. Existing same-day duplicate diaries remain
skipped, not guessed.

Before rollout, run the unit suite, privately validate the two database schemas and
relation target, then use an explicitly authorized dry-run. A dry-run reports aggregate
counts only. Missing configuration is a blocked deployment, not a successful recovery.
A code merge alone does not prove any existing workout links have been repaired.

Past Actions logs are not changed by this patch. Inventory affected runs privately.
Deleting workflow logs is irreversible and requires separate owner confirmation.
Do not copy those logs into issues, PRs, test fixtures or public artifacts.
