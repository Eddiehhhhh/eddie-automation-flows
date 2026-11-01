#!/usr/bin/env python3
"""Rewrite GetNote link notes into cleaned notes and delete the originals.

The script scans recent GetNote notes, looks for Bilibili or Xiaohongshu links,
creates a new plain-text note with the original link at the top, preserves tags
and topic memberships, then moves the source note to the trash.

It is intentionally conservative:
- only one target URL per note is processed
- only recent notes are considered by default
- no source note is deleted unless the replacement note is created first
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


BASE_URL = "https://openapi.biji.com"
LIST_PATH = "/open/api/v1/resource/note/list"
DETAIL_PATH = "/open/api/v1/resource/note/detail"
SAVE_PATH = "/open/api/v1/resource/note/save"
DELETE_PATH = "/open/api/v1/resource/note/delete"
TOPIC_ADD_PATH = "/open/api/v1/resource/knowledge/note/batch-add"
IMA_IMPORT_PATH = "openapi/wiki/v1/import_urls"

ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
STATE_PATH = ROOT / "Raw" / "03 Get" / "04 State" / "link_rewrite_state.json"
TZ = dt.timezone(dt.timedelta(hours=8))

TARGET_DOMAINS = (
    "bilibili.com",
    "xiaohongshu.com",
    "xhslink.com",
)

URL_RE = re.compile(r"https?://[^\s<>\]\"')]+", re.IGNORECASE)


def load_credentials() -> Tuple[str, str]:
    api_key = os.environ.get("GETNOTE_API_KEY") or os.environ.get("GET_API_KEY")
    client_id = os.environ.get("GETNOTE_CLIENT_ID") or os.environ.get("GET_CLIENT_ID")

    if api_key and client_id:
        return api_key, client_id

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

    return api_key, client_id


def load_ima_credentials() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    client_id = os.environ.get("IMA_OPENAPI_CLIENTID")
    api_key = os.environ.get("IMA_OPENAPI_APIKEY")
    kb_id = os.environ.get("IMA_LINK_REWRITE_KB_ID")

    config_dir = Path.home() / ".config" / "ima"
    if not client_id:
        client_file = config_dir / "client_id"
        if client_file.exists():
            client_id = client_file.read_text(encoding="utf-8").strip() or None
    if not api_key:
        key_file = config_dir / "api_key"
        if key_file.exists():
            api_key = key_file.read_text(encoding="utf-8").strip() or None

    if client_id and api_key and kb_id:
        return client_id, api_key, kb_id
    return None, None, None


def json_loads_tolerant(text: str) -> Any:
    return json.JSONDecoder(strict=False).decode(text)


def api_request(
    method: str,
    path: str,
    params: Optional[Dict[str, str]],
    payload: Optional[Dict[str, Any]],
    api_key: str,
    client_id: str,
) -> Any:
    url = f"{BASE_URL}{path}"
    if params:
        query = urllib.parse.urlencode(params)
        if query:
            url = f"{url}?{query}"

    headers = {
        "Authorization": api_key,
        "X-Client-ID": client_id,
        "User-Agent": "eddie-wiki-getnote-link-rewrite/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    last_error: Optional[Exception] = None
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            data_obj = json_loads_tolerant(body)
            if isinstance(data_obj, dict) and data_obj.get("success") is False:
                err = data_obj.get("error") or {}
                code = err.get("code")
                if code in {10202, 42900} and attempt < 4:
                    retry_after = 10
                    rate_limit = data_obj.get("rate_limit") or {}
                    if isinstance(rate_limit, dict):
                        retry_after = int(rate_limit.get("retry_after") or retry_after)
                    time.sleep(retry_after)
                    continue
                raise RuntimeError(f"GetNote API error {code}: {err.get('message') or err.get('reason')}")
            return data_obj
        except urllib.error.HTTPError as exc:
            last_error = exc
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < 4:
                retry_after = exc.headers.get("Retry-After")
                time.sleep(int(retry_after) if retry_after and retry_after.isdigit() else 10)
                continue
            if 500 <= exc.code < 600 and attempt < 4:
                time.sleep(5)
                continue
            raise RuntimeError(f"GetNote request failed for {method} {path} ({exc.code}): {body or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(5)
                continue
            raise RuntimeError(f"GetNote request failed for {method} {path}: {exc}") from exc

    raise RuntimeError(f"GetNote request failed: {last_error}")


def ima_api_request(path: str, payload: Dict[str, Any], client_id: str, api_key: str) -> Any:
    url = f"https://ima.qq.com/{path}"
    headers = {
        "ima-openapi-clientid": client_id,
        "ima-openapi-apikey": api_key,
        "Content-Type": "application/json",
        "User-Agent": "eddie-wiki-getnote-link-rewrite/1.0",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json_loads_tolerant(body)


def fetch_note_pages(
    api_key: str, client_id: str, max_pages: Optional[int]
) -> Iterable[Tuple[int, List[Dict[str, Any]], bool]]:
    cursor: Optional[str] = None
    page = 0

    while True:
        params = {}
        if cursor:
            params["cursor"] = cursor
        data = api_request("GET", LIST_PATH, params, None, api_key, client_id)
        payload = data.get("data", data)
        batch = payload.get("notes", [])
        page += 1
        yield page, [item for item in batch if isinstance(item, dict)], bool(payload.get("has_more"))

        if max_pages and page >= max_pages:
            break
        if not payload.get("has_more"):
            break
        next_cursor = payload.get("cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = str(next_cursor)


def note_id_from(note: Dict[str, Any]) -> str:
    value = note.get("note_id") or note.get("id")
    if value is None:
        raise ValueError("note missing note_id/id")
    return str(value)


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


def note_timestamp(note: Dict[str, Any]) -> Optional[dt.datetime]:
    return parse_time(note.get("updated_at")) or parse_time(note.get("created_at"))


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


def topic_ids(note: Dict[str, Any]) -> List[str]:
    topics = []
    for topic in note.get("topics") or []:
        if isinstance(topic, dict):
            topic_id = topic.get("topic_id") or topic.get("id")
        else:
            topic_id = None
        if topic_id:
            topics.append(str(topic_id))
    return topics


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


def fetch_note_detail(note_id: str, api_key: str, client_id: str) -> Dict[str, Any]:
    data = api_request("GET", DETAIL_PATH, {"id": note_id, "image_quality": "original"}, None, api_key, client_id)
    payload = data.get("data", data)
    return payload.get("note", payload)


def save_note(
    api_key: str,
    client_id: str,
    title: str,
    content: str,
    tags: Sequence[str],
    parent_id: Optional[str],
) -> str:
    payload: Dict[str, Any] = {
        "note_type": "plain_text",
        "title": title,
        "content": content,
    }
    if tags:
        payload["tags"] = list(tags)[:5]
    if parent_id:
        payload["parent_id"] = parent_id
    data = api_request("POST", SAVE_PATH, None, payload, api_key, client_id)
    payload = data.get("data", data)
    note_id = payload.get("note_id")
    if not note_id:
        raise RuntimeError(f"Unexpected save_note response: {data}")
    return str(note_id)


def delete_note(api_key: str, client_id: str, note_id: str) -> None:
    api_request("POST", DELETE_PATH, None, {"note_id": note_id}, api_key, client_id)


def add_note_to_topics(api_key: str, client_id: str, note_id: str, topic_ids_list: Sequence[str]) -> None:
    for topic_id in topic_ids_list:
        api_request(
            "POST",
            TOPIC_ADD_PATH,
            None,
            {"topic_id": topic_id, "note_ids": [note_id]},
            api_key,
            client_id,
        )


def try_ima_import(url: str) -> Optional[str]:
    client_id, api_key, kb_id = load_ima_credentials()
    if not client_id or not api_key or not kb_id:
        return None

    try:
        response = ima_api_request(
            IMA_IMPORT_PATH,
            {
                "knowledge_base_id": kb_id,
                "folder_id": kb_id,
                "urls": [url],
            },
            client_id,
            api_key,
        )
    except Exception:
        return None

    data = response.get("data", response) if isinstance(response, dict) else {}
    results = data.get("results") if isinstance(data, dict) else None
    if isinstance(results, dict):
        item = results.get(url)
        if isinstance(item, dict) and int(item.get("ret_code") or 0) == 0:
            media_id = item.get("media_id")
            if media_id:
                return str(media_id)
    return None


class MetaExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: List[str] = []
        self.meta: Dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = True
            return
        if lowered != "meta":
            return
        attrs_map = {key.lower(): (value or "") for key, value in attrs}
        content = attrs_map.get("content", "").strip()
        if not content:
            return
        for key in ("property", "name"):
            attr = attrs_map.get(key, "").strip().lower()
            if attr:
                self.meta.setdefault(attr, content)
                break

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def fetch_html_summary(url: str) -> Dict[str, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html_bytes = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except Exception:
        return {}

    charset = "utf-8"
    match = re.search(r"charset=([A-Za-z0-9_\-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    try:
        text = html_bytes.decode(charset, errors="replace")
    except LookupError:
        text = html_bytes.decode("utf-8", errors="replace")

    parser = MetaExtractor()
    parser.feed(text)
    body_text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    body_text = re.sub(r"<style\b.*?</style>", " ", body_text, flags=re.IGNORECASE | re.DOTALL)
    body_text = re.sub(r"<[^>]+>", " ", body_text)
    body_text = html.unescape(re.sub(r"\s+", " ", body_text)).strip()

    return {
        "title": " ".join(parser.title_parts).strip(),
        "description": parser.meta.get("og:description")
        or parser.meta.get("description")
        or parser.meta.get("twitter:description")
        or "",
        "og_title": parser.meta.get("og:title") or parser.meta.get("twitter:title") or "",
        "excerpt": body_text[:1200],
    }


def extract_urls(text: str) -> List[str]:
    urls = []
    for match in URL_RE.findall(text or ""):
        cleaned = match.rstrip(".,;!?)]}>'\"")
        if cleaned:
            urls.append(cleaned)
    return urls


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return url
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc.lower(), parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


def host_matches(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(domain in host for domain in TARGET_DOMAINS)


def choose_target_url(note: Dict[str, Any]) -> Optional[str]:
    candidates: List[str] = []
    web_page = note.get("web_page") or {}
    if isinstance(web_page, dict):
        url = web_page.get("url")
        if isinstance(url, str) and url.strip():
            candidates.append(url.strip())
        content = web_page.get("content")
        if isinstance(content, str):
            candidates.extend(extract_urls(content))
    content = note.get("content")
    if isinstance(content, str):
        candidates.extend(extract_urls(content))

    seen = set()
    ordered: List[str] = []
    for candidate in candidates:
        normalized = normalize_url(candidate)
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    for candidate in ordered:
        if host_matches(candidate):
            return candidate
    return None


def build_rewritten_content(note: Dict[str, Any], url: str, parsed: Dict[str, str]) -> Tuple[str, str]:
    source_title = str(note.get("title") or "").strip()
    derived_title = parsed.get("og_title") or parsed.get("title") or source_title or url
    excerpt = parsed.get("description") or parsed.get("excerpt") or ""
    web_page = note.get("web_page") or {}
    source_excerpt = ""
    if isinstance(web_page, dict):
        source_excerpt = str(web_page.get("excerpt") or "").strip()
        web_content = str(web_page.get("content") or "").strip()
    else:
        web_content = ""

    lines = [
        f"原始链接：{url}",
        "",
        f"# {derived_title}",
        "",
        "## 解析结果",
        "",
    ]
    if excerpt:
        lines.append(excerpt)
    elif source_excerpt:
        lines.append(source_excerpt)
    elif web_content:
        lines.append(web_content[:1200])
    elif parsed.get("excerpt"):
        lines.append(parsed["excerpt"])
    else:
        lines.append("（未能抓到网页摘要）")

    lines.extend(
        [
            "",
            "## 来源信息",
            "",
            f"- 原笔记 ID：`{note_id_from(note)}`",
            f"- 原笔记标题：{source_title or '（无）'}",
            f"- 创建时间：{note.get('created_at') or ''}",
            f"- 更新时间：{note.get('updated_at') or ''}",
            f"- 标签：{', '.join(tag_names(note)) or '无'}",
            f"- 知识库：{', '.join(topic_names(note)) or '无'}",
        ]
    )
    return derived_title[:120], "\n".join(lines).strip() + "\n"


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"processed": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"processed": {}}
    if not isinstance(data, dict):
        return {"processed": {}}
    processed = data.get("processed")
    if not isinstance(processed, dict):
        data["processed"] = {}
    return data


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite GetNote link notes and delete originals")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit list pages for testing")
    parser.add_argument("--workers", type=int, default=3, help="Concurrent detail fetches per page")
    parser.add_argument(
        "--hours-back",
        type=float,
        default=float(os.environ.get("GETNOTE_LINK_REWRITE_HOURS", "24")),
        help="Only inspect notes updated within the recent N hours",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report without mutating GetNote")
    parser.add_argument("--full-sync", action="store_true", help="Ignore time window and scan all notes")
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("GETNOTE_LINK_REWRITE_LIMIT", "200")),
        help="Maximum number of matching notes to process",
    )
    args = parser.parse_args()

    api_key, client_id = load_credentials()
    cutoff: Optional[dt.datetime] = None
    if not args.full_sync and args.hours_back > 0:
        cutoff = dt.datetime.now(TZ) - dt.timedelta(hours=args.hours_back)

    state = load_state()
    processed = state.setdefault("processed", {})
    if not isinstance(processed, dict):
        processed = {}
        state["processed"] = processed

    candidates: List[Dict[str, Any]] = []
    fetched = 0
    for page, list_notes, has_more in fetch_note_pages(api_key, client_id, args.max_pages):
        print(
            f"page={page} notes={len(list_notes)} has_more={str(has_more).lower()}",
            file=sys.stderr,
            flush=True,
        )
        if cutoff is not None:
            list_notes = [item for item in list_notes if note_timestamp(item) and note_timestamp(item) >= cutoff]

        workers = max(1, min(args.workers, len(list_notes) or 1))
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_id = {
                executor.submit(fetch_note_detail, note_id_from(item), api_key, client_id): note_id_from(item)
                for item in list_notes
            }
            for future in as_completed(future_to_id):
                fetched += 1
                note_id = future_to_id[future]
                try:
                    detail = future.result()
                except Exception as exc:
                    raise RuntimeError(f"failed to fetch note detail for {note_id}: {exc}") from exc
                url = choose_target_url(detail)
                if not url:
                    continue
                state_key = note_id_from(detail)
                if processed.get(state_key) == detail.get("updated_at"):
                    continue
                detail["__matched_url"] = url
                candidates.append(detail)
                if len(candidates) >= args.limit:
                    break
        print(f"processed={fetched}", file=sys.stderr, flush=True)
        if len(candidates) >= args.limit:
            break
        if cutoff is not None and not list_notes:
            break

    summary = {
        "fetched": fetched,
        "matched": len(candidates),
        "dry_run": args.dry_run,
        "mutated": [],
        "skipped": [],
    }

    for note in candidates:
        note_id = note_id_from(note)
        url = str(note.get("__matched_url") or "")
        if not url:
            summary["skipped"].append({"note_id": note_id, "reason": "missing_url"})
            continue

        ima_media_id = try_ima_import(url)
        parsed = fetch_html_summary(url)
        title, content = build_rewritten_content(note, url, parsed)
        if ima_media_id:
            content = content.rstrip() + f"\n\n## IMA 导入\n\n- media_id: `{ima_media_id}`\n"
        if args.dry_run:
            summary["mutated"].append(
                {
                    "note_id": note_id,
                    "title": title,
                    "url": url,
                    "ima_media_id": ima_media_id,
                    "topics": topic_ids(note),
                }
            )
            continue

        replacement_note_id = save_note(
            api_key=api_key,
            client_id=client_id,
            title=title,
            content=content,
            tags=tag_names(note),
            parent_id=str(note.get("parent_id") or "") or None,
        )
        topic_list = topic_ids(note)
        if topic_list:
            add_note_to_topics(api_key, client_id, replacement_note_id, topic_list)
        delete_note(api_key, client_id, note_id)
        processed[note_id] = note.get("updated_at")
        summary["mutated"].append(
            {
                "source_note_id": note_id,
                "replacement_note_id": replacement_note_id,
                "url": url,
                "ima_media_id": ima_media_id,
                "title": title,
                "topics": topic_list,
            }
        )

    if not args.dry_run:
        save_state(state)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
