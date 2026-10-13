#!/usr/bin/env python3
"""Build one reviewable weekly cloud bundle; Git publication remains separate.

The input pages are private, already synthesized by an authorized agent. This
script never sends them to a model or adds Wiki pages. It only materializes the
approved weekly report, indexes, frontend and receipts in an isolated checkout.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SHANGHAI = timezone(timedelta(hours=8))
TABS = ("个人状态", "系统状态", "启发洞察")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-c", "core.quotePath=false", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


def write_text(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def write_json(root: Path, relative: str, value: dict) -> None:
    write_text(root, relative, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def add_index_link(root: Path, relative: str, marker: str, line: str) -> None:
    path = root / relative
    old = path.read_text(encoding="utf-8")
    if marker in old:
        return
    write_text(root, relative, old.rstrip() + "\n\n" + line + "\n")


def add_jsonl(root: Path, relative: str, row: dict, run_key: str) -> None:
    path = root / relative
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if run_key in old:
        return
    write_text(root, relative, old.rstrip("\n") + "\n" + json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def allowed_paths(period: str, run_date: str) -> set[str]:
    folder = f"Observatory/weekly/{period[:4]}/{period}"
    return {
        *(f"{folder}/{name}.md" for name in TABS),
        "Observatory/index.html", "Observatory/metrics/observatory-data.json",
        "Observatory/_index.jsonl", "Observatory/索引.md",
        "Observatory/weekly/索引.md", f"Observatory/weekly/{period[:4]}/索引.md",
        "Raw/00 Meta/iteration-source-discovery-latest.json",
        "Raw/00 Meta/iteration-incremental-audit-latest.json",
        "Raw/00 Meta/iteration-weekly-closeout-latest.json",
        "Raw/00 Meta/iteration-weekly-latest.json",
        "Raw/00 Meta/system4-acceptance.json",
        f"Raw/05 Chat/{run_date}-艾迪宇宙每周迭代-{period}.md",
        "Raw/05 Chat/_index.jsonl",
    }


def changed_paths(root: Path) -> set[str]:
    raw = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"])
    records = [part for part in raw.split(b"\0") if part]
    if any(record[:2] in (b"R ", b" R", b"D ", b" D") for record in records):
        raise ValueError("Weekly bundle cannot rename or delete paths")
    return {record[3:].decode("utf-8") for record in records}


def verify_existing(root: Path, period: str, run_date: str, run_key: str) -> dict:
    changed = changed_paths(root)
    if not changed <= allowed_paths(period, run_date) or len(changed) > 16:
        raise ValueError("Changed paths exceed weekly bundle allowlist")
    latest = json.loads((root / "Raw/00 Meta/iteration-weekly-latest.json").read_text(encoding="utf-8"))
    if latest.get("run_key") != run_key or latest.get("period", {}).get("label") != period:
        raise ValueError("Weekly state/run key mismatch")
    for name in TABS:
        text = (root / f"Observatory/weekly/{period[:4]}/{period}/{name}.md").read_text(encoding="utf-8")
        if f"date: {period}" not in text or "## 标签" not in text or "## 引用文章" not in text:
            raise ValueError("Weekly tab readback failed")
    data = json.loads((root / "Observatory/metrics/observatory-data.json").read_text(encoding="utf-8"))
    if data["views"]["weekly"]["current_period"] != period:
        raise ValueError("Frontend readback failed")
    if (root / "Observatory/index.html").read_text(encoding="utf-8").find(period) < 0:
        raise ValueError("HTML readback failed")
    return {"run_key": run_key, "period": period, "changed_count": len(changed),
            "weekly_current": period, "daily_current": data["views"].get("daily", {}).get("current_period"),
            "monthly_current": data["views"].get("monthly", {}).get("current_period")}


def produce(root: Path, source_sha: str, pages: dict, run_date: str) -> dict:
    # Import after VAULT_ROOT is set: these existing deterministic modules bind
    # their paths at import time.
    os.environ["VAULT_ROOT"] = str(root)
    import iteration_source_discovery as discovery_module
    import iteration_incremental_audit as audit_module
    import build_observatory_frontend as frontend

    period = pages["period"]
    if not isinstance(period, str) or not period.startswith("20") or "-W" not in period:
        raise ValueError("Invalid period")
    target = date.fromisoformat(pages["window_end"])
    if target.weekday() != 6 or target.isocalendar()[:2] != (int(period[:4]), int(period[-2:])):
        raise ValueError("Weekly period/date mismatch")
    if set(pages["tabs"]) != set(TABS):
        raise ValueError("Exactly three weekly tabs required")
    for name in TABS:
        body = pages["tabs"][name]
        if not isinstance(body, str) or f"date: {period}" not in body or "## 标签" not in body or "## 引用文章" not in body:
            raise ValueError("Weekly tab lacks required structure")
    if git(root, "rev-parse", "HEAD") != source_sha or git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Expected clean fixed-source checkout")

    latest_path = root / "Raw/00 Meta/iteration-weekly-latest.json"
    previous = json.loads(latest_path.read_text(encoding="utf-8"))
    run_key = hashlib.sha256(f"weekly:{period}:{source_sha}:cloud".encode()).hexdigest()
    if previous.get("run_key") == run_key:
        raise ValueError("Run key already published")
    previous_discovery_path = discovery_module.DEFAULT_OUTPUT
    previous_discovery = json.loads(previous_discovery_path.read_text(encoding="utf-8")) if previous_discovery_path.exists() else None
    discovery = discovery_module.discover(previous_discovery)
    if discovery["summary"]["registered_missing"] or discovery["summary"]["unregistered_existing"]:
        raise ValueError("Source registry incomplete")
    audit = audit_module.audit(target, 7, 100000)
    if audit["counts"]["total_candidates"] != len(audit["candidates"]):
        raise ValueError("Audit candidate count mismatch")
    before = frontend.build_snapshot()
    folder = f"Observatory/weekly/{period[:4]}/{period}"
    written_tabs = [f"{folder}/{name}.md" for name in TABS]
    for name, relative in zip(TABS, written_tabs):
        write_text(root, relative, pages["tabs"][name].rstrip() + "\n")

    add_index_link(root, "Observatory/索引.md", period,
                   f"- [每周迭代 {period}](./weekly/{period[:4]}/{period}/系统状态.md)：三页周档与运行状态。")
    links = "\n".join(f"- [[weekly/{period[:4]}/{period}/{name}|{period} {name}]]" for name in TABS)
    add_index_link(root, "Observatory/weekly/索引.md", period, links)
    add_index_link(root, f"Observatory/weekly/{period[:4]}/索引.md", period,
                   "\n".join(f"- [[{period}/{name}|{period} {name}]]" for name in TABS))

    after = frontend.build_snapshot()
    if after["views"]["weekly"]["current_period"] != period:
        raise ValueError("Frontend current week mismatch")
    for view, old_view in before["views"].items():
        old_ids = {entry["id"] for entry in old_view["entries"]}
        new_ids = {entry["id"] for entry in after["views"][view]["entries"]}
        if not old_ids <= new_ids:
            raise ValueError("Frontend lost historical periods")
    frontend.write_outputs(after)

    stamp = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    receipt_path = f"Raw/05 Chat/{run_date}-艾迪宇宙每周迭代-{period}.md"
    receipt = (f"---\ndate: {run_date}\ntype: chat\nsource: codex\ntopic: 艾迪宇宙每周迭代 {period}\n---\n"
               f"# 艾迪宇宙每周迭代 {period} — 云迁移运行回执\n\n"
               f"- run_key: `{run_key}`\n- 固定来源提交: `{source_sha}`\n"
               f"- 本提交重新审计的候选: {audit['counts']['total_candidates']}；这不是此前 55 份正文 ledger，也不是旧 Actions 的 53 个候选。\n"
               "- 综合来源: 已交付的 W41 两页只读复盘；原 55 项逐文件 ledger 未恢复，不伪称本轮重新核读。\n"
               "- 周档: " + "、".join(f"`{p}`" for p in written_tabs) + "\n"
               "- Wiki 写入: 0；个人事实与规则候选未自动升格。\n"
               "- Mac 工作区、磁盘与本地同步: 云端未检查；保留为独立本机维护。\n"
               "- 生产状态: 本次为隔离工作树生成；远端及手机读回须在发布后单独核验。\n\n"
               "## 本轮实际读取规则\n- `AGENTS.md`\n- `Wiki/索引.md`\n- `Wiki/06 Systems/01 Routing/规则入口索引.md`\n"
               "- `Wiki/06 Systems/00 Architecture/迭代系统.md`\n- `Schema/执行规范.md`\n")
    write_text(root, receipt_path, receipt)
    add_jsonl(root, "Raw/05 Chat/_index.jsonl", {"ts": stamp, "agent": "Codex", "action": "weekly_cloud_bundle",
        "target": receipt_path, "run_key": run_key, "summary": f"{period} 周档与云迁移运行回执；Wiki 0 写入"}, run_key)
    add_jsonl(root, "Observatory/_index.jsonl", {"ts": stamp, "agent": "Codex", "action": "weekly_snapshot",
        "target": folder, "run_key": run_key, "summary": f"{period} 三页周档与前台同步"}, run_key)

    write_json(root, "Raw/00 Meta/iteration-source-discovery-latest.json", discovery)
    write_json(root, "Raw/00 Meta/iteration-incremental-audit-latest.json", audit)
    closeout = {"mode": "weekly", "period": period, "window_start": (target - timedelta(days=6)).isoformat(),
        "window_end": target.isoformat(), "source_sha": source_sha, "run_key": run_key,
        "written_tabs": written_tabs, "chat_record": receipt_path, "observatory_folder": folder,
        "actual_wiki_updates": [], "candidate_wiki_updates": pages.get("wiki_candidates", []),
        "candidate_skill_promotions": pages.get("skill_candidates", []),
        "source_completeness": "partial_historical_report_ledger_unavailable",
        "frontend": {"html_path": "Observatory/index.html", "json_path": "Observatory/metrics/observatory-data.json",
                     "views": {k: v["current_period"] for k, v in after["views"].items()}}}
    write_json(root, "Raw/00 Meta/iteration-weekly-closeout-latest.json", closeout)
    write_json(root, "Raw/00 Meta/iteration-weekly-latest.json", {"mode": "weekly", "period": {"label": period,
        "target_date": target.isoformat(), "window_start": (target - timedelta(days=6)).isoformat(), "window_end": target.isoformat()},
        "run_key": run_key, "source_sha": source_sha, "discovery": discovery["summary"],
        "audit": {"counts": audit["counts"]}, "closeout": closeout, "production_publish": "pending_remote_readback"})
    system4_path = root / "Raw/00 Meta/system4-acceptance.json"
    system4 = json.loads(system4_path.read_text(encoding="utf-8"))
    system4["generated_at"] = stamp
    system4["ready_for_4_0"] = False
    system4["weekly"]["through"] = period
    system4["weekly"]["current_streak"] = 0
    system4["weekly"]["ready"] = False
    system4["weekly"].setdefault("weeks", {})[period] = {"status": "partial", "missing": [
        "original_55_file_ledger_unavailable", "remote_and_mobile_readback_pending"]}
    write_json(root, "Raw/00 Meta/system4-acceptance.json", system4)

    result = verify_existing(root, period, run_date, run_key)
    result.update({"source_sha": source_sha, "audit_candidates": audit["counts"]["total_candidates"]})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--pages-json", required=True, type=Path)
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--verify-existing", action="store_true", help="Check an already generated isolated bundle without writing")
    args = parser.parse_args()
    root = args.vault.resolve()
    if root in args.pages_json.resolve().parents:
        raise ValueError("Private page input must be outside the checkout")
    date.fromisoformat(args.run_date)
    pages = json.loads(args.pages_json.read_text(encoding="utf-8"))
    if args.verify_existing:
        run_key = hashlib.sha256(f"weekly:{pages['period']}:{args.source_sha}:cloud".encode()).hexdigest()
        result = verify_existing(root, pages["period"], args.run_date, run_key)
    else:
        result = produce(root, args.source_sha, pages, args.run_date)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Weekly bundle failed; private details suppressed.", file=sys.stderr)
        raise SystemExit(1)
