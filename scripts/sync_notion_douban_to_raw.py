#!/usr/bin/env python3
"""Sync douban movie records from Notion into Raw/14 豆瓣影视."""

from __future__ import annotations

import argparse
import os
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional

from sync_notion_to_raw import (
    load_notion_api_key,
    normalize_notion_id,
    paginate_database,
    parse_time,
    render_frontmatter,
    resolve_database,
    rich_text_plain_text,
    title_text_from_database,
    write_text,
)


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
OUTPUT_ROOT = ROOT / "Raw" / "14 豆瓣影视"
TZ = dt.timezone(dt.timedelta(hours=8))


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


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


def page_select(page: Dict[str, Any], property_name: str) -> str:
    prop = (page.get("properties") or {}).get(property_name) or {}
    if not isinstance(prop, dict):
        return ""
    prop_type = prop.get("type")
    if prop_type == "select":
        return ((prop.get("select") or {}).get("name") or "").strip()
    if prop_type == "status":
        return ((prop.get("status") or {}).get("name") or "").strip()
    return ""


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


def page_multi_select(page: Dict[str, Any], property_name: str) -> List[str]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    if not isinstance(prop, dict) or prop.get("type") != "multi_select":
        return []
    return [
        item.get("name")
        for item in prop.get("multi_select") or []
        if isinstance(item, dict) and item.get("name")
    ]


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
    return ""


def movie_path(title: str, added_date: Optional[dt.datetime]) -> Path:
    dt_ref = added_date or now()
    year = dt_ref.strftime("%Y")
    year_month = dt_ref.strftime("%Y-%m")
    safe_title = title.replace("/", "／").replace("\\", "＼").replace(":", "：")
    safe_title = safe_title.replace("*", "＊").replace("?", "？")
    safe_title = safe_title.replace('"', "＂").replace("<", "＜").replace(">", "＞")
    safe_title = safe_title.replace("|", "｜")
    if len(safe_title.encode("utf-8")) > 120:
        trimmed = safe_title.encode("utf-8")[:120]
        while trimmed:
            try:
                safe_title = trimmed.decode("utf-8").rstrip("-. ")
                break
            except UnicodeDecodeError:
                trimmed = trimmed[:-1]
    if not safe_title:
        safe_title = "未命名影视"
    return OUTPUT_ROOT / year / year_month / f"{safe_title}.md"


def render_movie_note(
    page: Dict[str, Any],
    database: Dict[str, Any],
) -> tuple[str, Path]:
    title = page_title(page)
    director = page_text(page, "导演") or page_text(page, "作者")
    rating = page_number(page, "评分")
    status = page_select(page, "状态") or page_select(page, "观看状态") or page_select(page, "阅读状态")
    tags = page_multi_select(page, "标签") or page_multi_select(page, "Tags")
    added_date = page_date(page, "添加日期") or page_date(page, "日期")
    if added_date is None:
        created = parse_time(page.get("created_time"))
        if created:
            added_date = created
    notes = page_text(page, "短评") or page_text(page, "笔记") or page_text(page, "备注") or page_text(page, "影评")
    notion_url = (page.get("url") or "").strip()
    page_id = normalize_notion_id(str(page.get("id") or ""))

    frontmatter = render_frontmatter(
        {
            "source_system": "notion",
            "source_type": "douban_movie",
            "database_name": title_text_from_database(database),
            "database_id": normalize_notion_id(str(database.get("id") or "")),
            "title": title,
            "director": director,
            "rating": rating,
            "status": status,
            "tags": tags if tags else None,
            "date": added_date.strftime("%Y-%m-%d") if added_date else None,
            "page_id": page_id,
            "notion_url": notion_url,
            "synced_at": now_iso(),
        }
    )

    lines = [
        frontmatter,
        "",
        f"# {title}",
        "",
        "## 基本信息",
        "",
    ]
    if director:
        lines.append(f"- 导演：{director}")
    if rating is not None:
        lines.append(f"- 评分：{rating}")
    if status:
        lines.append(f"- 状态：{status}")
    if tags:
        lines.append(f"- 标签：{'、'.join(tags)}")
    if added_date:
        lines.append(f"- 添加日期：{added_date.strftime('%Y-%m-%d')}")
    if notion_url:
        lines.append(f"- Notion：{notion_url}")

    if notes:
        lines.extend(["", "## 笔记", "", notes])

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n", movie_path(title, added_date)


def render_index(rows: List[Dict[str, Any]]) -> str:
    generated_at = now_iso()
    lines = [
        "---",
        'source_system: "notion"',
        'source_type: "douban_movie_index"',
        f'generated_at: "{generated_at}"',
        f"movie_count: {len(rows)}",
        "---",
        "",
        "# 豆瓣影视索引",
        "",
        f"> 本地已同步 {len(rows)} 部影视。",
        "",
        "| 片名 | 导演 | 评分 | 状态 | 日期 | 文件 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda item: item.get("date") or "", reverse=True):
        rating_text = str(row["rating"]) if row.get("rating") is not None else "-"
        status_text = row.get("status") or "-"
        date_text = row.get("date") or "-"
        lines.append(
            f"| {row['title']} | {row.get('director') or '-'} | {rating_text} | {status_text} | {date_text} | [{row['path']}]({row['path']}) |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview target files without writing.")
    parser.add_argument("--max-pages", type=int, default=0, help="Limit the number of movie pages to process.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = load_notion_api_key()

    database_id = os.environ.get("DOUBAN_DATABASE_ID")
    if not database_id:
        for query in ["影视", "movie", "豆瓣影视", "影视记录"]:
            try:
                database = resolve_database(api_key, None, query)
                break
            except SystemExit:
                continue
        else:
            raise SystemExit(
                "Could not find douban movie database. "
                "Set DOUBAN_DATABASE_ID environment variable or ensure a Notion database "
                "titled '影视', 'movie', '豆瓣影视', or '影视记录' exists."
            )
    else:
        database = resolve_database(api_key, database_id, "豆瓣影视")

    database_name = title_text_from_database(database)
    print(f"Found database: {database_name}")

    rows: List[Dict[str, Any]] = []
    processed = 0
    for page in paginate_database(api_key, normalize_notion_id(str(database.get("id")))):
        if page.get("archived"):
            continue
        content, target = render_movie_note(page, database)
        title = page_title(page)

        director = page_text(page, "导演") or page_text(page, "作者")
        rating = page_number(page, "评分")
        status = page_select(page, "状态") or page_select(page, "观看状态") or page_select(page, "阅读状态")
        added_date = page_date(page, "添加日期") or page_date(page, "日期")
        if added_date is None:
            created = parse_time(page.get("created_time"))
            if created:
                added_date = created

        if args.dry_run:
            print(f"DRY-RUN {title} -> {target.relative_to(ROOT)}")
        else:
            write_text(target, content)

        rows.append(
            {
                "title": title,
                "director": director,
                "rating": rating,
                "status": status,
                "date": added_date.strftime("%Y-%m-%d") if added_date else None,
                "path": str(target.relative_to(OUTPUT_ROOT)),
            }
        )
        processed += 1
        if args.max_pages and processed >= args.max_pages:
            break

    index_path = OUTPUT_ROOT / "索引.md"
    if args.dry_run:
        print(f"DRY-RUN {index_path.relative_to(ROOT)}")
    else:
        write_text(index_path, render_index(rows))

    print(f"WROTE {len(rows)} movie notes")
    print(f"WROTE {index_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
