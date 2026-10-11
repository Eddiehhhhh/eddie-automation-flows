#!/usr/bin/env python3
"""Sync WeChat Read (微信读书) records from Notion into Raw/15 微信读书."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
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


def notion_page_id(value: str) -> str:
    """Only UUID identities can authorize overwriting a source note."""
    normalized = normalize_notion_id(value)
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", normalized):
        raise ValueError("Missing or invalid Notion page identity")
    return normalized


def stored_page_id(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return ""
    parts = text.split("---", 2)
    if len(parts) != 3:
        return ""
    header = parts[1]
    values = {}
    for line in header.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key] = value.strip().strip("\"'")
    if values.get("source_system") != "notion" or values.get("source_type") != "wechat_read":
        return ""
    identities = {notion_page_id(values[key]) for key in ("page_id", "source_id") if values.get(key)}
    if len(identities) > 1:
        raise ValueError("Conflicting source identities")
    return next(iter(identities), "")


def atomic_write(path: Path, content: str) -> None:
    """A failed write must leave the previous complete file available for retry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".weread-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class BookPaths:
    """Stable page identity index; legacy duplicates are preserved, never merged."""

    def __init__(self, root: Path):
        self.root = root
        self.state_path = root / ".notion-page-paths.json"
        self.existing: Dict[str, List[Path]] = {}
        self.paths: Dict[str, Path] = {}
        self.owners: Dict[Path, str] = {}
        for path in sorted(root.rglob("*.md")):
            self.checked(path)
            identity = stored_page_id(path)
            self.owners[path] = identity
            if identity:
                self.existing.setdefault(identity, []).append(path)
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("version") != 1 or not isinstance(state.get("pages"), dict):
                raise ValueError("Unsupported WeRead path index")
            for identity, relative in state["pages"].items():
                identity = notion_page_id(identity)
                if identity in self.paths:
                    raise ValueError("Duplicate normalized identity in WeRead index")
                path = self.checked(root / relative)
                if path in self.owners and self.owners[path] != identity:
                    raise ValueError("WeRead path index ownership mismatch")
                if path in self.paths.values():
                    raise ValueError("WeRead path index collision")
                self.paths[identity] = path
                self.owners[path] = identity
        for identity, paths in self.existing.items():
            # Lexically first legacy path is deterministic, not a content merge.
            self.paths.setdefault(identity, paths[0])

    def checked(self, path: Path) -> Path:
        if not path.is_relative_to(self.root) or ".." in path.parts or path.suffix != ".md":
            raise ValueError("Invalid WeRead canonical path")
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("WeRead canonical path escapes output root")
        return path

    def target(self, page: Dict[str, Any], proposed: Path) -> Path:
        identity = notion_page_id(str(page.get("id") or ""))
        if identity in self.paths:
            return self.checked(self.paths[identity])
        path = self.checked(proposed)
        if path in self.owners or path.exists():
            path = proposed.with_name(f"{proposed.stem}-{identity}.md")
            self.checked(path)
            if path in self.owners or path.exists():
                raise ValueError("WeRead identity suffix path already occupied")
        self.paths[identity] = path
        self.owners[path] = identity
        return path

    def save(self) -> None:
        atomic_write(self.state_path, json.dumps({
            "version": 1,
            "pages": {identity: str(path.relative_to(self.root)) for identity, path in sorted(self.paths.items())},
        }, ensure_ascii=False, indent=2) + "\n")

    def duplicate_report(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "policy": "review_only_no_deletions",
            "groups": [{
                "page_id": identity,
                "canonical_path": str(self.paths[identity].relative_to(self.root)),
                "files": [{
                    "path": str(path.relative_to(self.root)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                } for path in paths],
            } for identity, paths in sorted(self.existing.items()) if len(paths) > 1],
        }


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
    parser.add_argument("--audit-report", type=Path, help="Write a private duplicate candidate report; no API or note writes.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = BookPaths(OUTPUT_ROOT)
    if args.audit_report:
        if args.audit_report.resolve().is_relative_to(OUTPUT_ROOT.resolve()):
            raise ValueError("Audit report must be outside the source directory")
        atomic_write(args.audit_report, json.dumps(paths.duplicate_report(), ensure_ascii=False, indent=2) + "\n")
        print("WROTE duplicate review report; source notes unchanged")
        return
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
        target = paths.target(page, target)
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
            atomic_write(target, content)
            paths.save()

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
    elif not args.max_pages:
        atomic_write(index_path, render_index(rows))

    print(f"{'PREVIEWED' if args.dry_run else 'WROTE'} {len(rows)} book notes")
    if args.max_pages:
        print("PRESERVED full index during limited run")
    elif not args.dry_run:
        print(f"WROTE {index_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
