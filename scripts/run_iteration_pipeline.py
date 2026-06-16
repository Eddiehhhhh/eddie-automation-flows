#!/usr/bin/env python3
"""Run the executable iteration preflight for daily/weekly iteration.

This is the shared composition-layer entrypoint for agents and automations:
source discovery, incremental audit, and observatory front-end rebuild.
It intentionally does not replace later LLM judgment for Wiki promotion.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import build_observatory_frontend
import iteration_closeout
import iteration_incremental_audit
import iteration_source_discovery


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
META_DIR = ROOT / "Raw" / "00 Meta"
SHANGHAI = timezone(timedelta(hours=8))
LATEST_SUMMARY = {
    "daily": META_DIR / "iteration-daily-latest.json",
    "weekly": META_DIR / "iteration-weekly-latest.json",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--target-date", help="Target date in YYYY-MM-DD format")
    parser.add_argument("--window-days", type=int)
    parser.add_argument("--limit-per-source", type=int, default=80)
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--write-latest", action="store_true")
    parser.add_argument("--finalize", action="store_true", help="Write observatory closeout files after preflight")
    parser.add_argument("--state-output", help="Optional JSON path for last-run state output")
    return parser.parse_args()


def shanghai_today() -> date:
    return datetime.now(SHANGHAI).date()


def default_daily_target() -> date:
    return shanghai_today() - timedelta(days=1)


def previous_natural_week(today: date) -> tuple[date, date, str]:
    current_week_start = today - timedelta(days=today.weekday())
    prev_week_start = current_week_start - timedelta(days=7)
    prev_week_end = current_week_start - timedelta(days=1)
    iso_year, iso_week, _ = prev_week_start.isocalendar()
    return prev_week_start, prev_week_end, f"{iso_year}-W{iso_week:02d}"


def normalize_target(args: argparse.Namespace) -> tuple[date, int, dict[str, Any]]:
    if args.mode == "daily":
        target = date.fromisoformat(args.target_date) if args.target_date else default_daily_target()
        window_days = args.window_days or 3
        period = {
            "label": target.isoformat(),
            "target_date": target.isoformat(),
            "window_start": (target - timedelta(days=max(window_days - 1, 0))).isoformat(),
            "window_end": target.isoformat(),
        }
        return target, window_days, period

    if args.target_date:
        week_end = date.fromisoformat(args.target_date)
        week_start = week_end - timedelta(days=6)
        iso_year, iso_week, _ = week_start.isocalendar()
        week_label = f"{iso_year}-W{iso_week:02d}"
    else:
        week_start, week_end, week_label = previous_natural_week(shanghai_today())
    window_days = args.window_days or 7
    period = {
        "label": week_label,
        "target_date": week_end.isoformat(),
        "window_start": week_start.isoformat(),
        "window_end": week_end.isoformat(),
    }
    return week_end, window_days, period


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_volatile(item)
            for key, item in value.items()
            if key not in {"generated_at", "last_run_at"}
        }
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


def stable_write_json(path: Path, data: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_json(path)
    if existing is not None and strip_volatile(existing) == strip_volatile(data):
        return False
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def summarize_high_priority(audit_data: dict[str, Any], limit: int = 12) -> list[dict[str, Any]]:
    items = [
        {
            "source": item["source"],
            "path": item["path"],
            "date": item["evidence_date"],
            "reason": item["reason"],
        }
        for item in audit_data.get("candidates", [])
        if item.get("priority") == "high"
    ]
    return items[:limit]


def build_summary(
    mode: str,
    period: dict[str, Any],
    discovery_data: dict[str, Any],
    audit_data: dict[str, Any],
    frontend_result: dict[str, Any],
    closeout_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "period": period,
        "discovery": {
            "summary": discovery_data["summary"],
            "delta": discovery_data.get("delta", {}),
            "warnings": discovery_data.get("warnings", []),
            "path": iteration_source_discovery.rel(iteration_source_discovery.DEFAULT_OUTPUT),
        },
        "audit": {
            "counts": audit_data["counts"],
            "source_delta": audit_data.get("source_delta", {}),
            "high_priority": summarize_high_priority(audit_data),
            "path": iteration_source_discovery.rel(META_DIR / "iteration-incremental-audit-latest.json"),
        },
        "frontend": frontend_result,
    }
    if closeout_result is not None:
        summary["closeout"] = {
            "chat_record": closeout_result["chat_record"],
            "observatory_folder": closeout_result["observatory_folder"],
            "written_tabs": closeout_result["written_tabs"],
            "actual_wiki_updates": closeout_result.get("actual_wiki_updates", []),
            "candidate_wiki_updates": closeout_result["candidate_wiki_updates"],
            "candidate_skill_promotions": closeout_result["candidate_skill_promotions"],
            "follow_ups": closeout_result["follow_ups"],
            "known_context": closeout_result["known_context"],
        }
    return summary


def render_markdown(data: dict[str, Any]) -> str:
    period = data["period"]
    discovery = data["discovery"]
    audit = data["audit"]
    frontend = data["frontend"]
    lines = [
        f"# {data['mode']} iteration pipeline",
        "",
        f"- period: `{period['label']}`",
        f"- window: `{period['window_start']}` -> `{period['window_end']}`",
        f"- total_candidates: {audit['counts']['total_candidates']}",
        f"- high_priority_candidates: {audit['counts']['high_priority_candidates']}",
        f"- registered_missing: {len(discovery['summary'].get('registered_missing', []))}",
        f"- unregistered_existing: {len(discovery['summary'].get('unregistered_existing', []))}",
        "",
        "## Discovery Delta",
        "",
        f"- added_sources: {len(discovery['delta'].get('added_sources', []))}",
        f"- removed_sources: {len(discovery['delta'].get('removed_sources', []))}",
        f"- added_dirs: {len(discovery['delta'].get('added_dirs', []))}",
        f"- removed_dirs: {len(discovery['delta'].get('removed_dirs', []))}",
        "",
        "## High Priority",
        "",
    ]
    if not audit["high_priority"]:
        lines.append("- none")
    else:
        for item in audit["high_priority"]:
            lines.append(
                f"- `{item['path']}` | {item['date']} | {item['reason']}"
            )
    lines.extend([
        "",
        "## Observatory",
        "",
        f"- html: `{frontend['html_path']}`",
        f"- json: `{frontend['json_path']}`",
        f"- current_daily: `{frontend['views'].get('daily')}`",
        f"- current_weekly: `{frontend['views'].get('weekly')}`",
        f"- current_monthly: `{frontend['views'].get('monthly')}`",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    target, window_days, period = normalize_target(args)

    audit_data = iteration_incremental_audit.audit(target, window_days, args.limit_per_source)
    previous_discovery = load_json(iteration_source_discovery.DEFAULT_OUTPUT)
    discovery_data = iteration_source_discovery.discover(previous_discovery)
    closeout_result = None
    if args.finalize:
        closeout_result = iteration_closeout.closeout(args.mode, args.target_date, args.window_days)
        frontend_result = closeout_result["frontend"]
    else:
        frontend_snapshot = build_observatory_frontend.build_snapshot()
        frontend_changed = build_observatory_frontend.write_outputs(frontend_snapshot)
        frontend_result = {
            "html_path": iteration_source_discovery.rel(build_observatory_frontend.HTML_OUTPUT),
            "json_path": iteration_source_discovery.rel(build_observatory_frontend.JSON_OUTPUT),
            "views": {
                name: info["current_period"]
                for name, info in frontend_snapshot["views"].items()
            },
            "changed": frontend_changed,
        }
    summary = build_summary(args.mode, period, discovery_data, audit_data, frontend_result, closeout_result)

    if args.write_latest:
        stable_write_json(iteration_source_discovery.DEFAULT_OUTPUT, discovery_data)
        stable_write_json(META_DIR / "iteration-incremental-audit-latest.json", audit_data)
        if closeout_result is not None:
            stable_write_json(iteration_closeout.LATEST_CLOSEOUT[args.mode], closeout_result)
        summary_path = LATEST_SUMMARY[args.mode]
        summary["latest_path"] = iteration_source_discovery.rel(summary_path)
        summary["latest_changed"] = stable_write_json(summary_path, summary)

    if args.state_output:
        state_path = Path(args.state_output)
        if not state_path.is_absolute():
            state_path = ROOT / state_path
        summary["state_output_path"] = iteration_source_discovery.rel(state_path)
        summary["state_output_changed"] = stable_write_json(state_path, summary)

    if args.format == "markdown":
        print(render_markdown(summary), end="")
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
