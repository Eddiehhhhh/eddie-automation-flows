#!/usr/bin/env python3
"""Sync Notion workout records into daily Obsidian notes."""

from __future__ import annotations

import argparse
import os
import datetime as dt
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sync_notion_to_raw import (
    get_database,
    load_notion_api_key,
    normalize_notion_id,
    notion_request,
    paginate_database,
    parse_time,
    render_frontmatter,
    rich_text_plain_text,
    title_text_from_database,
    write_text,
)


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
OUTPUT_ROOT = ROOT / "Raw" / "12 我的运动"
DATABASE_ID = "26333b33-7f23-818c-a3af-e5e546d18118"
TZ = dt.timezone(dt.timedelta(hours=8))


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def format_seconds(value: Optional[float]) -> str:
    if value in (None, ""):
        return ""
    try:
        total = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: List[str] = []
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    if not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts)


def format_number(value: Optional[float], unit: str = "") -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        text = str(int(number))
    else:
        text = f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


def page_title(page: Dict[str, Any]) -> str:
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return rich_text_plain_text(prop.get("title") or []).strip()
    return page.get("id") or "unknown"


def page_date(page: Dict[str, Any], property_name: str) -> Optional[dt.datetime]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    if not isinstance(prop, dict) or prop.get("type") != "date":
        return None
    date_value = (prop.get("date") or {}).get("start")
    return parse_time(date_value)


def page_number(page: Dict[str, Any], property_name: str) -> Optional[float]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    if not isinstance(prop, dict):
        return None
    prop_type = prop.get("type")
    if prop_type == "number":
        return prop.get("number")
    if prop_type == "formula":
        formula = prop.get("formula") or {}
        if isinstance(formula, dict) and formula.get("type") == "number":
            return formula.get("number")
    return None


def meters_to_kilometers(value: Optional[float]) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def page_text(page: Dict[str, Any], property_name: str) -> str:
    prop = (page.get("properties") or {}).get(property_name) or {}
    if not isinstance(prop, dict):
        return ""
    prop_type = prop.get("type")
    if prop_type == "rich_text":
        return rich_text_plain_text(prop.get("rich_text") or []).strip()
    if prop_type == "title":
        return rich_text_plain_text(prop.get("title") or []).strip()
    if prop_type == "url":
        return (prop.get("url") or "").strip()
    if prop_type == "select":
        return (((prop.get("select") or {}).get("name")) or "").strip()
    if prop_type == "status":
        return (((prop.get("status") or {}).get("name")) or "").strip()
    return ""


def page_relation_ids(page: Dict[str, Any], property_name: str) -> List[str]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    if not isinstance(prop, dict) or prop.get("type") != "relation":
        return []
    ids = []
    for item in prop.get("relation") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        ids.append(normalize_notion_id(str(item["id"])))
    return ids


def fetch_page(api_key: str, page_id: str) -> Dict[str, Any]:
    return notion_request(api_key, "GET", f"/pages/{normalize_notion_id(page_id)}")


def fetch_relation_title(
    api_key: str,
    page_id: str,
    cache: Dict[str, str],
) -> str:
    normalized = normalize_notion_id(page_id)
    if normalized in cache:
        return cache[normalized]
    page = fetch_page(api_key, normalized)
    title = page_title(page)
    cache[normalized] = title
    return title


def fetch_relation_titles(
    api_key: str,
    page_ids: Sequence[str],
    cache: Dict[str, str],
) -> List[str]:
    titles: List[str] = []
    for page_id in page_ids:
        title = fetch_relation_title(api_key, page_id, cache)
        if title:
            titles.append(title)
    return titles


def daily_path(day: dt.date) -> Path:
    return OUTPUT_ROOT / f"{day:%Y}" / f"{day:%Y-%m}" / f"{day:%Y-%m-%d}" / "运动日记.md"


def render_session_entry(
    api_key: str,
    page: Dict[str, Any],
    relation_cache: Dict[str, str],
) -> Tuple[List[str], Dict[str, Any]]:
    title = page_title(page)
    start_at = page_date(page, "开始时间") or parse_time(page.get("created_time")) or now()
    end_at = page_date(page, "结束时间") or start_at
    duration_seconds = page_number(page, "运动时长") or 0
    calories = page_number(page, "消耗热量") or 0
    distance_meters = page_number(page, "距离") or 0
    distance = meters_to_kilometers(distance_meters) or 0
    avg_hr = page_number(page, "平均心率")
    max_hr = page_number(page, "最大心率")
    workout_type_ids = page_relation_ids(page, "运动类型")
    diary_ids = page_relation_ids(page, "📅 日记中心")
    workout_types = fetch_relation_titles(api_key, workout_type_ids, relation_cache)
    page_url = (page.get("url") or "").strip()

    lines = [f"### {start_at.strftime('%H:%M')} {title}"]
    lines.append(f"- 开始：{start_at.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"- 结束：{end_at.strftime('%Y-%m-%d %H:%M')}")
    duration_text = format_seconds(duration_seconds)
    if duration_text:
        lines.append(f"- 时长：{duration_text}")
    if calories:
        lines.append(f"- 消耗热量：{format_number(calories, ' 千卡')}")
    if distance:
        lines.append(f"- 距离：{format_number(distance, ' 公里')}")
    if avg_hr:
        lines.append(f"- 平均心率：{format_number(avg_hr)}")
    if max_hr:
        lines.append(f"- 最大心率：{format_number(max_hr)}")
    if workout_types:
        lines.append(f"- 运动类型：{'、'.join(workout_types)}")
    if diary_ids:
        lines.append(f"- 日记中心：已关联 {len(diary_ids)} 页")
    if page_url:
        lines.append(f"- Notion：{page_url}")

    summary = {
        "title": title,
        "started_at": start_at,
        "ended_at": end_at,
        "duration_seconds": int(round(float(duration_seconds or 0))),
        "calories": float(calories or 0),
        "distance": float(distance or 0),
        "distance_meters": float(distance_meters or 0),
        "page_id": normalize_notion_id(str(page.get("id") or "")),
        "page_url": page_url,
    }
    return lines, summary


def render_day_note(day: dt.date, session_lines: List[List[str]], sessions: List[Dict[str, Any]], database: Dict[str, Any]) -> str:
    session_count = len(sessions)
    total_duration_seconds = sum(int(item.get("duration_seconds") or 0) for item in sessions)
    total_calories = sum(float(item.get("calories") or 0) for item in sessions)
    total_distance = sum(float(item.get("distance") or 0) for item in sessions)
    total_distance_meters = sum(float(item.get("distance_meters") or 0) for item in sessions)
    session_page_ids = [item["page_id"] for item in sessions if item.get("page_id")]

    frontmatter = render_frontmatter(
        {
            "source_system": "notion",
            "source_type": "workout_daily",
            "database_name": title_text_from_database(database),
            "database_id": normalize_notion_id(str(database.get("id") or DATABASE_ID)),
            "title": "运动日记",
            "date": day.isoformat(),
            "session_count": session_count,
            "total_duration_seconds": total_duration_seconds,
            "total_calories": round(total_calories, 2),
            "total_distance": round(total_distance, 2),
            "total_distance_meters": round(total_distance_meters, 2),
            "session_page_ids": session_page_ids,
            "synced_at": now_iso(),
        }
    )

    lines = [
        frontmatter,
        "",
        "# 运动日记",
        "",
        "## 概览",
        "",
        f"- 日期：{day.isoformat()}",
        f"- 训练次数：{session_count}",
        f"- 总时长：{format_seconds(total_duration_seconds)}",
        f"- 总热量：{format_number(total_calories, ' 千卡')}",
        f"- 总距离：{format_number(total_distance, ' 公里')}",
        "",
        "## 明细",
        "",
    ]
    for entry_lines in session_lines:
        lines.extend(entry_lines)
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def render_index(rows: List[Dict[str, Any]]) -> str:
    generated_at = now_iso()
    total_sessions = sum(int(row["session_count"]) for row in rows)
    lines = [
        "---",
        'source_system: "notion"',
        'source_type: "workout_daily_index"',
        f'generated_at: "{generated_at}"',
        f"day_count: {len(rows)}",
        f"session_count: {total_sessions}",
        "---",
        "",
        "# 我的运动索引",
        "",
        f"> 本地已同步 {len(rows)} 天、{total_sessions} 次运动。",
        "",
        "| 日期 | 训练次数 | 总时长 | 总热量 | 总距离 | 文件 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda item: item["day"], reverse=True):
        lines.append(
            f"| {row['day']} | {row['session_count']} | {row['duration_text']} | {row['calories_text']} | {row['distance_text']} | [{row['path']}]({row['path']}) |"
        )
    lines.append("")
    return "\n".join(lines)


def prune_stale_notes(expected_paths: Iterable[Path]) -> List[Path]:
    expected = {path.resolve() for path in expected_paths}
    removed: List[Path] = []
    for path in OUTPUT_ROOT.rglob("运动日记.md"):
        if path.resolve() in expected:
            continue
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview target files without writing.")
    parser.add_argument("--max-pages", type=int, default=0, help="Limit the number of workout pages to process.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = load_notion_api_key()
    database = get_database(api_key, DATABASE_ID)
    relation_cache: Dict[str, str] = {}

    grouped: Dict[str, List[Tuple[List[str], Dict[str, Any]]]] = {}
    processed = 0
    for page in paginate_database(
        api_key,
        DATABASE_ID,
        sorts=[{"property": "开始时间", "direction": "ascending"}],
    ):
        if page.get("archived"):
            continue
        session_lines, summary = render_session_entry(api_key, page, relation_cache)
        day = summary["started_at"].date()
        grouped.setdefault(day.isoformat(), []).append((session_lines, summary))
        processed += 1
        if args.max_pages and processed >= args.max_pages:
            break

    expected_paths: List[Path] = []
    index_rows: List[Dict[str, Any]] = []
    for day_key in sorted(grouped):
        day = dt.date.fromisoformat(day_key)
        rows = grouped[day_key]
        sessions = [summary for _, summary in rows]
        session_lines = [entry_lines for entry_lines, _ in rows]
        target = daily_path(day)
        content = render_day_note(day, session_lines, sessions, database)
        expected_paths.append(target)
        if args.dry_run:
            print(f"DRY-RUN {day.isoformat()} -> {target.relative_to(ROOT)}")
        else:
            write_text(target, content)
        index_rows.append(
            {
                "day": day.isoformat(),
                "session_count": len(sessions),
                "duration_text": format_seconds(sum(int(item.get("duration_seconds") or 0) for item in sessions)),
                "calories_text": format_number(sum(float(item.get("calories") or 0) for item in sessions), " 千卡"),
                "distance_text": format_number(sum(float(item.get("distance") or 0) for item in sessions), " 公里"),
                "path": str(target.relative_to(OUTPUT_ROOT)),
            }
        )

    removed = [] if args.dry_run else prune_stale_notes(expected_paths)
    index_path = OUTPUT_ROOT / "索引.md"
    if args.dry_run:
        print(f"DRY-RUN {index_path.relative_to(ROOT)}")
    else:
        write_text(index_path, render_index(index_rows))

    print(f"WROTE {len(expected_paths)} workout day notes")
    print(f"REMOVED {len(removed)} stale workout day notes")
    print(f"WROTE {index_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
