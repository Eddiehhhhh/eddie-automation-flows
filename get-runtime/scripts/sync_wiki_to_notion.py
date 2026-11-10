#!/usr/bin/env python3
"""Mirror tracked 艾迪宇宙 markdown files into a Notion page tree.

Public GitHub state stays privacy-safe:
- no Notion page IDs
- no Notion URLs
- no mirrored page content

The script resolves Notion child pages by title on each run, then creates or
updates only pages whose local content hash changed.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")
TZ = dt.timezone(dt.timedelta(hours=8))

ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
STATE_PATH = ROOT / ".github" / "state" / "notion-wiki-mirror-state.json"
ENCRYPTED_SOURCE_PATH = ROOT / ".github" / "private" / "notion-wiki-mirror-source.tar.gz.enc"

DEFAULT_INCLUDE_PATTERNS = [
    "AGENTS.md",
    "README.md",
    "Wiki/*.md",
    "Wiki/**/*.md",
    "Schema/*.md",
    "Schema/**/*.md",
]
DEFAULT_EXCLUDE_PATTERNS = [
    "Wiki/更新日志.md",
]

DEFAULT_PARENT_PAGE_TITLE = "艾迪宇宙"
DEFAULT_ROOT_TITLE = "艾迪宇宙镜像"
ROOT_SIGNATURE = "root-v1"
DIRECTORY_SIGNATURE = "directory-v1"

NOTION_CODE_LANGUAGES = {
    "abap",
    "abc",
    "agda",
    "arduino",
    "ascii art",
    "assembly",
    "bash",
    "basic",
    "bnf",
    "c",
    "c#",
    "c++",
    "clojure",
    "coffeescript",
    "coq",
    "css",
    "dart",
    "dhall",
    "diff",
    "docker",
    "ebnf",
    "elixir",
    "elm",
    "erlang",
    "f#",
    "flow",
    "fortran",
    "gherkin",
    "glsl",
    "go",
    "graphql",
    "groovy",
    "haskell",
    "hcl",
    "html",
    "idris",
    "java",
    "javascript",
    "json",
    "julia",
    "kotlin",
    "latex",
    "less",
    "lisp",
    "livescript",
    "llvm ir",
    "lua",
    "makefile",
    "markdown",
    "markup",
    "matlab",
    "mathematica",
    "mermaid",
    "nix",
    "notion formula",
    "objective-c",
    "ocaml",
    "pascal",
    "perl",
    "php",
    "plain text",
    "powershell",
    "prolog",
    "protobuf",
    "purescript",
    "python",
    "r",
    "racket",
    "reason",
    "ruby",
    "rust",
    "sass",
    "scala",
    "scheme",
    "scss",
    "shell",
    "smalltalk",
    "solidity",
    "sql",
    "swift",
    "toml",
    "typescript",
    "vb.net",
    "verilog",
    "vhdl",
    "visual basic",
    "webassembly",
    "xml",
    "yaml",
    "java/c/c++/c#",
}

CODE_LANGUAGE_ALIASES = {
    "csharp": "c#",
    "cpp": "c++",
    "dockerfile": "docker",
    "html5": "html",
    "js": "javascript",
    "jsonc": "json",
    "md": "markdown",
    "plaintext": "plain text",
    "py": "python",
    "rb": "ruby",
    "rs": "rust",
    "sh": "shell",
    "shellscript": "shell",
    "text": "plain text",
    "ts": "typescript",
    "tsx": "typescript",
    "txt": "plain text",
    "xml+html": "html",
    "yml": "yaml",
    "zsh": "shell",
}

UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})"
)


@dataclass(frozen=True)
class Node:
    path: str
    parent_path: Optional[str]
    title: str
    kind: str
    signature: str


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip()


load_dotenv_file(ROOT / ".env")


def load_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def format_time(value: dt.datetime) -> str:
    return value.astimezone(TZ).isoformat(timespec="seconds")


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

    raise SystemExit(
        "Missing required Notion credential: NOTION_API_KEY or ~/.codex/secrets/services.json notion.auth.token"
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
        "User-Agent": "eddie-wiki-notion-wiki-mirror/1.0",
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


def page_title_from_page(page: Dict[str, Any]) -> str:
    properties = page.get("properties") or {}
    for value in properties.values():
        if isinstance(value, dict) and value.get("type") == "title":
            return rich_text_plain_text(value.get("title") or []).strip()
    if page.get("child_page") and isinstance(page["child_page"], dict):
        return str(page["child_page"].get("title") or "").strip()
    return str(page.get("id") or "").strip()


def search_pages(api_key: str, query: str) -> List[Dict[str, Any]]:
    payload = {"query": query, "filter": {"property": "object", "value": "page"}}
    response = notion_request(api_key, "POST", "/search", payload)
    return [item for item in response.get("results", []) if item.get("object") == "page"]


def get_page(api_key: str, page_id: str) -> Dict[str, Any]:
    return notion_request(api_key, "GET", f"/pages/{normalize_notion_id(page_id)}")


def resolve_parent_page_id(api_key: str, parent_page_id: Optional[str], parent_page_title: str) -> str:
    if parent_page_id:
        return normalize_notion_id(parent_page_id)

    matches: List[Tuple[str, str]] = []
    for item in search_pages(api_key, parent_page_title):
        candidate_id = normalize_notion_id(str(item.get("id") or ""))
        if not candidate_id:
            continue
        page = get_page(api_key, candidate_id)
        title = page_title_from_page(page)
        if title == parent_page_title:
            matches.append((candidate_id, title))

    unique_ids = []
    seen = set()
    for candidate_id, title in matches:
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        unique_ids.append((candidate_id, title))

    if len(unique_ids) == 1:
        return unique_ids[0][0]
    if not unique_ids:
        raise SystemExit(f"Could not find parent Notion page titled {parent_page_title!r}")
    raise SystemExit(
        f"Multiple Notion pages match parent title {parent_page_title!r}; set NOTION_WIKI_MIRROR_PARENT_PAGE_ID explicitly"
    )


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_signature(path: Path) -> str:
    return sha256_text(path.read_text(encoding="utf-8"))


def hmac_sha256(secret: str, text: str) -> str:
    return hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def state_secret() -> Optional[str]:
    return load_env("NOTION_WIKI_MIRROR_STATE_KEY") or load_env("NOTION_WIKI_MIRROR_BUNDLE_PASSPHRASE")


def state_entry_key(path: str, secret: Optional[str]) -> str:
    if not secret:
        return path
    return f"hmac256:{hmac_sha256(secret, path)}"


def previous_entry_for_path(previous_entries: Dict[str, Any], path: str, secret: Optional[str]) -> Dict[str, Any]:
    token = state_entry_key(path, secret)
    if token in previous_entries and isinstance(previous_entries[token], dict):
        return previous_entries[token]
    if path in previous_entries and isinstance(previous_entries[path], dict):
        return previous_entries[path]
    return {}


def is_included(path: str, include_patterns: Sequence[str], exclude_patterns: Sequence[str]) -> bool:
    included = any(fnmatch.fnmatch(path, pattern) for pattern in include_patterns)
    if not included:
        return False
    return not any(fnmatch.fnmatch(path, pattern) for pattern in exclude_patterns)


def tracked_markdown_paths(include_patterns: Sequence[str], exclude_patterns: Sequence[str]) -> List[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths: List[str] = []
    for raw in result.stdout.splitlines():
        path = raw.strip()
        if not path or not path.endswith(".md"):
            continue
        if is_included(path, include_patterns, exclude_patterns):
            paths.append(path)
    return sorted(set(paths))


def unpack_source_bundle(bundle_path: Path, passphrase: str) -> Tuple[tempfile.TemporaryDirectory[str], Path]:
    if not shutil.which("openssl"):
        raise SystemExit("Missing required dependency: openssl")
    tempdir = tempfile.TemporaryDirectory(prefix="notion-wiki-mirror-")
    temp_root = Path(tempdir.name)
    encrypted_binary_path = temp_root / "source.tar.gz.enc"
    tarball_path = temp_root / "source.tar.gz"
    encrypted_binary_path.write_bytes(base64.b64decode(bundle_path.read_text(encoding="ascii")))
    env = os.environ.copy()
    env["NOTION_WIKI_MIRROR_BUNDLE_PASSPHRASE"] = passphrase
    subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-pass",
            "env:NOTION_WIKI_MIRROR_BUNDLE_PASSPHRASE",
            "-in",
            str(encrypted_binary_path),
            "-out",
            str(tarball_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    extract_root = temp_root / "source"
    extract_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise SystemExit(f"Unsafe path in encrypted source bundle: {member.name}")
        archive.extractall(extract_root)
    return tempdir, extract_root


def bundle_markdown_paths(
    source_root: Path,
    include_patterns: Sequence[str],
    exclude_patterns: Sequence[str],
) -> List[str]:
    paths: List[str] = []
    for path in source_root.rglob("*.md"):
        if path.is_file():
            rel = path.relative_to(source_root).as_posix()
            if is_included(rel, include_patterns, exclude_patterns):
                paths.append(rel)
    return sorted(set(paths))


def resolve_source_paths(
    include_patterns: Sequence[str],
    exclude_patterns: Sequence[str],
) -> Tuple[Path, List[str], Optional[tempfile.TemporaryDirectory[str]], str]:
    bundle_path = Path(
        load_env("NOTION_WIKI_MIRROR_SOURCE_ARCHIVE_PATH", str(ENCRYPTED_SOURCE_PATH)) or str(ENCRYPTED_SOURCE_PATH)
    )
    bundle_passphrase = load_env("NOTION_WIKI_MIRROR_BUNDLE_PASSPHRASE")

    if bundle_path.exists() and bundle_passphrase:
        tempdir, source_root = unpack_source_bundle(bundle_path, bundle_passphrase)
        return (
            source_root,
            bundle_markdown_paths(source_root, include_patterns, exclude_patterns),
            tempdir,
            "encrypted_bundle",
        )

    return ROOT, tracked_markdown_paths(include_patterns, exclude_patterns), None, "git_tracked_repo"


def build_nodes(paths: Sequence[str], source_root: Path) -> List[Node]:
    nodes: Dict[str, Node] = {
        ".": Node(path=".", parent_path=None, title=DEFAULT_ROOT_TITLE, kind="root", signature=ROOT_SIGNATURE)
    }

    for path in paths:
        file_path = Path(path)
        parents = list(file_path.parents)
        for parent in reversed(parents[:-1]):
            rel = parent.as_posix()
            if rel == ".":
                continue
            if rel not in nodes:
                parent_rel = parent.parent.as_posix() if parent.parent.as_posix() != "." else "."
                nodes[rel] = Node(
                    path=rel,
                    parent_path=parent_rel,
                    title=parent.name,
                    kind="directory",
                    signature=DIRECTORY_SIGNATURE,
                )

        parent_path = file_path.parent.as_posix() if file_path.parent.as_posix() != "." else "."
        nodes[path] = Node(
            path=path,
            parent_path=parent_path,
            title=file_path.name,
            kind="file",
            signature=file_signature(source_root / path),
        )

    def sort_key(node: Node) -> Tuple[int, int, str]:
        if node.path == ".":
            return (0, 0, node.path)
        depth = len(Path(node.path).parts)
        kind_order = 0 if node.kind in {"directory", "root"} else 1
        return (depth, kind_order, node.path)

    return sorted(nodes.values(), key=sort_key)


def chunk_text(text: str, limit: int = 1800) -> List[str]:
    if not text:
        return [""]
    chunks: List[str] = []
    remaining = text
    while remaining:
        part = remaining[:limit]
        if len(part) == len(remaining):
            chunks.append(part)
            break
        split_at = part.rfind("\n")
        if split_at < 400:
            split_at = part.rfind(" ")
        if split_at < 400:
            split_at = len(part)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks or [text]


def text_rich_text(text: str) -> List[Dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": {"content": chunk},
        }
        for chunk in chunk_text(text)
        if chunk != ""
    ]


def paragraph_block(text: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": text_rich_text(text)},
    }


def heading_block(level: int, text: str) -> Dict[str, Any]:
    key = {1: "heading_1", 2: "heading_2", 3: "heading_3"}[max(1, min(level, 3))]
    return {
        "object": "block",
        "type": key,
        key: {"rich_text": text_rich_text(text)},
    }


def quote_block(text: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "quote",
        "quote": {"rich_text": text_rich_text(text)},
    }


def bulleted_block(text: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": text_rich_text(text)},
    }


def numbered_block(text: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": text_rich_text(text)},
    }


def code_block(text: str, language: str = "plain text") -> Dict[str, Any]:
    normalized_language = normalize_code_language(language)
    return {
        "object": "block",
        "type": "code",
        "code": {
            "language": normalized_language,
            "rich_text": text_rich_text(text or " "),
        },
    }


def normalize_code_language(language: str) -> str:
    raw = (language or "").strip().lower()
    if not raw:
        return "plain text"
    candidate = CODE_LANGUAGE_ALIASES.get(raw, raw)
    if candidate in NOTION_CODE_LANGUAGES:
        return candidate
    return "plain text"


def divider_block() -> Dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def split_frontmatter(text: str) -> Tuple[Optional[str], str]:
    if not text.startswith("---\n"):
        return None, text
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return None, text
    head, body = parts
    return head[4:], body


def markdown_to_blocks(markdown: str) -> List[Dict[str, Any]]:
    frontmatter, body = split_frontmatter(markdown.replace("\r\n", "\n").replace("\r", "\n"))
    blocks: List[Dict[str, Any]] = []

    if frontmatter is not None:
        blocks.append(heading_block(3, "Frontmatter"))
        blocks.append(code_block(frontmatter.strip("\n"), "yaml"))

    lines = body.split("\n")
    paragraph_lines: List[str] = []
    quote_lines: List[str] = []
    in_code = False
    code_language = "plain text"
    code_lines: List[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if paragraph_lines:
            blocks.append(paragraph_block("\n".join(paragraph_lines).strip()))
            paragraph_lines = []

    def flush_quote() -> None:
        nonlocal quote_lines
        if quote_lines:
            blocks.append(quote_block("\n".join(quote_lines).strip()))
            quote_lines = []

    def flush_code() -> None:
        nonlocal code_lines, code_language
        blocks.append(code_block("\n".join(code_lines), code_language))
        code_lines = []
        code_language = "plain text"

    for line in lines:
        if in_code:
            if line.startswith("```"):
                flush_code()
                in_code = False
            else:
                code_lines.append(line)
            continue

        if line.startswith("```"):
            flush_paragraph()
            flush_quote()
            in_code = True
            code_language = line[3:].strip() or "plain text"
            code_lines = []
            continue

        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_quote()
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading_match:
            flush_paragraph()
            flush_quote()
            level = min(len(heading_match.group(1)), 3)
            blocks.append(heading_block(level, heading_match.group(2).strip()))
            continue

        if re.fullmatch(r"---|___|\*\*\*", stripped):
            flush_paragraph()
            flush_quote()
            blocks.append(divider_block())
            continue

        bullet_match = re.match(r"^[-*]\s+(.*)$", line)
        if bullet_match:
            flush_paragraph()
            flush_quote()
            blocks.append(bulleted_block(bullet_match.group(1).strip()))
            continue

        number_match = re.match(r"^\d+\.\s+(.*)$", line)
        if number_match:
            flush_paragraph()
            flush_quote()
            blocks.append(numbered_block(number_match.group(1).strip()))
            continue

        quote_match = re.match(r"^>\s?(.*)$", line)
        if quote_match:
            flush_paragraph()
            quote_lines.append(quote_match.group(1))
            continue

        flush_quote()
        paragraph_lines.append(line.rstrip())

    flush_paragraph()
    flush_quote()
    if in_code:
        flush_code()

    return blocks


def root_page_blocks(root_title: str) -> List[Dict[str, Any]]:
    lines = [
        f"这个页面由 GitHub workflow 自动维护，用来镜像 `{root_title}` 对应的本地 Markdown 结构。",
        "这里只同步 `AGENTS.md`、`README.md`、`Wiki/` 和 `Schema/` 下的受管 Markdown 文件。",
        "公开仓库不会保存 Notion page id、私有正文缓存或运行期目标链接。",
    ]
    return [paragraph_block(line) for line in lines]


def directory_page_blocks(path: str) -> List[Dict[str, Any]]:
    lines = [
        f"来源路径：`{path}`",
        "这是自动镜像的目录页，子页面结构对应仓库目录结构。",
        "如需长期稳定引用，优先读取子页面，不在这里手动堆内容。",
    ]
    return [paragraph_block(line) for line in lines]


def file_page_blocks(source_root: Path, path: str) -> List[Dict[str, Any]]:
    source_block = quote_block(f"来源路径：{path}")
    body = markdown_to_blocks((source_root / path).read_text(encoding="utf-8"))
    return [source_block, divider_block(), *body] if body else [source_block]


def list_child_pages(api_key: str, parent_page_id: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    duplicates: Dict[str, List[str]] = {}
    for block in retrieve_block_children(api_key, parent_page_id):
        if block.get("type") != "child_page":
            continue
        title = str((block.get("child_page") or {}).get("title") or "").strip()
        block_id = normalize_notion_id(str(block.get("id") or ""))
        if not title or not block_id:
            continue
        if title in mapping:
            duplicates.setdefault(title, [mapping[title]]).append(block_id)
            continue
        mapping[title] = block_id
    if duplicates:
        names = ", ".join(sorted(duplicates))
        raise RuntimeError(f"Duplicate child page titles under parent {parent_page_id}: {names}")
    return mapping


def create_page(
    api_key: str,
    parent_page_id: str,
    title: str,
    icon: str,
    children: Optional[List[Dict[str, Any]]] = None,
) -> str:
    payload: Dict[str, Any] = {
        "parent": {"type": "page_id", "page_id": normalize_notion_id(parent_page_id)},
        "icon": {"type": "emoji", "emoji": icon},
        "properties": {
            "title": [
                {
                    "type": "text",
                    "text": {"content": title},
                }
            ]
        },
    }
    if children:
        payload["children"] = children[:100]
    response = notion_request(api_key, "POST", "/pages", payload)
    page_id = normalize_notion_id(str(response.get("id") or ""))
    if not page_id:
        raise RuntimeError(f"Failed to create Notion page {title!r}")
    if children and len(children) > 100:
        append_blocks(api_key, page_id, children[100:])
    return page_id


def update_page_title(api_key: str, page_id: str, title: str) -> None:
    payload = {
        "properties": {
            "title": [
                {
                    "type": "text",
                    "text": {"content": title},
                }
            ]
        }
    }
    notion_request(api_key, "PATCH", f"/pages/{normalize_notion_id(page_id)}", payload)


def archive_block(api_key: str, block_id: str) -> None:
    notion_request(api_key, "PATCH", f"/blocks/{normalize_notion_id(block_id)}", {"archived": True})


def append_blocks(api_key: str, page_id: str, blocks: Sequence[Dict[str, Any]]) -> None:
    block_list = list(blocks)
    for index in range(0, len(block_list), 100):
        chunk = block_list[index:index + 100]
        notion_request(
            api_key,
            "PATCH",
            f"/blocks/{normalize_notion_id(page_id)}/children",
            {"children": chunk},
        )


def replace_page_content(api_key: str, page_id: str, blocks: Sequence[Dict[str, Any]]) -> None:
    existing = retrieve_block_children(api_key, page_id)
    for block in existing:
        # Keep nested child pages in place. The blocks API cannot archive them,
        # and directory/root pages rely on those children to preserve structure.
        if block.get("type") == "child_page":
            continue
        block_id = block.get("id")
        if block_id:
            archive_block(api_key, str(block_id))
    if blocks:
        append_blocks(api_key, page_id, blocks)


def load_patterns(args: argparse.Namespace) -> Tuple[List[str], List[str]]:
    include_patterns = list(DEFAULT_INCLUDE_PATTERNS)
    exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)

    include_json = args.include_json or load_env("NOTION_WIKI_MIRROR_INCLUDE_JSON")
    exclude_json = args.exclude_json or load_env("NOTION_WIKI_MIRROR_EXCLUDE_JSON")

    if include_json:
        include_patterns = json.loads(include_json)
    if exclude_json:
        exclude_patterns = json.loads(exclude_json)

    return include_patterns, exclude_patterns


def icon_for_node(kind: str) -> str:
    if kind == "root":
        return "🧠"
    if kind == "directory":
        return "📁"
    return "📄"


def blocks_for_node(source_root: Path, node: Node, root_title: str) -> List[Dict[str, Any]]:
    if node.kind == "root":
        return root_page_blocks(root_title)
    if node.kind == "directory":
        return directory_page_blocks(node.path)
    return file_page_blocks(source_root, node.path)


def save_state(
    args: argparse.Namespace,
    generated_at: dt.datetime,
    root_title: str,
    parent_title: str,
    source_mode: str,
    include_patterns: Sequence[str],
    exclude_patterns: Sequence[str],
    entries: Dict[str, Dict[str, Any]],
    counts: Dict[str, int],
) -> None:
    payload = {
        "source_system": source_mode,
        "scope": "public-safe",
        "generated_at": format_time(generated_at),
        "root_title": root_title,
        "parent_page_title": parent_title,
        "full_sync": bool(args.full_sync),
        "entries": entries,
        "counts": counts,
        "include_patterns": list(include_patterns),
        "exclude_patterns": list(exclude_patterns),
    }
    write_json(STATE_PATH, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Resolve and plan changes without writing Notion or state.")
    parser.add_argument("--plan-only", action="store_true", help="Only enumerate local files and directories.")
    parser.add_argument("--full-sync", action="store_true", help="Rewrite mirrored page content even when hashes are unchanged.")
    parser.add_argument("--limit", type=int, default=0, help="Limit total processed nodes after dependency expansion.")
    parser.add_argument("--path-prefix", action="append", help="Only sync files under the given path prefix. May be repeated.")
    parser.add_argument("--include-json", help="JSON array of include glob patterns.")
    parser.add_argument("--exclude-json", help="JSON array of exclude glob patterns.")
    parser.add_argument("--root-title", default=load_env("NOTION_WIKI_MIRROR_ROOT_TITLE", DEFAULT_ROOT_TITLE))
    parser.add_argument("--parent-page-id", default=load_env("NOTION_WIKI_MIRROR_PARENT_PAGE_ID"))
    parser.add_argument(
        "--parent-page-title",
        default=load_env("NOTION_WIKI_MIRROR_PARENT_PAGE_TITLE", DEFAULT_PARENT_PAGE_TITLE),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated_at = now()
    include_patterns, exclude_patterns = load_patterns(args)
    source_root, source_paths, temp_source_dir, source_mode = resolve_source_paths(include_patterns, exclude_patterns)

    if args.path_prefix:
        prefixes = tuple(prefix.rstrip("/") for prefix in args.path_prefix)
        source_paths = [path for path in source_paths if path.startswith(prefixes)]

    nodes = build_nodes(source_paths, source_root)
    if args.limit > 0:
        nodes = nodes[: args.limit]

    counts = {
        "files": sum(1 for node in nodes if node.kind == "file"),
        "directories": sum(1 for node in nodes if node.kind == "directory"),
        "total_nodes": len(nodes),
    }

    if args.plan_only:
        print(
            json.dumps(
                {
                    "root_title": args.root_title,
                    "parent_page_title": args.parent_page_title,
                    "source_mode": source_mode,
                    "counts": counts,
                    "first_nodes": [node.path for node in nodes[:20]],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    api_key = load_notion_api_key()
    state = load_state()
    previous_entries = state.get("entries") or {}
    next_entries: Dict[str, Dict[str, Any]] = {}
    state_key_secret = state_secret()

    parent_page_id = resolve_parent_page_id(api_key, args.parent_page_id, args.parent_page_title)
    child_cache: Dict[str, Dict[str, str]] = {}
    resolved_page_ids: Dict[str, str] = {}

    def get_children(parent_id: str) -> Dict[str, str]:
        if parent_id not in child_cache:
            child_cache[parent_id] = list_child_pages(api_key, parent_id)
        return child_cache[parent_id]

    def ensure_page(parent_id: str, title: str, icon: str, blocks: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, bool]:
        children = get_children(parent_id)
        if title in children:
            return children[title], False
        if args.dry_run:
            pseudo_id = f"dryrun::{parent_id}::{title}"
            children[title] = pseudo_id
            return pseudo_id, True
        page_id = create_page(api_key, parent_id, title, icon, children=blocks)
        child_cache.pop(parent_id, None)
        get_children(parent_id)
        return page_id, True

    root_blocks = blocks_for_node(source_root, nodes[0], args.root_title)
    root_page_id, root_created = ensure_page(parent_page_id, args.root_title, icon_for_node("root"), root_blocks if not args.dry_run else None)
    resolved_page_ids["."] = root_page_id
    root_entry_key = state_entry_key(".", state_key_secret)

    if root_created:
        if args.dry_run:
            pass
        else:
            next_entries[root_entry_key] = {"type": "root", "signature": ROOT_SIGNATURE}
    else:
        root_changed = args.full_sync or previous_entry_for_path(previous_entries, ".", state_key_secret).get("signature") != ROOT_SIGNATURE
        if root_changed and not args.dry_run:
            replace_page_content(api_key, root_page_id, root_blocks)
        next_entries[root_entry_key] = {"type": "root", "signature": ROOT_SIGNATURE}

    created_count = 1 if root_created else 0
    updated_count = 0
    skipped_count = 0

    for node in nodes[1:]:
        parent_path = node.parent_path or "."
        parent_id = resolved_page_ids[parent_path]
        existing_signature = str(previous_entry_for_path(previous_entries, node.path, state_key_secret).get("signature") or "")
        desired_blocks = blocks_for_node(source_root, node, args.root_title)
        page_id, created = ensure_page(parent_id, node.title, icon_for_node(node.kind))
        resolved_page_ids[node.path] = page_id
        next_entries[state_entry_key(node.path, state_key_secret)] = {"type": node.kind, "signature": node.signature}

        if created:
            created_count += 1
            if args.dry_run:
                continue
            replace_page_content(api_key, page_id, desired_blocks)
            continue

        changed = args.full_sync or existing_signature != node.signature
        if not changed:
            skipped_count += 1
            continue

        updated_count += 1
        if args.dry_run:
            continue
        update_page_title(api_key, page_id, node.title)
        replace_page_content(api_key, page_id, desired_blocks)

    if not args.dry_run:
        save_state(
            args=args,
            generated_at=generated_at,
            root_title=args.root_title,
            parent_title=args.parent_page_title,
            source_mode=source_mode,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            entries=next_entries,
            counts=counts,
        )

    print(
        json.dumps(
            {
                "root_title": args.root_title,
                "parent_page_title": args.parent_page_title,
                "source_mode": source_mode,
                "dry_run": bool(args.dry_run),
                "counts": counts,
                "created": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
            },
            ensure_ascii=False,
        )
    )
    if temp_source_dir is not None:
        temp_source_dir.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
