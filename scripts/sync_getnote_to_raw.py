#!/usr/bin/env python3
"""Sync GetNote notes into 艾迪宇宙 Raw/03 Get."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import http.client
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from source_sync_assets import AssetStore
from source_sync_store import IdentityIndex, source_id, attachment_manifest, json_text
from source_sync_store import write_if_changed as atomic_write
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Leading-date stripping — shared pattern with other sync scripts
# ---------------------------------------------------------------------------
LEADING_DATE_PREFIX_RE = re.compile(
    r"""^\s*
    (?:
        \d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?
        (?:\s*[Tt]\s*\d{1,2}:\d{2}(?::\d{2})?)?
        |\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?
        |\d{4}/\d{1,2}/\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?
        |\d{4}\.\d{1,2}\.\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?
        # MM-DD / MM/DD / MM.DD (with optional time)
        |\d{2}[-/.]\d{2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?
    )
    """,
    re.VERBOSE,
)


def clean_title(text: str) -> str:
    """Strip leading date/time prefix from a title string."""
    value = re.sub(r"\s+", " ", (text or "").strip())
    match = LEADING_DATE_PREFIX_RE.match(value)
    if match:
        remainder = value[match.end():].lstrip(" -—_:：·|#.")
        if remainder:
            value = remainder
    value = re.sub(r"\s+", " ", value).strip()
    return value


# ---------------------------------------------------------------------------


BASE_URL = "https://openapi.biji.com"
LIST_PATH = "/open/api/v1/resource/note/list"
DETAIL_PATH = "/open/api/v1/resource/note/detail"

ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
RAW_DIR = ROOT / "Raw" / "03 Get"
NOTES_DIR = RAW_DIR / "01 Notes"
INDEX_DIR = RAW_DIR / "02 Index"
STATE_DIR = RAW_DIR / "03 State"
VICKY_INDEX_PATH = INDEX_DIR / "给Vicky的新增入口.md"


def load_credentials() -> Tuple[str, str]:
    api_key = os.environ.get("GETNOTE_API_KEY") or os.environ.get("GET_API_KEY")
    client_id = os.environ.get("GETNOTE_CLIENT_ID") or os.environ.get("GET_CLIENT_ID")

    if api_key and client_id:
        return api_key, client_id

    missing = []
    if not api_key:
        missing.append("GETNOTE_API_KEY")
    if not client_id:
        missing.append("GETNOTE_CLIENT_ID")
    if missing:
        raise SystemExit("Missing required GetNote credentials: " + ", ".join(missing))

    return api_key, client_id


def json_loads_tolerant(text: str) -> Any:
    return json.JSONDecoder(strict=False).decode(text)


def api_get(path: str, params: Dict[str, str], api_key: str, client_id: str) -> Any:
    query = urllib.parse.urlencode(params)
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"

    headers = {
        "Authorization": api_key,
        "X-Client-ID": client_id,
        "User-Agent": "eddie-wiki-getnote-sync/1.0",
    }

    last_error: Optional[Exception] = None
    for attempt in range(5):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            data = json_loads_tolerant(body)
            if isinstance(data, dict) and data.get("success") is False:
                err = data.get("error") or {}
                code = err.get("code")
                if code == 10202 and attempt < 4:
                    retry_after = data.get("rate_limit", {}).get("retry_after", 10)
                    time.sleep(int(retry_after))
                    continue
                raise RuntimeError(f"GetNote API error {code}: {err.get('message') or err.get('reason')}")
            return data
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt < 4:
                retry_after = exc.headers.get("Retry-After")
                time.sleep(int(retry_after) if retry_after and retry_after.isdigit() else 10)
                continue
            if 500 <= exc.code < 600 and attempt < 4:
                time.sleep(5)
                continue
            raise
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.RemoteDisconnected,
            ConnectionError,
        ) as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(5)
                continue
            raise

    raise RuntimeError(f"GetNote request failed: {last_error}")


def fetch_note_list(api_key, client_id, max_pages=None, pagination="cursor"):
    return [note for _, batch, _ in fetch_note_pages(api_key, client_id, max_pages, pagination) for note in batch]


def fetch_note_pages(api_key, client_id, max_pages=None, pagination="cursor"):
    """Official cursor contract, plus explicit plugin since_id compatibility.

    Never use time order to infer completeness. Caps and cursor cycles fail closed.
    """
    cursor = None
    seen_cursors, seen_ids = set(), set()
    page = 0
    while True:
        params = {pagination: cursor} if cursor is not None else ({"since_id": "0"} if pagination == "since_id" else {})
        data = api_get(LIST_PATH, params, api_key, client_id)
        payload = data.get("data", data)
        batch = payload if isinstance(payload, list) else next((payload[k] for k in ("notes", "list", "records", "items") if k in payload), None)
        if not isinstance(batch, list) or any(not isinstance(n, dict) for n in batch):
            raise RuntimeError("invalid Get list payload")
        ids = [note_id_from(n) for n in batch]
        if len(ids) != len(set(ids)) or seen_ids.intersection(ids):
            raise RuntimeError("repeated Get list IDs; enumeration is incomplete")
        seen_ids.update(ids)
        page += 1
        if isinstance(payload, dict) and "has_more" in payload:
            if not isinstance(payload["has_more"], bool):
                raise RuntimeError("invalid has_more")
            more = payload["has_more"]
        elif pagination == "since_id":
            more = bool(batch)
        else:
            raise RuntimeError("official cursor response missing has_more")
        if more and not batch:
            raise RuntimeError("empty page claims more records")
        if max_pages and page >= max_pages and more:
            raise RuntimeError("page cap reached; refusing incomplete sync")
        yield page, batch, more
        if not more:
            break
        keys = ("since_id", "next_since_id", "cursor", "next_cursor") if pagination == "since_id" else ("cursor", "next_cursor")
        value = next((payload[k] for k in keys if isinstance(payload, dict) and payload.get(k) is not None), None)
        if value is None and pagination == "since_id":
            value = ids[-1]
        cursor = source_id(value)
        if cursor in seen_cursors:
            raise RuntimeError("Get cursor did not advance")
        seen_cursors.add(cursor)


def fetch_note_detail(note_id: str, api_key: str, client_id: str) -> Dict[str, Any]:
    data = api_get(DETAIL_PATH, {"id": note_id, "image_quality": "original"}, api_key, client_id)
    payload = data.get("data", data)
    return payload.get("note", payload)


def parse_time(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return parsed.astimezone(dt.timezone(dt.timedelta(hours=8)))


def note_timestamp(note: Dict[str, Any]) -> Optional[dt.datetime]:
    return parse_time(note.get("updated_at")) or parse_time(note.get("created_at"))


def safe_slug(text: str, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"[\\/:*?\"<>|#\[\]\n\r\t]+", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-. ")
    if not text:
        text = fallback
    return text[:80]


def tag_names(note: Dict[str, Any]) -> List[str]:
    tags = []
    for tag in note.get("tags") or []:
        if isinstance(tag, dict):
            name = tag.get("name")
        else:
            name = str(tag)
        if name:
            tags.append(str(name))
    return tags


def topic_names(note: Dict[str, Any]) -> List[str]:
    topics = []
    for topic in note.get("topics") or []:
        if isinstance(topic, dict):
            name = topic.get("name")
        else:
            name = str(topic)
        if name:
            topics.append(str(name))
    return topics


def yaml_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def yaml_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(yaml_scalar(v) for v in values) + "]"


def note_id_from(note: Dict[str, Any]) -> str:
    value = note.get("note_id") or note.get("id")
    if value is None:
        raise ValueError("note missing note_id/id")
    return source_id(value)


def output_path_for(note: Dict[str, Any], index=None) -> Path:
    """文件名不含日期；仅标题 + 冲突兜底（ID 尾缀）。日期信息保留在目录 YYYY/YYYY-MM/ 中。"""
    note_id = note_id_from(note)
    created = parse_time(note.get("created_at")) or parse_time(note.get("updated_at"))
    if created:
        year = f"{created.year:04d}"
        month = f"{created.year:04d}-{created.month:02d}"
    else:
        year = "unknown"
        month = "unknown"
    raw_title = note.get("title") or ""
    clean = clean_title(raw_title) or raw_title or note_id
    title = safe_slug(clean, note_id).encode("utf-8")[:160].decode("utf-8", errors="ignore")
    filename = f"{title}.md"
    base = RAW_DIR / "99 录音卡" if note.get("note_type") in {"recorder_audio", "recorder_flash_audio"} else NOTES_DIR
    target_dir = base / year / month
    index = index or IdentityIndex(RAW_DIR, "getnote", "note_id", boundary=ROOT)
    return index.path(note_id, target_dir / filename)


def render_note(note: Dict[str, Any], assets=None, path=None) -> str:
    note_id = note_id_from(note)
    raw_title = note.get("title") or ""
    title = clean_title(raw_title) or raw_title or f"GetNote {note_id}"
    content = note.get("content") or ""
    web_page = note.get("web_page") or {}
    attachments = note.get("attachments") or []
    audio = note.get("audio") or {}

    lines = [
        "---",
        "source_system: getnote",
        f"note_id: {yaml_scalar(note_id)}",
        f"title: {yaml_scalar(title)}",
        f"note_type: {yaml_scalar(note.get('note_type') or '')}",
        f"source: {yaml_scalar(note.get('source') or '')}",
        f"entry_type: {yaml_scalar(note.get('entry_type') or '')}",
        f"created_at: {yaml_scalar(note.get('created_at') or '')}",
        f"updated_at: {yaml_scalar(note.get('updated_at') or '')}",
        f"tags: {yaml_list(tag_names(note))}",
        f"topics: {yaml_list(topic_names(note))}",
        f"is_child_note: {str(bool(note.get('is_child_note'))).lower()}",
        f"parent_id: {yaml_scalar(str(note.get('parent_id') or ''))}",
        f"children_ids: {yaml_list([str(x) for x in note.get('children_ids') or []])}",
        f"web_url: {yaml_scalar(web_page.get('url') or '')}",
        f"share_id: {yaml_scalar(note.get('share_id') or '')}",
        "---",
        "",
        "## AI 总结" if note.get("note_type") in {"recorder_audio", "recorder_flash_audio"} else "## 正文",
        "",
    ]
    lines.append(content.strip() or "（空）")

    if web_page:
        lines.extend(["", "## 网页信息", ""])
        for key in ("url", "domain", "excerpt"):
            if web_page.get(key):
                lines.append(f"- {key}: {web_page.get(key)}")
        if web_page.get("content"):
            lines.extend(["", "### 网页原文", "", str(web_page.get("content")).strip()])

    if audio:
        lines.extend(["", "## 音频信息", ""])
        for key in ("duration", "play_url"):
            if audio.get(key):
                lines.append(f"- {key}: {audio.get(key)}")
        for key in ("transcript", "original"):
            if audio.get(key):
                lines.extend(["", f"### {key}", "", str(audio.get(key)).strip()])

    for key in ("timeline", "chapter_timeline", "quick_note"):
        value = note.get(key, audio.get(key))
        if value is not None:
            lines.extend(["", f"## {key}", "", json.dumps(value, ensure_ascii=False, indent=2)])

    if attachments:
        lines.extend(["", "## 附件", ""])
        for item in attachments:
            if not isinstance(item, dict):
                continue
            label = item.get("title") or item.get("type") or "attachment"
            url = item.get("original_url") or item.get("url") or ""
            lines.append(f"- {label}: {url}")

    if assets and path is not None:
        lines.extend(["", "## 本地附件", ""])
        for asset in assets:
            if asset.get("status") == "downloaded":
                relative = os.path.relpath(RAW_DIR / asset["path"], path.parent)
                prefix = "!" if asset["kind"] == "image" else ""
                lines.append(f"{prefix}[{asset['kind']}]({urllib.parse.quote(relative)})")
    lines.append("")
    return "\n".join(lines)


def write_if_changed(path: Path, content: str) -> bool:
    return atomic_write(path, content)


def _set_file_timestamps(path: Path, content: str) -> None:
    """Set file timestamps to match created_at in frontmatter."""
    if not content or "created_at:" not in content:
        return
    m = __import__("re").search(r"^created_at:\s*\"?([^\n\"]+)\"?", content, __import__("re").MULTILINE)
    if not m:
        return
    raw = m.group(1).strip().rstrip('"')
    try:
        dt_str = raw.replace("T", " ")
        if "+" in dt_str:
            dt_str = dt_str.split("+")[0]
        if "Z" in dt_str:
            dt_str = dt_str.replace("Z", "")
        parsed = __import__("time").strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
        ts = __import__("time").mktime(parsed)
    except (ValueError, __import__("time").error):
        return
    __import__("os").utime(path, (ts, ts))
    try:
        ds = f"{parsed.tm_mon:02d}/{parsed.tm_mday:02d}/{parsed.tm_year} {parsed.tm_hour:02d}:{parsed.tm_min:02d}:{parsed.tm_sec:02d}"
        __import__("subprocess").run(["SetFile", "-d", ds, "-m", ds, str(path)], capture_output=True, timeout=10)
    except Exception:
        pass


def render_index(records: List[Tuple[Dict[str, Any], Optional[Path]]]) -> str:
    rows = sorted(
        records,
        key=lambda item: item[0].get("created_at") or item[0].get("updated_at") or "",
        reverse=True,
    )
    lines = [
        "# Get 笔记同步索引",
        "",
        f"- 最近同步：{dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')}",
        f"- 笔记数量：{len(rows)}",
        "",
        "## 笔记",
        "",
    ]
    for note, path in rows:
        title = note.get("title") or note_id_from(note)
        created = note.get("created_at") or ""
        note_type = note.get("note_type") or ""
        note_id = note_id_from(note)
        if path is not None:
            rel = path.relative_to(RAW_DIR).as_posix()
            lines.append(f"- {created} · `{note_type}` · [{title}]({rel})")
        else:
            lines.append(f"- {created} · `{note_type}` · {title} · `{note_id}`")
    lines.append("")
    return "\n".join(lines)


def render_vicky_entry(records: List[Tuple[Dict[str, Any], Optional[Path]]]) -> str:
    rows = sorted(
        records,
        key=lambda item: item[0].get("updated_at") or item[0].get("created_at") or "",
        reverse=True,
    )
    lines = [
        "# 给 Vicky 的新增入口",
        "",
        "这个文件只提供新增材料的阅读入口，不展开正文，避免在索引层重复暴露内容。",
        "",
        f"- 最近同步：{dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')}",
        f"- 本次窗口内笔记数：{len(rows)}",
        "- 建议阅读顺序：先看这个文件，再打开对应 Raw 笔记，再回到稳定 Wiki 页面做综合理解。",
        "",
        "## 本次窗口内材料",
        "",
    ]
    for note, path in rows[:80]:
        title = note.get("title") or note_id_from(note)
        updated = note.get("updated_at") or note.get("created_at") or ""
        note_type = note.get("note_type") or ""
        if path is not None:
            rel = path.relative_to(RAW_DIR).as_posix()
            lines.append(f"- {updated} · `{note_type}` · [{title}]({rel})")
        else:
            lines.append(f"- {updated} · `{note_type}` · {title} · `{note_id_from(note)}`")
    if not rows:
        lines.append("- 本次窗口内没有新增或更新笔记。")
    lines.append("")
    return "\n".join(lines)


def render_manifest(records: List[Tuple[Dict[str, Any], Optional[Path]]]) -> str:
    payload = {
        "source_system": "getnote",
        "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "count": len(records),
        "notes": [
            {
                "note_id": note_id_from(note),
                "title": note.get("title") or "",
                "created_at": note.get("created_at") or "",
                "updated_at": note.get("updated_at") or "",
                "note_type": note.get("note_type") or "",
                "path": path.relative_to(ROOT).as_posix() if path is not None else "",
            }
            for note, path in sorted(records, key=lambda item: note_id_from(item[0]))
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def transcription_pending(note):
    return note.get("note_type") in {"recorder_audio", "recorder_flash_audio"} and not (note.get("audio") or {}).get("original")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile Get notes; explicit private VAULT_ROOT required")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--pagination", choices=("cursor", "since_id"), default="cursor")
    parser.add_argument("--hours-back", type=float, default=0, help="Deprecated; full reconciliation catches old edits and delayed transcripts")
    parser.add_argument("--full-sync", action="store_true")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--asset-max-bytes", type=int, default=25 * 1024 * 1024)
    parser.add_argument("--asset-run-max-bytes", type=int, default=200 * 1024 * 1024)
    parser.add_argument("--asset-max-count", type=int, default=128)
    args = parser.parse_args()
    if not os.environ.get("VAULT_ROOT"):
        raise RuntimeError("explicit private VAULT_ROOT required")
    api_key, client_id = load_credentials()
    # Enumerate and validate every detail before writing anything. No time cutoff:
    # upstream creation-order pages cannot prove an older note was not edited.
    summaries = fetch_note_list(api_key, client_id, args.max_pages, args.pagination)
    details = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(fetch_note_detail, note_id_from(n), api_key, client_id): note_id_from(n) for n in summaries}
        for future in concurrent.futures.as_completed(futures):
            try:
                detail = future.result()
            except Exception:
                for pending_future in futures:
                    pending_future.cancel()
                raise
            if note_id_from(detail) != futures[future] or not isinstance(detail.get("content"), str):
                raise RuntimeError("Get detail identity/content validation failed")
            details.append(detail)
    details.sort(key=note_id_from)
    index = IdentityIndex(RAW_DIR, "getnote", "note_id", boundary=ROOT)
    # Finish bounded asset acquisition before touching notes or run receipts.
    assets_by_id = {}
    if not args.dry_run and not args.metadata_only:
        store = AssetStore(RAW_DIR, max_file=args.asset_max_bytes, max_run=args.asset_run_max_bytes, max_assets=args.asset_max_count)
        for note in details:
            assets_by_id[note_id_from(note)] = store.materialize(note)
    changed = 0
    pending = []
    for note in details:
        nid = note_id_from(note)
        if transcription_pending(note):
            pending.append(nid)
        if args.dry_run or args.metadata_only:
            continue
        # Do not replace a previously complete recording with a transient pending response.
        import hashlib
        cache_id = hashlib.sha256(nid.encode()).hexdigest()
        cache_path = STATE_DIR / "raw-cache" / (cache_id + ".json")
        if transcription_pending(note) and cache_path.exists():
            previous = json.loads(cache_path.read_text(encoding="utf-8"))
            if (previous.get("audio") or {}).get("original"):
                continue
        path = output_path_for(note, index)
        changed += write_if_changed(path, render_note(note, assets_by_id[nid], path))
        # Audio stays remote; downloaded images/files and raw descriptors stay private.
        write_if_changed(cache_path, json_text(note))
        write_if_changed(STATE_DIR / "attachments" / (cache_id + ".json"), json_text(dict(attachment_manifest(note), assets=assets_by_id[nid])))
    if not args.dry_run and not args.metadata_only:
        write_if_changed(STATE_DIR / "pending.json", json_text({"source_system": "getnote", "pending_ids": pending}))
        # This receipt is committed with the data; it is NOT a published watermark.
        write_if_changed(STATE_DIR / "receipt.json", json_text({"source_system": "getnote", "enumerated": len(details), "complete": True}))
    print(json.dumps({"fetched": len(details), "written_or_changed": changed, "pending": len(pending), "dry_run": args.dry_run}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
