#!/usr/bin/env python3
"""Sync listened Xiaoyuzhou episodes from Notion into Raw/13 小宇宙."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sync_notion_to_raw import (
    get_database,
    query_database,
    TZ,
    parse_time,
    load_notion_api_key,
    notion_request,
    normalize_notion_id,
    render_blocks,
    render_frontmatter,
    rich_text_plain_text,
)


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
OUTPUT_ROOT = ROOT / "Raw" / "13 小宇宙"
LEGACY_OUTPUT_ROOTS = [ROOT / "Raw" / "11 我的播客"]
EPISODE_DATABASE_ID = "11b33b33-7f23-81d1-b263-dce871488a9f"
STATE_PATH = ROOT / ".github" / "state" / "xiaoyuzhou-sync-state.json"
DEFAULT_HOURS_BACK = 168
LISTENED_FILTER = {
    "or": [
        {"property": "状态", "status": {"equals": "听过"}},
        {"property": "状态", "status": {"equals": "在听"}},
        {"property": "日期", "date": {"is_not_empty": True}},
        {"property": "收听进度", "number": {"greater_than": 0}},
    ]
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-id", action="append", default=[], help="Notion episode page ID. May be repeated.")
    parser.add_argument("--all-current", action="store_true", help="Run a full backfill for all listened episodes.")
    parser.add_argument("--hours-back", type=int, default=DEFAULT_HOURS_BACK, help="Incremental lookback window for edited episodes.")
    parser.add_argument("--max-episodes", type=int, default=0, help="Stop after this many matching episodes. 0 means no limit.")
    parser.add_argument("--only-missing", action="store_true", help="For full backfills, skip episodes already written under Raw/13 小宇宙.")
    parser.add_argument("--dry-run", action="store_true", help="Preview target files without writing.")
    parser.add_argument("--rebuild-index", action="store_true", help="Rebuild Raw/13 小宇宙/索引.md from local files.")
    args = parser.parse_args()
    if args.hours_back < 0 or args.max_episodes < 0:
        parser.error("--hours-back and --max-episodes must be non-negative")
    return args


def fetch_page(api_key: str, page_id: str) -> Dict[str, Any]:
    return notion_request(api_key, "GET", f"/pages/{normalize_notion_id(page_id)}")


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def read_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise RuntimeError("Invalid sync state: expected an object; state preserved")
    return state


def write_verified_text(path: Path, content: str, *, create_only: bool = False) -> None:
    """Replace one file atomically; do not treat an unverified write as success."""
    encoded = content.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not create_only and path.exists() and path.read_bytes() == encoded:
        return
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.read_bytes() != encoded:
            raise RuntimeError("Temporary file verification failed")
        if create_only:
            # Atomic no-clobber publish: a concurrent destination is a conflict.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        if path.read_bytes() != encoded:
            raise RuntimeError("File readback failed")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_state(data: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if STATE_PATH.exists() and STATE_PATH.read_text(encoding="utf-8") == rendered:
        return
    write_verified_text(STATE_PATH, rendered)


def format_time(value: dt.datetime) -> str:
    return value.astimezone(TZ).isoformat(timespec="seconds")


def build_episode_filter(
    *, full_sync: bool, hours_back: int, scan_started_at: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    started_at = scan_started_at if scan_started_at is not None else now()
    state = read_state()
    cutoff = parse_time(state.get("last_sync_at"))
    if "last_sync_at" in state and cutoff is None:
        raise RuntimeError("Invalid last_sync_at; state preserved, repair it explicitly")
    filters = [LISTENED_FILTER]
    if not full_sync:
        lookback = started_at - dt.timedelta(hours=hours_back)
        cutoff = min(cutoff, lookback) if cutoff is not None else lookback
        filters.append({
            "timestamp": "last_edited_time",
            "last_edited_time": {"on_or_after": format_time(cutoff)},
        })
    filters.append({
        "timestamp": "last_edited_time",
        "last_edited_time": {"on_or_before": format_time(started_at)},
    })
    return {"and": filters}


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def current_episode_pages(
    api_key: str,
    *,
    full_sync: bool,
    hours_back: int,
    max_episodes: int,
    only_missing: bool,
    scan_started_at: Optional[dt.datetime] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Return pages and proven query exhaustion, never infer it from a limit."""
    episode_db = get_database(api_key, EPISODE_DATABASE_ID)
    episode_db_id = normalize_notion_id(str(episode_db.get("id") or EPISODE_DATABASE_ID))
    pages: List[Dict[str, Any]] = []
    filter_obj = build_episode_filter(
        full_sync=full_sync, hours_back=hours_back, scan_started_at=scan_started_at,
    )
    scanned = 0
    cursor: Optional[str] = None
    seen_cursors = set()
    seen_ids = set()
    while True:
        response = query_database(api_key, episode_db_id, filter_obj=filter_obj, start_cursor=cursor)
        if (not isinstance(response, dict) or not isinstance(response.get("results"), list)
                or not isinstance(response.get("has_more"), bool)):
            raise RuntimeError("Unknown episode query result; no watermark advancement")
        has_more = response["has_more"]
        next_cursor = response.get("next_cursor")
        if has_more and (not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors):
            raise RuntimeError("Incomplete or repeated episode cursor; no watermark advancement")
        if not has_more and next_cursor is not None:
            raise RuntimeError("Conflicting episode pagination terminal; no watermark advancement")
        for page in response["results"]:
            if not isinstance(page, dict) or not isinstance(page.get("id"), str) or not page["id"].strip():
                raise RuntimeError("Missing episode identity; no watermark advancement")
            page_id = normalize_notion_id(page["id"])
            if page_id in seen_ids:
                raise RuntimeError("Repeated episode identity; no watermark advancement")
            seen_ids.add(page_id)
            scanned += 1
            if scanned == 1 or scanned % 50 == 0:
                print(f"SCANNED {scanned} listened episode candidates", flush=True)
            if page.get("archived"):
                continue
            if only_missing:
                existing = find_existing_file_across_roots(page_id)
                if existing and is_within(existing, OUTPUT_ROOT):
                    continue
            pages.append(page)
            if max_episodes > 0 and len(pages) >= max_episodes:
                print(f"FOUND {len(pages)} listened episodes; limit reached, scan not certified complete", flush=True)
                return pages, False
        if not has_more:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    print(f"FOUND {len(pages)} listened episodes to sync", flush=True)
    return pages, True


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def title_from_properties(properties: Dict[str, Any]) -> str:
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return rich_text_plain_text(prop.get("title") or []).strip()
    return ""


def prop_text(prop: Dict[str, Any]) -> str:
    prop_type = prop.get("type")
    if prop_type == "rich_text":
        return rich_text_plain_text(prop.get("rich_text") or []).strip()
    if prop_type == "title":
        return rich_text_plain_text(prop.get("title") or []).strip()
    if prop_type == "url":
        return (prop.get("url") or "").strip()
    if prop_type == "date":
        return ((prop.get("date") or {}).get("start") or "").strip()
    if prop_type == "select":
        return (((prop.get("select") or {}).get("name")) or "").strip()
    if prop_type == "status":
        return (((prop.get("status") or {}).get("name")) or "").strip()
    if prop_type == "email":
        return (prop.get("email") or "").strip()
    if prop_type == "phone_number":
        return (prop.get("phone_number") or "").strip()
    return ""


def prop_number(prop: Dict[str, Any]) -> Optional[float]:
    if prop.get("type") != "number":
        return None
    return prop.get("number")


def prop_relation_ids(prop: Dict[str, Any]) -> List[str]:
    if prop.get("type") != "relation":
        return []
    return [normalize_notion_id(str(item.get("id"))) for item in (prop.get("relation") or []) if item.get("id")]


def prop_files(prop: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(prop, dict) or prop.get("type") != "files" or not isinstance(prop.get("files"), list):
        raise RuntimeError("Invalid transcript files property")
    files = prop["files"]
    for item in files:
        if not isinstance(item, dict) or item.get("type") not in {"file", "external"}:
            raise RuntimeError("Invalid transcript attachment type")
        if not isinstance(item.get("name", ""), str):
            raise RuntimeError("Invalid transcript attachment name")
        file_data = item.get(item["type"])
        url = file_data.get("url") if isinstance(file_data, dict) else None
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError("Missing transcript attachment URL")
        try:
            parsed = urllib.parse.urlsplit(url)
            valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid or url != url.strip() or any(char.isspace() for char in url):
            raise RuntimeError("Invalid transcript attachment URL")
    return files


def parse_iso_to_local(value: str) -> dt.datetime:
    cleaned = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def safe_title_stem(title: str, fallback: str) -> str:
    stem = (title or "").strip().replace("/", "／")
    stem = stem.replace(":", "：")
    return stem or fallback


def unique_target_path(base_dir: Path, title: str, page_id: str) -> Path:
    stem = safe_title_stem(title, page_id[:8])
    candidate = base_dir / f"{stem}.md"
    if not candidate.exists():
        return candidate
    suffix = normalize_notion_id(page_id).split("-")[0]
    alternate = base_dir / f"{stem}-{suffix}.md"
    return alternate


def find_existing_file_by_episode_id(base_dir: Path, page_id: str) -> Optional[Path]:
    normalized = normalize_notion_id(page_id)
    matches: List[Path] = []
    for path in sorted(base_dir.rglob("*.md")):
        if path.name == "索引.md":
            continue
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
        if normalize_notion_id(meta.get("episode_page_id", "")) == normalized:
            matches.append(path)
    if not matches:
        return None
    return sorted(matches, key=lambda item: (len(item.name), item.name))[0]


def find_existing_file_across_roots(page_id: str) -> Optional[Path]:
    for root in [OUTPUT_ROOT, *LEGACY_OUTPUT_ROOTS]:
        if not root.exists():
            continue
        existing = find_existing_file_by_episode_id(root, page_id)
        if existing:
            return existing
    return None


def check_managed_path(path: Path, *, exists: bool = True) -> None:
    """Only ordinary notes in registered year/month directories may be moved."""
    roots = [OUTPUT_ROOT, *LEGACY_OUTPUT_ROOTS]
    containing = [root for root in roots if is_within(path, root)]
    if len(containing) != 1 or not is_within(path.resolve(), ROOT.resolve()):
        raise RuntimeError("Unmanaged episode path; migration stopped")
    root = containing[0]
    parts = path.relative_to(root).parts
    if (len(parts) != 3 or not re.fullmatch(r"[0-9]{4}", parts[0])
            or not re.fullmatch(re.escape(parts[0]) + r"-(0[1-9]|1[0-2])", parts[1])
            or path.suffix != ".md" or path.name == "索引.md"):
        raise RuntimeError("Unmanaged episode path; migration stopped")
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise RuntimeError("Symlink episode path; migration stopped")
        if component == ROOT:
            break
    if exists and not path.is_file():
        raise RuntimeError("Episode is not an ordinary file; migration stopped")


def all_episode_files(page_id: str) -> List[Path]:
    """Do not conceal duplicate source IDs by choosing the first/shortest path."""
    matches: List[Path] = []
    for root in [OUTPUT_ROOT, *LEGACY_OUTPUT_ROOTS]:
        if root.is_symlink() or not is_within(root.resolve(), ROOT.resolve()):
            raise RuntimeError("Unmanaged episode root; migration stopped")
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not is_within(path.resolve(), root.resolve()):
                raise RuntimeError("Symlink episode path; migration stopped")
            if path.suffix != ".md" or path.name == "索引.md" or path.is_dir():
                continue
            meta = parse_frontmatter(path.read_bytes().decode("utf-8"))
            if normalize_notion_id(meta.get("episode_page_id", "")) == normalize_notion_id(page_id):
                matches.append(path)
    return matches


def managed_note_bytes(path: Path, page_id: str) -> bytes:
    check_managed_path(path)
    data = path.read_bytes()
    meta = parse_frontmatter(data.decode("utf-8"))
    identity = meta.get("episode_page_id", "").lower()
    normalized = normalize_notion_id(page_id)
    if (not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", normalized)
            or identity not in {normalized, normalized.replace("-", "")}
            or meta.get("source_system") != "notion"
            or meta.get("source_type") != "xiaoyuzhou_episode"
            or parse_time(meta.get("synced_at")) is None
            or parse_time(meta.get("listened_at")) is None):
        raise RuntimeError("Unmanaged episode identity; migration stopped")
    return data


def migration_signature(
    data: bytes, ignore_fields: Tuple[str, ...] = ("listened_at", "updated_at", "synced_at"),
) -> Tuple[Dict[str, str], str]:
    """Compare all rendered bytes except the narrowly allowed date/version fields."""
    text = data.decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text:
        raise RuntimeError("Unknown episode format; migration stopped")
    header, body = text.split("\n---\n", 1)
    fields: Dict[str, str] = {}
    key = ""
    for line in header.split("\n")[1:]:
        if line.startswith("  ") and key:
            fields[key] += "\n" + line
        else:
            if ": " not in line:
                raise RuntimeError("Unknown episode metadata; migration stopped")
            key, value = line.split(": ", 1)
            if key in fields:
                raise RuntimeError("Duplicate episode metadata; migration stopped")
            fields[key] = value
    for key in ignore_fields:
        fields.pop(key, None)
    return fields, body


def migration_matches(before: bytes, content: bytes) -> bool:
    old_fields, old_body = migration_signature(before)
    new_fields, new_body = migration_signature(content)
    # Missing additive fields are compatible with the original renderer. Present
    # fields must match exactly, so edits cannot hide in the compatibility case.
    for key in ("transcript_file_names", "transcript_file_count"):
        if key not in old_fields:
            new_fields.pop(key, None)
    return old_fields == new_fields and old_body == new_body


def git_read(*args: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-optional-locks", "--no-replace-objects", "-C", str(ROOT), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Recoverable Git before version unavailable; migration stopped")
    return result.stdout


def recoverable_before(path: Path, data: bytes) -> str:
    """Require the complete original bytes in a pinned, clean Git HEAD blob."""
    if Path(git_read("rev-parse", "--show-toplevel").decode().strip()).resolve() != ROOT.resolve():
        raise RuntimeError("Wrong Git root; migration stopped")
    head = git_read("rev-parse", "--verify", "HEAD").decode().strip()
    relative = path.relative_to(ROOT).as_posix()
    entry = git_read("ls-tree", "-z", head, "--", relative)
    if not entry.startswith((b"100644 blob ", b"100755 blob ")):
        raise RuntimeError("Untracked or non-file before version; migration stopped")
    if git_read("show", f"{head}:{relative}") != data:
        raise RuntimeError("Modified episode before version; migration stopped")
    git_read("diff", "--cached", "--quiet", head, "--", relative)
    return head


def migrate_episode(old: Path, target: Path, page_id: str, content: str) -> None:
    before = managed_note_bytes(old, page_id)
    head = recoverable_before(old, before)
    encoded = content.encode("utf-8")
    if not migration_matches(before, encoded):
        raise RuntimeError("Episode content changed or unknown; migration stopped")
    check_managed_path(target, exists=False)
    if target.exists():
        # Resume only an exact completed destination (apart from its prior sync time).
        destination = managed_note_bytes(target, page_id)
        if (migration_signature(destination, ("synced_at",))
                != migration_signature(encoded, ("synced_at",))):
            raise RuntimeError("Episode destination conflict; migration stopped")
    else:
        write_verified_text(target, content, create_only=True)
        destination = encoded
    # Recheck HEAD first, then both files immediately before removing one old note.
    if recoverable_before(old, before) != head:
        raise RuntimeError("Episode changed during migration; old path preserved")
    check_managed_path(target)
    if target.read_bytes() != destination or managed_note_bytes(old, page_id) != before:
        raise RuntimeError("Episode changed during migration; old path preserved")
    old.unlink()
    if old.exists() or target.read_bytes() != destination:
        raise RuntimeError("Unknown episode migration outcome; watermark unchanged")


def download_text(url: str) -> str:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                text = resp.read().decode("utf-8", errors="strict")
            if not text.removeprefix("\ufeff").strip():
                raise RuntimeError("Empty transcript attachment")
            return text
        except UnicodeDecodeError:
            raise RuntimeError("Transcript attachment is not valid UTF-8") from None
        except urllib.error.HTTPError as exc:
            if (exc.code == 429 or 500 <= exc.code < 600) and attempt < 2:
                time.sleep(3)
                continue
            raise RuntimeError("Transcript attachment download failed") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < 2:
                time.sleep(3)
                continue
            raise RuntimeError("Transcript attachment download failed") from None
    raise RuntimeError("Transcript attachment download failed")


def download_transcripts(files: List[Dict[str, Any]]) -> str:
    """Read every declared attachment before allowing the current note to change."""
    parts: List[str] = []
    for index, item in enumerate(files, start=1):
        text = download_text(item[item["type"]]["url"])
        if len(files) > 1:
            parts.append(f"### 逐字稿附件 {index}\n\n{text}")
        else:
            parts.append(text)
    return "\n\n".join(parts)


def retrieve_block_children_checked(api_key: str, block_id: str) -> List[Dict[str, Any]]:
    """Refuse silent partial page bodies, including nested block collections."""
    children: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    seen_cursors = set()
    seen_ids = set()
    while True:
        params = {"page_size": "100"}
        if cursor:
            params["start_cursor"] = cursor
        query = urllib.parse.urlencode(params)
        response = notion_request(api_key, "GET", f"/blocks/{normalize_notion_id(block_id)}/children?{query}")
        if (not isinstance(response, dict) or not isinstance(response.get("results"), list)
                or not isinstance(response.get("has_more"), bool)):
            raise RuntimeError("Unknown block query result; no watermark advancement")
        has_more = response["has_more"]
        next_cursor = response.get("next_cursor")
        if has_more and (not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors):
            raise RuntimeError("Incomplete or repeated block cursor; no watermark advancement")
        if not has_more and next_cursor is not None:
            raise RuntimeError("Conflicting block pagination terminal; no watermark advancement")
        for block in response["results"]:
            if not isinstance(block, dict) or not isinstance(block.get("id"), str) or not block["id"].strip():
                raise RuntimeError("Missing block identity; no watermark advancement")
            child_id = normalize_notion_id(block["id"])
            if child_id in seen_ids:
                raise RuntimeError("Repeated block identity; no watermark advancement")
            seen_ids.add(child_id)
            children.append(block)
        if not has_more:
            return children
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def build_body(api_key: str, page_id: str, description: str, transcript_text: str) -> str:
    blocks = retrieve_block_children_checked(api_key, page_id)
    rendered = render_blocks(api_key, blocks, children_loader=retrieve_block_children_checked)
    parts: List[str] = []
    if description:
        parts.append("## 简介")
        parts.append("")
        parts.append(description)
    if transcript_text:
        if parts:
            parts.append("")
        parts.append("## 逐字稿")
        parts.append("")
        parts.append(transcript_text)
    if rendered:
        if parts:
            parts.append("")
        parts.append("## 页面内容")
        parts.append("")
        parts.extend(rendered)
    body = "\n".join(parts)
    return body + "\n" if body and not body.endswith("\n") else body or "\n"


def sync_one(
    api_key: str,
    page_id: str,
    dry_run: bool,
    podcast_cache: Dict[str, Dict[str, str]],
    page: Optional[Dict[str, Any]] = None,
) -> Path:
    page = page or fetch_page(api_key, page_id)
    if normalize_notion_id(str(page.get("id") or "")) != normalize_notion_id(page_id):
        raise RuntimeError("Source episode identity conflict; sync stopped")
    properties = page.get("properties") or {}
    title = title_from_properties(properties)
    listened_at = prop_text(properties.get("日期") or {})
    if not listened_at:
        listened_at = page.get("created_time") or page.get("last_edited_time") or now_iso()
    listened_local = parse_iso_to_local(listened_at)
    year_dir = OUTPUT_ROOT / f"{listened_local:%Y}" / f"{listened_local:%Y-%m}"

    podcast_relation_ids = prop_relation_ids(properties.get("Podcast") or {})
    podcast_name = ""
    podcast_page_id = ""
    podcast_url = ""
    if podcast_relation_ids:
        podcast_page_id = podcast_relation_ids[0]
        cached = podcast_cache.get(podcast_page_id)
        if cached is None:
            podcast_page = fetch_page(api_key, podcast_page_id)
            podcast_props = podcast_page.get("properties") or {}
            cached = {
                "podcast_name": title_from_properties(podcast_props),
                "podcast_url": prop_text(podcast_props.get("链接") or {}),
            }
            podcast_cache[podcast_page_id] = cached
        podcast_name = cached["podcast_name"]
        podcast_url = cached["podcast_url"]

    description = prop_text(properties.get("Description") or {})
    episode_url = prop_text(properties.get("链接") or {})
    episode_published_at = prop_text(properties.get("发布时间") or {})
    duration_seconds = prop_number(properties.get("时长") or {})
    eid = prop_text(properties.get("Eid") or {})
    transcript_status = prop_text(properties.get("语音转文字状态") or {})
    transcript_files = prop_files(properties["逐字稿"]) if "逐字稿" in properties else []
    transcript_file_name = transcript_files[0].get("name", "") if transcript_files else ""
    rating = prop_text(properties.get("评分") or {})

    frontmatter = render_frontmatter(
        {
            "source_system": "notion",
            "source_type": "xiaoyuzhou_episode",
            "podcast_name": podcast_name,
            "podcast_page_id": podcast_page_id,
            "podcast_url": podcast_url,
            "episode_page_id": normalize_notion_id(page_id),
            "episode_url": episode_url,
            "title": title,
            "listened_at": listened_local.isoformat(timespec="seconds"),
            "episode_published_at": episode_published_at,
            "updated_at": page.get("last_edited_time") or "",
            "eid": eid,
            "duration_seconds": int(duration_seconds) if duration_seconds is not None else 0,
            "status": prop_text(properties.get("状态") or {}),
            "listened_progress_seconds": int(prop_number(properties.get("收听进度") or {}) or 0),
            "transcript_status": transcript_status,
            "transcript_file_name": transcript_file_name,
            "transcript_file_names": [item.get("name", "") for item in transcript_files],
            "transcript_file_count": len(transcript_files),
            "rating": rating,
            "synced_at": now_iso(),
        }
    )
    existing_files = all_episode_files(page_id)
    if any(not is_within(path, OUTPUT_ROOT) for path in existing_files):
        raise RuntimeError("Unmanaged legacy episode root; separately scoped owner repair required")
    current = [path for path in existing_files if path.parent == year_dir]
    previous = [path for path in existing_files if path.parent != year_dir]
    if len(current) > 1 or len(previous) > 1:
        raise RuntimeError("Ambiguous episode copies; migration stopped")
    existing = previous[0] if previous else None
    preferred = year_dir / f"{safe_title_stem(title, page_id[:8])}.md"
    if existing and not current and preferred.exists():
        # A different managed source may reserve this title; an unrecognized file
        # may instead be an interrupted write, so never silently route around it.
        preferred_meta = parse_frontmatter(preferred.read_bytes().decode("utf-8"))
        managed_note_bytes(preferred, preferred_meta.get("episode_page_id", ""))
    target = current[0] if current else unique_target_path(year_dir, title, page_id)
    for path in existing_files:
        managed_note_bytes(path, page_id)
    check_managed_path(target, exists=False)
    if target.exists():
        target_meta = parse_frontmatter(target.read_text(encoding="utf-8"))
        if normalize_notion_id(target_meta.get("episode_page_id", "")) != normalize_notion_id(page_id):
            raise RuntimeError("Episode target identity conflict; refusing overwrite")
    if dry_run:
        print("DRY-RUN episode target")
    else:
        transcript_text = download_transcripts(transcript_files)
        body = build_body(api_key, normalize_notion_id(page_id), description, transcript_text)
        content = f"{frontmatter}\n\n{body}"
        if existing:
            migrate_episode(existing, target, page_id, content)
        else:
            write_verified_text(target, content)
        print("WROTE episode")
    return target


def parse_frontmatter(text: str) -> Dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return {}
    meta: Dict[str, str] = {}
    for line in parts[0].splitlines()[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta


def rebuild_index() -> Path:
    rows_by_podcast: Dict[str, List[Dict[str, str]]] = {}
    for path in sorted(OUTPUT_ROOT.rglob("*.md")):
        if path.name == "索引.md":
            continue
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
        if not meta:
            continue
        podcast_name = meta.get("podcast_name") or "未分类播客"
        row = {
            "listened_at": meta.get("listened_at", meta.get("published_at", "")),
            "title": meta.get("title", path.stem),
            "episode_url": meta.get("episode_url", ""),
            "duration_seconds": meta.get("duration_seconds", "0"),
            "rating": meta.get("rating", ""),
            "path": str(path.relative_to(OUTPUT_ROOT)),
        }
        rows_by_podcast.setdefault(podcast_name, []).append(row)

    generated_at = now_iso()
    lines = [
        "---",
        'source_system: "notion"',
        'source_type: "xiaoyuzhou_episode_index"',
        f'generated_at: "{generated_at}"',
        f"episode_count: {sum(len(rows) for rows in rows_by_podcast.values())}",
        f"podcast_count: {len(rows_by_podcast)}",
        "---",
        "",
        "# 小宇宙索引",
        "",
        f"> 本地已同步 {sum(len(rows) for rows in rows_by_podcast.values())} 期，覆盖 {len(rows_by_podcast)} 档播客。",
        "",
    ]
    for podcast_name in sorted(rows_by_podcast):
        rows = sorted(rows_by_podcast[podcast_name], key=lambda item: item["listened_at"])
        lines.append(f"## {podcast_name}")
        lines.append("")
        lines.append("| 发布时间 | 标题 | 时长(分) | 评分 | 文件 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in rows:
            duration_minutes = ""
            try:
                duration_minutes = str(round(int(row["duration_seconds"]) / 60, 1))
            except (TypeError, ValueError):
                duration_minutes = ""
            title_cell = row["title"]
            if row["episode_url"]:
                title_cell = f"[{title_cell}]({row['episode_url']})"
            path_cell = f"[{row['path']}]({row['path']})"
            listened_day = row["listened_at"][:10] if row["listened_at"] else ""
            lines.append(f"| {listened_day} | {title_cell} | {duration_minutes} | {row['rating']} | {path_cell} |")
        lines.append("")

    target = OUTPUT_ROOT / "索引.md"
    write_verified_text(target, "\n".join(lines).rstrip() + "\n")
    return target


def main() -> None:
    args = parse_args()
    scan_started_at = now()
    scan_complete = False
    page_ids = [normalize_notion_id(item) for item in args.page_id]
    pages_by_id: Dict[str, Dict[str, Any]] = {}
    if args.all_current or (not page_ids and not args.rebuild_index):
        api_key = load_notion_api_key()
        current_pages, scan_complete = current_episode_pages(
            api_key,
            full_sync=args.all_current,
            hours_back=args.hours_back,
            max_episodes=args.max_episodes,
            only_missing=args.only_missing,
            scan_started_at=scan_started_at,
        )
        for page in current_pages:
            page_id = normalize_notion_id(str(page.get("id") or ""))
            if not page_id:
                continue
            pages_by_id[page_id] = page
            page_ids.append(page_id)
    page_ids = list(dict.fromkeys(page_ids))
    if not page_ids and args.page_id and not args.rebuild_index:
        raise SystemExit("No episode pages found to sync")

    if page_ids:
        api_key = load_notion_api_key()
        podcast_cache: Dict[str, Dict[str, str]] = {}
        total = len(page_ids)
        for index, page_id in enumerate(page_ids, start=1):
            print(f"SYNC {index}/{total}", flush=True)
            sync_one(
                api_key,
                page_id,
                dry_run=args.dry_run,
                podcast_cache=podcast_cache,
                page=pages_by_id.get(page_id),
            )

    if not args.dry_run:
        index_path = rebuild_index()
        print("WROTE index")
        if scan_complete and not args.only_missing and not args.rebuild_index:
            state = read_state()
            state["last_sync_at"] = format_time(scan_started_at)
            if args.all_current:
                state["last_full_sync_at"] = state["last_sync_at"]
            write_state(state)
        else:
            print("SOURCE_WATERMARK_UNCHANGED: limited, targeted, missing-only, or index-only run", flush=True)


def run_cli() -> int:
    """Keep source payloads, private paths and signed URLs out of direct logs."""
    try:
        main()
    except Exception as exc:
        print(f"ERROR PODCAST_SYNC_FAILED {type(exc).__name__}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        if isinstance(exc.code, int) or exc.code is None:
            return int(exc.code or 0)
        print("ERROR PODCAST_SYNC_FAILED SystemExit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
