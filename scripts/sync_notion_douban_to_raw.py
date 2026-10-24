#!/usr/bin/env python3
"""Sync douban movie records from Notion into Raw/14 豆瓣影视."""

from __future__ import annotations

import argparse
import os
import re
import time
import warnings
import datetime as dt
from urllib.parse import quote, unquote, urlencode
from pathlib import Path
from typing import Any, Dict, List, Optional

from sync_notion_to_raw import (
    load_notion_api_key,
    normalize_notion_id,
    notion_request,
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


def page_rating(page: Dict[str, Any]) -> Optional[float | str]:
    """Preserve numeric ratings and select display text without numeric normalization."""
    prop = (page.get("properties") or {}).get("评分")
    if prop is None:
        return None
    if not isinstance(prop, dict):
        raise ValueError("Unsupported rating property schema")
    if prop.get("type") in ("number", "formula"):
        return page_number(page, "评分")
    if prop.get("type") != "select":
        raise ValueError("Unsupported rating property schema")
    selected = prop.get("select")
    if selected is None:
        return None
    if not isinstance(selected, dict) or not isinstance(selected.get("name"), str):
        raise ValueError("Unsupported rating select schema")
    label = selected["name"]
    if not label.strip():
        return None
    # Explicit display vocabulary. Never infer a score from arbitrary text.
    if label not in {star * count for star in ("⭐️", "⭐") for count in range(1, 6)}:
        warnings.warn("Unrecognized rating select label preserved without numeric normalization", UserWarning)
    return label


class RelationTitleReader:
    """Read only relation IDs and title property items; cache titles for this run."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.titles: Dict[str, str] = {}

    @staticmethod
    def page_id(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", value
        ):
            raise ValueError("Invalid relation page identifier")
        return normalize_notion_id(value).lower()

    def items(self, page_id: str, property_id: str, kind: str) -> List[Dict[str, Any]]:
        if not isinstance(property_id, str) or not property_id:
            raise ValueError("Missing relation property identifier")
        # Notion returns already URL-encoded property IDs. Do not double-encode.
        path = f"/pages/{self.page_id(page_id)}/properties/{quote(unquote(property_id), safe='')}"
        result: List[Dict[str, Any]] = []
        cursor = None
        seen = set()
        while True:
            params = {"page_size": "100"}
            if cursor:
                params["start_cursor"] = cursor
            time.sleep(0.35)  # Keep additional reads below Notion's average 3 req/s.
            try:
                response = notion_request(self.api_key, "GET", path + "?" + urlencode(params))
            except Exception:
                # Do not print API exception payloads, private IDs, or person data.
                raise RuntimeError("Director relation/title read failed; sync incomplete. Check existing Notion read access.") from None
            if (not isinstance(response, dict) or response.get("object") != "list"
                    or not isinstance(response.get("results"), list)
                    or type(response.get("has_more")) is not bool):
                raise ValueError("Unsupported relation/title pagination schema")
            for item in response["results"]:
                if (not isinstance(item, dict) or item.get("type") != kind
                        or not isinstance(item.get(kind), dict)):
                    raise ValueError("Unsupported relation/title item schema")
                result.append(item[kind])
            if not response["has_more"]:
                return result
            cursor = response.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("Incomplete relation/title pagination")
            seen.add(cursor)

    def title(self, page_id: str) -> str:
        page_id = self.page_id(page_id)
        if page_id not in self.titles:
            parts = self.items(page_id, "title", "title")
            if any(not isinstance(part.get("plain_text"), str) for part in parts):
                raise ValueError("Unsupported director title schema")
            title = rich_text_plain_text(parts).strip()
            if not title:
                raise ValueError("Director title missing; sync incomplete")
            self.titles[page_id] = title
        return self.titles[page_id]

    def relation(self, page: Dict[str, Any], prop: Dict[str, Any]) -> str:
        if not isinstance(prop.get("relation"), list) or type(prop.get("has_more")) is not bool:
            raise ValueError("Unsupported director relation schema")
        values = prop["relation"]
        if prop["has_more"]:
            values = self.items(page.get("id"), prop.get("id"), "relation")
            preview = prop["relation"]
            if (len(values) <= len(preview)
                    or [item.get("id") for item in values[:len(preview)]]
                    != [item.get("id") if isinstance(item, dict) else None for item in preview]):
                raise ValueError("Director relation changed or truncated during pagination")
        titles = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("Unsupported director relation item")
            titles.append(self.title(value.get("id")))
        return "、".join(titles)


def page_director(page: Dict[str, Any], reader: Optional[RelationTitleReader] = None) -> str:
    for name in ("导演", "作者"):
        prop = (page.get("properties") or {}).get(name)
        if prop is None:
            continue
        if not isinstance(prop, dict):
            raise ValueError("Unsupported director property schema")
        if prop.get("type") == "relation":
            if reader is None:
                raise ValueError("Director relation requires an authorized title reader")
            value = reader.relation(page, prop)
        elif prop.get("type") in ("rich_text", "title", "url"):
            value = page_text(page, name)
        else:
            raise ValueError("Unsupported director property schema")
        if value:
            return value
    return ""


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


def render_movie_frontmatter(fields: Dict[str, Any]) -> str:
    """Keep the shared renderer unchanged; make new string values YAML-safe.

    JSON's ensure_ascii=False leaves DEL/C1 and Unicode line separators literal.
    YAML rejects some of these and normalizes others, so escape only the two
    mapping fields here. Ordinary legacy text/numbers and emoji stay unchanged.
    """
    lines = render_frontmatter(fields).split("\n")
    for index, line in enumerate(lines):
        if line.startswith(("director: ", "rating: ")):
            lines[index] = re.sub(
                r"[\u007f-\u009f\u2028\u2029]",
                lambda match: "\\u" + format(ord(match.group()), "04x"),
                line,
            )
    return "\n".join(lines)


def render_movie_note(
    page: Dict[str, Any],
    database: Dict[str, Any],
    director: Optional[str] = None,
) -> tuple[str, Path]:
    title = page_title(page)
    director = page_director(page) if director is None else director
    rating = page_rating(page)
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

    frontmatter = render_movie_frontmatter(
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


def index_cell(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


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
            f"| {index_cell(row['title'])} | {index_cell(row.get('director') or '-')} | {index_cell(rating_text)} | {index_cell(status_text)} | {index_cell(date_text)} | [{row['path']}]({row['path']}) |"
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
    title_reader = RelationTitleReader(api_key)
    processed = 0
    for page in paginate_database(api_key, normalize_notion_id(str(database.get("id")))):
        if page.get("archived"):
            continue
        director = page_director(page, title_reader)
        content, target = render_movie_note(page, database, director=director)
        title = page_title(page)

        rating = page_rating(page)
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
