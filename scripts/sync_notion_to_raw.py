#!/usr/bin/env python3
"""Sync Notion databases into 艾迪宇宙 Raw/02 Notion.

Public GitHub runs keep only safe state and logs.
Full page bodies are written only when --export-body is enabled, and they
live under Raw/02 Notion/01 Databases/, which is gitignored.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")
TZ = dt.timezone(dt.timedelta(hours=8))

ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
RAW_DIR = ROOT / "Raw" / "02 Notion"
PUBLIC_STATE_DIR = RAW_DIR / "03 State"
PUBLIC_LOG_DIR = RAW_DIR / "04 Import Logs"
DATABASES_DIR = RAW_DIR / "01 Databases"
SNAPSHOTS_DIR = RAW_DIR / "02 Snapshots"
DATABASES_INDEX_PATH = DATABASES_DIR / "index.md"
STRUCTURE_INDEX_PATH = SNAPSHOTS_DIR / "notion-structure-index.md"
LOCAL_STATE_PATH = PUBLIC_STATE_DIR / ".local-sync-state.json"
LEGACY_PRIVATE_STATE_PATH = RAW_DIR / "private" / "03 State" / "sync-state.json"
PUBLIC_MANIFEST_PATH = PUBLIC_STATE_DIR / "manifest.json"

DEFAULT_SYNC_DATABASE_SPECS = [
    {"name": "日记中心", "database_id": "4e6607f4-7140-4317-8fc9-d52102337869"},
    {"name": "晨间日记", "database_id": "1ba33b33-7f23-8054-988c-c976153e354a"},
    {"name": "任务中心", "database_id": "18133b33-7f23-8032-8f8e-e9e7c821f021"},
    {"name": "周复盘", "database_id": "13333b33-7f23-8085-9a1f-d944b627052b"},
    {"name": "年复盘", "database_id": "2d033b33-7f23-8062-be33-f32f9b3740a1"},
    {"name": "情绪", "database_id": "18933b33-7f23-80f0-9d83-ff497ac5a887"},
    {"name": "人际中心", "database_id": "17733b33-7f23-8019-bfb6-e1d27105a4ca"},
    {"name": "碎片中心", "database_id": "11233b33-7f23-8024-9555-cb8de8c58e02"},
    {"name": "运动", "database_id": "26333b33-7f23-818c-a3af-e5e546d18118"},
    {"name": "收支项", "database_id": "2c533b33-7f23-8122-88ac-edb4227beb8c"},
    {"name": "Podcast", "database_id": "11b33b33-7f23-8156-84a4-c49c0cd07692"},
]

UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})"
)


def load_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


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


def format_time(value: Optional[dt.datetime]) -> str:
    if value is None:
        return ""
    return value.astimezone(TZ).isoformat(timespec="seconds")


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def load_notion_api_key() -> str:
    value = load_env("NOTION_API_KEY")
    if value:
        return value.strip()

    legacy_value = load_env("NOTION_TOKEN")
    if legacy_value:
        return legacy_value.strip()

    services_path = Path.home() / ".codex" / "secrets" / "services.json"
    if services_path.exists():
        data = json.loads(services_path.read_text(encoding="utf-8"))
        notion = data.get("notion") or {}
        auth = notion.get("auth") or {}
        token = auth.get("token")
        if token:
            return str(token).strip()

    raise SystemExit(
        "Missing required Notion credential: NOTION_API_KEY, NOTION_TOKEN, or ~/.codex/secrets/services.json notion.auth.token"
    )


def normalize_notion_id(value: str) -> str:
    raw = value.strip()
    match = UUID_RE.search(raw)
    if not match:
        return raw
    token = match.group(1).replace("-", "").lower()
    if len(token) != 32:
        return match.group(1).lower()
    return f"{token[0:8]}-{token[8:12]}-{token[12:16]}-{token[16:20]}-{token[20:32]}"


def notion_request(
    api_key: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "User-Agent": "eddie-wiki-notion-sync/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    url = f"{BASE_URL}{path}"
    last_error: Optional[Exception] = None
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
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
            message = body or exc.reason
            raise RuntimeError(f"Notion API {method} {path} failed ({exc.code}): {message}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(5)
                continue
            raise RuntimeError(f"Notion request failed for {method} {path}: {exc}") from exc
    raise RuntimeError(f"Notion request failed for {method} {path}: {last_error}")


def search_databases(api_key: str, query: str) -> List[Dict[str, Any]]:
    payload = {"query": query, "filter": {"property": "object", "value": "database"}}
    response = notion_request(api_key, "POST", "/search", payload)
    return [item for item in response.get("results", []) if item.get("object") == "database"]


def get_database(api_key: str, database_id: str) -> Dict[str, Any]:
    return notion_request(api_key, "GET", f"/databases/{normalize_notion_id(database_id)}")


def resolve_database(api_key: str, configured: Optional[str], query: str) -> Dict[str, Any]:
    if configured:
        return get_database(api_key, configured)

    matches = search_databases(api_key, query)
    exact = [item for item in matches if title_text_from_database(item) == query]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"Could not find a Notion database titled {query!r}")

    options = ", ".join(f"{item.get('id')}:{title_text_from_database(item)}" for item in matches[:10])
    raise SystemExit(f"Multiple Notion databases match {query!r}. Candidates: {options}")


def query_database(
    api_key: str,
    database_id: str,
    filter_obj: Optional[Dict[str, Any]] = None,
    page_size: int = 100,
    start_cursor: Optional[str] = None,
    sorts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"page_size": page_size}
    if filter_obj is not None:
        payload["filter"] = filter_obj
    if sorts:
        payload["sorts"] = sorts
    if start_cursor:
        payload["start_cursor"] = start_cursor
    return notion_request(api_key, "POST", f"/databases/{normalize_notion_id(database_id)}/query", payload)


def paginate_database(
    api_key: str,
    database_id: str,
    filter_obj: Optional[Dict[str, Any]] = None,
    sorts: Optional[List[Dict[str, Any]]] = None,
) -> Iterable[Dict[str, Any]]:
    cursor: Optional[str] = None
    while True:
        response = query_database(api_key, database_id, filter_obj=filter_obj, start_cursor=cursor, sorts=sorts)
        for item in response.get("results", []):
            yield item
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
        if not cursor:
            break


def retrieve_block_children(api_key: str, block_id: str) -> List[Dict[str, Any]]:
    children: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        params = {"page_size": "100"}
        if cursor:
            params["start_cursor"] = cursor
        query = urllib.parse.urlencode(params)
        response = notion_request(api_key, "GET", f"/blocks/{normalize_notion_id(block_id)}/children?{query}")
        children.extend(response.get("results", []))
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
        if not cursor:
            break
    return children


def rich_text_plain_text(parts: Sequence[Dict[str, Any]]) -> str:
    return "".join(part.get("plain_text", "") for part in parts if isinstance(part, dict))


def title_text_from_database(database: Dict[str, Any]) -> str:
    title = database.get("title") or []
    return rich_text_plain_text(title).strip()


def title_property_name(database: Dict[str, Any]) -> str:
    properties = database.get("properties") or {}
    for name, prop in properties.items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return name
    raise SystemExit(f"Could not determine title property for database {title_text_from_database(database)!r}")


def date_property_name(database: Dict[str, Any], configured: Optional[str] = None) -> Optional[str]:
    if configured:
        return configured
    properties = database.get("properties") or {}
    for name, prop in properties.items():
        if isinstance(prop, dict) and prop.get("type") == "date":
            return name
    return None


def property_value(database: Dict[str, Any], page: Dict[str, Any], property_name: str) -> Dict[str, Any]:
    properties = page.get("properties") or {}
    if property_name in properties and isinstance(properties[property_name], dict):
        return properties[property_name]
    prop_id = (database.get("properties") or {}).get(property_name, {}).get("id")
    for value in properties.values():
        if isinstance(value, dict) and value.get("id") == prop_id:
            return value
    raise KeyError(property_name)


def plain_value(value: Any) -> Any:
    if isinstance(value, dict):
        if "plain_text" in value:
            return value.get("plain_text")
        if "name" in value and len(value) == 1:
            return value.get("name")
        if "formula" in value and isinstance(value.get("formula"), dict):
            return plain_value(value.get("formula"))
        if "rollup" in value and isinstance(value.get("rollup"), dict):
            return plain_value(value.get("rollup"))
        value_type = value.get("type")
        if value_type in {"string", "number", "boolean"} and value_type in value:
            return value.get(value_type)
        if value_type == "formula" and "formula" in value:
            return plain_value(value.get("formula"))
        if value_type == "rollup" and "rollup" in value:
            return plain_value(value.get("rollup"))
        return {key: plain_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain_value(item) for item in value]
    return value


def summarize_property(prop: Dict[str, Any]) -> Any:
    prop_type = prop.get("type")
    data = prop.get(prop_type) if prop_type else None
    if prop_type == "title":
        return rich_text_plain_text(data or [])
    if prop_type == "rich_text":
        return rich_text_plain_text(data or [])
    if prop_type == "number":
        return data
    if prop_type == "checkbox":
        return bool(data)
    if prop_type == "select":
        return data.get("name") if isinstance(data, dict) else None
    if prop_type == "status":
        return data.get("name") if isinstance(data, dict) else None
    if prop_type == "multi_select":
        return [item.get("name") for item in data or [] if isinstance(item, dict) and item.get("name")]
    if prop_type == "date":
        if isinstance(data, dict):
            start = data.get("start") or ""
            end = data.get("end") or ""
            if start and end and start != end:
                return f"{start} → {end}"
            return start or end or ""
        return None
    if prop_type == "url":
        return data
    if prop_type == "email":
        return data
    if prop_type == "phone_number":
        return data
    if prop_type == "people":
        people = []
        for item in data or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("id")
            if name:
                people.append(name)
        return people
    if prop_type == "relation":
        ids = [item.get("id") for item in data or [] if isinstance(item, dict) and item.get("id")]
        return {"count": len(ids), "ids": ids}
    if prop_type == "files":
        files = []
        for item in data or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("type") or "file"
            files.append(name)
        return files
    if prop_type in {"formula", "rollup"}:
        if isinstance(data, dict):
            inner_type = data.get("type")
            if inner_type == "string":
                return data.get("string") or ""
            if inner_type == "number":
                return data.get("number")
            if inner_type == "boolean":
                return bool(data.get("boolean"))
            if inner_type == "date":
                date_value = data.get("date") or {}
                if isinstance(date_value, dict):
                    return {
                        "start": date_value.get("start"),
                        "end": date_value.get("end"),
                        "time_zone": date_value.get("time_zone"),
                    }
            if inner_type == "array":
                return [plain_value(item) for item in data.get("array") or []]
        return plain_value(data)
    return plain_value(data)


def summarize_properties(page: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for name, prop in (page.get("properties") or {}).items():
        if isinstance(prop, dict):
            summary[name] = summarize_property(prop)
    return summary


def page_title(page: Dict[str, Any], title_prop: str) -> str:
    prop = page.get("properties", {}).get(title_prop) or {}
    if not isinstance(prop, dict):
        return page.get("id") or "unknown-page"
    title = prop.get("title") or prop.get("rich_text") or []
    text = rich_text_plain_text(title).strip()
    return text or page.get("id") or "unknown-page"


def page_date_hint(page: Dict[str, Any], date_prop: Optional[str], fallback: Optional[dt.datetime]) -> dt.datetime:
    if date_prop:
        prop = page.get("properties", {}).get(date_prop) or {}
        if isinstance(prop, dict) and prop.get("type") == "date":
            date_value = (prop.get("date") or {}).get("start")
            parsed = parse_time(date_value)
            if parsed:
                return parsed
    created = parse_time(page.get("created_time"))
    if created:
        return created
    edited = parse_time(page.get("last_edited_time"))
    if edited:
        return edited
    return fallback or now()


def safe_slug(text: str, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"[\\/:*?\"<>|#\[\]\n\r\t]+", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-. ")
    if not text:
        text = fallback
    encoded = text.encode("utf-8")
    if len(encoded) > 120:
        trimmed = encoded[:120]
        while trimmed:
            try:
                text = trimmed.decode("utf-8").rstrip("-. ")
                break
            except UnicodeDecodeError:
                trimmed = trimmed[:-1]
    return text or fallback


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


def yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


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
        lines.append(f"{key}: {yaml_quote(str(value))}")
    lines.append("---")
    return "\n".join(lines)


def block_text(block: Dict[str, Any]) -> str:
    block_type = block.get("type")
    data = block.get(block_type) if block_type else {}
    if not isinstance(data, dict):
        return ""
    if "rich_text" in data:
        return rich_text_plain_text(data.get("rich_text") or [])
    if block_type == "table_row":
        cells = []
        for cell in data.get("cells") or []:
            cells.append(rich_text_plain_text(cell or []))
        return " | ".join(cells)
    if block_type == "code":
        return rich_text_plain_text(data.get("rich_text") or [])
    return ""


def render_blocks(api_key: str, blocks: List[Dict[str, Any]], depth: int = 0) -> List[str]:
    lines: List[str] = []
    indent = "  " * depth
    for block in blocks:
        block_type = block.get("type")
        text = block_text(block)
        if block_type in {"paragraph", "callout", "quote"}:
            if text:
                prefix = "> " if block_type in {"callout", "quote"} else ""
                lines.append(f"{indent}{prefix}{text}".rstrip())
                lines.append("")
        elif block_type in {"heading_1", "heading_2", "heading_3"}:
            level = {"heading_1": 1, "heading_2": 2, "heading_3": 3}[block_type]
            if text:
                lines.append(f"{indent}{'#' * level} {text}".rstrip())
                lines.append("")
        elif block_type == "bulleted_list_item":
            if text:
                lines.append(f"{indent}- {text}".rstrip())
        elif block_type == "numbered_list_item":
            if text:
                lines.append(f"{indent}1. {text}".rstrip())
        elif block_type == "to_do":
            checked = bool((block.get("to_do") or {}).get("checked"))
            if text:
                lines.append(f"{indent}- [{'x' if checked else ' '}] {text}".rstrip())
        elif block_type == "toggle":
            if text:
                lines.append(f"{indent}- {text}".rstrip())
        elif block_type == "code":
            language = (block.get("code") or {}).get("language") or ""
            lines.append(f"{indent}```{language}".rstrip())
            if text:
                for line in text.splitlines():
                    lines.append(f"{indent}{line}")
            lines.append(f"{indent}```")
            lines.append("")
        elif block_type == "divider":
            lines.append(f"{indent}---")
            lines.append("")
        elif block_type == "table_row":
            if text:
                lines.append(f"{indent}| {text} |")
        elif block_type == "table":
            lines.append(f"{indent}[table]")
            lines.append("")
        elif block_type in {"image", "file", "bookmark", "embed", "pdf", "video", "audio", "link_preview"}:
            url = ""
            data = block.get(block_type) or {}
            if isinstance(data, dict):
                if "external" in data and isinstance(data["external"], dict):
                    url = data["external"].get("url") or ""
                elif "file" in data and isinstance(data["file"], dict):
                    url = data["file"].get("url") or ""
                elif "url" in data:
                    url = data.get("url") or ""
            label = text or block_type
            if url:
                lines.append(f"{indent}[{label}]({url})")
            elif label:
                lines.append(f"{indent}[{label}]")
            lines.append("")
        elif block_type == "child_page":
            if text:
                lines.append(f"{indent}[child page] {text}".rstrip())
                lines.append("")
        elif block_type == "child_database":
            if text:
                lines.append(f"{indent}[child database] {text}".rstrip())
                lines.append("")
        elif block_type == "synced_block":
            if text:
                lines.append(f"{indent}{text}")
                lines.append("")
        elif block_type == "equation":
            if text:
                lines.append(f"{indent}$ {text} $")
                lines.append("")
        elif block_type == "table_of_contents":
            lines.append(f"{indent}[table of contents]")
            lines.append("")
        else:
            if text:
                lines.append(f"{indent}{text}".rstrip())
                lines.append("")

        if block.get("has_children"):
            children = retrieve_block_children(api_key, str(block.get("id")))
            child_lines = render_blocks(api_key, children, depth=depth + 1)
            if child_lines:
                if lines and lines[-1] != "":
                    lines.append("")
                lines.extend(child_lines)

    while lines and lines[-1] == "":
        lines.pop()
    return lines


def render_page_markdown(
    api_key: str,
    database: Dict[str, Any],
    page: Dict[str, Any],
    title_prop: str,
    date_prop: Optional[str],
    include_body: bool,
    page_path: Optional[Path] = None,
    page_index: Optional[Dict[str, Dict[str, Any]]] = None,
    page_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[str, Dict[str, Any], dt.datetime]:
    title = page_title(page, title_prop)
    date_hint = page_date_hint(page, date_prop, None)
    properties = summarize_properties(page)
    frontmatter = render_frontmatter(
        {
            "source_system": "notion",
            "database_name": title_text_from_database(database),
            "database_id": normalize_notion_id(str(database.get("id") or "")),
            "database_slug": safe_slug(title_text_from_database(database), "database"),
            "page_id": normalize_notion_id(str(page.get("id") or "")),
            "page_url": page.get("url"),
            "title": title,
            "last_edited_time": page.get("last_edited_time"),
            "date_hint": format_time(date_hint),
            "title_property": title_prop,
            "date_property": date_prop or "",
        }
    )

    lookup = page_index or {}
    cache = page_cache or {}
    relation_sections: Dict[str, List[str]] = {
        "输入": [],
        "输出": [],
        "身心/环境": [],
        "情绪/关系": [],
        "财务": [],
        "其他": [],
    }
    reading_sections: Dict[str, Dict[str, List[str]]] = {
        "输入": {},
        "输出": {},
        "身心/环境": {},
        "情绪/关系": {},
        "财务": {},
        "其他": {},
    }
    for name, value in properties.items():
        if isinstance(value, dict) and "count" in value and "ids" in value and value.get("ids"):
            group = classify_relation_group(name)
            group_lines = relation_sections.setdefault(group, [])
            group_lines.append(f"### {name}")
            group_lines.append("")
            reading_group = classify_reading_group(name)
            relation_reading_lines = reading_sections.setdefault(reading_group, {}).setdefault(name, [])
            for rel_id in value.get("ids"):
                group_lines.append(render_relation_index_item(name, str(rel_id), lookup, page_path))
                rendered_card = render_relation_reading_card(
                    api_key=api_key,
                    relation_name=name,
                    relation_page_id=str(rel_id),
                    page_index=lookup,
                    page_cache=cache,
                )
                if rendered_card:
                    relation_reading_lines.extend(rendered_card)
                    relation_reading_lines.append("")
            group_lines.append("")

    relation_block: List[str] = []
    if any(relation_sections[group] for group in relation_sections):
        relation_block.extend(["> [!info]- 关联索引", ">"])
        for group in ["输入", "输出", "身心/环境", "情绪/关系", "财务", "其他"]:
            group_lines = relation_sections.get(group) or []
            if not group_lines:
                continue
            relation_block.append(f"> ### {group}")
            relation_block.append(">")
            relation_block.extend(f"> {line}" if line else ">" for line in group_lines)

    parts = [frontmatter, ""]
    if relation_block:
        parts.extend(relation_block)
    parts.append("## 正文")
    parts.append("")
    for group in ["输入", "输出", "身心/环境", "情绪/关系", "财务", "其他"]:
        relation_map = reading_sections.get(group) or {}
        relation_names = [
            name for name, lines in relation_map.items()
            if lines and any(line.strip() for line in lines)
        ]
        if not relation_names:
            continue
        parts.append(f"### {display_group_heading(group)}")
        parts.append("")
        for relation_name in sorted(relation_names, key=lambda name: relation_reading_order(group, name)):
            relation_lines = relation_map.get(relation_name) or []
            if not relation_lines:
                continue
            parts.append(f"#### {display_relation_heading(relation_name)}")
            parts.append("")
            parts.extend(relation_lines)
    return "\n".join(parts).rstrip() + "\n", properties, date_hint


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        return
    path.write_text(rendered + "\n", encoding="utf-8")


def write_text(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def slug_for_database(database_name: str, database_id: str) -> str:
    base = safe_slug(database_name, "database")
    if base:
        return base
    return database_id[:8]


def target_specs(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if args.database_json:
        raw = json.loads(args.database_json)
        if not isinstance(raw, list):
            raise SystemExit("--database-json must be a JSON array")
        items = raw
    elif args.database:
        items = list(args.database)
    else:
        env_value = load_env("NOTION_SYNC_DATABASES_JSON")
        if env_value:
            raw = json.loads(env_value)
            if not isinstance(raw, list):
                raise SystemExit("NOTION_SYNC_DATABASES_JSON must be a JSON array")
            items = raw
        else:
            items = DEFAULT_SYNC_DATABASE_SPECS

    specs: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            specs.append({"name": item})
        elif isinstance(item, dict):
            spec = dict(item)
            if "name" not in spec and "title" in spec:
                spec["name"] = spec["title"]
            specs.append(spec)
        else:
            raise SystemExit(f"Unsupported database spec: {item!r}")
    return specs


def load_public_manifest() -> Dict[str, Any]:
    data = read_json(PUBLIC_MANIFEST_PATH)
    databases = data.get("databases")
    if isinstance(databases, list):
        normalized: Dict[str, Any] = {}
        for item in databases:
            if not isinstance(item, dict):
                continue
            key = str(item.get("slug") or item.get("name") or item.get("id") or len(normalized))
            normalized[key] = item
        data["databases"] = normalized
    elif not isinstance(databases, dict):
        data["databases"] = {}
    return data


def load_local_state() -> Dict[str, Any]:
    if LOCAL_STATE_PATH.exists():
        return read_json(LOCAL_STATE_PATH)
    if LEGACY_PRIVATE_STATE_PATH.exists():
        return read_json(LEGACY_PRIVATE_STATE_PATH)
    return {}


def build_page_index(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    page_index: Dict[str, Dict[str, Any]] = {}
    for page_file in RAW_DIR.glob("01 Databases/*/pages/**/*.md"):
        try:
            text = page_file.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter = text.split("---", 2)
        if len(frontmatter) < 3:
            continue
        head = frontmatter[1].splitlines()
        meta: Dict[str, str] = {}
        for line in head:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"')
        page_id = meta.get("page_id")
        if not page_id:
            continue
        normalized_page_id = normalize_notion_id(page_id)
        database_slug = page_file.parts[page_file.parts.index("01 Databases") + 1]
        page_index[normalized_page_id] = {
            "database_slug": meta.get("database_slug") or database_slug,
            "database_name": meta.get("database_name") or database_slug,
            "title": meta.get("title") or "",
            "path": str(page_file.relative_to(RAW_DIR / "01 Databases" / database_slug)),
            "date_hint": meta.get("date_hint") or "",
        }

    for database in (state.get("databases") or {}).values():
        if not isinstance(database, dict):
            continue
        database_slug = database.get("database_slug") or database.get("slug")
        database_name = database.get("database_name") or database.get("name")
        pages = database.get("pages") or {}
        if not isinstance(pages, dict):
            continue
        for page_id, meta in pages.items():
            if not isinstance(meta, dict):
                continue
            page_index[normalize_notion_id(str(page_id))] = {
                "database_slug": database_slug,
                "database_name": database_name,
                "title": meta.get("title") or "",
                "path": meta.get("path") or "",
                "date_hint": meta.get("date_hint") or "",
            }
    return page_index


def extract_frontmatter_value(text: str, key: str) -> str:
    parts = text.split("---", 2)
    if len(parts) < 3:
        return ""
    prefix = f"{key}:"
    for line in parts[1].splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip().strip('"')
    return ""


def prune_duplicate_page_files(database_dir: Path, page_map: Dict[str, Any]) -> List[str]:
    keep_paths = {
        str(meta.get("path"))
        for meta in page_map.values()
        if isinstance(meta, dict) and meta.get("path")
    }
    page_files_by_id: Dict[str, List[Path]] = {}
    for page_file in (database_dir / "pages").rglob("*.md"):
        try:
            text = page_file.read_text(encoding="utf-8")
        except OSError:
            continue
        page_id = extract_frontmatter_value(text, "page_id")
        if not page_id:
            continue
        normalized_id = normalize_notion_id(page_id)
        page_files_by_id.setdefault(normalized_id, []).append(page_file)

    removed: List[str] = []
    for _, files in page_files_by_id.items():
        if len(files) < 2:
            continue
        keep_file: Optional[Path] = None
        for file_path in files:
            relative_path = str(file_path.relative_to(database_dir))
            if relative_path in keep_paths:
                keep_file = file_path
                break
        if keep_file is None:
            files_sorted = sorted(files, key=lambda path: (len(path.name), path.name))
            keep_file = files_sorted[-1]
        for file_path in files:
            if file_path == keep_file:
                continue
            file_path.unlink(missing_ok=True)
            removed.append(str(file_path.relative_to(database_dir)))
    return removed


def state_cutoff(state: Dict[str, Any], database_key: str) -> Optional[dt.datetime]:
    database_state = (state.get("databases") or {}).get(database_key) or {}
    return parse_time(database_state.get("last_sync_at"))


def merge_database_state(
    state: Dict[str, Any],
    database_key: str,
    database_meta: Dict[str, Any],
    synced_count: int,
    last_sync_at: dt.datetime,
    page_map: Optional[Dict[str, Any]] = None,
    full_sync: bool = False,
) -> Dict[str, Any]:
    databases = state.setdefault("databases", {})
    entry = databases.setdefault(database_key, {})
    entry.update(database_meta)
    entry["synced_page_count"] = synced_count
    entry["last_sync_at"] = format_time(last_sync_at)
    if full_sync:
        entry["last_full_sync_at"] = format_time(last_sync_at)
    if page_map is not None:
        entry["pages"] = page_map
    return state


def make_page_path(
    database_dir: Path,
    date_hint: dt.datetime,
    title: str,
    page_id: str,
    existing_map: Dict[str, Any],
) -> Path:
    """文件名不含日期；仅标题 + 冲突兜底。日期信息保留在目录 YYYY/YYYY-MM/ 中。"""
    year_dir = database_dir / "pages" / f"{date_hint.year:04d}" / f"{date_hint.year:04d}-{date_hint.month:02d}"
    slug = safe_slug(title, page_id[:8])
    stem = slug
    candidate = year_dir / f"{stem}.md"
    current = existing_map.get(page_id) or {}
    if isinstance(current, dict):
        stored = current.get("path")
        if stored:
            stored_path = database_dir / stored
            if stored_path.name == candidate.name and stored_path.parent == candidate.parent:
                return stored_path
    if candidate.exists():
        if isinstance(current, dict) and current.get("path") == str(candidate.relative_to(database_dir)):
            return candidate
        short = page_id.replace("-", "")[:16]
        candidate = year_dir / f"{stem}--{short}.md"
    return candidate


def render_database_index(databases: List[Dict[str, Any]], generated_at: dt.datetime) -> str:
    lines = [
        "---",
        "source_system: notion",
        "scope: local-body-mirror",
        f"generated_at: {yaml_quote(format_time(generated_at))}",
        "---",
        "",
        "# Notion 本地正文索引",
        "",
        "这个索引只用于本地读取，`Raw/02 Notion/01 Databases/` 不会进入 public GitHub。",
        "",
    ]
    for db in databases:
        status = db.get("status") or "synced"
        suffix = f" ({status})" if status != "synced" else ""
        slug = db.get("slug") or safe_slug(str(db.get("name") or "database"), "database")
        sample = db.get("sample_limit")
        sample_note = f", sampled first {sample}" if sample else ""
        lines.append(f"- [{db['name']}](./{slug}/index.md): {db['page_count']} pages{sample_note}{suffix}")
    lines.append("")
    return "\n".join(lines)


def render_database_page_index(database: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    lines = [
        "---",
        "source_system: notion",
        f"database_name: {yaml_quote(database['name'])}",
        f"database_id: {yaml_quote(database['id'])}",
        f"title_property: {yaml_quote(database['title_property'])}",
        f"sample_limit: {database.get('sample_limit') or 0}",
        "---",
        "",
        f"# {database['name']}",
        "",
    ]
    if database.get("sample_limit"):
        lines.extend(
            [
                f"> [!warning] 这是抽样索引，只包含本轮前 {database['sample_limit']} 页，不代表数据库已完整导出。",
                "",
            ]
        )
    lines.extend(
        [
            "| 日期 | 标题 | Page ID | 更新时间 | 本地文件 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        date_text = row.get("date_text") or ""
        title = row.get("title") or ""
        page_id_short = row.get("page_id_short") or ""
        updated = row.get("updated") or ""
        path = row.get("path") or ""
        lines.append(
            f"| {date_text} | {title} | {page_id_short} | {updated} | [{path}]({path}) |"
        )
    lines.append("")
    return "\n".join(lines)


def relation_target_label(property_data: Dict[str, Any]) -> str:
    relation = property_data.get("relation")
    if not isinstance(relation, dict):
        return ""
    database_id = relation.get("database_id")
    if not database_id:
        return ""

    normalized_id = normalize_notion_id(str(database_id))
    for spec in DEFAULT_SYNC_DATABASE_SPECS:
        spec_id = spec.get("database_id") or spec.get("id")
        if spec_id and normalize_notion_id(str(spec_id)) == normalized_id:
            return f"{spec.get('name')} ({normalized_id})"
    return normalized_id


def property_type_label(prop_type: str, property_data: Dict[str, Any]) -> str:
    if prop_type == "relation":
        target = relation_target_label(property_data)
        return f"relation -> {target}" if target else "relation"
    if prop_type == "rollup":
        rollup = property_data.get("rollup")
        relation_name = ""
        if isinstance(rollup, dict):
            relation_name = str(rollup.get("relation_property_name") or "").strip()
        return f"rollup <- {relation_name}" if relation_name else "rollup"
    if prop_type == "formula":
        formula = property_data.get("formula")
        expression = ""
        if isinstance(formula, dict):
            expression = str(formula.get("expression") or "").strip()
        if expression:
            return f"formula: {truncate_utf8_bytes(expression, 80)}"
        return "formula"
    return prop_type


def render_database_schema(database: Dict[str, Any]) -> str:
    database_name = title_text_from_database(database)
    database_id = normalize_notion_id(str(database.get("id") or ""))
    lines = [
        "---",
        "source_system: notion",
        f"database_name: {yaml_quote(database_name)}",
        f"database_id: {yaml_quote(database_id)}",
        "---",
        "",
        f"# {database_name} Schema",
        "",
        "| 字段 | 类型 | 说明 |",
        "| --- | --- | --- |",
    ]

    properties = database.get("properties") or {}
    for name in sorted(properties):
        property_data = properties.get(name) or {}
        if not isinstance(property_data, dict):
            continue
        prop_type = str(property_data.get("type") or "")
        lines.append(
            f"| {name} | {prop_type or 'unknown'} | {property_type_label(prop_type, property_data)} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_structure_index(databases: List[Dict[str, Any]], generated_at: dt.datetime) -> str:
    lines = [
        "---",
        "source_system: notion",
        "scope: local-structure-index",
        f"generated_at: {yaml_quote(format_time(generated_at))}",
        "---",
        "",
        "# Notion 结构索引",
        "",
        "这个索引用于把高频 Notion hub、数据库 schema 和本地正文镜像串起来，供检索和 Hermes 先走结构入口。",
        "",
        "| Hub | 页面索引 | Schema | title/date | pages |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in databases:
        if item.get("status") == "skipped":
            continue
        slug = item["slug"]
        title_date = f"{item.get('title_property') or '-'} / {item.get('date_property') or '-'}"
        pages = f"{item.get('page_count') or 0}"
        if item.get("sample_limit"):
            pages += f" sample/{item['sample_limit']}"
        lines.append(
            f"| {item['name']} | [index](../01 Databases/{slug}/index.md) | [schema](../01 Databases/{slug}/schema.md) | {title_date} | {pages} |"
        )
    lines.append("")
    return "\n".join(lines)


def classify_relation_group(relation_name: str) -> str:
    name = relation_name.strip()
    if name in {"播客", "文章", "书籍", "阅读", "当日阅读", "影视", "影视（旧）", "笔记中心", "旧书架"}:
        return "输入"
    if name in {"事件与任务", "自动关联任务", "本周工作", "周复盘", "成功日记", "今日领悟", "总结", "工作展示"}:
        return "输出"
    if name in {"睡眠", "睡眠记录", "运动", "健康", "位置", "天气记录"}:
        return "身心/环境"
    if name in {"情绪", "情绪旧", "感恩日记", "人际中心"}:
        return "情绪/关系"
    if name in {"日收支", "每日收支", "收支项"}:
        return "财务"
    return "其他"


def classify_reading_group(relation_name: str) -> str:
    if relation_name == "感恩日记":
        return "输出"
    return classify_relation_group(relation_name)


def relation_reading_order(group: str, relation_name: str) -> Tuple[int, str]:
    order_map = {
        "输入": ["播客", "文章", "书籍", "阅读", "影视"],
        "输出": ["成功日记", "感恩日记", "今日领悟", "事件与任务"],
        "身心/环境": ["睡眠记录", "健康", "天气记录", "位置"],
        "情绪/关系": ["情绪", "人际中心"],
        "财务": ["日收支", "每日收支", "收支项"],
    }
    order = order_map.get(group, [])
    if relation_name in order:
        return order.index(relation_name), relation_name
    return len(order), relation_name


def display_group_heading(group: str) -> str:
    labels = {
        "输入": "🎧 输入",
        "输出": "📝 输出",
        "身心/环境": "🌿 身心/环境",
        "情绪/关系": "🤝 情绪/关系",
        "财务": "💰 财务",
        "其他": "📦 其他",
    }
    return labels.get(group, group)


def display_relation_heading(relation_name: str) -> str:
    labels = {
        "播客": "🎙️ 播客",
        "文章": "📄 文章",
        "成功日记": "✅ 成功日记",
        "感恩日记": "🙏 感恩日记",
        "今日领悟": "💡 今日领悟",
        "事件与任务": "📌 事件与任务",
        "睡眠记录": "😴 睡眠",
        "健康": "🩺 健康",
        "天气记录": "🌤️ 天气",
        "位置": "📍 位置",
        "日收支": "📊 今日总计",
        "每日收支": "🧾 收支明细",
    }
    return labels.get(relation_name, relation_name)


def page_title_from_page(page: Dict[str, Any]) -> str:
    properties = page.get("properties") or {}
    title_prop = None
    for name, prop in properties.items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title_prop = name
            break
    if not title_prop:
        return page.get("id") or "unknown-page"
    return page_title(page, title_prop)


def fetch_page_overview(
    api_key: str,
    page_id: str,
    page_index: Dict[str, Dict[str, Any]],
    page_cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    normalized_id = normalize_notion_id(page_id)
    if normalized_id in page_cache:
        return page_cache[normalized_id]
    page = notion_request(api_key, "GET", f"/pages/{normalized_id}")
    summary = summarize_properties(page)
    title = page_title_from_page(page)
    database_name = ""
    local_info = page_index.get(normalized_id)
    if local_info:
        database_name = local_info.get("database_name") or ""
    overview = {
        "page_id": normalized_id,
        "title": title,
        "database_name": database_name,
        "page_url": page.get("url") or f"https://www.notion.so/{normalized_id.replace('-', '')}",
        "summary": summary,
    }
    page_cache[normalized_id] = overview
    return overview


def choose_relation_fields(overview: Dict[str, Any], relation_name: str) -> List[Tuple[str, Any]]:
    summary = overview.get("summary") or {}
    if not isinstance(summary, dict):
        return []

    skipped_keys = {
        "记录/备注",
        "备注记录",
        "详细内容",
        "正文",
        "内容",
        "图片时间汇总",
        "创建时间",
        "创建日期",
        "Created At",
        "Created time",
        "created_time",
        "last_edited_time",
        "删除",
        "分割线",
        "一键绑定",
        "按钮",
        "补绑定",
        "添加某日事件",
    }
    priority_map = {
        "输入": ["主播", "作者", "评分", "状态", "收听时长", "收听进度", "时长", "发布时间", "日期", "短评", "Description", "链接"],
        "输出": ["状态", "评分", "日期", "业务", "能量", "短评", "总结", "Description", "链接"],
        "身心/环境": ["状态", "日期", "地点", "位置", "天气", "时长", "评分", "短评"],
        "情绪/关系": ["状态", "日期", "情绪", "评分", "感受", "短评", "Description"],
        "财务": ["金额", "日期", "类别", "类型", "状态", "备注", "Description"],
        "其他": ["状态", "日期", "评分", "短评", "Description", "链接"],
    }

    preferred = priority_map.get(classify_relation_group(relation_name), priority_map["其他"])
    chosen: List[Tuple[str, Any]] = []
    used_keys = set()

    for key in preferred:
        if key in skipped_keys:
            continue
        if key in summary and summary[key] not in (None, "", [], {}):
            chosen.append((key, summary[key]))
            used_keys.add(key)
            if len(chosen) >= 4:
                return chosen

    for key, value in summary.items():
        if key in used_keys:
            continue
        if key in skipped_keys:
            continue
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict) and "count" in value and "ids" in value:
            continue
        if isinstance(value, list) and len(value) > 4:
            continue
        chosen.append((key, value))
        if len(chosen) >= 4:
            break
    return chosen


def clean_preview_text(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("![") or line.startswith("[image]"):
            continue
        line = re.sub(r"#+\s*", "", line)
        line = re.sub(r"\b\d{12,19}\b", "[redacted-number]", line)
        line = re.sub(r"\b\d{3,4}\b", lambda m: "[redacted-code]" if len(text) > 80 else m.group(0), line)
        lines.append(line)
        if len(" / ".join(lines)) > 180:
            break
    cleaned = " / ".join(lines).strip()
    return cleaned[:177] + "..." if len(cleaned) > 180 else cleaned


def format_preview_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        if "start" in value or "end" in value:
            start = value.get("start") or ""
            end = value.get("end") or ""
            if start and end and start != end:
                return f"{start} → {end}"
            return start or end or ""
        if "count" in value and "ids" in value:
            return f"{value.get('count')} 条关联"
        rendered = json.dumps(value, ensure_ascii=False)
        return clean_preview_text(rendered)
    if isinstance(value, list):
        items = [clean_preview_text(str(item)) for item in value if item not in (None, "", [], {})]
        items = [item for item in items if item]
        if not items:
            return ""
        text = "、".join(items[:4])
        if len(items) > 4:
            text += f" 等 {len(items)} 项"
        return clean_preview_text(text)
    return clean_preview_text(str(value))


def is_id_like_title(title: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{24,40}", title.strip()))


def clean_relation_title(raw_title: Any, fallback_id: str) -> str:
    title = clean_preview_text(str(raw_title or ""))
    if not title or title.lower() == "none":
        return fallback_id
    return title


def relation_body_preview(api_key: str, page_id: str) -> str:
    try:
        blocks = retrieve_block_children(api_key, page_id)
    except Exception:
        return ""
    rendered = clean_preview_text("\n".join(render_blocks(api_key, blocks)))
    if rendered.startswith("[") and rendered.endswith("]"):
        return ""
    return rendered


def format_date_for_reading(value: Any, keep_time: bool = False) -> str:
    rendered = format_preview_value(value)
    if not rendered:
        return ""

    def trim_one(part: str) -> str:
        part = part.strip()
        parsed = parse_time(part)
        if not parsed:
            return part
        if keep_time and ("T" in part or re.search(r"\d{1,2}:\d{2}", part)):
            return parsed.strftime("%Y-%m-%d %H:%M")
        return parsed.strftime("%Y-%m-%d")

    if " → " in rendered:
        start, end = rendered.split(" → ", 1)
        start_text = trim_one(start)
        end_text = trim_one(end)
        if start_text == end_text:
            return start_text
        return f"{start_text} → {end_text}"
    return trim_one(rendered)


def format_time_for_reading(value: Any) -> str:
    rendered = format_preview_value(value)
    parsed = parse_time(rendered)
    if parsed:
        return parsed.strftime("%H:%M")
    return rendered


def first_summary_value(summary: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        value = summary.get(key)
        if isinstance(value, dict) and "count" in value and "ids" in value and not value.get("ids"):
            continue
        if value not in (None, "", [], {}):
            return value
    return None


def format_sleep_duration(value: Any) -> str:
    rendered = format_preview_value(value)
    if not rendered:
        return ""
    if re.fullmatch(r"\d+(\.\d+)?", rendered):
        return f"{rendered} 小时"
    return rendered


def first_relation_title(
    api_key: str,
    summary: Dict[str, Any],
    keys: List[str],
    page_index: Dict[str, Dict[str, Any]],
    page_cache: Dict[str, Dict[str, Any]],
) -> str:
    for key in keys:
        value = summary.get(key)
        if not (isinstance(value, dict) and value.get("ids")):
            continue
        rel_id = str(value["ids"][0])
        overview = fetch_page_overview(api_key, rel_id, page_index, page_cache)
        title = clean_relation_title(overview.get("title"), rel_id[:8])
        if title and not is_id_like_title(title):
            return title
    return ""


def append_field(lines: List[str], label: str, value: Any) -> None:
    rendered = format_preview_value(value)
    if rendered:
        lines.append(f"- {label}：{rendered}")


def render_relation_card(
    api_key: str,
    relation_name: str,
    relation_page_id: str,
    page_index: Dict[str, Dict[str, Any]],
    page_cache: Dict[str, Dict[str, Any]],
    current_page_path: Optional[Path],
) -> List[str]:
    overview = fetch_page_overview(api_key, relation_page_id, page_index, page_cache)
    title = overview.get("title") or relation_page_id[:8]
    local_info = page_index.get(normalize_notion_id(relation_page_id))
    link_text = title
    if local_info and local_info.get("path") and local_info.get("database_slug") and current_page_path:
        link_path = Path("..") / ".." / local_info["database_slug"] / local_info["path"]
        link_text = f"[{title}]({link_path.as_posix()})"
    else:
        link_text = f"[{title}]({overview.get('page_url')})"

    lines = [f"- {link_text}"]
    if overview.get("database_name"):
        lines.append(f"  - 来源：{overview['database_name']}")

    preview_fields = choose_relation_fields(overview, relation_name)
    for key, value in preview_fields:
        rendered = format_preview_value(value)
        if rendered:
            lines.append(f"  - {key}：{rendered}")
    return lines


def render_relation_index_item(
    relation_name: str,
    relation_page_id: str,
    page_index: Dict[str, Dict[str, Any]],
    current_page_path: Optional[Path],
) -> str:
    normalized_id = normalize_notion_id(relation_page_id)
    local_info = page_index.get(normalized_id)
    if local_info and local_info.get("path") and local_info.get("database_slug") and current_page_path:
        link_path = Path("..") / ".." / local_info["database_slug"] / local_info["path"]
        target = link_path.as_posix()
    else:
        target = f"https://www.notion.so/{normalized_id.replace('-', '')}"
    return f"- {relation_name}: [{normalized_id[:8]}]({target})"


def render_relation_reading_card(
    api_key: str,
    relation_name: str,
    relation_page_id: str,
    page_index: Dict[str, Dict[str, Any]],
    page_cache: Dict[str, Dict[str, Any]],
) -> List[str]:
    overview = fetch_page_overview(api_key, relation_page_id, page_index, page_cache)
    title = clean_relation_title(overview.get("title"), relation_page_id[:8])
    summary = overview.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}

    if relation_name in {"周复盘", "图片"}:
        return []

    if relation_name == "播客":
        lines = [f"##### {title}"]
        podcast_title = first_relation_title(api_key, summary, ["Podcast", "播客"], page_index, page_cache)
        append_field(lines, "节目", podcast_title)
        append_field(lines, "评分", first_summary_value(summary, ["评分"]))
        append_field(lines, "状态", first_summary_value(summary, ["状态"]))
        append_field(lines, "收听时长", first_summary_value(summary, ["收听时长"]))
        return lines

    if relation_name == "文章":
        lines = [f"##### {title}"]
        append_field(lines, "作者", first_summary_value(summary, ["作者"]))
        append_field(lines, "标签", first_summary_value(summary, ["Tags", "标签"]))
        return lines

    if relation_name in {"成功日记", "感恩日记"}:
        if is_id_like_title(title):
            title = relation_body_preview(api_key, relation_page_id) or title
        lines: List[str] = []
        if title and not is_id_like_title(title):
            lines.append(f"- {title}")
        for key in ["内容", "正文", "记录/备注", "备注记录", "Description", "描述"]:
            value = summary.get(key)
            rendered = format_preview_value(value)
            if rendered and rendered != title:
                lines.append(f"- {rendered}")
                break
        return lines

    if relation_name == "今日领悟":
        lines = [f"##### {title}"]
        tags = first_summary_value(summary, ["Tags", "标签"])
        append_field(lines, "标签", tags)
        return lines

    if relation_name == "事件与任务":
        status = format_preview_value(first_summary_value(summary, ["状态", "Status"]))
        marker = "✅" if status in {"已完成", "完成", "Done", "done"} else "▶️"
        lines = [f"##### {marker} {title}"]
        date_value = first_summary_value(summary, ["日期", "Date", "时间"])
        date_text = format_date_for_reading(date_value, keep_time=True)
        if date_text:
            lines.append(f"- 时间：{date_text}")
        return lines

    if relation_name == "位置":
        lines = [f"##### {title}"]
        time_text = format_time_for_reading(first_summary_value(summary, ["时间", "日期", "Date"]))
        if time_text:
            lines.append(f"- 时间：{time_text}")
        return lines

    if relation_name == "天气记录":
        weather = format_preview_value(first_summary_value(summary, ["气候", "天气", "状态"]))
        lines = [f"##### {weather or title}"]
        return lines

    if relation_name == "健康":
        lines = [f"##### {title}"]
        desc = first_summary_value(summary, ["描述", "Description", "记录/备注", "备注", "状态"])
        rendered = format_preview_value(desc)
        if rendered and rendered != title and rendered != "默认":
            lines.append(f"- {rendered}")
        return lines

    if relation_name == "睡眠记录":
        lines = [f"##### {title}"]
        duration = first_summary_value(summary, ["睡眠时长", "时长"])
        period = first_summary_value(summary, ["睡眠时间", "睡眠时间段", "时间段", "入睡时间", "睡觉时间"])
        wake_state = format_preview_value(first_summary_value(summary, ["起床状态", "状态"]))
        append_field(lines, "睡眠时间段", period)
        duration_text = format_sleep_duration(duration)
        if duration_text:
            lines.append(f"- 睡眠时长：{duration_text}")
        if wake_state and wake_state != "未选择":
            lines.append(f"- 起床状态：{wake_state}")
        return lines

    if relation_name == "日收支":
        total_text = format_preview_value(first_summary_value(summary, ["日支出展示", "今日支出展示", "今日支出", "数字"]))
        if total_text and "今日支出" not in total_text:
            total_text = f"今日支出：¥{total_text}"
        return [f"##### {total_text or title}"]

    if relation_name == "每日收支":
        amount = format_preview_value(first_summary_value(summary, ["金额", "数字"]))
        category = format_preview_value(first_summary_value(summary, ["类别", "类型"]))
        time_text = format_time_for_reading(first_summary_value(summary, ["时间", "日期", "Date"]))
        suffix = f" ¥{amount}" if amount else ""
        lines = [f"##### {title}{suffix}"]
        details = "，".join(part for part in [category, time_text] if part)
        if details:
            lines.append(f"- {details}")
        return lines

    lines = [f"##### {title}"]
    for key, value in choose_relation_fields(overview, relation_name):
        if key in {"链接", "Link", "原文地址", "URL", "url"}:
            continue
        rendered = format_preview_value(value)
        if rendered and rendered != title:
            lines.append(f"- {key}：{rendered}")
    return lines


def render_public_manifest(
    databases: List[Dict[str, Any]],
    generated_at: dt.datetime,
    export_body: bool,
) -> Dict[str, Any]:
    return {
        "source_system": "notion",
        "scope": "public-safe",
        "generated_at": format_time(generated_at),
        "export_body": export_body,
        "databases": databases,
    }


def sync_database(
    api_key: str,
    spec: Dict[str, Any],
    args: argparse.Namespace,
    public_state: Dict[str, Any],
    local_state: Dict[str, Any],
    page_index: Dict[str, Dict[str, Any]],
    page_cache: Dict[str, Dict[str, Any]],
    generated_at: dt.datetime,
) -> Dict[str, Any]:
    query = spec.get("name") or spec.get("database_id") or spec.get("id")
    configured_id = spec.get("database_id") or spec.get("id")
    database = resolve_database(api_key, configured_id, str(query))
    database_name = title_text_from_database(database)
    database_id = normalize_notion_id(str(database.get("id") or configured_id or query))
    database_slug = slug_for_database(database_name, database_id)
    title_prop = spec.get("title_property") or title_property_name(database)
    date_prop = date_property_name(database, spec.get("date_property"))

    db_public_key = database_slug
    db_local_key = database_id
    database_meta = {
        "database_name": database_name,
        "database_id": database_id,
        "database_slug": database_slug,
        "title_property": title_prop,
        "date_property": date_prop,
    }

    cutoff = None
    if not args.full_sync:
        cutoff = state_cutoff(local_state, db_local_key) or state_cutoff(public_state, db_public_key)
    if cutoff is None and not args.full_sync:
        cutoff = generated_at - dt.timedelta(hours=args.window_hours)

    filter_obj = None
    if cutoff is not None:
        filter_obj = {
            "timestamp": "last_edited_time",
            "last_edited_time": {"on_or_after": format_time(cutoff)},
        }
    sorts = [{"timestamp": "last_edited_time", "direction": "descending"}]

    synced_rows: List[Dict[str, Any]] = []
    synced_count = 0
    page_map = (local_state.get("databases") or {}).get(db_local_key, {}).get("pages", {}) or {}

    for page in paginate_database(api_key, database_id, filter_obj=filter_obj, sorts=sorts):
        if page.get("archived"):
            continue
        page_id = normalize_notion_id(str(page.get("id") or ""))
        title = page_title(page, title_prop)
        page_last_edited = parse_time(page.get("last_edited_time")) or generated_at
        date_hint = page_date_hint(page, date_prop, page_last_edited)
        relative_path = ""
        if args.export_body:
            database_dir = DATABASES_DIR / database_slug
            path = make_page_path(database_dir, date_hint, title, page_id, page_map)
            relative_path = str(path.relative_to(database_dir))
            markdown, properties, date_hint = render_page_markdown(
                api_key=api_key,
                database=database,
                page=page,
                title_prop=title_prop,
                date_prop=date_prop,
                include_body=True,
                page_path=path,
                page_index=page_index,
                page_cache=page_cache,
            )
            page_meta = {
                "title": title,
                "page_id_short": page_id.replace("-", "")[:16],
                "updated": format_time(page_last_edited),
                "date_text": date_hint.strftime("%Y-%m-%d"),
                "path": relative_path,
            }
            synced_rows.append(page_meta)
            if not args.dry_run:
                old_path = None
                current = page_map.get(page_id)
                if isinstance(current, dict):
                    stored = current.get("path")
                    if stored:
                        old_path = database_dir / stored
                if old_path and old_path != path and old_path.exists():
                    old_path.unlink()
                if write_text(path, markdown):
                    pass
                page_map[page_id] = {
                    "path": relative_path,
                    "title": title,
                    "last_edited_time": format_time(page_last_edited),
                    "date_hint": format_time(date_hint),
                }
                page_index[page_id] = {
                    "database_slug": database_slug,
                    "database_name": database_name,
                    "title": title,
                    "path": relative_path,
                    "date_hint": format_time(date_hint),
                }
        synced_count += 1
        if args.max_pages and synced_count >= args.max_pages:
            break

    last_sync_at = generated_at if cutoff is None else max(cutoff, generated_at)

    if args.export_body and not args.dry_run:
        local_state = merge_database_state(
            local_state,
            db_local_key,
            database_meta,
            synced_count,
            last_sync_at,
            page_map=page_map,
            full_sync=args.full_sync,
        )
        removed_duplicates = prune_duplicate_page_files(DATABASES_DIR / database_slug, page_map)
        if removed_duplicates:
            print(f"Pruned {len(removed_duplicates)} duplicate local files for {database_name}")
    public_state = merge_database_state(
        public_state,
        db_public_key,
        database_meta,
        synced_count,
        last_sync_at,
        page_map=None,
        full_sync=args.full_sync,
    )

    return {
        "name": database_name,
        "id": database_id,
        "slug": database_slug,
        "title_property": title_prop,
        "date_property": date_prop,
        "page_count": synced_count,
        "sample_limit": args.max_pages or 0,
        "updated_at": format_time(last_sync_at),
        "export_body": bool(args.export_body),
        "page_rows": synced_rows,
        "database_object": database,
    }


def write_logs(
    generated_at: dt.datetime,
    results: List[Dict[str, Any]],
    args: argparse.Namespace,
) -> None:
    PUBLIC_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = PUBLIC_LOG_DIR / f"{generated_at.strftime('%Y-%m-%d')}-notion-sync.md"
    lines = [
        "---",
        "source_system: notion",
        f"generated_at: {yaml_quote(format_time(generated_at))}",
        f"mode: {yaml_quote('full-export' if args.export_body else 'metadata-only')}",
        f"full_sync: {'true' if args.full_sync else 'false'}",
        "---",
        "",
        "# Notion 同步日志",
        "",
    ]
    total = 0
    for item in results:
        total += int(item.get("page_count") or 0)
        status = item.get("status") or "synced"
        suffix = f" [{status}]" if status != "synced" else ""
        lines.append(f"- {item['name']}: {item['page_count']} pages{suffix}")
    lines.append("")
    lines.append(f"总计：{total} pages")
    lines.append("")
    write_text(log_path, "\n".join(lines))


def save_state_and_manifest(
    synced_databases: Dict[str, Dict[str, Any]],
    public_state: Dict[str, Any],
    local_state: Dict[str, Any],
    results: List[Dict[str, Any]],
    generated_at: dt.datetime,
    args: argparse.Namespace,
) -> None:
    manifest_databases = [
        {
            "name": item["name"],
            "id": item["id"],
            "slug": item["slug"],
            "title_property": item["title_property"],
            "date_property": item["date_property"],
            "page_count": item["page_count"],
            "updated_at": item["updated_at"],
        }
        for item in results
    ]
    public_manifest = render_public_manifest(manifest_databases, generated_at, bool(args.export_body))
    if not args.dry_run:
        write_json(PUBLIC_MANIFEST_PATH, public_manifest)
        write_json(PUBLIC_STATE_DIR / "sync-state.json", public_state)
        if args.export_body:
            write_json(LOCAL_STATE_PATH, local_state)
            index_rows = []
            for item in results:
                index_rows.append(
                    {
                        "name": item["name"],
                        "slug": item["slug"],
                        "page_count": item["page_count"],
                        "sample_limit": item.get("sample_limit") or 0,
                        "status": item.get("status") or "synced",
                    }
                )
            write_text(DATABASES_INDEX_PATH, render_database_index(index_rows, generated_at))
            for item in results:
                if item.get("status") == "skipped":
                    continue
                database_dir = DATABASES_DIR / item["slug"]
                rows = item.get("page_rows") or []
                write_text(database_dir / "index.md", render_database_page_index(item, rows))
                database_object = synced_databases.get(item["id"])
                if database_object:
                    write_text(database_dir / "schema.md", render_database_schema(database_object))
            write_text(STRUCTURE_INDEX_PATH, render_structure_index(results, generated_at))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        action="append",
        help="Notion database title or database ID. May be repeated.",
    )
    parser.add_argument(
        "--database-json",
        help="JSON array of database specs. Each item may be a string title/ID or an object.",
    )
    parser.add_argument("--full-sync", action="store_true", help="Ignore sync state and fetch all pages.")
    parser.add_argument(
        "--export-body",
        action="store_true",
        help="Write full page bodies into Raw/02 Notion/01 Databases/ (local-only).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing files.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(load_env("NOTION_SYNC_MAX_PAGES", "0")),
        help="Limit pages per database for batching and testing. 0 means no limit.",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=int(load_env("NOTION_SYNC_WINDOW_HOURS", "24")),
        help="Fallback lookback window when no sync state exists.",
    )
    parser.add_argument(
        "--skip-missing-databases",
        action="store_true",
        help="Skip database lookup failures instead of aborting the whole sync.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.export_body:
        args.export_body = False

    generated_at = now()
    api_key = load_notion_api_key()
    specs = target_specs(args)

    public_state = load_public_manifest()
    local_state = load_local_state()
    page_index = build_page_index(local_state)
    page_cache: Dict[str, Dict[str, Any]] = {}

    results: List[Dict[str, Any]] = []
    synced_databases: Dict[str, Dict[str, Any]] = {}
    for spec in specs:
        try:
            result = sync_database(api_key, spec, args, public_state, local_state, page_index, page_cache, generated_at)
        except SystemExit as exc:
            if args.skip_missing_databases:
                results.append(
                    {
                        "name": spec.get("name") or spec.get("database_id") or spec.get("id") or "unknown",
                        "id": normalize_notion_id(str(spec.get("database_id") or spec.get("id") or "")),
                        "slug": safe_slug(str(spec.get("name") or "unknown"), "database"),
                        "title_property": spec.get("title_property"),
                        "date_property": spec.get("date_property"),
                        "page_count": 0,
                        "updated_at": format_time(generated_at),
                        "export_body": bool(args.export_body),
                        "page_rows": [],
                        "status": "skipped",
                        "error": str(exc),
                    }
                )
                continue
            raise
        except RuntimeError as exc:
            if args.skip_missing_databases and (
                "object_not_found" in str(exc)
                or "Could not find database" in str(exc)
                or "404" in str(exc)
            ):
                results.append(
                    {
                        "name": spec.get("name") or spec.get("database_id") or spec.get("id") or "unknown",
                        "id": normalize_notion_id(str(spec.get("database_id") or spec.get("id") or "")),
                        "slug": safe_slug(str(spec.get("name") or "unknown"), "database"),
                        "title_property": spec.get("title_property"),
                        "date_property": spec.get("date_property"),
                        "page_count": 0,
                        "updated_at": format_time(generated_at),
                        "export_body": bool(args.export_body),
                        "page_rows": [],
                        "status": "skipped",
                        "error": str(exc),
                    }
                )
                continue
            raise
        database_object = result.pop("database_object", None)
        if isinstance(database_object, dict):
            synced_databases[result["id"]] = database_object
        results.append(result)

    save_state_and_manifest(synced_databases, public_state, local_state, results, generated_at, args)
    write_logs(generated_at, results, args)

    total_pages = sum(int(item.get("page_count") or 0) for item in results)
    mode = "full-export" if args.export_body else "metadata-only"
    print(f"Synced {len(results)} databases, {total_pages} pages, mode={mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
