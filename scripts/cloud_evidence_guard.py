#!/usr/bin/env python3
"""Pure read-only evidence adapter. No network, credentials, scheduler or vault writes.

The caller supplies observations from authorized readers. This does not turn a
successful job, an index, or a directory observation into a full content audit.
Detailed output is private; public logs receive counts only.
"""
import argparse
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from cloud_acceptance import SHANGHAI, report_status

SHA = re.compile(r"[0-9a-f]{40}")
EXPECTED_POLICIES = {"paused", "archive_only", "manual_only"}


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def freshness(value, now, max_age_hours=None):
    observed = timestamp(value)
    if observed is None:
        return {"status": "unknown", "at": value}
    age = (now - observed).total_seconds() / 3600
    if age < -5 / 60:
        status = "future_timestamp"
    elif max_age_hours is None:
        status = "observed_no_sla"
    elif not isinstance(max_age_hours, (int, float)) or isinstance(max_age_hours, bool) or not math.isfinite(max_age_hours) or max_age_hours <= 0:
        status = "invalid_sla"
    else:
        status = "stale" if age > max_age_hours else "within_sla"
    return {"status": status, "at": value, "age_hours": round(age, 3),
            "max_age_hours": max_age_hours}


def valid_policy(source):
    evidence = source.get("policy_evidence") or {}
    return (evidence.get("kind") in {"owner_message", "repository_policy"}
            and isinstance(evidence.get("reference"), str)
            and bool(evidence["reference"].strip()))


def inspect_source(source, now, snapshot_sha=None):
    mode = source.get("mode", "unknown")
    expected = mode in EXPECTED_POLICIES and valid_policy(source)
    gaps = []
    issues = []
    run = source.get("run") or {}
    definition = source.get("workflow") or {}
    if run:
        repository = definition.get("repository", "")
        run_id = run.get("id")
        valid_run = (isinstance(run_id, int) and not isinstance(run_id, bool)
                     and run_id > 0 and bool(repository)
                     and run.get("url") == f"https://github.com/{repository}/actions/runs/{run_id}"
                     and run.get("path") == definition.get("path")
                     and bool(SHA.fullmatch(str(run.get("head_sha", "")))))
        if not valid_run:
            execution = "invalid_run_evidence"
            gaps.append(execution)
        elif run.get("status") == "in_progress":
            execution = "in_progress"
        elif run.get("status") in {"queued", "waiting", "requested", "pending"}:
            execution = run["status"]
        elif run.get("status") != "completed":
            execution = "unverified"
            gaps.append("workflow_status_unverified")
        elif run.get("conclusion") == "success":
            execution = "latest_run_succeeded"
        else:
            execution = "latest_run_unsuccessful"
            if mode in {"production", "partial_production"} and not expected:
                issues.append(execution)
    else:
        execution = "not_expected" if expected else "unverified"
        if not expected:
            gaps.append("workflow_run_unverified")

    arrivals = []
    for artifact in source.get("artifacts", []):
        valid_version = (bool(SHA.fullmatch(str(artifact.get("commit_sha", ""))))
                         and bool(SHA.fullmatch(str(artifact.get("blob_sha", ""))))
                         and isinstance(artifact.get("path"), str)
                         and bool(artifact["path"]))
        item = dict(artifact)
        item["arrival"] = "version_observed" if valid_version else "unverified"
        if valid_version and snapshot_sha and artifact.get("commit_sha") != snapshot_sha:
            item["arrival"] = "historical_version_observed"
            gaps.append("artifact_snapshot_mismatch")
        item["freshness"] = freshness(artifact.get("generated_at"), now,
                                      source.get("max_age_hours"))
        # A read timestamp or Git commit time is never substituted for data time.
        item["source_content_freshness"] = freshness(artifact.get("latest_source_at"), now)
        arrivals.append(item)
        if not valid_version:
            gaps.append("artifact_version_unverified")
        if item["freshness"]["status"] in {"unknown", "observed_no_sla"}:
            gaps.append("artifact_freshness_not_established")
        if item["freshness"]["status"] in {"future_timestamp", "invalid_sla"}:
            gaps.append("artifact_timestamp_invalid")
        if item["freshness"]["status"] == "stale" and mode in {"production", "partial_production"}:
            issues.append("artifact_stale_under_explicit_sla")
    if not arrivals and not expected:
        gaps.append("destination_artifact_unverified")
    coverage = source.get("coverage") or {"status": "unknown"}
    if coverage.get("status") != "complete" and not expected:
        gaps.append("coverage_" + str(coverage.get("status", "unknown")))
    # Even a caller's complete coverage claim is reported, not re-verified here.
    coverage = {**coverage, "verification": "reported_by_evidence_collector",
                "full_sync_proven_by_adapter": False}
    due = timestamp(source.get("next_due_at"))
    if mode in EXPECTED_POLICIES:
        expectation = f"{mode}_expected" if expected else "policy_unverified"
        if not expected:
            gaps.append("policy_evidence_missing")
    elif due and now < due:
        expectation = "pending_future"
    elif mode == "preview":
        expectation = "preview_only"
    else:
        expectation = mode
    run_freshness = freshness(run.get("created_at"), now, source.get("max_age_hours"))
    if run_freshness["status"] == "stale" and mode in {"production", "partial_production"}:
        issues.append("workflow_run_stale_under_explicit_sla")
    return {"id": source["id"], "label": source.get("label", source["id"]),
            "expectation": expectation, "policy_evidence": source.get("policy_evidence"),
            "next_due_at": source.get("next_due_at"), "execution": execution,
            "workflow": definition, "run": run or None,
            "run_freshness": run_freshness,
            "artifacts": arrivals, "coverage": coverage,
            "gaps": sorted(set(gaps)), "issues": sorted(set(issues))}


def inspect_promotion(promotion, now):
    """Reuse the existing formal-status parser and preserve cursor consistency."""
    if not promotion:
        return {"status": "unverified", "reason": "no_evidence"}
    if promotion.get("mode") == "paused" and valid_policy(promotion):
        return {"status": "paused_expected", "policy_evidence": promotion["policy_evidence"]}
    day = now.astimezone(SHANGHAI).date().isoformat()
    body = promotion.get("report")
    if body is None:
        if promotion.get("scheduler_control") != "enabled_verified":
            return {"status": "unverified", "reason": "scheduler_control_unknown"}
        return {"status": "pending_schedule" if now.astimezone(SHANGHAI).hour < 12 else "missing_report"}
    status = report_status(body)
    if status not in {"success", "no_wiki_update"}:
        return {"status": "reported_incomplete" if status else "invalid_report", "reported_status": status}
    match = re.search(r"^- 扫描终点：`([0-9a-f]{40})`$", body, re.M)
    if not match:
        match = re.search(r"^- 扫描范围：`[0-9a-f]{40}\.\.([0-9a-f]{40})`$", body, re.M)
    state = promotion.get("state") or {}
    scan = state.get("raw_scan") or {}
    result = scan.get("result", "")
    valid_result = isinstance(result, str) and (result.startswith("success") or
                   (status == "no_wiki_update" and result == "no_wiki_update"))
    consistent = bool(match and (state.get("runs") or state.get("daily_runs") or {}).get(day, {}).get("effective_status") == status
                      and state.get("raw_cursor", {}).get("last_processed_head_sha") == match[1]
                      and state.get("last_processed_head_sha") == match[1]
                      and scan.get("head_sha") == match[1]
                      and scan.get("cursor_advanced") is True and valid_result)
    return {"status": "consistent_reported_success" if consistent else "cursor_mismatch",
            "reported_status": status, "full_source_audit": "not_performed"}


def inspect(evidence, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    if evidence.get("schema_version") != 1:
        raise ValueError("unsupported evidence schema")
    sources = evidence.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("nonempty sources required")
    ids = [item.get("id") for item in sources]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("unique nonempty source ids required")
    registered = (evidence.get("snapshot") or {}).get("registered_paths", [])
    if any(path not in ids for path in registered):
        raise ValueError("every registered source needs an explicit observed or unknown entry")
    snapshot_sha = (evidence.get("snapshot") or {}).get("commit_sha")
    output = [inspect_source(source, now, snapshot_sha) for source in sources]
    promotion = inspect_promotion(evidence.get("promotion"), now)
    control = evidence.get("scheduler_control") or {"status": "unknown"}
    return {"schema_version": 1, "mode": "readonly_cloud_evidence", "checked_at": now.isoformat(),
            "snapshot": evidence.get("snapshot"), "sources": output, "promotion": promotion,
            "scheduler_control": control, "local_maintenance": "not_checked_cloud",
            "overall": "attention" if any(s["issues"] for s in output) or promotion["status"] in {"missing_report", "cursor_mismatch", "invalid_report"} else "partial",
            "full_system_health": "not_established",
            "summary": {"sources": len(output), "issues": sum(len(s["issues"]) for s in output),
                        "evidence_gaps": sum(len(s["gaps"]) for s in output)},
            "mutations": "none"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="Private local report, never a formal vault state or public artifact")
    parser.add_argument("--vault", type=Path, help="Optional extra safeguard: output must be outside this tree")
    parser.add_argument("--now", help="Timezone-qualified test/replay timestamp")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.name.endswith(".private.json"):
        raise ValueError("private report suffix required; never target formal state")
    if output.exists() or output == args.evidence.resolve():
        raise ValueError("report output must be a new file")
    if args.vault:
        vault = args.vault.resolve()
        if output == vault or vault in output.parents:
            raise ValueError("output must be outside vault")
    now = timestamp(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        raise ValueError("invalid now timestamp")
    report = inspect(json.loads(args.evidence.read_text(encoding="utf-8")), now)
    # x prevents replacing a file created by another process after the check.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Cloud evidence inspection failed; private details suppressed.")
