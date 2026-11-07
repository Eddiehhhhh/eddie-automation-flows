#!/usr/bin/env python3
"""Link Notion workout entries to the same-day diary page."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")
TZ = dt.timezone(dt.timedelta(hours=8))
UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})"
)


def load_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def require_env(name: str) -> str:
    value = load_env(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value.strip()


def load_notion_api_key() -> str:
    value = load_env("NOTION_API_KEY")
    if value:
        return value.strip()

    services_path = Path.home() / ".codex" / "secrets" / "services.json"
    if services_path.exists():
        data = json.loads(services_path.read_text(encoding="utf-8"))
        notion = data.get("notion") or {}
        auth = notion.get("auth") or {}
        token = auth.get("token")
        if token:
            return str(token).strip()

    raise SystemExit("Missing required Notion credential: NOTION_API_KEY or ~/.codex/secrets/services.json notion.auth.token")


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
        "User-Agent": "eddie-wiki-notion-workout-link/1.0",
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
            with urllib.request.urlopen(req, timeout=30) as resp:
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


def get_database(api_key: str, database_id: str) -> Dict[str, Any]:
    return notion_request(api_key, "GET", f"/databases/{normalize_notion_id(database_id)}")


def search_databases(api_key: str, query: str) -> List[Dict[str, Any]]:
    payload = {"query": query, "filter": {"property": "object", "value": "database"}}
    response = notion_request(api_key, "POST", "/search", payload)
    return [item for item in response.get("results", []) if item.get("object") == "database"]


def title_text_from_database(database: Dict[str, Any]) -> str:
    title = database.get("title") or []
    return rich_text_plain_text(title).strip()


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
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"page_size": page_size}
    if filter_obj is not None:
        payload["filter"] = filter_obj
    if start_cursor:
        payload["start_cursor"] = start_cursor
    return notion_request(api_key, "POST", f"/databases/{normalize_notion_id(database_id)}/query", payload)


def update_page(api_key: str, page_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
    return notion_request(api_key, "PATCH", f"/pages/{normalize_notion_id(page_id)}", {"properties": properties})


def paginate_database(
    api_key: str,
    database_id: str,
    filter_obj: Optional[Dict[str, Any]] = None,
) -> Iterable[Dict[str, Any]]:
    cursor: Optional[str] = None
    while True:
        response = query_database(api_key, database_id, filter_obj=filter_obj, start_cursor=cursor)
        for item in response.get("results", []):
            yield item
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
        if not cursor:
            break


def rich_text_plain_text(parts: Sequence[Dict[str, Any]]) -> str:
    return "".join(part.get("plain_text", "") for part in parts if isinstance(part, dict))


def title_from_page(page: Dict[str, Any], title_property: str) -> str:
    properties = page.get("properties") or {}
    value = properties.get(title_property) or {}
    if not isinstance(value, dict):
        return page.get("id") or "unknown-page"
    title = value.get("title") or value.get("rich_text") or []
    text = rich_text_plain_text(title)
    return text.strip() or page.get("id") or "unknown-page"


def page_property(page: Dict[str, Any], property_name: str) -> Dict[str, Any]:
    properties = page.get("properties") or {}
    if property_name in properties and isinstance(properties[property_name], dict):
        return properties[property_name]
    for value in properties.values():
        if isinstance(value, dict) and value.get("id") == property_name:
            return value
    raise KeyError(property_name)


def property_choice(
    database: Dict[str, Any],
    configured: Optional[str],
    expected_type: Optional[str] = None,
    keywords: Sequence[str] = (),
    relation_target_database_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    properties = database.get("properties") or {}
    normalized_target = normalize_notion_id(relation_target_database_id) if relation_target_database_id else None

    if configured:
        for name, prop in properties.items():
            if configured == name or configured == prop.get("id"):
                if expected_type and prop.get("type") != expected_type:
                    raise SystemExit(
                        f"Configured property {configured!r} on {database.get('title', database.get('id'))!r} has type "
                        f"{prop.get('type')!r}, expected {expected_type!r}"
                    )
                return name, prop
        available = ", ".join(sorted(properties.keys()))
        raise SystemExit(
            f"Configured property {configured!r} was not found on database {database.get('id')!r}. "
            f"Available properties: {available}"
        )

    candidates: List[Tuple[int, str, Dict[str, Any]]] = []
    for name, prop in properties.items():
        if expected_type and prop.get("type") != expected_type:
            continue
        if normalized_target and prop.get("type") == "relation":
            relation = prop.get("relation") or {}
            target = relation.get("database_id") or relation.get("data_source_id") or relation.get("related_database_id")
            if target and normalize_notion_id(str(target)) != normalized_target:
                continue
        score = 0
        lower_name = name.lower()
        for keyword in keywords:
            if keyword.lower() in lower_name or keyword in name:
                score += 10
        if expected_type:
            score += 1
        candidates.append((score, name, prop))

    if not candidates and normalized_target:
        for name, prop in properties.items():
            if expected_type and prop.get("type") != expected_type:
                continue
            if prop.get("type") == "relation":
                candidates.append((0, name, prop))

    if not candidates:
        available = ", ".join(sorted(properties.keys()))
        raise SystemExit(
            f"Could not auto-detect a {expected_type or 'property'} on database {database.get('id')!r}. "
            f"Available properties: {available}"
        )

    best_score = max(item[0] for item in candidates)
    best = [item for item in candidates if item[0] == best_score]
    if len(best) == 1:
        return best[0][1], best[0][2]

    keyword_matches = [
        item for item in best if any(keyword.lower() in item[1].lower() or keyword in item[1] for keyword in keywords)
    ]
    if len(keyword_matches) == 1:
        return keyword_matches[0][1], keyword_matches[0][2]

    available = ", ".join(sorted(name for _, name, _ in best))
    raise SystemExit(
        f"Ambiguous {expected_type or 'property'} selection on database {database.get('id')!r}. "
        f"Candidates: {available}"
    )


def date_value(page: Dict[str, Any], property_name: str) -> Optional[str]:
    try:
        value = page_property(page, property_name)
    except KeyError:
        return None
    if value.get("type") != "date":
        return None
    date_obj = value.get("date") or {}
    start = date_obj.get("start")
    if not start:
        return None
    return str(start)[:10]


def relation_ids(page: Dict[str, Any], property_name: str) -> List[str]:
    try:
        value = page_property(page, property_name)
    except KeyError:
        return []
    if value.get("type") != "relation":
        return []
    ids = []
    for item in value.get("relation") or []:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
    return ids


def iso_cutoff(hours_back: float) -> str:
    cutoff = dt.datetime.now(TZ) - dt.timedelta(hours=hours_back)
    return cutoff.isoformat(timespec="seconds")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Link Notion workout entries to the same-day diary page")
    parser.add_argument("--dry-run", action="store_true", help="Resolve links without updating Notion")
    parser.add_argument(
        "--hours-back",
        type=float,
        default=float(load_env("NOTION_WORKOUT_SYNC_HOURS", "72") or 72),
        help="Look back window in hours for recently edited workout pages",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit number of workout pages processed")
    args = parser.parse_args()

    if load_env("NOTION_DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}:
        args.dry_run = True

    api_key = load_notion_api_key()
    workout_database_id = load_env("NOTION_WORKOUT_DATABASE_ID")
    diary_database_id = load_env("NOTION_DIARY_DATABASE_ID")

    workout_database = resolve_database(api_key, workout_database_id, load_env("NOTION_WORKOUT_DATABASE_NAME", "运动") or "运动")
    diary_database = resolve_database(api_key, diary_database_id, load_env("NOTION_DIARY_DATABASE_NAME", "日记中心") or "日记中心")
    workout_database_id = str(workout_database.get("id"))
    diary_database_id = str(diary_database.get("id"))

    workout_date_property, _ = property_choice(
        workout_database,
        load_env("NOTION_WORKOUT_DATE_PROPERTY"),
        expected_type="date",
        keywords=("开始", "start", "日期", "date"),
    )
    diary_date_property, _ = property_choice(
        diary_database,
        load_env("NOTION_DIARY_DATE_PROPERTY"),
        expected_type="date",
        keywords=("日期", "date"),
    )
    workout_relation_property, _ = property_choice(
        workout_database,
        load_env("NOTION_WORKOUT_DIARY_RELATION_PROPERTY"),
        expected_type="relation",
        keywords=("日记", "diary", "journal"),
        relation_target_database_id=diary_database_id,
    )
    title_property, _ = property_choice(workout_database, None, expected_type="title", keywords=("标题", "名称", "title", "name"))

    cutoff = iso_cutoff(args.hours_back)
    filter_obj = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": cutoff}}

    diary_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    stats = {
        "scanned": 0,
        "linked": 0,
        "unchanged": 0,
        "missing_start_date": 0,
        "missing_diary": 0,
        "ambiguous_diary": 0,
        "dry_run": args.dry_run,
        "hours_back": args.hours_back,
    }

    log(
        "Resolved Notion properties: "
        f"workout_date={workout_date_property!r}, diary_date={diary_date_property!r}, "
        f"workout_relation={workout_relation_property!r}, title={title_property!r}"
    )
    log(f"Using cutoff: {cutoff}")

    for page in paginate_database(api_key, workout_database_id, filter_obj=filter_obj):
        stats["scanned"] += 1
        if args.limit is not None and stats["scanned"] > args.limit:
            break

        page_id = str(page.get("id") or "")
        page_title = title_from_page(page, title_property)
        start_date = date_value(page, workout_date_property)
        if not start_date:
            stats["missing_start_date"] += 1
            log(f"skip page={page_id} title={page_title!r} reason=missing_start_date")
            continue

        if start_date not in diary_cache:
            diary_filter = {"property": diary_date_property, "date": {"equals": start_date}}
            diary_matches = list(paginate_database(api_key, diary_database_id, filter_obj=diary_filter))
            if len(diary_matches) == 1:
                diary_cache[start_date] = diary_matches[0]
            elif len(diary_matches) == 0:
                diary_cache[start_date] = None
            else:
                diary_cache[start_date] = {"_ambiguous": diary_matches}  # type: ignore[assignment]

        diary_entry = diary_cache[start_date]
        if diary_entry is None:
            stats["missing_diary"] += 1
            log(f"skip page={page_id} title={page_title!r} date={start_date} reason=diary_not_found")
            continue
        if isinstance(diary_entry, dict) and diary_entry.get("_ambiguous"):
            stats["ambiguous_diary"] += 1
            candidates = [
                f"{candidate.get('id')}:{date_value(candidate, diary_date_property)}"
                for candidate in diary_entry["_ambiguous"]  # type: ignore[index]
            ]
            log(
                f"skip page={page_id} title={page_title!r} date={start_date} "
                f"reason=diary_ambiguous candidates={candidates}"
            )
            continue

        diary_page_id = str(diary_entry.get("id"))
        current_relation_ids = relation_ids(page, workout_relation_property)
        if current_relation_ids == [diary_page_id]:
            stats["unchanged"] += 1
            log(f"unchanged page={page_id} title={page_title!r} date={start_date} diary={diary_page_id}")
            continue

        if args.dry_run:
            stats["linked"] += 1
            log(
                f"dry_run page={page_id} title={page_title!r} date={start_date} "
                f"relation={current_relation_ids} -> diary={diary_page_id}"
            )
            continue

        update_page(api_key, page_id, {workout_relation_property: {"relation": [{"id": diary_page_id}]}})
        stats["linked"] += 1
        log(
            f"linked page={page_id} title={page_title!r} date={start_date} "
            f"relation={current_relation_ids} -> diary={diary_page_id}"
        )

    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
