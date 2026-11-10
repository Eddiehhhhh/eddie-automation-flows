#!/usr/bin/env python3
"""Sync flomo memos into 艾迪宇宙 Raw/01 Flomo."""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


MCP_ENDPOINT = "https://flomoapp.com/mcp"
ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
RAW_DIR = ROOT / "Raw" / "01 Flomo"
MEMOS_DIR = RAW_DIR
INDEX_DIR = RAW_DIR / "03 Index"
LOG_DIR = RAW_DIR / "04 Import Logs"
STATE_PATH = INDEX_DIR / "同步状态.json"
RECENT_INDEX_PATH = INDEX_DIR / "最近同步.md"

TZ = dt.timezone(dt.timedelta(hours=8))
DEFAULT_WINDOW_DAYS = int(os.environ.get("FLOMO_SYNC_DAYS_BACK", "7"))
DEFAULT_OVERLAP_DAYS = int(os.environ.get("FLOMO_SYNC_OVERLAP_DAYS", "2"))
DEFAULT_LIMIT = int(os.environ.get("FLOMO_SYNC_LIMIT", "1000"))


def last_sync_from_state(state: Dict[str, Any]) -> Optional[dt.datetime]:
    return parse_time(state.get("last_sync_at") or state.get("synced_at"))


def load_token() -> str:
    token = os.environ.get("FLOMO_TOKEN")
    if token:
        return token

    services_path = Path.home() / ".codex" / "secrets" / "services.json"
    if services_path.exists():
        with services_path.open("r", encoding="utf-8") as f:
            services = json.load(f)
        token = services.get("flomo_mcp", {}).get("auth", {}).get("token")
        if token:
            return str(token)

    raise SystemExit("Missing required flomo token: FLOMO_TOKEN")


def parse_time(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def json_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    _set_file_timestamps(path, content)
    return True


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


def safe_slug(text: str, fallback: str) -> str:
    text = re.sub(r"[\u0000-\u001f\\/:*?\"<>|]+", "-", (text or "").strip())
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-. ")
    if not text:
        text = fallback
    return text[:90]


def truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    trimmed = encoded[:max_bytes]
    while trimmed:
        try:
            return trimmed.decode("utf-8").rstrip("-. ")
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]
    return ""


def strip_tags(line: str) -> str:
    text = line.strip()
    text = re.sub(r"(?:^|\s)#[^\s#]+(?:/[^\s#]+)*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


LEADING_DATE_PREFIX_RE = re.compile(
    r"""^\s*
    (?:
        # YYYY-MM-DD/YYYY/MM/DD/YYYY.MM.DD/YYYY年MM月DD日 (with optional time)
        \d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?
        (?:\s*[Tt]\s*\d{1,2}:\d{2}(?::\d{2})?)?
        |\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?
        |\d{4}/\d{1,2}/\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?
        |\d{4}\.\d{1,2}\.\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?
        # MM-DD / MM/DD / MM.DD (with optional time) — added 2026-05-26
        |\d{2}[-/.]\d{2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?
    )
    """,
    re.VERBOSE,
)


def clean_title(text: str) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    match = LEADING_DATE_PREFIX_RE.match(value)
    if match:
        remainder = value[match.end():].lstrip(" -—_:：·|#.")
        if remainder:
            value = remainder
    if "#" in value:
        left = value.split("#", 1)[0].strip()
        if left:
            value = left
    value = re.sub(r"\s+", " ", value).strip()
    return value


def memo_title(memo: Dict[str, Any]) -> str:
    content = str(memo.get("content") or "").splitlines()
    memo_id_value = str(memo.get("id") or "memo")
    for line in content:
        candidate = clean_title(strip_tags(line))
        if candidate and not candidate.startswith("#"):
            return candidate[:90]
    return f"flomo memo {memo_id_value}"


def clean_body_text(content: str) -> str:
    lines: List[str] = []
    for raw_line in str(content or "").splitlines():
        cleaned = strip_tags(raw_line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip()


def memo_id(memo: Dict[str, Any]) -> str:
    value = memo.get("id")
    if value is None:
        raise ValueError("memo missing id")
    return str(value)


def decode_mcp_body(body: str) -> Dict[str, Any]:
    stripped = body.lstrip()
    if stripped.startswith("{"):
        return json.loads(stripped)

    data_lines: List[str] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            data_lines.append(line[6:])

    if not data_lines:
        raise ValueError(f"Unable to parse MCP response: {body[:200]}")

    parsed = json.loads("\n".join(data_lines))
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("MCP response is not a JSON object")


def rpc_call(token: str, method: str, params: Dict[str, Any], request_id: int) -> Dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    req = urllib.request.Request(
        MCP_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "eddie-wiki-flomo-sync/1.0",
        },
    )

    last_error: Optional[Exception] = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            message = decode_mcp_body(body)
            if isinstance(message, dict):
                return message
            raise RuntimeError("Unexpected MCP response payload")
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            http.client.RemoteDisconnected,
            ConnectionError,
        ) as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(5)
                continue
            raise RuntimeError(f"flomo MCP request failed: {exc}") from exc

    raise RuntimeError(f"flomo MCP request failed: {last_error}")


def initialize_client(token: str) -> None:
    response = rpc_call(
        token,
        "initialize",
        {
            "clientInfo": {
                "name": "eddie-wiki-flomo-sync",
                "version": "1.0.0",
            },
            "capabilities": {},
        },
        request_id=1,
    )
    server = response.get("result", {}).get("serverInfo", {})
    if server.get("name") not in {"flomo-mcp", "flomo_mcp"}:
        raise RuntimeError(f"Unexpected flomo MCP server: {server.get('name')!r}")


def call_tool(token: str, tool_name: str, arguments: Dict[str, Any], request_id: int) -> Dict[str, Any]:
    response = rpc_call(
        token,
        "tools/call",
        {
            "name": tool_name,
            "arguments": arguments,
        },
        request_id=request_id,
    )
    result = response.get("result", {})
    content = result.get("content") or []
    if content and isinstance(content[0], dict):
        if "json" in content[0]:
            payload = content[0]["json"]
            if isinstance(payload, dict):
                return payload
            return {"value": payload}
        if "text" in content[0]:
            text = content[0]["text"]
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
    if isinstance(result, dict):
        return result
    raise RuntimeError(f"Unexpected tool output for {tool_name}")


def search_memos_for_day(token: str, day: dt.date, limit: int) -> List[Dict[str, Any]]:
    payload = call_tool(
        token,
        "memo_search",
        {
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "limit": limit,
        },
        request_id=1000 + day.toordinal(),
    )
    memos = payload.get("memos")
    if memos is None:
        return []
    if not isinstance(memos, list):
        raise RuntimeError("memo_search returned an unexpected payload shape")
    return [item for item in memos if isinstance(item, dict)]


def date_range(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    current = start
    while current <= end:
        yield current
        current += dt.timedelta(days=1)


def choose_window(args: argparse.Namespace, state: Dict[str, Any]) -> Tuple[dt.date, dt.date]:
    now = dt.datetime.now(TZ)
    end_day = dt.date.fromisoformat(args.end_date) if args.end_date else now.date()

    if args.start_date:
        start_day = dt.date.fromisoformat(args.start_date)
    else:
        last_sync = last_sync_from_state(state) if state else None
        if last_sync is not None:
            start_anchor = last_sync - dt.timedelta(days=max(0, args.overlap_days))
        else:
            start_anchor = now - dt.timedelta(days=max(1, args.window_days))
        start_day = start_anchor.date()

    if start_day > end_day:
        raise SystemExit("start date must be earlier than or equal to end date")

    return start_day, end_day


def memo_path_for(memo: Dict[str, Any]) -> Path:
    """文件名不含日期；仅标题 + 冲突兜底（memo_id 尾缀）。日期信息保留在目录 YYYY/YYYY-MM/ 中。"""
    mid = memo_id(memo)
    created = parse_time(memo.get("created_at")) or parse_time(memo.get("updated_at"))
    if created is None:
        year = "unknown"
        month = "unknown"
    else:
        year = f"{created.year:04d}"
        month = f"{created.year:04d}-{created.month:02d}"
    title = truncate_utf8_bytes(safe_slug(memo_title(memo), mid), 160) or mid
    filename = f"{title}.md"
    target_dir = MEMOS_DIR / year / month
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = target_dir / filename
    if not candidate.exists():
        return candidate
    # 同目录下标题冲突 → memo_id 尾缀兜底
    return target_dir / f"{title}-{mid}.md"


def render_memo(memo: Dict[str, Any]) -> str:
    memo_id_value = memo_id(memo)
    title = memo_title(memo)
    content = clean_body_text(memo.get("content") or "")
    tags = [str(item) for item in memo.get("tags") or [] if item]
    linked_memos = [str(item) for item in memo.get("linked_memos") or [] if item]

    lines = [
        "---",
        "source_system: flomo",
        f"memo_id: {json_scalar(memo_id_value)}",
        f"title: {json_scalar(title)}",
        f"created_at: {json_scalar(memo.get('created_at') or '')}",
        f"updated_at: {json_scalar(memo.get('updated_at') or '')}",
        f"from: {json_scalar(memo.get('from') or '')}",
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
        f"has_image: {str(bool(memo.get('has_image'))).lower()}",
        f"has_link: {str(bool(memo.get('has_link'))).lower()}",
        f"has_voice: {str(bool(memo.get('has_voice'))).lower()}",
        f"linked_memos: {json.dumps(linked_memos, ensure_ascii=False)}",
        f"word_count: {int(memo.get('word_count') or 0)}",
        "---",
        "",
    ]

    if content:
        lines.extend(["", content])
    lines.append("")
    return "\n".join(lines)


def render_recent_index(records: List[Tuple[Dict[str, Any], Path]], start_day: dt.date, end_day: dt.date) -> str:
    rows = sorted(
        records,
        key=lambda item: parse_time(item[0].get("updated_at")) or parse_time(item[0].get("created_at")) or dt.datetime.min.replace(tzinfo=TZ),
        reverse=True,
    )
    lines = [
        "# flomo 最近同步",
        "",
        f"- 最近同步时间：{dt.datetime.now(TZ).isoformat(timespec='seconds')}",
        f"- 窗口：{start_day.isoformat()} → {end_day.isoformat()}",
        f"- 本次笔记数：{len(rows)}",
        "",
        "## 笔记",
        "",
    ]
    if not rows:
        lines.append("- 本次窗口内没有新增或更新 memo。")
    else:
        for memo, path in rows[:120]:
            rel = path.relative_to(RAW_DIR).as_posix()
            title = memo_title(memo)
            updated = memo.get("updated_at") or memo.get("created_at") or ""
            tags = ", ".join([str(item) for item in memo.get("tags") or [] if item])
            lines.append(f"- {updated} · [{title}]({rel})")
            if tags:
                lines.append(f"  - 标签：{tags}")
    lines.append("")
    return "\n".join(lines)


def render_state(records: List[Tuple[Dict[str, Any], Path]], start_day: dt.date, end_day: dt.date) -> str:
    synced_at = dt.datetime.now(TZ)
    latest_updated_at = max(
        (
            parse_time(memo.get("updated_at")) or parse_time(memo.get("created_at")) or synced_at
            for memo, _ in records
        ),
        default=synced_at,
    )
    payload = {
        "state_version": 2,
        "source_system": "flomo",
        "last_sync_at": synced_at.isoformat(timespec="seconds"),
        "synced_at": synced_at.isoformat(timespec="seconds"),
        "window_start": start_day.isoformat(),
        "window_end": end_day.isoformat(),
        "count": len(records),
        "memo_ids": sorted({memo_id(memo) for memo, _ in records}),
        "latest_updated_at": latest_updated_at.isoformat(timespec="seconds"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_log(records: List[Tuple[Dict[str, Any], Path]], start_day: dt.date, end_day: dt.date) -> str:
    synced_at = dt.datetime.now(TZ).isoformat(timespec="seconds")
    rows = sorted(
        records,
        key=lambda item: parse_time(item[0].get("updated_at")) or parse_time(item[0].get("created_at")) or dt.datetime.min.replace(tzinfo=TZ),
        reverse=True,
    )
    lines = [
        "# flomo GitHub 同步记录",
        "",
        f"- 同步时间：{synced_at}",
        f"- 窗口：{start_day.isoformat()} → {end_day.isoformat()}",
        f"- 同步 memo 数：{len(rows)}",
        "",
        "## 新增或更新 memo",
        "",
    ]
    if not rows:
        lines.append("- 本次窗口内没有新增或更新 memo。")
    else:
        for memo, path in rows[:120]:
            rel = path.relative_to(RAW_DIR).as_posix()
            title = memo_title(memo)
            updated = memo.get("updated_at") or memo.get("created_at") or ""
            lines.append(f"- {updated} · [{title}]({rel})")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync flomo memos into Raw/01 Flomo")
    parser.add_argument("--start-date", default=None, help="Inclusive sync start date in YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="Inclusive sync end date in YYYY-MM-DD")
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help="Fallback lookback window when no state exists",
    )
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=DEFAULT_OVERLAP_DAYS,
        help="Re-read this many days before the last sync to catch edits",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Maximum memos to request per day from flomo",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report without writing files")
    args = parser.parse_args()

    token = load_token()
    initialize_client(token)

    state: Dict[str, Any] = {}
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as f:
            try:
                state = json.load(f)
            except json.JSONDecodeError:
                state = {}

    start_day, end_day = choose_window(args, state)
    previous_last_sync = last_sync_from_state(state)

    memos_by_id: Dict[str, Dict[str, Any]] = {}
    for day in date_range(start_day, end_day):
        batch = search_memos_for_day(token, day, args.limit)
        print(f"day={day.isoformat()} memos={len(batch)}", file=sys.stderr, flush=True)
        for memo in batch:
            current_id = memo_id(memo)
            current_updated = parse_time(memo.get("updated_at")) or parse_time(memo.get("created_at")) or dt.datetime.min.replace(tzinfo=TZ)
            existing = memos_by_id.get(current_id)
            if existing is None:
                memos_by_id[current_id] = memo
                continue
            existing_updated = parse_time(existing.get("updated_at")) or parse_time(existing.get("created_at")) or dt.datetime.min.replace(tzinfo=TZ)
            if current_updated >= existing_updated:
                memos_by_id[current_id] = memo

    records: List[Tuple[Dict[str, Any], Path]] = []
    changed = 0
    for memo in sorted(
        memos_by_id.values(),
        key=lambda item: parse_time(item.get("updated_at")) or parse_time(item.get("created_at")) or dt.datetime.min.replace(tzinfo=TZ),
    ):
        path = memo_path_for(memo)
        records.append((memo, path))
        if not args.dry_run and write_if_changed(path, render_memo(memo)):
            changed += 1

    if not args.dry_run:
        write_if_changed(RECENT_INDEX_PATH, render_recent_index(records, start_day, end_day))
        write_if_changed(STATE_PATH, render_state(records, start_day, end_day))
        write_if_changed(LOG_DIR / f"{dt.datetime.now(TZ).strftime('%Y-%m-%d')}-flomo-sync.md", render_log(records, start_day, end_day))

    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "previous_last_sync_at": previous_last_sync.isoformat(timespec="seconds") if previous_last_sync else None,
                "window_start": start_day.isoformat(),
                "window_end": end_day.isoformat(),
                "fetched": len(memos_by_id),
                "written_or_changed": 0 if args.dry_run else changed,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
