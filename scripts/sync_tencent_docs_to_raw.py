#!/usr/bin/env python3
"""Sync Tencent Docs targets into Raw/06 TencentDocs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, request

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
RAW_ROOT = ROOT / "Raw" / "06 TencentDocs"
DEFAULT_CONFIG = ROOT / ".github" / "tencent-docs-sync-targets.json"
MCP_ENDPOINT = "https://docs.qq.com/openapi/mcp"
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Path to Tencent Docs sync config JSON.",
    )
    return parser.parse_args()


def today_shanghai() -> str:
    if ZoneInfo is None:
        return datetime.utcnow().strftime("%Y-%m-%d")
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def load_token() -> str:
    token = os.environ.get("TENCENT_DOCS_TOKEN", "").strip()
    if token:
        return token

    credentials_path = Path.home() / ".mcporter" / "credentials.json"
    if credentials_path.exists():
        data = json.loads(credentials_path.read_text(encoding="utf-8"))
        for entry in (data.get("entries") or {}).values():
            if entry.get("serverName") != "tencent-docs":
                continue
            candidate = ((entry.get("tokens") or {}).get("access_token") or "").strip()
            if candidate:
                return candidate

    raise RuntimeError(
        "Tencent Docs token not found. Set TENCENT_DOCS_TOKEN or provide ~/.mcporter/credentials.json."
    )


def mcp_call(tool_name: str, arguments: dict[str, Any], token: str) -> dict[str, Any]:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
    ).encode("utf-8")
    req = request.Request(
        MCP_ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:  # pragma: no cover
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MCP HTTP error {exc.code}: {detail}") from exc

    result = body.get("result") or {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    blocks = result.get("content") or []
    if blocks and isinstance(blocks[0], dict) and "text" in blocks[0]:
        return json.loads(blocks[0]["text"])
    if isinstance(result, dict):
        return result
    raise RuntimeError(f"Unexpected MCP response for {tool_name}: {body}")


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Config root must be an object.")
    payload.setdefault("search_keywords", [])
    payload.setdefault("pinned_files", [])
    payload.setdefault("ignore_file_ids", [])
    return payload


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def existing_file_map() -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    if not RAW_ROOT.exists():
        return mapping
    for path in RAW_ROOT.rglob("*.md"):
        if path.name == "README.md":
            continue
        frontmatter = parse_frontmatter(path.read_text(encoding="utf-8"))
        file_id = frontmatter.get("file_id")
        if file_id:
            mapping[file_id] = path
    return mapping


def infer_doc_type(source_url: str) -> str:
    if "/form/page/" in source_url:
        return "form"
    parts = source_url.split("docs.qq.com/", 1)
    if len(parts) < 2:
        return "doc"
    head = parts[1].split("/", 1)[0].split("?", 1)[0].strip()
    return head or "doc"


def normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", value).strip().lower()


def clean_content(title: str, content: str) -> str:
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\n", "\n")
    lines = text.splitlines()
    while lines and normalize_title(lines[0]) == normalize_title(title):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def sanitize_filename(title: str) -> str:
    cleaned = title.strip()
    cleaned = re.sub(r"[\\/:*?\"<>|]", "-", cleaned)
    cleaned = cleaned.replace("\u0000", "")
    return cleaned or "未命名腾讯文档"


def classify_folder(title: str, content: str, source_url: str, doc_type: str) -> str:
    title_text = title.strip()
    full_text = f"{title}\n{content}"

    def title_has_any(*terms: str) -> bool:
        return any(term in title_text for term in terms)

    def text_has_any(*terms: str) -> bool:
        return any(term in full_text for term in terms)

    if title_has_any("一面", "二面", "面试记录") or (
        title_has_any("评定表") and text_has_any("面试")
    ):
        return "面试记录"
    if title_has_any("工作站"):
        return "工作站"
    if title_has_any("述职", "选举", "打分表"):
        return "述职选举"
    if title_has_any("换届"):
        return "换届"
    if title_has_any("百科", "关系"):
        return "组织关系与百科"
    if title_has_any("制度", "管理台", "To Do", "职责", "排版标准", "生存指南", "管理办法"):
        return "制度与管理"
    if title_has_any("招新", "报名", "宣讲"):
        return "招新"
    if "/form/page/" in source_url or doc_type == "form" or title_has_any("问卷", "调查", "收集表"):
        return "问卷"
    if title_has_any("会议录", "点子", "智库", "活动", "项目", "策划书", "汇总", "复盘"):
        return "活动与项目"
    if text_has_any("面试者", "面试官", "签到签退表", "无领导小组讨论"):
        return "面试记录"
    if text_has_any("组织关系", "百科", "团委", "青志协"):
        return "组织关系与百科"
    return "其他"


def render_markdown(source_url: str, file_id: str, fetched_at: str, doc_type: str, body: str) -> str:
    frontmatter = "\n".join(
        [
            "---",
            "source: tencent-docs",
            f"source_url: {source_url}",
            f"file_id: {file_id}",
            f"fetched_at: {fetched_at}",
            f"type: {doc_type}",
            "---",
            "",
        ]
    )
    if body:
        return frontmatter + body.strip() + "\n"
    return frontmatter + "\n"


def ensure_unique_path(path: Path, file_id: str, existing_paths: dict[str, Path]) -> Path:
    if not path.exists():
        return path
    current = existing_paths.get(file_id)
    if current == path:
        return path
    stem = path.stem
    suffix = path.suffix
    return path.with_name(f"{stem}-{file_id}{suffix}")


def collect_targets(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    targets: dict[str, dict[str, str]] = {}
    ignored = set(config.get("ignore_file_ids") or [])
    token = load_token()

    for item in config.get("pinned_files") or []:
        file_id = str(item.get("file_id") or "").strip()
        if not file_id or file_id in ignored:
            continue
        targets[file_id] = {
            "file_id": file_id,
            "title": str(item.get("title") or "").strip(),
            "url": str(item.get("source_url") or "").strip(),
            "relative_path": str(item.get("relative_path") or "").strip(),
        }

    for keyword in config.get("search_keywords") or []:
        search_result = mcp_call("manage.search_file", {"search_key": str(keyword)}, token)
        for item in search_result.get("list") or []:
            file_id = str(item.get("file_id") or "").strip()
            if not file_id or file_id in ignored:
                continue
            targets.setdefault(
                file_id,
                {
                    "file_id": file_id,
                    "title": str(item.get("title") or "").strip(),
                    "url": str(item.get("url") or "").strip(),
                    "relative_path": "",
                },
            )
    return targets


def sync_target(
    target: dict[str, str],
    existing_paths: dict[str, Path],
    fetched_at: str,
    token: str,
) -> tuple[str, bool, str]:
    file_id = target["file_id"]
    fetch_result = mcp_call("get_content", {"file_id": file_id}, token)
    content = str(fetch_result.get("content") or "")
    raw_title = target.get("title") or file_id
    title = clean_title(raw_title) or raw_title
    source_url = target.get("url") or f"https://docs.qq.com/doc/{file_id}"
    doc_type = infer_doc_type(source_url)
    folder = classify_folder(title, content, source_url, doc_type)

    current_path = existing_paths.get(file_id)
    if current_path is not None:
        destination = current_path
    elif target.get("relative_path"):
        destination = RAW_ROOT / target["relative_path"]
    else:
        destination = RAW_ROOT / folder / f"{sanitize_filename(title)}.md"

    destination = ensure_unique_path(destination, file_id, existing_paths)
    destination.parent.mkdir(parents=True, exist_ok=True)

    rendered = render_markdown(
        source_url=source_url,
        file_id=file_id,
        fetched_at=fetched_at,
        doc_type=doc_type,
        body=clean_content(title, content),
    )
    previous = destination.read_text(encoding="utf-8") if destination.exists() else None
    changed = previous != rendered
    if changed:
        destination.write_text(rendered, encoding="utf-8")
    existing_paths[file_id] = destination
    relative = destination.relative_to(RAW_ROOT).as_posix()
    actual_folder = destination.relative_to(RAW_ROOT).parts[0]
    return relative, changed, actual_folder


def main() -> int:
    args = parse_args()
    config = load_config(Path(args.config))
    existing_paths = existing_file_map()
    targets = collect_targets(config)
    fetched_at = today_shanghai()
    token = load_token()

    changed = 0
    folders: dict[str, int] = {}
    results: list[dict[str, Any]] = []

    for file_id in sorted(targets):
        relative_path, did_change, folder = sync_target(
            targets[file_id],
            existing_paths,
            fetched_at,
            token,
        )
        changed += int(did_change)
        folders[folder] = folders.get(folder, 0) + 1
        results.append(
            {
                "file_id": file_id,
                "relative_path": relative_path,
                "folder": folder,
                "changed": did_change,
            }
        )

    payload = {
        "synced_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "fetched_at": fetched_at,
        "target_count": len(targets),
        "written_or_changed": changed,
        "folders": folders,
        "files": results,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
