#!/usr/bin/env python3
"""Rewrite Xinzhi link notes into cleaned document notes and archive originals.

The script scans recent Xinzhi notes, looks for Bilibili or Xiaohongshu links,
creates a new document note with the original link at the top, preserves tags
and the source container, then archives the source note.

This workflow is intentionally conservative:
- only recent notes are considered by default
- only one primary target URL per note is processed
- the source note is not archived unless the replacement note is created first
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
STATE_PATH = ROOT / ".github" / "state" / "xinzhi-link-rewrite.json"
TZ = dt.timezone(dt.timedelta(hours=8))

TARGET_DOMAINS = (
    "bilibili.com",
    "b23.tv",
    "xiaohongshu.com",
    "xhslink.com",
)

SEARCH_KEYWORDS = (
    "bilibili",
    "b23.tv",
    "xiaohongshu",
    "xhslink",
    "小红书",
    "哔哩哔哩",
    "B站",
)

DEFAULT_NOTE_TYPES = ("LINK", "VIDEO", "DOCUMENT", "RICH_TEXT")
URL_RE = re.compile(r"https?://[^\s<>\]\"')]+", re.IGNORECASE)


def load_token() -> str:
    token = (
        os.environ.get("XINZHI_CLI_ACCESS_TOKEN")
        or os.environ.get("XINZHI_ACCESS_TOKEN")
        or os.environ.get("XINZHI_TOKEN")
    )
    if token and token.strip():
        return token.strip()
    raise SystemExit("Missing required Xinzhi token: XINZHI_CLI_ACCESS_TOKEN")


def login_to_xinzhi(token: str) -> None:
    proc = subprocess.run(
        ["xinzhi", "login", token],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if proc.returncode != 0:
        raise SystemExit(
            "Xinzhi login failed: "
            + (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
        )


def run_xinzhi_json(args: Sequence[str]) -> Any:
    proc = subprocess.run(
        ["xinzhi", *args],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Xinzhi command failed: "
            + " ".join(args)
            + " :: "
            + (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
        )
    text = proc.stdout.strip()
    if not text:
        return {}
    return json.loads(text)


def as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def unwrap_list_payload(value: Any) -> Dict[str, Any]:
    payload = as_dict(value)
    if "list" in payload:
        return payload
    data = payload.get("data")
    if isinstance(data, dict) and "list" in data:
        return data
    return payload


def unwrap_note_payload(value: Any) -> Dict[str, Any]:
    payload = as_dict(value)
    if "note" in payload and isinstance(payload["note"], dict):
        return payload["note"]
    data = payload.get("data")
    if isinstance(data, dict):
        if "note" in data and isinstance(data["note"], dict):
            return data["note"]
        return data
    return payload


def parse_dt(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return dt.datetime.fromtimestamp(numeric, tz=TZ)
    if isinstance(value, str) and value.strip().isdigit():
        numeric = float(value.strip())
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return dt.datetime.fromtimestamp(numeric, tz=TZ)
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def note_timestamp(note: Dict[str, Any]) -> Optional[dt.datetime]:
    for key in ("createTime", "createdAt", "created_at", "publishTime", "publish_time", "updateTime", "updatedAt", "updated_at"):
        parsed = parse_dt(note.get(key))
        if parsed:
            return parsed
    return None


def note_id(note: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "noteId", "note_id"):
        value = note.get(key)
        if value:
            return str(value)
    return None


def note_type(note: Dict[str, Any]) -> str:
    for key in ("noteType", "note_type", "type"):
        value = note.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return ""


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return ""


def note_title(note: Dict[str, Any]) -> str:
    for key in ("title", "name"):
        value = text_value(note.get(key))
        if value:
            return value
    return ""


def extract_container(note: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    container = note.get("container")
    if isinstance(container, dict):
        cid = text_value(container.get("id") or container.get("containerId") or container.get("container_id"))
        name = text_value(container.get("title") or container.get("name") or container.get("containerTitle"))
        return cid or None, name or None
    cid = text_value(note.get("containerId") or note.get("container_id"))
    name = text_value(note.get("containerName") or note.get("container_name"))
    return cid or None, name or None


def extract_tags(note: Dict[str, Any]) -> List[str]:
    tags = []
    for key in ("tags", "tagList", "tag_list"):
        raw = note.get(key)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    name = text_value(item.get("name") or item.get("title"))
                else:
                    name = text_value(item)
                if name:
                    tags.append(name)
    seen: Set[str] = set()
    deduped: List[str] = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            deduped.append(tag)
    return deduped


def recursive_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if value.strip():
            yield value.strip()
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from recursive_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from recursive_strings(item)


def collected_text(note: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in (
        "title",
        "content",
        "excerpt",
        "summary",
        "description",
        "url",
        "link",
        "body",
        "text",
        "webPage",
        "web_page",
    ):
        value = note.get(key)
        if value is not None:
            parts.extend(recursive_strings(value))
    if not parts:
        parts.extend(recursive_strings(note))
    return "\n".join(parts)


def extract_urls(note: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    seen: Set[str] = set()
    for match in URL_RE.finditer(collected_text(note)):
        url = match.group(0).strip().rstrip(".,;:!?)]}>\u3002\uff01\uff1f\uff0c")
        lower = url.lower()
        if any(domain in lower for domain in TARGET_DOMAINS) and url not in seen:
            seen.add(url)
            found.append(url)
    return found


def primary_url(note: Dict[str, Any]) -> Optional[str]:
    urls = extract_urls(note)
    return urls[0] if urls else None


def normalized_text(note: Dict[str, Any]) -> str:
    return collected_text(note).strip().replace("\r\n", "\n")


def is_rewritten_note(note: Dict[str, Any]) -> bool:
    text = normalized_text(note)
    return text.startswith("原链接：") or "自动整理标记：xinzhi-link-rewrite" in text


def in_time_window(note: Dict[str, Any], days_back: int) -> bool:
    ts = note_timestamp(note)
    if not ts:
        return True
    cutoff = dt.datetime.now(TZ) - dt.timedelta(days=days_back)
    return ts >= cutoff


def search_notes(keyword: str, page_size: int, max_pages: int) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    page_index = 1
    while True:
        payload = unwrap_list_payload(
            run_xinzhi_json(
                [
                    "note",
                    "search",
                    "--keyword",
                    keyword,
                    "--page-index",
                    str(page_index),
                    "--page-size",
                    str(page_size),
                ]
            )
        )
        batch = payload.get("list") or []
        if isinstance(batch, list):
            for item in batch:
                if isinstance(item, dict):
                    matches.append(item)
        has_more = bool(payload.get("hasMore"))
        if not has_more or page_index >= max_pages:
            break
        page_index += 1
    return matches


def get_note(note_id_value: str) -> Dict[str, Any]:
    return unwrap_note_payload(run_xinzhi_json(["note", "get", "--id", note_id_value]))


def create_document(
    title: str,
    content: str,
    container_id: Optional[str],
    container_name: Optional[str],
    tags: Sequence[str],
) -> Dict[str, Any]:
    args = ["note", "create-document", "--title", title, "--content", content]
    if container_id:
        args.extend(["--container-id", container_id])
    elif container_name:
        args.extend(["--container-name", container_name])
    if tags:
        args.extend(["--tag-names", ",".join(tags)])
    return unwrap_note_payload(run_xinzhi_json(args))


def archive_note(note_id_value: str) -> Dict[str, Any]:
    return unwrap_note_payload(run_xinzhi_json(["note", "archive", "--id", note_id_value]))


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "processed_source_ids": [],
            "rewrite_note_ids": [],
            "pending_archive_ids": [],
            "last_run_at": None,
        }
    with STATE_PATH.open("r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {
                "processed_source_ids": [],
                "rewrite_note_ids": [],
                "pending_archive_ids": [],
                "last_run_at": None,
            }
    if not isinstance(data, dict):
        return {
            "processed_source_ids": [],
            "rewrite_note_ids": [],
            "pending_archive_ids": [],
            "last_run_at": None,
        }
    data.setdefault("processed_source_ids", [])
    data.setdefault("rewrite_note_ids", [])
    data.setdefault("pending_archive_ids", [])
    data.setdefault("last_run_at", None)
    return data


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def trim_ids(values: List[str], limit: int = 1000) -> List[str]:
    if len(values) <= limit:
        return values
    return values[-limit:]


def source_container_selector(note: Dict[str, Any], default_container: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    cid, cname = extract_container(note)
    if cid or cname:
        return cid, cname
    if default_container:
        return None, default_container
    return None, "暂存箱"


def rewrite_content(note: Dict[str, Any], url: str, content_limit: int) -> str:
    title = note_title(note)
    body = normalized_text(note)
    if body.startswith("原链接："):
        return body

    parts: List[str] = [f"原链接：{url}"]
    if title:
        parts.extend(["", f"原始标题：{title}"])

    container_id, container_name = extract_container(note)
    if container_id or container_name:
        parts.extend(["", f"原始容器：{container_name or container_id}"])

    tags = extract_tags(note)
    if tags:
        parts.extend(["", f"原始标签：{', '.join(tags)}"])

    if body:
        snippet = body.strip()
        if len(snippet) > content_limit:
            snippet = snippet[:content_limit].rstrip() + "\n\n[内容已截断]"
        parts.extend(["", "原始内容", "", snippet])

    parts.extend(["", "自动整理标记：xinzhi-link-rewrite"])
    return "\n".join(parts).strip() + "\n"


def build_title(note: Dict[str, Any], url: str) -> str:
    title = note_title(note)
    if title:
        return title
    if "xiaohongshu" in url or "xhslink" in url:
        return "小红书链接整理"
    if "bilibili" in url or "b23.tv" in url:
        return "B站链接整理"
    return "链接整理"


def collect_candidates(
    days_back: int,
    limit: int,
    page_size: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    for keyword in SEARCH_KEYWORDS:
        try:
            hits = search_notes(keyword, page_size=page_size, max_pages=max_pages)
        except Exception as exc:
            raise RuntimeError(f"Search failed for keyword {keyword!r}: {exc}") from exc
        for item in hits:
            nid = note_id(item)
            if nid and nid not in candidates:
                candidates[nid] = item
            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    detailed: List[Dict[str, Any]] = []
    for nid in sorted(candidates.keys()):
        try:
            note = get_note(nid)
        except Exception:
            continue
        if not in_time_window(note, days_back=days_back):
            continue
        if not extract_urls(note):
            continue
        detailed.append(note)
    detailed.sort(key=lambda item: note_timestamp(item) or dt.datetime.min.replace(tzinfo=TZ), reverse=True)
    return detailed[:limit]


def process_note(
    note: Dict[str, Any],
    dry_run: bool,
    default_container: Optional[str],
    content_limit: int,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    nid = note_id(note)
    if not nid:
        return {"status": "skipped", "reason": "missing-note-id"}

    if is_rewritten_note(note):
        return {"status": "skipped", "note_id": nid, "reason": "already-rewritten"}

    urls = extract_urls(note)
    if not urls:
        return {"status": "skipped", "note_id": nid, "reason": "no-target-url"}
    url = urls[0]

    processed_source_ids = set(state.get("processed_source_ids") or [])
    rewrite_note_ids = set(state.get("rewrite_note_ids") or [])
    pending_archive_ids = set(state.get("pending_archive_ids") or [])
    if nid in processed_source_ids or nid in rewrite_note_ids:
        return {"status": "skipped", "note_id": nid, "reason": "already-processed"}

    container_id, container_name = source_container_selector(note, default_container)
    tags = extract_tags(note)
    title = build_title(note, url)
    content = rewrite_content(note, url, content_limit=content_limit)

    if dry_run:
        return {
            "status": "dry-run",
            "note_id": nid,
            "title": title,
            "url": url,
            "container": container_name or container_id,
            "tags": tags,
        }

    created = create_document(title, content, container_id, container_name, tags)
    created_id = note_id(created)
    if not created_id:
        raise RuntimeError(f"Replacement note created but missing id for source {nid}")

    try:
        archive_note(nid)
        processed_source_ids.add(nid)
        rewrite_note_ids.add(created_id)
        pending_archive_ids.discard(nid)
    except Exception:
        processed_source_ids.add(nid)
        rewrite_note_ids.add(created_id)
        pending_archive_ids.add(nid)

    state["processed_source_ids"] = trim_ids(sorted(processed_source_ids))
    state["rewrite_note_ids"] = trim_ids(sorted(rewrite_note_ids))
    state["pending_archive_ids"] = trim_ids(sorted(pending_archive_ids))
    state["last_run_at"] = dt.datetime.now(TZ).isoformat()
    save_state(state)

    return {
        "status": "rewritten",
        "source_note_id": nid,
        "rewrite_note_id": created_id,
        "url": url,
        "title": title,
        "container": container_name or container_id,
    }


def reconcile_pending_archives(state: Dict[str, Any], dry_run: bool) -> List[Dict[str, Any]]:
    pending = list(dict.fromkeys(state.get("pending_archive_ids") or []))
    if not pending:
        return []

    results: List[Dict[str, Any]] = []
    still_pending: List[str] = []
    for nid in pending:
        if dry_run:
            results.append({"status": "pending-archive", "note_id": nid})
            still_pending.append(nid)
            continue
        try:
            archive_note(nid)
            results.append({"status": "archived-pending", "note_id": nid})
        except Exception as exc:
            results.append({"status": "archive-failed", "note_id": nid, "error": str(exc)})
            still_pending.append(nid)

    state["pending_archive_ids"] = trim_ids(still_pending)
    if not dry_run:
        state["last_run_at"] = dt.datetime.now(TZ).isoformat()
        save_state(state)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rewrite Xinzhi link notes and archive originals")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("XINZHI_REWRITE_DRY_RUN", "false").lower() == "true",
        help="Fetch and report without mutating Xinzhi",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=int(os.environ.get("XINZHI_REWRITE_DAYS", "3")),
        help="Look back window in days",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("XINZHI_REWRITE_LIMIT", "200")),
        help="Maximum number of notes to process",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=int(os.environ.get("XINZHI_REWRITE_PAGE_SIZE", "50")),
        help="Search page size",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(os.environ.get("XINZHI_REWRITE_MAX_PAGES", "10")),
        help="Maximum search pages per keyword",
    )
    parser.add_argument(
        "--content-limit",
        type=int,
        default=int(os.environ.get("XINZHI_REWRITE_CONTENT_LIMIT", "12000")),
        help="Maximum characters of original content to embed",
    )
    parser.add_argument(
        "--default-container",
        default=os.environ.get("XINZHI_DEFAULT_CONTAINER_NAME") or "暂存箱",
        help="Fallback container name if the source note does not expose one",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = load_token()
    login_to_xinzhi(token)

    state = load_state()
    pending_results = reconcile_pending_archives(state, dry_run=args.dry_run)

    candidates = collect_candidates(
        days_back=args.days_back,
        limit=args.limit,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )

    results: List[Dict[str, Any]] = []
    for note in candidates:
        try:
            result = process_note(
                note,
                dry_run=args.dry_run,
                default_container=args.default_container,
                content_limit=args.content_limit,
                state=state,
            )
        except Exception as exc:
            result = {
                "status": "error",
                "note_id": note_id(note),
                "error": str(exc),
            }
        results.append(result)

    summary = {
        "dry_run": args.dry_run,
        "window_days": args.days_back,
        "candidates": len(candidates),
        "pending_archives": len(state.get("pending_archive_ids") or []),
        "results": results,
        "reconciled": pending_results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") in {"skipped", "dry-run", "rewritten", "archived-pending", "pending-archive"} for item in results + pending_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
