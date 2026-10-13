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


def ensure_new_run_key(previous: dict, run_key: str) -> None:
    if previous.get("run_key") == run_key:
        raise ValueError("Run key already published")


def validate_evidence(root: Path, source_sha: str, pages: dict, discovery: dict, audit: dict) -> dict:
    """Fail closed on missing inference/coverage attestations; verify file hashes."""
    evidence = pages["evidence"]
    required = {"source_snapshot_sha", "synthesis_status", "synthesis_reference",
                "semantic_review_run_id", "registered_sources_examined", "read_receipts", "coverage_gaps"}
    if not isinstance(evidence, dict) or not required <= evidence.keys():
        raise ValueError("Weekly evidence contract incomplete")
    if evidence["source_snapshot_sha"] != source_sha or evidence["synthesis_status"] not in ("complete", "partial"):
        raise ValueError("Weekly evidence source/status mismatch")
    if not isinstance(evidence["synthesis_reference"], str) or not evidence["synthesis_reference"].strip():
        raise ValueError("Synthesis reference required")
    gaps = evidence["coverage_gaps"]
    if not isinstance(gaps, list) or any(not isinstance(g, str) or not g.strip() for g in gaps):
        raise ValueError("Invalid source gaps")
    examined = evidence["registered_sources_examined"]
    if not isinstance(examined, list) or len(examined) != len(set(examined)):
        raise ValueError("Invalid registered-source coverage")
    registered = set(discovery["summary"]["registered_existing"])
    if not set(examined) <= registered:
        raise ValueError("Unknown registered source")
    receipts = evidence["read_receipts"]
    if not isinstance(receipts, list):
        raise ValueError("Read receipts required")
    seen = set()
    for item in receipts:
        relative = Path(item["path"])
        path = (root / relative).resolve()
        if (relative.is_absolute() or root.resolve() not in path.parents or not path.is_file() or
                relative.as_posix() in seen or len(item["sha256"]) != 64):
            raise ValueError("Invalid read receipt")
        seen.add(relative.as_posix())
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Read receipt source changed")
    selected = {item["path"] for item in audit["candidates"]}
    complete = evidence["synthesis_status"] == "complete"
    if complete and (gaps or set(examined) != registered or not selected <= seen or
                     not isinstance(evidence["semantic_review_run_id"], str) or
                     not evidence["semantic_review_run_id"].strip()):
        raise ValueError("Complete synthesis lacks full evidence")
    if not complete and not gaps:
        raise ValueError("Partial synthesis requires explicit gaps")
    for key in ("wiki_candidates", "skill_candidates", "confirmed_facts"):
        if key not in pages or not isinstance(pages[key], list):
            raise ValueError("Candidate/confirmed lists must be explicit")
    if "#待确认" in pages["tabs"]["个人状态"] and not any(
        isinstance(item, dict) and item.get("kind") == "personal_memory" and
        item.get("status") == "pending_confirmation" and item.get("source_refs")
        for item in pages["wiki_candidates"]
    ):
        raise ValueError("Personal status has untracked confirmation candidate")
    return {"status": evidence["synthesis_status"], "gaps": gaps,
            "synthesis_reference": evidence["synthesis_reference"],
            "semantic_review_run_id": evidence["semantic_review_run_id"],
            "registered_examined": len(examined), "registered_total": len(registered),
            "read_count": len(seen), "selected_count": len(selected)}


def update_system4(system4: dict, period: str, completeness: dict, acceptance=None) -> dict:
    """Preserve daily and other weeks, and recompute real consecutive results."""
    weekly = system4["weekly"]
    missing = list(completeness["gaps"])
    if acceptance is None:
        missing.extend(("remote_readback_pending", "mobile_readback_pending", "natural_trigger_pending"))
    else:
        required = {"remote_commit", "remote_checked_at", "mobile_result_url", "mobile_checked_at", "natural_run_id"}
        if not required <= acceptance.keys() or any(not isinstance(acceptance[k], str) or not acceptance[k].strip() for k in required):
            missing.append("production_acceptance_incomplete")
    status = "success" if completeness["status"] == "complete" and not missing else "partial"
    weekly.setdefault("weeks", {})[period] = {"status": status, "missing": missing}
    weekly["through"] = max(weekly.get("through", period), period)
    start_year, start_week = map(int, weekly["start"].split("-W"))
    cursor = date.fromisocalendar(start_year, start_week, 1)
    through_year, through_week = map(int, weekly["through"].split("-W"))
    through = date.fromisocalendar(through_year, through_week, 1)
    current = 0
    maximum = int(weekly.get("max_streak", 0))
    maximum_end = weekly.get("max_streak_end")
    while cursor <= through:
        year, week, _ = cursor.isocalendar()
        label = f"{year}-W{week:02d}"
        current = current + 1 if weekly["weeks"].get(label, {}).get("status") == "success" else 0
        if current > maximum:
            maximum, maximum_end = current, label
        cursor += timedelta(days=7)
    weekly["current_streak"] = current
    weekly["max_streak"] = maximum
    weekly["max_streak_end"] = maximum_end
    weekly["ready"] = maximum >= int(weekly["required"])
    system4["ready_for_4_0"] = bool(system4["daily"].get("ready") and weekly["ready"])
    return system4


def render_receipt(period: str, run_date: str, run_key: str, source_sha: str,
                   audit_count: int, written_tabs: list[str], completeness: dict) -> str:
    return (f"---\ndate: {run_date}\ntype: chat\nsource: codex\ntopic: 艾迪宇宙每周迭代 {period}\n---\n"
            f"# 艾迪宇宙每周迭代 {period} — 云迁移运行回执\n\n"
            f"- run_key: `{run_key}`\n- 固定来源提交: `{source_sha}`\n"
            f"- 本次元数据候选: {audit_count}；实际全文读取回执: {completeness['read_count']}；"
            f"注册来源核看: {completeness['registered_examined']}/{completeness['registered_total']}。\n"
            f"- 综合来源: {completeness['synthesis_reference']}；语义综合运行: `{completeness['semantic_review_run_id'] or '未证实'}`。\n"
            f"- 来源覆盖: {completeness['status']}；缺口: {'、'.join(completeness['gaps']) if completeness['gaps'] else '无已知缺口'}。\n"
            "- 周档: " + "、".join(f"`{p}`" for p in written_tabs) + "\n"
            "- Wiki 写入: 0；个人事实与规则候选未自动升格。\n"
            "- Mac 工作区、磁盘与本地同步: 云端未检查；保留为独立本机维护。\n"
            "- 生产状态: 本次为隔离工作树生成；远端及手机读回须在发布后单独核验。\n\n"
            "## 本轮实际读取规则\n- `AGENTS.md`\n- `Wiki/索引.md`\n- `Wiki/06 Systems/01 Routing/规则入口索引.md`\n"
            "- `Wiki/06 Systems/00 Architecture/迭代系统.md`\n- `Schema/执行规范.md`\n")


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
        if name == "个人状态" and "#待确认" in text:
            closeout = json.loads((root / "Raw/00 Meta/iteration-weekly-closeout-latest.json").read_text(encoding="utf-8"))
            if not any(isinstance(item, dict) and item.get("kind") == "personal_memory" and
                       item.get("status") == "pending_confirmation"
                       for item in closeout.get("candidate_wiki_updates", [])):
                raise ValueError("Personal confirmation candidate missing from closeout")
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
    ensure_new_run_key(previous, run_key)
    previous_discovery_path = discovery_module.DEFAULT_OUTPUT
    previous_discovery = json.loads(previous_discovery_path.read_text(encoding="utf-8")) if previous_discovery_path.exists() else None
    discovery = discovery_module.discover(previous_discovery)
    if discovery["summary"]["registered_missing"] or discovery["summary"]["unregistered_existing"]:
        raise ValueError("Source registry incomplete")
    audit = audit_module.audit(target, 7, 100000)
    if audit["counts"]["total_candidates"] != len(audit["candidates"]):
        raise ValueError("Audit candidate count mismatch")
    completeness = validate_evidence(root, source_sha, pages, discovery, audit)
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
    receipt = render_receipt(period, run_date, run_key, source_sha,
                             audit["counts"]["total_candidates"], written_tabs, completeness)
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
        "actual_wiki_updates": [], "candidate_wiki_updates": pages["wiki_candidates"],
        "candidate_skill_promotions": pages["skill_candidates"],
        "confirmed_facts": pages["confirmed_facts"],
        "source_completeness": completeness["status"], "source_gaps": completeness["gaps"],
        "read_coverage": {"registered_examined": completeness["registered_examined"],
                          "registered_total": completeness["registered_total"],
                          "full_text_receipts": completeness["read_count"],
                          "selected_candidates": completeness["selected_count"]},
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
    update_system4(system4, period, completeness)
    write_json(root, "Raw/00 Meta/system4-acceptance.json", system4)

    result = verify_existing(root, period, run_date, run_key)
    result.update({"source_sha": source_sha, "audit_candidates": audit["counts"]["total_candidates"]})
    return result


def record_acceptance(root: Path, source_sha: str, pages: dict, run_date: str, acceptance: dict) -> dict:
    """Record separately verified publication, mobile result and natural run."""
    period = pages["period"]
    run_key = hashlib.sha256(f"weekly:{period}:{source_sha}:cloud".encode()).hexdigest()
    required = {"remote_commit", "remote_checked_at", "mobile_result_url", "mobile_checked_at", "natural_run_id",
                "source_sha", "run_key"}
    if (not isinstance(acceptance, dict) or not required <= acceptance.keys() or
            any(not isinstance(acceptance[key], str) or not acceptance[key].strip() for key in required) or
            acceptance["source_sha"] != source_sha or acceptance["run_key"] != run_key or
            acceptance["remote_commit"] != git(root, "rev-parse", "HEAD") or
            git(root, "status", "--porcelain", "--untracked-files=all")):
        raise ValueError("Production acceptance evidence incomplete or checkout not clean")
    latest_path = "Raw/00 Meta/iteration-weekly-latest.json"
    closeout_path = "Raw/00 Meta/iteration-weekly-closeout-latest.json"
    latest = json.loads((root / latest_path).read_text(encoding="utf-8"))
    closeout = json.loads((root / closeout_path).read_text(encoding="utf-8"))
    if latest.get("run_key") != run_key or closeout.get("run_key") != run_key:
        raise ValueError("Published run key mismatch")
    completeness = {"status": closeout["source_completeness"], "gaps": closeout["source_gaps"]}
    system4_path = "Raw/00 Meta/system4-acceptance.json"
    system4 = json.loads((root / system4_path).read_text(encoding="utf-8"))
    update_system4(system4, period, completeness, acceptance)
    system4["generated_at"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    write_json(root, system4_path, system4)
    latest["production_publish"] = "remote_and_mobile_verified"
    latest["production_acceptance"] = acceptance
    closeout["production_acceptance"] = acceptance
    write_json(root, latest_path, latest)
    write_json(root, closeout_path, closeout)
    receipt_path = f"Raw/05 Chat/{run_date}-艾迪宇宙每周迭代-{period}.md"
    receipt = (root / receipt_path).read_text(encoding="utf-8")
    if acceptance["natural_run_id"] not in receipt:
        receipt += ("\n## 自然云运行验收\n"
                    f"- 自然运行：`{acceptance['natural_run_id']}`；私库回读：`{acceptance['remote_commit']}`。\n"
                    f"- 手机结果：{acceptance['mobile_result_url']}；回读时间：`{acceptance['mobile_checked_at']}`。\n"
                    f"- System 4.0 周状态：{system4['weekly']['weeks'][period]['status']}；"
                    "来源缺口若仍存在则继续 partial。\n")
        write_text(root, receipt_path, receipt)
    changed = changed_paths(root)
    if not changed <= {latest_path, closeout_path, system4_path, receipt_path}:
        raise ValueError("Acceptance changed unauthorized paths")
    return {"run_key": run_key, "accepted_paths": len(changed),
            "weekly_status": system4["weekly"]["weeks"][period]["status"],
            "weekly_streak": system4["weekly"]["current_streak"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--pages-json", required=True, type=Path)
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--verify-existing", action="store_true", help="Check an already generated isolated bundle without writing")
    parser.add_argument("--acceptance-json", type=Path, help="Post-publication evidence from verified remote, phone and natural run")
    args = parser.parse_args()
    root = args.vault.resolve()
    if root in args.pages_json.resolve().parents:
        raise ValueError("Private page input must be outside the checkout")
    date.fromisoformat(args.run_date)
    pages = json.loads(args.pages_json.read_text(encoding="utf-8"))
    if args.verify_existing and args.acceptance_json:
        raise ValueError("Choose verification or acceptance recording")
    if args.acceptance_json:
        acceptance = json.loads(args.acceptance_json.read_text(encoding="utf-8"))
        result = record_acceptance(root, args.source_sha, pages, args.run_date, acceptance)
    elif args.verify_existing:
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
