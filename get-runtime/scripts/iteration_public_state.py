"""Allowlisted aggregate metadata for iteration --state-output files.

Detailed reports still belong in the private vault via --write-latest. Never
copy arbitrary strings, source names, paths, candidate text or per-source maps
into a state file that may be committed to the public automation repository.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


def _date(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def public_state(summary: dict[str, Any]) -> dict[str, Any]:
    """Return only validated dates, a bounded mode, and fixed-key counts."""
    output: dict[str, Any] = {
        "schema_version": 1,
        "content_scope": "aggregate_metadata_only",
    }
    generated = summary.get("generated_at")
    if isinstance(generated, str):
        try:
            parsed = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and re.fullmatch(r"[0-9T:.+Z-]+", generated):
                output["generated_at"] = generated
        except ValueError:
            pass
    if isinstance(summary.get("mode"), str) and summary["mode"] in {"daily", "weekly", "monthly"}:
        output["mode"] = summary["mode"]
    period = summary.get("period")
    if isinstance(period, dict):
        safe_period = {key: period[key] for key in ("target_date", "window_start", "window_end")
                       if _date(period.get(key))}
        label = period.get("label")
        if isinstance(label, str) and re.fullmatch(r"\d{4}-(?:W(?:0[1-9]|[1-4]\d|5[0-3])|(?:0[1-9]|1[0-2])|(?:0[1-9]|1[0-2])-\d{2})", label):
            safe_period["label"] = label
        if safe_period:
            output["period"] = safe_period
    discovery = summary.get("discovery")
    if isinstance(discovery, dict):
        discovery = discovery.get("summary", discovery)
        if isinstance(discovery, dict):
            counts = {key: discovery[key] for key in ("source_count", "tree_path_count")
                      if _count(discovery.get(key))}
            for key in ("registered_existing", "registered_missing", "unregistered_existing",
                        "auxiliary_unregistered", "archive_only_existing", "archive_only_missing"):
                if isinstance(discovery.get(key), list):
                    counts[key] = len(discovery[key])
            if counts:
                output["discovery_counts"] = counts
    audit = summary.get("audit")
    if isinstance(audit, dict):
        audit = audit.get("counts", audit)
        if isinstance(audit, dict):
            counts = {key: audit[key] for key in ("total_candidates", "high_priority_candidates")
                      if _count(audit.get(key))}
            if counts:
                output["audit_counts"] = counts
    closeout = summary.get("closeout", summary)
    if isinstance(closeout, dict):
        counts = {key: len(closeout[key]) for key in
                  ("written_tabs", "actual_wiki_updates", "candidate_wiki_updates",
                   "candidate_skill_promotions", "follow_ups")
                  if isinstance(closeout.get(key), list)}
        if counts:
            output["closeout_counts"] = counts
    return output
