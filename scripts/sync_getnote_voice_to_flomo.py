#!/usr/bin/env python3
"""Sync selected GetNote voice notes into flomo.

This flow is intentionally narrow:
- only ordinary GetNote audio notes are candidates;
- recorder-card notes and daily-summary notes are skipped;
- GetNote system/AI tags are not copied;
- text after "添加标签" is matched to existing flomo tags.
- every created memo gets the fixed flomo tag "来源/get笔记".
"""

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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


GETNOTE_BASE_URL = "https://openapi.biji.com"
GETNOTE_LIST_PATH = "/open/api/v1/resource/note/list"
GETNOTE_DETAIL_PATH = "/open/api/v1/resource/note/detail"
FLOMO_MCP_ENDPOINT = "https://flomoapp.com/mcp"

ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
STATE_DIR = ROOT / ".github" / "state"
STATE_PATH = STATE_DIR / "getnote-voice-flomo-sync.json"

TZ = dt.timezone(dt.timedelta(hours=8))
DEFAULT_HOURS_BACK = float(os.environ.get("GETNOTE_VOICE_FLOMO_HOURS", "36"))
DEFAULT_MAX_NOTES = int(os.environ.get("GETNOTE_VOICE_FLOMO_LIMIT", "80"))
SOURCE_TAG = "来源/get笔记"

RECORDER_CARD_TAGS = {"录音卡笔记", "录音卡"}
VOICE_SYSTEM_TAGS = {"录音笔记"}
DAILY_SUMMARY_TAGS = {
    "每日总结",
    "日常总结",
    "工作总结",
    "日报",
    "状态记录",
    "心情记录",
    "情绪记录",
}
DAILY_TITLE_RE = re.compile(
    r"(每日总结|日常总结|今日总结|今天总结|今日状态|今日主要事项|今天做了什么|"
    r"\d{4}年\d{1,2}月\d{1,2}日每日总结)"
)
TAG_DIRECTIVE_MARKER = "添加标签"
TRAILING_HASH_TAG_LINE_RE = re.compile(r"^\s*(#[^\s#]+(?:\s+#[^\s#]+)*)\s*$")
TRAILING_LABEL_LINE_RE = re.compile(r"^\s*(?:标签|tags)\s*[:：].*$", re.IGNORECASE)


def parse_time(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    cleaned = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def json_loads_tolerant(text: str) -> Any:
    return json.JSONDecoder(strict=False).decode(text)


def load_getnote_credentials() -> Tuple[str, str]:
    api_key = os.environ.get("GETNOTE_API_KEY") or os.environ.get("GET_API_KEY")
    client_id = os.environ.get("GETNOTE_CLIENT_ID") or os.environ.get("GET_CLIENT_ID")

    config_path = Path.home() / ".openclaw" / "openclaw.json"
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            config = json.load(f)
        getnote = config.get("skills", {}).get("entries", {}).get("getnote", {})
        api_key = api_key or getnote.get("apiKey")
        client_id = client_id or getnote.get("env", {}).get("GETNOTE_CLIENT_ID")

    missing = []
    if not api_key:
        missing.append("GETNOTE_API_KEY")
    if not client_id:
        missing.append("GETNOTE_CLIENT_ID")
    if missing:
        raise SystemExit("Missing required GetNote credentials: " + ", ".join(missing))

    return str(api_key), str(client_id)


def load_flomo_token() -> str:
    token = os.environ.get("FLOMO_TOKEN") or os.environ.get("FLOMO_MCP_TOKEN")
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


def getnote_get(path: str, params: Dict[str, str], api_key: str, client_id: str) -> Any:
    query = urllib.parse.urlencode(params)
    url = f"{GETNOTE_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    headers = {
        "Authorization": api_key,
        "X-Client-ID": client_id,
        "User-Agent": "eddie-wiki-getnote-voice-flomo/1.0",
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


def fetch_note_pages(
    api_key: str, client_id: str, max_pages: Optional[int]
) -> Iterable[Tuple[int, List[Dict[str, Any]], bool]]:
    cursor: Optional[str] = None
    page = 0

    while True:
        params = {}
        if cursor:
            params["cursor"] = cursor
        data = getnote_get(GETNOTE_LIST_PATH, params, api_key, client_id)
        payload = data.get("data", data)
        batch = payload.get("notes", [])
        page += 1
        yield page, [item for item in batch if isinstance(item, dict)], bool(payload.get("has_more"))

        if max_pages and page >= max_pages:
            break
        if not payload.get("has_more"):
            break
        next_cursor = payload.get("cursor")
        if not next_cursor or str(next_cursor) == str(cursor):
            break
        cursor = str(next_cursor)


def fetch_note_detail(note_id: str, api_key: str, client_id: str) -> Dict[str, Any]:
    data = getnote_get(GETNOTE_DETAIL_PATH, {"id": note_id, "image_quality": "original"}, api_key, client_id)
    payload = data.get("data", data)
    note = payload.get("note", payload)
    if not isinstance(note, dict):
        raise RuntimeError("GetNote detail returned an unexpected payload shape")
    return note


def note_id(note: Dict[str, Any]) -> str:
    value = note.get("note_id") or note.get("id")
    if value is None:
        raise ValueError("note missing note_id/id")
    return str(value)


def note_time(note: Dict[str, Any]) -> Optional[dt.datetime]:
    return parse_time(note.get("updated_at")) or parse_time(note.get("created_at"))


def tag_items(note: Dict[str, Any]) -> List[Dict[str, str]]:
    items = []
    for item in note.get("tags") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            tag_type = str(item.get("type") or "").strip()
        else:
            name = str(item).strip()
            tag_type = ""
        if name:
            items.append({"name": name, "type": tag_type})
    return items


def tag_names(note: Dict[str, Any]) -> List[str]:
    return [item["name"] for item in tag_items(note)]


def is_recorder_card(note: Dict[str, Any]) -> bool:
    note_type = str(note.get("note_type") or "").lower()
    if note_type == "recorder_audio":
        return True
    return any(name in RECORDER_CARD_TAGS for name in tag_names(note))


def is_ordinary_voice_note(note: Dict[str, Any]) -> bool:
    note_type = str(note.get("note_type") or "").lower()
    return note_type == "audio"


def is_daily_summary(note: Dict[str, Any]) -> bool:
    title = str(note.get("title") or "")
    if DAILY_TITLE_RE.search(title):
        return True

    names = set(tag_names(note))
    if names.intersection({"每日总结", "日常总结", "状态记录"}):
        return True

    # Avoid syncing the existing daily-summary flow, but do not classify every
    # "日常记录" memo as a daily summary.
    if names.intersection(DAILY_SUMMARY_TAGS) and re.search(r"(今日|今天|每日|日常|总结|评分|能量|睡眠)", title):
        return True

    content = source_text(note)
    if re.search(r"每日总结|今日总结|今天评分|今日主要事项", content[:240]):
        return True
    return False


def is_candidate(note: Dict[str, Any]) -> Tuple[bool, str]:
    if is_recorder_card(note):
        return False, "recorder_card"
    if not is_ordinary_voice_note(note):
        return False, "not_audio"
    if is_daily_summary(note):
        return False, "daily_summary"
    return True, "candidate"


def source_text(note: Dict[str, Any]) -> str:
    audio = note.get("audio") or {}
    if isinstance(audio, dict):
        original = str(audio.get("original") or audio.get("transcript") or "").strip()
        if original:
            return original
    return str(note.get("content") or "").strip()


def strip_getnote_generated_tag_text(text: str) -> str:
    lines = text.strip().splitlines()
    while lines:
        current = lines[-1].strip()
        if not current:
            lines.pop()
            continue
        if TRAILING_HASH_TAG_LINE_RE.match(current) or TRAILING_LABEL_LINE_RE.match(current):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()


def split_tag_tokens(text: str) -> List[str]:
    cleaned = text.strip()
    cleaned = re.sub(r"[。！？!?；;]+$", "", cleaned).strip()
    cleaned = cleaned.replace("#", " ")
    parts = re.split(r"[,，、。.!！?？；;\s]+|(?:和|以及|还有)", cleaned)
    tokens = []
    seen = set()
    for part in parts:
        token = part.strip(" \t\r\n:：，,。.!！?？；;\"'“”‘’")
        if not token or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def extract_requested_tags(text: str) -> Tuple[str, List[str]]:
    cleaned = text.strip()
    marker_index = cleaned.rfind(TAG_DIRECTIVE_MARKER)
    if marker_index < 0:
        return cleaned, []
    body = cleaned[:marker_index].strip()
    tag_text = cleaned[marker_index + len(TAG_DIRECTIVE_MARKER):].strip()
    tag_text = tag_text.lstrip(" \t\r\n:：，,。.!！?？；;\"'“”‘’")
    requested = split_tag_tokens(tag_text)
    return body, requested


def decode_mcp_body(body: str) -> Dict[str, Any]:
    stripped = body.lstrip()
    if stripped.startswith("{"):
        return json.loads(stripped)

    data_lines = []
    for line in body.splitlines():
        if line.startswith("data: "):
            data_lines.append(line[6:])
    if not data_lines:
        raise ValueError(f"Unable to parse MCP response: {body[:200]}")
    parsed = json.loads("\n".join(data_lines))
    if not isinstance(parsed, dict):
        raise ValueError("MCP response is not a JSON object")
    return parsed


def rpc_call(token: str, method: str, params: Dict[str, Any], request_id: int) -> Dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    req = urllib.request.Request(
        FLOMO_MCP_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "eddie-wiki-getnote-voice-flomo/1.0",
        },
    )

    last_error: Optional[Exception] = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return decode_mcp_body(resp.read().decode("utf-8", errors="replace"))
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


def initialize_flomo(token: str) -> None:
    response = rpc_call(
        token,
        "initialize",
        {
            "clientInfo": {
                "name": "eddie-wiki-getnote-voice-flomo",
                "version": "1.0.0",
            },
            "capabilities": {},
        },
        request_id=1,
    )
    server = response.get("result", {}).get("serverInfo", {})
    if server.get("name") not in {"flomo-mcp", "flomo_mcp"}:
        raise RuntimeError(f"Unexpected flomo MCP server: {server.get('name')!r}")


def call_flomo_tool(token: str, tool_name: str, arguments: Dict[str, Any], request_id: int) -> Dict[str, Any]:
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
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content") or []
        if content and isinstance(content[0], dict):
            if "json" in content[0] and isinstance(content[0]["json"], dict):
                return content[0]["json"]
            if "text" in content[0]:
                try:
                    parsed = json.loads(content[0]["text"])
                except json.JSONDecodeError:
                    return {"text": content[0]["text"]}
                if isinstance(parsed, dict):
                    return parsed
                return {"value": parsed}
        return result
    raise RuntimeError(f"Unexpected tool output for {tool_name}")


def choose_flomo_tag(requested: str, candidates: Sequence[Dict[str, Any]]) -> Optional[str]:
    names = [str(item.get("name") or "").strip() for item in candidates if str(item.get("name") or "").strip()]
    if not names:
        return None
    requested_clean = requested.strip().lstrip("#")
    if requested_clean in names:
        return requested_clean

    for name in names:
        if name.split("/")[-1] == requested_clean:
            return name
    for name in names:
        if requested_clean in name:
            return name
    return names[0]


def resolve_flomo_tags(token: str, requested_tags: Sequence[str]) -> Tuple[List[str], List[str]]:
    resolved = []
    unresolved = []
    seen = set()
    for offset, requested in enumerate(requested_tags):
        payload = call_flomo_tool(
            token,
            "tag_search",
            {"keywords": requested, "limit": 10},
            request_id=200 + offset,
        )
        tag = choose_flomo_tag(requested, payload.get("tags") or [])
        if tag and tag not in seen:
            seen.add(tag)
            resolved.append(tag)
        elif not tag:
            unresolved.append(requested)
    return resolved, unresolved


def render_flomo_content(body: str, tags: Sequence[str]) -> str:
    content = body.strip()
    output_tags = []
    seen = set()
    for tag in [*tags, SOURCE_TAG]:
        if tag and tag not in seen:
            seen.add(tag)
            output_tags.append(tag)
    if output_tags:
        tag_line = " ".join(f"#{tag}" for tag in output_tags)
        content = f"{content}\n\n{tag_line}" if content else tag_line
    return content.strip()


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"state_version": 1, "processed": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except json.JSONDecodeError:
        return {"state_version": 1, "processed": {}}
    if not isinstance(state.get("processed"), dict):
        state["processed"] = {}
    return state


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def should_stop_page(notes: Sequence[Dict[str, Any]], cutoff: Optional[dt.datetime]) -> bool:
    if cutoff is None or not notes:
        return False
    timestamps = [note_time(item) for item in notes]
    timestamps = [item for item in timestamps if item is not None]
    return bool(timestamps) and max(timestamps) < cutoff


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync ordinary GetNote voice notes into flomo")
    parser.add_argument("--hours-back", type=float, default=DEFAULT_HOURS_BACK)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--max-notes", type=int, default=DEFAULT_MAX_NOTES)
    parser.add_argument("--state-path", default=str(STATE_PATH))
    parser.add_argument("--dry-run", action="store_true", help="Inspect candidates without creating flomo memos")
    args = parser.parse_args()

    state_path = Path(args.state_path)
    state = load_state(state_path)
    processed: Dict[str, Any] = state["processed"]

    cutoff = None
    if args.hours_back > 0:
        cutoff = dt.datetime.now(TZ) - dt.timedelta(hours=args.hours_back)

    getnote_api_key, getnote_client_id = load_getnote_credentials()
    flomo_token = load_flomo_token()
    initialize_flomo(flomo_token)

    stats = {
        "dry_run": args.dry_run,
        "scanned": 0,
        "candidate": 0,
        "created": 0,
        "already_processed": 0,
        "skipped": {},
        "requested_tags": 0,
        "resolved_tags": 0,
        "unresolved_tags": 0,
    }

    now = dt.datetime.now(TZ).isoformat(timespec="seconds")
    handled = 0
    for page, list_notes, has_more in fetch_note_pages(getnote_api_key, getnote_client_id, args.max_pages):
        print(f"page={page} notes={len(list_notes)} has_more={str(has_more).lower()}", file=sys.stderr, flush=True)
        for listed_note in list_notes:
            stats["scanned"] += 1
            current_id = note_id(listed_note)
            if current_id in processed:
                stats["already_processed"] += 1
                continue
            timestamp = note_time(listed_note)
            if cutoff is not None and timestamp is not None and timestamp < cutoff:
                continue

            ok, reason = is_candidate(listed_note)
            if not ok:
                stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1
                continue

            detail = fetch_note_detail(current_id, getnote_api_key, getnote_client_id)
            ok, reason = is_candidate(detail)
            if not ok:
                stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1
                continue

            raw_body = strip_getnote_generated_tag_text(source_text(detail))
            body, requested_tags = extract_requested_tags(raw_body)
            if not body:
                stats["skipped"]["empty_body"] = stats["skipped"].get("empty_body", 0) + 1
                continue

            resolved_tags, unresolved_tags = resolve_flomo_tags(flomo_token, requested_tags)
            content = render_flomo_content(body, resolved_tags)
            stats["candidate"] += 1
            stats["requested_tags"] += len(requested_tags)
            stats["resolved_tags"] += len(resolved_tags)
            stats["unresolved_tags"] += len(unresolved_tags)

            if not args.dry_run:
                created = call_flomo_tool(
                    flomo_token,
                    "memo_create",
                    {"content": content, "created_at": str(detail.get("created_at") or "")},
                    request_id=1000 + stats["candidate"],
                )
                memo_id = str(created.get("id") or "")
                if not memo_id:
                    raise RuntimeError("flomo memo_create returned no id")
                processed[current_id] = {
                    "flomo_memo_id": memo_id,
                    "synced_at": now,
                    "created_at": detail.get("created_at") or "",
                    "updated_at": detail.get("updated_at") or "",
                }
                stats["created"] += 1

            handled += 1
            if handled >= args.max_notes:
                break

        if handled >= args.max_notes:
            break
        if should_stop_page(list_notes, cutoff):
            break

    state.update(
        {
            "state_version": 1,
            "source_system": "getnote",
            "destination_system": "flomo",
            "last_run_at": now,
            "last_dry_run": args.dry_run,
            "processed": processed,
        }
    )
    if not args.dry_run:
        save_state(state_path, state)

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
