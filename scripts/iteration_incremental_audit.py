#!/usr/bin/env python3
"""Build an incremental input candidate list for daily/weekly iteration.

The script is read-only unless --write is provided. It does not promote
anything to Wiki; it only prepares evidence for human/agent judgment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import iteration_source_discovery


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
RAW_DIR = ROOT / "Raw"
DATE_RE = re.compile(r"(20\d{2})[-/](\d{2})[-/](\d{2})")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
SHANGHAI = timezone(timedelta(hours=8))


@dataclass
class Candidate:
    source: str
    path: str
    title: str
    evidence_date: str | None
    evidence_field: str
    note_type: str | None
    priority: str
    reason: str


def parse_args() -> argparse.Namespace:
    yesterday = datetime.now(SHANGHAI).date() - timedelta(days=1)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-date", default=yesterday.isoformat())
    parser.add_argument("--window-days", type=int, default=3)
    parser.add_argument("--limit-per-source", type=int, default=80)
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--write", type=Path, help="Write JSON report to this path")
    return parser.parse_args()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def read_text_head(path: Path, limit: int = 12000) -> str:
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        return f.read(limit)


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def title_for(path: Path, fields: dict[str, str]) -> str:
    return fields.get("title") or path.stem


def note_type_for(fields: dict[str, str]) -> str | None:
    return fields.get("note_type") or fields.get("type") or fields.get("kind")


def priority_for(source: str, path: Path, note_type: str | None) -> tuple[str, str]:
    path_text = path.as_posix()
    if source == "Raw/03 Get/" and (
        "01 语音记录" in path_text or "99 录音卡笔记" in path_text or note_type in {"audio", "recorder_audio", "recorder_flash_audio"}
    ):
        return "high", "Get voice/recorder note"
    if source == "Raw/05 Chat/":
        return "medium", "conversation/runtime evidence"
    if source in {"Raw/01 Flomo/", "Raw/02 Notion/"}:
        return "medium", "core personal input"
    return "normal", "incremental source evidence"


def candidate_from_file(path: Path, source: str, start: date, end: date) -> Candidate | None:
    if path.suffix.lower() != ".md":
        return None
    if "_assets" in path.parts or "Asserts" in path.parts:
        return None

    text = read_text_head(path)
    fields = parse_frontmatter(text)
    date_fields = [
        ("created_at", fields.get("created_at")),
        ("updated_at", fields.get("updated_at")),
        ("date", fields.get("date")),
        ("path", path.as_posix()),
    ]
    for field, raw_value in date_fields:
        found = parse_date(raw_value)
        if found and start <= found <= end:
            note_type = note_type_for(fields)
            priority, reason = priority_for(source, path, note_type)
            return Candidate(
                source=source,
                path=rel(path),
                title=title_for(path, fields),
                evidence_date=found.isoformat(),
                evidence_field=field,
                note_type=note_type,
                priority=priority,
                reason=reason,
            )

    # mtime is only a weak fallback when the file has no usable date evidence.
    if not any(parse_date(raw) for _, raw in date_fields[:-1]):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, SHANGHAI).date()
        if start <= mtime <= end:
            note_type = note_type_for(fields)
            priority, reason = priority_for(source, path, note_type)
            return Candidate(
                source=source,
                path=rel(path),
                title=title_for(path, fields),
                evidence_date=mtime.isoformat(),
                evidence_field="mtime_weak",
                note_type=note_type,
                priority=priority,
                reason=reason,
            )
    return None


def audit(target: date, window_days: int, limit_per_source: int) -> dict:
    start = target - timedelta(days=max(window_days - 1, 0))
    end = target
    previous_discovery = None
    discovery_snapshot = iteration_source_discovery.DEFAULT_OUTPUT
    if discovery_snapshot.exists():
        try:
            previous_discovery = json.loads(discovery_snapshot.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous_discovery = None

    discovery = iteration_source_discovery.discover(previous_discovery)
    candidates: list[Candidate] = []
    per_source_counts: dict[str, int] = {}

    for item in discovery["sources"]:
        source = item["path"]
        if not item["exists"]:
            continue
        source_path = ROOT / source.rstrip("/")
        source_candidates: list[Candidate] = []
        for path in source_path.rglob("*.md"):
            if not path.is_file():
                continue
            candidate = candidate_from_file(path, source, start, end)
            if candidate:
                source_candidates.append(candidate)
        source_candidates.sort(key=lambda c: (c.priority != "high", c.evidence_date or "", c.path))
        per_source_counts[source] = len(source_candidates)
        candidates.extend(source_candidates[:limit_per_source])

    high_priority = [c for c in candidates if c.priority == "high"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_date": target.isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "source_discovery": discovery["summary"],
        "source_delta": discovery.get("delta", {}),
        "counts": {
            "total_candidates": len(candidates),
            "high_priority_candidates": len(high_priority),
            "per_source": per_source_counts,
        },
        "candidates": [asdict(c) for c in candidates],
        "notes": [
            "mtime_weak means date metadata was absent; do not promote without reading the file.",
            "each discovered source is scanned recursively, so new files and subfolders under it are automatically included.",
            "Get voice/recorder candidates should be read before judging personal state.",
            "source_delta is read from the previous iteration-source-discovery snapshot when available.",
            "directory tree additions/removals are reported through source_delta as added_dirs and removed_dirs.",
        ],
    }


def render_markdown(data: dict) -> str:
    lines = [
        "# 增量输入审计",
        "",
        f"- target_date: `{data['target_date']}`",
        f"- window: `{data['window_start']}` -> `{data['window_end']}`",
        f"- total_candidates: {data['counts']['total_candidates']}",
        f"- high_priority_candidates: {data['counts']['high_priority_candidates']}",
        "",
        "## Source Delta",
        "",
    ]
    delta = data.get("source_delta", {})
    if delta:
        lines.append(f"- added_sources: {len(delta.get('added_sources', []))}")
        lines.append(f"- removed_sources: {len(delta.get('removed_sources', []))}")
        lines.append(
            f"- newly_unregistered_existing: {len(delta.get('newly_unregistered_existing', []))}"
        )
        lines.append(
            f"- no_longer_unregistered_existing: {len(delta.get('no_longer_unregistered_existing', []))}"
        )
        lines.append(f"- added_dirs: {len(delta.get('added_dirs', []))}")
        lines.append(f"- removed_dirs: {len(delta.get('removed_dirs', []))}")
    else:
        lines.append("- none")
    lines.extend([
        "",
        "## Per Source",
        "",
    ])
    for source, count in data["counts"]["per_source"].items():
        lines.append(f"- `{source}`: {count}")
    lines.extend(["", "## High Priority", ""])
    high = [c for c in data["candidates"] if c["priority"] == "high"]
    if not high:
        lines.append("- none")
    for item in high[:30]:
        lines.append(f"- `{item['path']}` | {item['evidence_date']} | {item['reason']}")
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in data["notes"])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    data = audit(date.fromisoformat(args.target_date), args.window_days, args.limit_per_source)
    if args.write:
        output = args.write if args.write.is_absolute() else ROOT / args.write
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.format == "markdown":
        print(render_markdown(data), end="")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
