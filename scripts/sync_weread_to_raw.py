#!/usr/bin/env python3
"""Sync weread (微信读书) data into Raw/15 微信读书."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from sync_notion_to_raw import render_frontmatter, write_text

    _HAVE_SYNC_UTILITIES = True
except ImportError:
    _HAVE_SYNC_UTILITIES = False


# ---------------------------------------------------------------------------
# Fallback implementations when sync_notion_to_raw is not importable
# ---------------------------------------------------------------------------

if not _HAVE_SYNC_UTILITIES:

    def render_frontmatter(fields: Dict[str, Any]) -> str:
        lines = ["---"]
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
                lines.append(f"{key}: |-")
                for line in rendered.splitlines():
                    lines.append(f"  {line}")
                continue
            if isinstance(value, bool):
                lines.append(f"{key}: {'true' if value else 'false'}")
                continue
            if isinstance(value, (int, float)):
                lines.append(f"{key}: {value}")
                continue
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
        lines.append("---")
        return "\n".join(lines)

    def write_text(path: Path, content: str) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return False
        path.write_text(content, encoding="utf-8")
        return True


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
OUTPUT_ROOT = ROOT / "Raw" / "15 微信读书"
TZ = dt.timezone(dt.timedelta(hours=8))
WEREAD_API_BASE = "https://i.weread.qq.com/api/agent/gateway"
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Weread API helpers
# ---------------------------------------------------------------------------

def _load_api_key() -> str:
    key = os.environ.get("WEREAD_API_KEY")
    if not key:
        raise SystemExit(
            "Missing required credential: WEREAD_API_KEY environment variable. "
            "Get your API key from the weread API gateway."
        )
    return key.strip()


def _weread_request(
    api_key: str,
    api_name: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body = {
        "api_name": api_name,
        "count": 100,
        "skill_version": "1.0.3",
    }
    if payload:
        body.update(payload)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "eddie-wiki-weread-sync/1.0",
    }

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_error: Optional[Exception] = None

    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(
            WEREAD_API_BASE,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            result = json.loads(raw) if raw else {}
            # weread gateway may wrap in {"code": 0, "data": ...}
            if isinstance(result, dict) and "data" in result:
                return result["data"] if isinstance(result["data"], dict) else result
            return result
        except urllib.error.HTTPError as exc:
            last_error = exc
            body_text = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < MAX_RETRIES - 1:
                retry_after = exc.headers.get("Retry-After")
                time.sleep(int(retry_after) if retry_after and retry_after.isdigit() else 10)
                continue
            if 500 <= exc.code < 600 and attempt < MAX_RETRIES - 1:
                time.sleep(5)
                continue
            raise RuntimeError(
                f"Weread API {api_name} failed ({exc.code}): {body_text or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(5)
                continue
            raise RuntimeError(f"Weread request failed for {api_name}: {exc}") from exc

    raise RuntimeError(f"Weread request failed for {api_name}: {last_error}")


def fetch_bookshelf(api_key: str) -> List[Dict[str, Any]]:
    """Fetch all books from the weread bookshelf via /shelf/sync with pagination."""
    all_books: List[Dict[str, Any]] = []
    last_sort = 0
    seen_ids: set = set()

    while True:
        payload: Dict[str, Any] = {"count": 100}
        if last_sort > 0:
            payload["lastSort"] = last_sort

        data = _weread_request(api_key, "/shelf/sync", payload)
        books = data.get("records") or data.get("books") or []
        if not isinstance(books, list):
            break

        new_count = 0
        for book in books:
            book_id = book.get("bookId") or book.get("id")
            if book_id and book_id not in seen_ids:
                seen_ids.add(book_id)
                all_books.append(book)
                new_count += 1

        if new_count == 0:
            break

        # Get the sort value of the last book for pagination
        last_book = books[-1]
        next_sort = last_book.get("sort")
        if next_sort is not None:
            last_sort = int(next_sort)
        else:
            break

    return all_books


def fetch_book_notes(api_key: str, book_id: str) -> List[Dict[str, Any]]:
    """Fetch highlights and notes for a specific book."""
    all_notes: List[Dict[str, Any]] = []
    last_sort = 0
    seen_ids: set = set()

    while True:
        payload: Dict[str, Any] = {
            "bookId": book_id,
            "count": 100,
        }
        if last_sort > 0:
            payload["lastSort"] = last_sort

        data = _weread_request(api_key, "/user/notebooks", payload)
        notes = data.get("notebooks") or data.get("records") or []
        if not isinstance(notes, list):
            break

        new_count = 0
        for note in notes:
            note_id = note.get("noteId") or note.get("id")
            if note_id and note_id not in seen_ids:
                seen_ids.add(note_id)
                all_notes.append(note)
                new_count += 1

        if new_count == 0:
            break

        last_note = notes[-1]
        next_sort = last_note.get("sort")
        if next_sort is not None:
            last_sort = int(next_sort)
        else:
            break

    return all_notes


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _safe_filename(title: str) -> str:
    safe = title.replace("/", "／").replace("\\", "＼").replace(":", "：")
    safe = safe.replace("*", "＊").replace("?", "？")
    safe = safe.replace('"', "＂").replace("<", "＜").replace(">", "＞")
    safe = safe.replace("|", "｜")
    if len(safe.encode("utf-8")) > 120:
        trimmed = safe.encode("utf-8")[:120]
        while trimmed:
            try:
                safe = trimmed.decode("utf-8").rstrip("-. ")
                break
            except UnicodeDecodeError:
                trimmed = trimmed[:-1]
    return safe or "未命名书籍"


def _format_timestamp(ts_ms: Any) -> str:
    """Convert millisecond timestamp to readable date string."""
    if not ts_ms:
        return ""
    try:
        ts = int(ts_ms) / 1000.0
        dt_obj = dt.datetime.fromtimestamp(ts, tz=TZ)
        return dt_obj.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _reading_progress(book: Dict[str, Any]) -> str:
    """Format reading progress from book data."""
    progress = book.get("progress") or book.get("readingProgress")
    if progress is not None:
        try:
            pct = float(progress)
            if pct <= 1:
                pct *= 100
            return f"{pct:.0f}%"
        except (TypeError, ValueError):
            return str(progress)
    return ""


def render_book_note(
    book: Dict[str, Any],
    notes: List[Dict[str, Any]],
) -> tuple[str, Path]:
    title = book.get("title") or book.get("bookName") or "未知书名"
    author = book.get("author") or book.get("authorName") or ""
    book_id = book.get("bookId") or book.get("id") or ""
    progress = _reading_progress(book)
    last_read = _format_timestamp(book.get("lastReadTime") or book.get("finishTime"))

    frontmatter = render_frontmatter(
        {
            "source_system": "weread",
            "source_type": "weread_book",
            "title": title,
            "author": author,
            "book_id": str(book_id),
            "reading_progress": progress or None,
            "last_read_time": last_read or None,
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
    if progress:
        lines.append(f"- 阅读进度：{progress}")
    if last_read:
        lines.append(f"- 最近阅读：{last_read}")
    if book_id:
        lines.append(f"- 微信读书 ID：{book_id}")

    if notes:
        highlights = [n for n in notes if n.get("type") == 0 or n.get("noteType") == 0]
        user_notes = [n for n in notes if n.get("type") == 1 or n.get("noteType") == 1]

        if highlights:
            lines.extend(["", "## 划线", ""])
            for h in highlights:
                content = h.get("content") or h.get("markText") or h.get("abstract") or ""
                if content:
                    # Strip HTML tags simply
                    import re
                    content = re.sub(r"<[^>]+>", "", content).strip()
                    created = _format_timestamp(h.get("createTime") or h.get("createdTime"))
                    time_suffix = f" _{created}_" if created else ""
                    lines.append(f"> {content}{time_suffix}")
                    lines.append("")

        if user_notes:
            lines.extend(["", "## 笔记", ""])
            for n in user_notes:
                content = n.get("content") or n.get("noteContent") or ""
                if content:
                    import re
                    content = re.sub(r"<[^>]+>", "", content).strip()
                    created = _format_timestamp(n.get("createTime") or n.get("createdTime"))
                    time_suffix = f" _{created}_" if created else ""
                    lines.append(f"- {content}{time_suffix}")

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n", OUTPUT_ROOT / f"{_safe_filename(title)}.md"


def render_index(rows: List[Dict[str, Any]]) -> str:
    generated_at = now_iso()
    lines = [
        "---",
        'source_system: "weread"',
        'source_type: "weread_book_index"',
        f'generated_at: "{generated_at}"',
        f"book_count: {len(rows)}",
        "---",
        "",
        "# 微信读书索引",
        "",
        f"> 本地已同步 {len(rows)} 本书。",
        "",
        "| 书名 | 作者 | 进度 | 最近阅读 | 文件 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda item: item.get("last_read") or "", reverse=True):
        progress_text = row.get("progress") or "-"
        last_read_text = row.get("last_read") or "-"
        lines.append(
            f"| {row['title']} | {row.get('author') or '-'} | {progress_text} | {last_read_text} | [{row['path']}]({row['path']}) |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview target files without writing.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = _load_api_key()

    print("Fetching weread bookshelf...")
    books = fetch_bookshelf(api_key)
    print(f"Found {len(books)} books on bookshelf")

    rows: List[Dict[str, Any]] = []

    for book in books:
        book_id = book.get("bookId") or book.get("id")
        title = book.get("title") or book.get("bookName") or "未知书名"
        author = book.get("author") or book.get("authorName") or ""
        progress = _reading_progress(book)
        last_read = _format_timestamp(book.get("lastReadTime") or book.get("finishTime"))

        # Fetch notes/highlights for this book
        notes: List[Dict[str, Any]] = []
        if book_id:
            try:
                notes = fetch_book_notes(api_key, str(book_id))
                if notes:
                    print(f"  [{title}] fetched {len(notes)} notes/highlights")
            except Exception as exc:
                print(f"  WARNING: Failed to fetch notes for {title}: {exc}")

        content, target = render_book_note(book, notes)

        if args.dry_run:
            print(f"DRY-RUN {title} -> {target.relative_to(ROOT)}")
        else:
            write_text(target, content)

        rows.append(
            {
                "title": title,
                "author": author,
                "progress": progress,
                "last_read": last_read,
                "path": str(target.relative_to(OUTPUT_ROOT)),
            }
        )

    index_path = OUTPUT_ROOT / "索引.md"
    if args.dry_run:
        print(f"DRY-RUN {index_path.relative_to(ROOT)}")
    else:
        write_text(index_path, render_index(rows))

    print(f"WROTE {len(rows)} book notes")
    print(f"WROTE {index_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
