#!/usr/bin/env python3
"""Sync WeChat Read (微信读书) records from Notion into Raw/15 微信读书."""

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
OUTPUT_ROOT = ROOT / "Raw" / "15 微信读书"
TZ = dt.timezone(dt.timedelta(hours=8))

# 书架数据库 ID (微信读书的主数据库)
SHELF_DATABASE_ID = "1f933b33-7f23-810a-8556-f8874a5fcd59"

# 阅读记录数据库 ID
RECORD_DATABASE_ID = "1f933b33-7f23-8172-8373-c93875a66dcd"


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


def page_files(page: Dict[str, Any], property_name: str) -> List[str]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    if not isinstance(prop, dict) or prop.get("type") != "files":
        return []
    return [f.get("file", {}).get("url", "") for f in prop.get("files") or [] if f]


def page_relation_ids(page: Dict[str, Any], property_name: str) -> List[str]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    if not isinstance(prop, dict) or prop.get("type") != "relation":
        return []
    return [r["id"] for r in prop.get("relation") or [] if isinstance(r, dict)]


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    total_minutes = int(seconds // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def book_path(title: str, start_date: Optional[dt.datetime]) -> Path:
    dt_ref = start_date or now()
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
        safe_title = "未命名书籍"
    return OUTPUT_ROOT / year / year_month / f"{safe_title}.md"


def render_book_note(
    page: Dict[str, Any],
    database: Dict[str, Any],
) -> tuple[str, Path]:
    title = page_title(page)
    author_list = page_relation_ids(page, "作者")
    author = page_text(page, "作者")  # fallback to text if relation not resolved
    rating = page_number(page, "评分")
    my_rating = page_select(page, "我的评分")
    status = page_select(page, "阅读状态")
    progress = page_number(page, "阅读进度")
    reading_duration = page_number(page, "阅读时长")
    reading_days = page_number(page, "阅读天数")
    start_date = page_date(page, "开始阅读时间")
    last_read = page_date(page, "最后阅读时间")
    isbn = page_text(page, "ISBN")
    book_id = page_text(page, "BookId")
    intro = page_text(page, "简介")
    douban_url = page_text(page, "豆瓣链接")
    notion_url = (page.get("url") or "").strip()
    page_id = normalize_notion_id(str(page.get("id") or ""))
    covers = page_files(page, "封面")

    frontmatter = render_frontmatter(
        {
            "source_system": "notion",
            "source_type": "wechat_read",
            "database_name": title_text_from_database(database),
            "database_id": normalize_notion_id(str(database.get("id") or "")),
            "title": title,
            "author": author if author else None,
            "rating": rating,
            "my_rating": my_rating if my_rating else None,
            "status": status if status else None,
            "progress": progress,
            "reading_duration": int(reading_duration) if reading_duration else None,
            "reading_days": int(reading_days) if reading_days else None,
            "isbn": isbn if isbn else None,
            "book_id": book_id if book_id else None,
            "start_date": start_date.strftime("%Y-%m-%d") if start_date else None,
            "last_read": last_read.strftime("%Y-%m-%d") if last_read else None,
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
    if author:
        lines.append(f"- 作者：{author}")
    if rating is not None:
        lines.append(f"- 评分：{rating}")
    if my_rating:
        lines.append(f"- 我的评分：{my_rating}")
    if status:
        lines.append(f"- 阅读状态：{status}")
    if progress is not None:
        lines.append(f"- 阅读进度：{progress:.0%}")
    if reading_duration is not None:
        lines.append(f"- 阅读时长：{format_duration(reading_duration)}")
    if reading_days is not None:
        lines.append(f"- 阅读天数：{int(reading_days)}天")
    if start_date:
        lines.append(f"- 开始阅读：{start_date.strftime('%Y-%m-%d')}")
    if last_read:
        lines.append(f"- 最后阅读：{last_read.strftime('%Y-%m-%d')}")
    if isbn:
        lines.append(f"- ISBN：{isbn}")
    if book_id:
        lines.append(f"- 微信读书 ID：{book_id}")
    if douban_url:
        lines.append(f"- 豆瓣：{douban_url}")
    if notion_url:
        lines.append(f"- Notion：{notion_url}")

    if intro:
        lines.extend(["", "## 简介", "", intro])

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n", book_path(title, start_date)


def render_index(rows: List[Dict[str, Any]]) -> str:
    generated_at = now_iso()
    lines = [
        "---",
        'source_system: "notion"',
        'source_type: "wechat_read_index"',
        f'generated_at: "{generated_at}"',
        f"book_count: {len(rows)}",
        "---",
        "",
        "# 微信读书索引",
        "",
        f"> 本地已同步 {len(rows)} 本书。",
        "",
        "| 书名 | 作者 | 评分 | 状态 | 进度 | 时长 | 文件 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda item: item.get("last_read") or "", reverse=True):
        rating_text = str(row["rating"]) if row.get("rating") is not None else "-"
        status_text = row.get("status") or "-"
        progress_text = f"{row['progress']:.0%}" if row.get("progress") is not None else "-"
        duration_text = format_duration(row.get("reading_duration")) if row.get("reading_duration") else "-"
        lines.append(
            f"| {row['title']} | {row.get('author') or '-'} | {rating_text} | {status_text} | {progress_text} | {duration_text} | [{row['path']}]({row['path']}) |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview target files without writing.")
    parser.add_argument("--max-pages", type=int, default=0, help="Limit the number of book pages to process.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = load_notion_api_key()

    # Use configured database ID or resolve by name
    database_id = os.environ.get("WEREAD_DATABASE_ID") or SHELF_DATABASE_ID
    database = resolve_database(api_key, database_id, "书架")

    database_name = title_text_from_database(database)
    print(f"Found database: {database_name}")

    rows: List[Dict[str, Any]] = []
    processed = 0
    for page in paginate_database(api_key, normalize_notion_id(str(database.get("id")))):
        if page.get("archived"):
            continue
        content, target = render_book_note(page, database)
        title = page_title(page)

        author = page_text(page, "作者")
        rating = page_number(page, "评分")
        my_rating = page_select(page, "我的评分")
        status = page_select(page, "阅读状态")
        progress = page_number(page, "阅读进度")
        reading_duration = page_number(page, "阅读时长")
        start_date = page_date(page, "开始阅读时间")
        last_read = page_date(page, "最后阅读时间")

        if args.dry_run:
            print(f"DRY-RUN {title} -> {target.relative_to(ROOT)}")
        else:
            write_text(target, content)

        rows.append(
            {
                "title": title,
                "author": author,
                "rating": rating,
                "my_rating": my_rating,
                "status": status,
                "progress": progress,
                "reading_duration": reading_duration,
                "last_read": last_read.strftime("%Y-%m-%d") if last_read else None,
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

    print(f"WROTE {len(rows)} book notes")
    print(f"WROTE {index_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
