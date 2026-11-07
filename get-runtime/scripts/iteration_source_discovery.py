#!/usr/bin/env python3
"""Discover Raw sources for daily/weekly iteration.

This script is intentionally read-only unless --write is provided. It gives
agents a shared source map before they run incremental audits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
RAW_DIR = ROOT / "Raw"
REGISTRY = ROOT / "Schema" / "来源目录注册.md"
DEFAULT_OUTPUT = ROOT / "Raw" / "00 Meta" / "iteration-source-discovery-latest.json"

RAW_PATH_RE = re.compile(r"`(Raw/[^`\n]+?/?)`")
STATE_FILE_NAMES = {
    "index.md",
    "schema.md",
    "manifest.json",
    "同步状态.json",
    "最近同步.md",
}
STATE_DIR_HINTS = {
    "02 Index",
    "03 State",
    "02 Snapshots",
    "04 Import Logs",
}


@dataclass
class SourceRecord:
    path: str
    exists: bool
    registered: bool
    file_count: int
    markdown_file_count: int
    recent_mtime: str | None
    descendant_dirs: list[str]
    state_files: list[str]
    notes: list[str]


@dataclass
class DiscoveryDelta:
    added_sources: list[str]
    removed_sources: list[str]
    newly_unregistered_existing: list[str]
    no_longer_unregistered_existing: list[str]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def read_registered_sources() -> set[str]:
    if not REGISTRY.exists():
        return set()
    text = REGISTRY.read_text(encoding="utf-8")
    sources: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        match = RAW_PATH_RE.search(line)
        if not match:
            continue
        raw = match.group(1).strip()
        if raw.startswith("Raw/") and raw.count("/") >= 2:
            parts = raw.rstrip("/").split("/")
            sources.add("/".join(parts[:2]) + "/")
    return sources


def list_raw_sources() -> set[str]:
    if not RAW_DIR.exists():
        return set()
    return {
        f"Raw/{child.name}/"
        for child in RAW_DIR.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    }


def iter_files(path: Path) -> Iterable[Path]:
    if not path.exists():
        return []
    return (p for p in path.rglob("*") if p.is_file() and not p.name.startswith(".DS_Store"))


def is_state_file(path: Path) -> bool:
    if path.name in STATE_FILE_NAMES:
        return True
    if path.name.endswith(("-state.json", "_state.json")):
        return True
    return any(part in STATE_DIR_HINTS for part in path.parts)


def build_record(raw_path: str, registered: bool) -> SourceRecord:
    abs_path = ROOT / raw_path.rstrip("/")
    files = list(iter_files(abs_path)) if abs_path.exists() else []
    markdown_files = [p for p in files if p.suffix.lower() == ".md"]
    descendant_dirs = [
        rel(p)
        for p in abs_path.rglob("*")
        if p.is_dir() and not p.name.startswith(".")
    ] if abs_path.exists() else []
    recent = max((p.stat().st_mtime for p in files), default=None)
    state_files = [rel(p) for p in files if is_state_file(p)][:30]

    notes: list[str] = []
    if raw_path == "Raw/03 Get/":
        notes.append("high_priority_personal_corpus: voice/recorder notes must be audited")
    if raw_path == "Raw/10 新枝/":
        notes.append("historical_snapshot: sync workflow is documented as stopped")
    if raw_path == "Raw/05 Chat/":
        notes.append("iteration_runtime_evidence: contains decisions, fixes, and prior outputs")
    if abs_path.exists() and files and not markdown_files:
        notes.append("auxiliary_only: no markdown corpus yet, keep out of iteration warnings")

    return SourceRecord(
        path=raw_path,
        exists=abs_path.exists(),
        registered=registered,
        file_count=len(files),
        markdown_file_count=len(markdown_files),
        recent_mtime=(
            datetime.fromtimestamp(recent, tz=timezone.utc).isoformat()
            if recent is not None
            else None
        ),
        descendant_dirs=sorted(descendant_dirs),
        state_files=state_files,
        notes=notes,
    )


def discover(previous: dict | None = None) -> dict:
    registered = read_registered_sources()
    actual = list_raw_sources()
    all_sources = sorted(registered | actual)
    records = [build_record(src, src in registered) for src in all_sources]

    registered_existing = [r.path for r in records if r.registered and r.exists]
    registered_missing = [r.path for r in records if r.registered and not r.exists]
    unregistered_existing = [
        r.path
        for r in records
        if not r.registered and r.exists and (r.markdown_file_count > 0 or r.state_files)
    ]
    auxiliary_unregistered = [
        r.path
        for r in records
        if not r.registered and r.exists and r.path not in unregistered_existing
    ]
    tree_paths = collect_tree_paths(records)

    github_notes = {
        "path": ".github/",
        "role": "execution_layer",
        "notes": [
            "Do not assume Raw/GitHub exists.",
            "Audit workflows, .github/state, system pages, and Raw/05 Chat for GitHub evidence.",
        ],
    }

    data = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "workspace": str(ROOT),
        "registry": rel(REGISTRY),
        "summary": {
            "registered_existing": registered_existing,
            "registered_missing": registered_missing,
            "unregistered_existing": unregistered_existing,
            "auxiliary_unregistered": auxiliary_unregistered,
            "source_count": len(records),
            "tree_path_count": len(tree_paths),
        },
        "sources": [asdict(r) for r in records],
        "tree_paths": tree_paths,
        "execution_layers": [github_notes],
        "warnings": build_warnings(registered_missing, unregistered_existing),
    }
    if previous is not None:
        data["delta"] = build_delta(previous, data)
    return data


def build_delta(previous: dict, current: dict) -> dict:
    prev_sources = {item["path"] for item in previous.get("sources", []) if item.get("exists")}
    curr_sources = {item["path"] for item in current.get("sources", []) if item.get("exists")}

    prev_unregistered = set(previous.get("summary", {}).get("unregistered_existing", []))
    curr_unregistered = set(current.get("summary", {}).get("unregistered_existing", []))
    prev_tree_raw = previous.get("tree_paths")
    curr_tree_raw = current.get("tree_paths")
    prev_tree = set(prev_tree_raw or [])
    curr_tree = set(curr_tree_raw or [])
    tree_delta_ready = "tree_paths" in previous and "tree_paths" in current

    delta = DiscoveryDelta(
        added_sources=sorted(curr_sources - prev_sources),
        removed_sources=sorted(prev_sources - curr_sources),
        newly_unregistered_existing=sorted(curr_unregistered - prev_unregistered),
        no_longer_unregistered_existing=sorted(prev_unregistered - curr_unregistered),
    )
    data = asdict(delta)
    if tree_delta_ready:
        data["added_dirs"] = sorted(curr_tree - prev_tree)
        data["removed_dirs"] = sorted(prev_tree - curr_tree)
    else:
        data["added_dirs"] = []
        data["removed_dirs"] = []
        data["tree_delta_initialized"] = False
    return data


def collect_tree_paths(records: list[SourceRecord]) -> list[str]:
    paths: set[str] = set()
    for record in records:
        if not record.exists:
            continue
        paths.add(record.path)
        paths.update(record.descendant_dirs)
    return sorted(paths)


def build_warnings(registered_missing: list[str], unregistered_existing: list[str]) -> list[str]:
    warnings: list[str] = []
    if registered_missing:
        warnings.append("registered_sources_missing_on_disk")
    if unregistered_existing:
        warnings.append("raw_sources_missing_registry_entries")
    return warnings


def render_markdown(data: dict) -> str:
    lines = [
        "# 来源自发现报告",
        "",
        f"- generated_at: `{data['generated_at']}`",
        f"- workspace: `{data['workspace']}`",
        "",
        "## Summary",
        "",
        f"- registered_existing: {len(data['summary']['registered_existing'])}",
        f"- registered_missing: {len(data['summary']['registered_missing'])}",
        f"- unregistered_existing: {len(data['summary']['unregistered_existing'])}",
        f"- tree_path_count: {data['summary'].get('tree_path_count', 0)}",
        "",
        "## Sources",
        "",
    ]
    for item in data["sources"]:
        flags = []
        if item["registered"]:
            flags.append("registered")
        if item["exists"]:
            flags.append("exists")
        lines.append(
            f"- `{item['path']}` | {', '.join(flags) or 'untracked'} | files={item['file_count']} | markdown={item['markdown_file_count']}"
        )
        for note in item["notes"]:
            lines.append(f"  - note: {note}")
    lines.extend(["", "## Warnings", ""])
    if data["warnings"]:
        lines.extend(f"- {warning}" for warning in data["warnings"])
    else:
        lines.append("- none")
    if data.get("delta"):
        delta = data["delta"]
        lines.extend(["", "## Delta", ""])
        lines.append(f"- added_sources: {len(delta['added_sources'])}")
        lines.append(f"- removed_sources: {len(delta['removed_sources'])}")
        lines.append(
            f"- newly_unregistered_existing: {len(delta['newly_unregistered_existing'])}"
        )
        lines.append(
            f"- no_longer_unregistered_existing: {len(delta['no_longer_unregistered_existing'])}"
        )
        if delta.get("tree_delta_initialized") is False:
            lines.append("- added_dirs: 0 (tree delta initialized in this snapshot)")
            lines.append("- removed_dirs: 0 (tree delta initialized in this snapshot)")
        else:
            lines.append(f"- added_dirs: {len(delta.get('added_dirs', []))}")
            lines.append(f"- removed_dirs: {len(delta.get('removed_dirs', []))}")
        if delta["added_sources"]:
            lines.append("- added:")
            lines.extend(f"  - {item}" for item in delta["added_sources"])
        if delta["removed_sources"]:
            lines.append("- removed:")
            lines.extend(f"  - {item}" for item in delta["removed_sources"])
        if delta.get("added_dirs"):
            lines.append("- added_dirs_examples:")
            lines.extend(f"  - {item}" for item in delta["added_dirs"][:20])
        if delta.get("removed_dirs"):
            lines.append("- removed_dirs_examples:")
            lines.extend(f"  - {item}" for item in delta["removed_dirs"][:20])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--write", type=Path, help="Write JSON report to this path")
    args = parser.parse_args()

    previous = None
    output_path = None
    if args.write:
        output_path = args.write if args.write.is_absolute() else ROOT / args.write
        if output_path.exists():
            try:
                previous = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                previous = None

    data = discover(previous)
    if args.write:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.format == "markdown":
        print(render_markdown(data), end="")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
