#!/usr/bin/env python3
"""Sync listened Xiaoyuzhou episodes from Notion into Raw/13 小宇宙."""

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

from sync_notion_to_raw import (
    get_database,
    paginate_database,
    TZ,
    parse_time,
    load_notion_api_key,
    notion_request,
    normalize_notion_id,
    render_blocks,
    render_frontmatter,
    retrieve_block_children,
    rich_text_plain_text,
    write_text,
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
    return parser.parse_args()


def fetch_page(api_key: str, page_id: str) -> Dict[str, Any]:
    return notion_request(api_key, "GET", f"/pages/{normalize_notion_id(page_id)}")


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def read_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def write_state(data: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if STATE_PATH.exists() and STATE_PATH.read_text(encoding="utf-8") == rendered:
        return
    STATE_PATH.write_text(rendered, encoding="utf-8")


def format_time(value: dt.datetime) -> str:
    return value.astimezone(TZ).isoformat(timespec="seconds")


def build_episode_filter(*, full_sync: bool, hours_back: int) -> Dict[str, Any]:
    if full_sync:
        return LISTENED_FILTER

    state = read_state()
    cutoff = parse_time(state.get("last_sync_at"))
    if cutoff is None:
        cutoff = now() - dt.timedelta(hours=hours_back)
    return {
        "and": [
            LISTENED_FILTER,
            {
                "timestamp": "last_edited_time",
                "last_edited_time": {"on_or_after": format_time(cutoff)},
            },
        ]
    }


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
) -> List[Dict[str, Any]]:
    episode_db = get_database(api_key, EPISODE_DATABASE_ID)
    episode_db_id = normalize_notion_id(str(episode_db.get("id") or EPISODE_DATABASE_ID))
    pages: List[Dict[str, Any]] = []
    filter_obj = build_episode_filter(full_sync=full_sync, hours_back=hours_back)
    scanned = 0
    for page in paginate_database(
        api_key,
        episode_db_id,
        filter_obj=filter_obj,
    ):
        scanned += 1
        if scanned == 1 or scanned % 50 == 0:
            print(f"SCANNED {scanned} listened episode candidates", flush=True)
        if page.get("archived"):
            continue
        page_id = normalize_notion_id(str(page.get("id") or ""))
        if not page_id:
            continue
        if only_missing:
            existing = find_existing_file_across_roots(page_id)
            if existing and is_within(existing, OUTPUT_ROOT):
                continue
        pages.append(page)
        if max_episodes > 0 and len(pages) >= max_episodes:
            break
    print(f"FOUND {len(pages)} listened episodes to sync", flush=True)
    return pages


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
    if prop.get("type") != "files":
        return []
    return [item for item in (prop.get("files") or []) if isinstance(item, dict)]


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


def download_text(url: str) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace").strip()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(3)
                continue
            raise RuntimeError(f"Failed to download transcript file: {exc}") from exc
    raise RuntimeError(f"Failed to download transcript file: {last_error}")


def build_body(api_key: str, page_id: str, description: str, transcript_text: str) -> str:
    blocks = retrieve_block_children(api_key, page_id)
    rendered = render_blocks(api_key, blocks)
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
    body = "\n".join(parts).strip()
    return body + "\n" if body else "\n"


def sync_one(
    api_key: str,
    page_id: str,
    dry_run: bool,
    podcast_cache: Dict[str, Dict[str, str]],
    page: Optional[Dict[str, Any]] = None,
) -> Path:
    page = page or fetch_page(api_key, page_id)
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
    transcript_files = prop_files(properties.get("逐字稿") or {})
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
            "rating": rating,
            "synced_at": now_iso(),
        }
    )
    existing = find_existing_file_across_roots(page_id)
    if existing and is_within(existing, OUTPUT_ROOT):
        target = existing
    else:
        target = unique_target_path(year_dir, title, page_id)
    if dry_run:
        print(f"DRY-RUN {normalize_notion_id(page_id)} -> {target.relative_to(ROOT)}")
    else:
        transcript_text = ""
        if transcript_files:
            transcript_file = transcript_files[0]
            file_obj = transcript_file.get(transcript_file.get("type") or "") or {}
            transcript_url = file_obj.get("url") or ""
            if transcript_url:
                transcript_text = download_text(transcript_url)
        body = build_body(api_key, normalize_notion_id(page_id), description, transcript_text)
        content = f"{frontmatter}\n\n{body}"
        if existing and existing != target:
            existing.unlink(missing_ok=True)
        write_text(target, content)
        print(f"WROTE {target.relative_to(ROOT)}")
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
    write_text(target, "\n".join(lines).rstrip() + "\n")
    return target


def main() -> None:
    args = parse_args()
    page_ids = [normalize_notion_id(item) for item in args.page_id]
    pages_by_id: Dict[str, Dict[str, Any]] = {}
    if args.all_current or (not page_ids and not args.rebuild_index):
        api_key = load_notion_api_key()
        current_pages = current_episode_pages(
            api_key,
            full_sync=args.all_current,
            hours_back=args.hours_back,
            max_episodes=args.max_episodes,
            only_missing=args.only_missing,
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
            print(f"SYNC {index}/{total} {page_id}", flush=True)
            sync_one(
                api_key,
                page_id,
                dry_run=args.dry_run,
                podcast_cache=podcast_cache,
                page=pages_by_id.get(page_id),
            )

    if not args.dry_run:
        index_path = rebuild_index()
        print(f"WROTE {index_path.relative_to(ROOT)}")
        state = read_state()
        state["last_sync_at"] = now_iso()
        if args.all_current:
            state["last_full_sync_at"] = state["last_sync_at"]
        write_state(state)


if __name__ == "__main__":
    main()
