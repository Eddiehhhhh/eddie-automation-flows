#!/usr/bin/env python3
"""Generate a workspace intake report for 艾迪宇宙.

This script is intentionally read-only for repository content. It classifies
the current git working tree so an agent or human can see what needs staging,
ignoring, review, or follow-up without guessing from a long git status dump.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
REPORT_PATH = ROOT / "Wiki" / "06 Systems" / "90 Incidents" / "工作区自动收口报告.md"
JSON_PATH = ROOT / "Raw" / "00 Meta" / "workspace-intake-latest.json"
TZ = dt.timezone(dt.timedelta(hours=8))


@dataclass(frozen=True)
class StatusItem:
    status: str
    path: str
    old_path: str | None = None


def run_git_status() -> list[StatusItem]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    parts = result.stdout.decode("utf-8", errors="replace").split("\0")
    items: list[StatusItem] = []
    i = 0
    while i < len(parts):
        raw = parts[i]
        i += 1
        if not raw:
            continue
        status = raw[:2]
        path = raw[3:]
        old_path = None
        if status[0] in {"R", "C"}:
            if i < len(parts):
                old_path = parts[i] or None
                i += 1
        items.append(StatusItem(status=status, path=path, old_path=old_path))
    return items


def classify_path(path: str) -> tuple[str, str, str]:
    """Return group, recommendation, risk."""
    local_prefixes = (
        ".obsidian/",
        ".workbuddy/",
        ".claude/",
        ".claudian/",
        "Excalidraw/",
        "obsidian-reader/",
        "_work_getnote_daily_summary/",
        "outputs/",
    )
    temporary_names = {
        "task_plan.md",
        "findings.md",
        "progress.md",
        "migrate_getnote_filenames.py",
    }

    if path == ".DS_Store" or path.endswith("/.DS_Store"):
        return "本地应用状态", "加入忽略或删除本地缓存", "低"
    if path.startswith(local_prefixes):
        return "本地应用状态", "优先加入忽略；必要配置单独确认", "中"
    if path in temporary_names:
        return "临时任务文件", "有交接价值则归档，否则清理或忽略", "中"
    if path.startswith(".github/") or path.startswith("scripts/") or path in {
        ".env.example",
        ".gitignore",
        "README.md",
        "AGENTS.md",
    }:
        return "同步与自动化代码", "跑语法/最小检查后作为代码批次处理", "中"
    if path.startswith("Raw/"):
        if path.startswith("Raw/00 Meta/") or path.startswith("Raw/05 Chat/"):
            return "Raw 来源层", "确认无密钥后按来源批次处理", "中"
        return "Raw 来源层", "确认 frontmatter/正文清洁度后按来源批次处理", "中"
    if path.startswith("Wiki/"):
        return "Wiki 综合层", "检查索引/来源/更新日志后作为 Wiki 批次处理", "低"
    if path.startswith("Schema/"):
        return "Schema 与模板", "确认是可复用协议后作为 Schema 批次处理", "低"
    if path.startswith("Secrets/") or ".env" in path or "token" in path.lower():
        return "敏感风险项", "不要提交；检查是否应加入忽略", "高"
    return "未分类", "人工判断归属后再处理", "中"


def status_label(status: str) -> str:
    if status == "??":
        return "未跟踪"
    if "D" in status:
        return "删除"
    if "T" in status:
        return "类型变化"
    if "M" in status:
        return "修改"
    if "A" in status:
        return "新增"
    if "R" in status:
        return "重命名"
    return status.strip() or "未知"


def sample_lines(items: Iterable[StatusItem], limit: int = 12) -> list[str]:
    lines: list[str] = []
    for item in list(items)[:limit]:
        label = status_label(item.status)
        path = item.path
        if item.old_path:
            path = f"{item.old_path} -> {path}"
        lines.append(f"- `{label}` `{path}`")
    return lines


def build_report(items: list[StatusItem]) -> tuple[str, dict[str, object]]:
    now = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %z")
    groups: dict[str, list[StatusItem]] = defaultdict(list)
    recs: dict[str, Counter[str]] = defaultdict(Counter)
    risks: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()

    for item in items:
        group, rec, risk = classify_path(item.path)
        groups[group].append(item)
        recs[group][rec] += 1
        risks[risk] += 1
        status_counts[status_label(item.status)] += 1

    ordered_groups = [
        "敏感风险项",
        "同步与自动化代码",
        "Raw 来源层",
        "Wiki 综合层",
        "Schema 与模板",
        "本地应用状态",
        "临时任务文件",
        "未分类",
    ]

    lines = [
        "# 工作区自动收口报告",
        "",
        f"- 生成时间：`{now}`",
        f"- 变化总数：`{len(items)}`",
        "- 脚本行为：只读分类，不删除、不提交、不推送。",
        "",
        "## 总览",
        "",
        "| 状态 | 数量 |",
        "| --- | ---: |",
    ]
    for label, count in status_counts.most_common():
        lines.append(f"| {label} | {count} |")

    lines.extend(
        [
            "",
            "## 风险分布",
            "",
            "| 风险 | 数量 |",
            "| --- | ---: |",
        ]
    )
    for label in ["高", "中", "低"]:
        lines.append(f"| {label} | {risks.get(label, 0)} |")

    lines.extend(["", "## 分组", ""])
    for group in ordered_groups:
        group_items = groups.get(group, [])
        if not group_items:
            continue
        lines.extend([f"### {group}", ""])
        lines.append(f"- 数量：`{len(group_items)}`")
        if recs[group]:
            top_rec, _ = recs[group].most_common(1)[0]
            lines.append(f"- 默认处理：{top_rec}")
        lines.append("")
        lines.extend(sample_lines(group_items))
        if len(group_items) > 12:
            lines.append(f"- 还有 `{len(group_items) - 12}` 项未展示，详见 JSON 数据。")
        lines.append("")

    lines.extend(
        [
            "## 自动化建议",
            "",
            "1. 高风险项只提醒，不自动处理。",
            "2. 本地应用状态优先进入 `.gitignore` 候选，不直接删除。",
            "3. Raw / Wiki / Schema 分批处理，避免把证据、结论和协议混在一次提交。",
            "4. 删除和类型变化必须先确认来源，再决定是否提交。",
            "",
        ]
    )

    data = {
        "generated_at": now,
        "total": len(items),
        "status_counts": dict(status_counts),
        "risk_counts": dict(risks),
        "groups": {
            group: {
                "count": len(group_items),
                "recommendations": dict(recs[group]),
                "items": [
                    {
                        "status": item.status,
                        "label": status_label(item.status),
                        "path": item.path,
                        "old_path": item.old_path,
                    }
                    for item in group_items
                ],
            }
            for group, group_items in groups.items()
        },
    }
    return "\n".join(lines), data


def write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--json", type=Path, default=JSON_PATH)
    args = parser.parse_args()

    items = run_git_status()
    report, data = build_report(items)
    report_changed = write_if_changed(args.report, report + "\n")
    json_changed = write_if_changed(
        args.json,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )
    print(f"workspace changes: {len(items)}")
    print(f"report: {args.report} ({'updated' if report_changed else 'unchanged'})")
    print(f"json: {args.json} ({'updated' if json_changed else 'unchanged'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
