#!/usr/bin/env python3
"""Mirror tracked 艾迪宇宙 markdown files into a Notion page tree.

Public GitHub state stays privacy-safe:
- no Notion page IDs
- no Notion URLs
- no mirrored page content

Automatic Notion mutations are held pending a reviewed intent executor.
Private intent checkpoints and read-only recovery reuse the existing mirror state.
Source hashes alone cannot protect edits made concurrently in Notion.
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
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from cloud_private_checkout import verify_private_checkout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")
NATIVE_RENDERER_REVISION = "native-code-exact-v1"
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


class MirrorHeld(RuntimeError):
    """The mirror needs review and must not report a successful sync."""

    def __init__(self, reason: str, code: str = "held"):
        self.code = code if code in {"held", "checkpoint_helper_unavailable", "checkpoint_publication_failed", "checkpoint_timeout"} else "held"
        super().__init__(reason)


class UnknownWrite(MirrorHeld):
    """A non-idempotent request may have reached Notion; never retry blindly."""


def notion_request(
    api_key: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if method.upper() != "GET" and not (method.upper() == "POST" and path == "/search"):
        raise MirrorHeld("Notion mirror mutation disabled pending reviewed intent executor")
    non_idempotent = (method.upper() == "POST" and path != "/search") or (method.upper() == "PATCH" and path.endswith("/children"))
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
            try:
                return json.loads(body) if body else {}
            except ValueError as exc:
                if non_idempotent:
                    raise UnknownWrite("non-idempotent write response unreadable; target recovery required") from exc
                raise
        except urllib.error.HTTPError as exc:
            last_error = exc
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < 4:
                retry_after = exc.headers.get("Retry-After")
                time.sleep(int(retry_after) if retry_after and retry_after.isdigit() else 10)
                continue
            if 500 <= exc.code < 600 and non_idempotent:
                raise UnknownWrite("non-idempotent write returned a server error; target recovery required") from exc
            if 500 <= exc.code < 600 and attempt < 4:
                time.sleep(5)
                continue
            message = body or exc.reason
            raise RuntimeError(f"Notion API {method} {path} failed ({exc.code}): {message}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if non_idempotent:
                raise UnknownWrite("non-idempotent write response lost; target recovery required") from exc
            if attempt < 4:
                time.sleep(5)
                continue
            raise RuntimeError(f"Notion request failed for {method} {path}: {exc}") from exc
    raise RuntimeError(f"Notion request failed for {method} {path}: {last_error}")


def retrieve_block_children(api_key: str, block_id: str) -> List[Dict[str, Any]]:
    children: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    seen_cursors = set()
    while True:
        params = {"page_size": "100"}
        if cursor:
            params["start_cursor"] = cursor
        query = urllib.parse.urlencode(params)
        response = notion_request(api_key, "GET", f"/blocks/{normalize_notion_id(block_id)}/children?{query}")
        results = response.get("results")
        if not isinstance(results, list) or any(not isinstance(block, dict) for block in results) or type(response.get("has_more")) is not bool:
            raise MirrorHeld("incomplete block pagination")
        children.extend(results)
        if response["has_more"] is False:
            break
        cursor = response.get("next_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise MirrorHeld("incomplete block pagination")
        seen_cursors.add(cursor)
    return children


def retrieve_block_tree(api_key: str, target: str, seen: Optional[set] = None, depth: int = 0) -> List[Dict[str, Any]]:
    """Read every nested block before trusting a content hash, including table cells."""
    seen = set() if seen is None else seen
    if target in seen or depth > 32 or len(seen) >= 10000:
        raise MirrorHeld("nested target pagination incomplete")
    seen.add(target)
    blocks = retrieve_block_children(api_key, target)
    for block in blocks:
        kind = block.get("type")
        if not isinstance(kind, str) or not isinstance(block.get(kind), dict):
            raise MirrorHeld("target block payload incomplete")
        if block.get("has_children") and kind not in {"child_page", "child_database"}:
            if not block.get("id"):
                raise MirrorHeld("nested target identity unavailable")
            block[kind]["children"] = retrieve_block_tree(api_key, block["id"], seen, depth + 1)
    return blocks


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


def build_nodes(paths: Sequence[str], source_root: Path, signatures: Optional[Dict[str, str]] = None) -> List[Node]:
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
            signature=signatures[path] if signatures is not None else file_signature(source_root / path),
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


def exact_text_rich_text(text: str) -> List[Dict[str, Any]]:
    """Split at API-safe Unicode boundaries without trimming any source character."""
    chunks: List[str] = []
    start, units = 0, 0
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > 1800:
            chunks.append(text[start:index])
            start, units = index, 0
        units += width
    if start < len(text):
        chunks.append(text[start:])
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]


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


def code_block(text: str, language: str = "plain text", preserve_text: bool = False) -> Dict[str, Any]:
    normalized_language = normalize_code_language(language)
    return {
        "object": "block",
        "type": "code",
        "code": {
            "language": normalized_language,
            "rich_text": exact_text_rich_text(text or " ") if preserve_text else text_rich_text(text or " "),
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


def markdown_to_blocks(markdown: str, preserve_code: bool = False) -> List[Dict[str, Any]]:
    frontmatter, body = split_frontmatter(markdown.replace("\r\n", "\n").replace("\r", "\n"))
    blocks: List[Dict[str, Any]] = []

    if frontmatter is not None:
        blocks.append(heading_block(3, "Frontmatter"))
        blocks.append(code_block(frontmatter.strip("\n"), "yaml", preserve_text=True) if preserve_code else code_block(frontmatter.strip("\n"), "yaml"))

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
        blocks.append(code_block("\n".join(code_lines), code_language, preserve_text=True) if preserve_code else code_block("\n".join(code_lines), code_language))
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


def native_markdown_to_blocks(markdown: str) -> List[Dict[str, Any]]:
    """Preserve the established renderer, adding real tables outside fenced code."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: List[Dict[str, Any]] = []
    pending: List[str] = []
    fenced = False
    index = 0
    def cells(line: str) -> List[str]:
        return [v.strip().replace("\\|", "|") for v in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
    def flush() -> None:
        if pending:
            blocks.extend(markdown_to_blocks("\n".join(pending), preserve_code=True))
            pending.clear()
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            fenced = not fenced
        divider = cells(lines[index + 1]) if index + 1 < len(lines) else []
        is_table = not fenced and "|" in line and len(divider) > 1 and all(re.fullmatch(r":?-{3,}:?", v) for v in divider)
        if not is_table:
            pending.append(line); index += 1; continue
        flush()
        rows = [cells(line)]
        index += 2
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            rows.append(cells(lines[index])); index += 1
        width = max(len(row) for row in rows)
        header = rows[0] + [""] * (width - len(rows[0]))
        # Keep every table create below the children limit, repeating the header for readable continuation.
        for offset in range(0, max(1, len(rows) - 1), 80):
            subset = [header, *rows[1 + offset:81 + offset]]
            children = [{"object": "block", "type": "table_row", "table_row": {
                "cells": [text_rich_text(cell) for cell in row + [""] * (width - len(row))]}} for row in subset]
            blocks.append({"object": "block", "type": "table", "table": {
                "table_width": width, "has_column_header": True, "has_row_header": False, "children": children}})
    flush()
    return blocks


def payload_chunks(blocks: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Respect top-level, recursive block and encoded request-size bounds."""
    chunks: List[List[Dict[str, Any]]] = []
    chunk: List[Dict[str, Any]] = []
    count, size = 3, 4096  # operation marker, version heading, end marker and envelope
    def weight(block: Dict[str, Any]) -> int:
        return 1 + sum(weight(child) for child in block.get(block.get("type"), {}).get("children", []))
    for block in blocks:
        block_count = weight(block)
        encoded_size = len(json.dumps(block, ensure_ascii=False).encode("utf-8"))
        if block_count > 900 or encoded_size > 440000:
            raise MirrorHeld("source block exceeds safe request size")
        if chunk and (len(chunk) >= 97 or count + block_count > 900 or size + encoded_size > 440000):
            chunks.append(chunk); chunk = []; count, size = 3, 4096
        chunk.append(block); count += block_count; size += encoded_size
    if chunk:
        chunks.append(chunk)
    return chunks


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


def file_page_blocks(source_root: Path, path: str, markdown: Optional[str] = None) -> List[Dict[str, Any]]:
    source_block = quote_block(f"来源路径：{path}")
    body = markdown_to_blocks((source_root / path).read_text(encoding="utf-8") if markdown is None else markdown)
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
    if not UUID_RE.fullmatch(page_id):
        raise UnknownWrite("create response lacks a verified page ID; target recovery required")
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
    raise MirrorHeld("needs_review: destructive page replacement is disabled")


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
        **load_state(),
        "source_system": source_mode,
        "scope": "private-state",
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


def _run_checkpoint(command: Sequence[str]) -> int:
    """Bound the helper and all child Git processes without exposing output."""
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        raise MirrorHeld("private checkpoint could not start", "checkpoint_publication_failed") from None
    try:
        process.communicate(timeout=180)
        return process.returncode
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
                break
            except subprocess.TimeoutExpired:
                continue
        raise MirrorHeld("private checkpoint timed out", "checkpoint_timeout") from None


def persist_private_state(state: Dict[str, Any]) -> None:
    """Publish the existing private state before allowing any Notion request."""
    commit = Path(__file__).with_name("commit_private_vault.py")
    if not commit.is_file():
        raise MirrorHeld("private checkpoint helper unavailable", "checkpoint_helper_unavailable")
    try:
        verify_private_checkout(ROOT, os.environ.get("PRIVATE_VAULT_TOKEN", ""))
    except Exception:
        raise MirrorHeld("checkpoint repository verification failed") from None
    relative = STATE_PATH.resolve().relative_to(ROOT.resolve()).as_posix()
    result = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--cached", "--quiet"], capture_output=True,
    )
    if result.returncode != 0:
        raise MirrorHeld("private checkpoint requires an empty staging area")
    write_json(STATE_PATH, state)
    result = _run_checkpoint(
        [sys.executable, str(commit), "--vault", str(ROOT), "--message", "Checkpoint Notion mirror intent",
         "--path", relative],
    )
    if result:
        raise MirrorHeld("private checkpoint publication failed", "checkpoint_publication_failed")
    local = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
    remote = subprocess.run(["git", "-C", str(ROOT), "ls-remote", "origin", "refs/heads/main"], capture_output=True, text=True, timeout=60)
    if local.returncode or remote.returncode or remote.stdout.split()[:1] != [local.stdout.strip()]:
        raise MirrorHeld("private checkpoint remote readback failed")


_ASSET_READBACK_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_SOURCE_TREE_CACHE: Dict[str, set] = {}


def source_tree_paths(snapshot: str) -> set:
    if snapshot not in _SOURCE_TREE_CACHE:
        result = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-rz", snapshot], capture_output=True, timeout=30)
        if result.returncode:
            raise MirrorHeld("source reference tree unavailable")
        _SOURCE_TREE_CACHE[snapshot] = {row.split(b"\t", 1)[1].decode("utf-8") for row in result.stdout.split(b"\0") if row and row.split()[0] in {b"100644", b"100755"}}
    return _SOURCE_TREE_CACHE[snapshot]



def asset_block_identity(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind = block.get("type")
    if kind not in {"file", "image", "pdf", "audio", "video"}:
        return None
    payload = block.get(kind) or {}
    mode = payload.get("type")
    if mode not in {"file_upload", "file"}:
        return None
    import notion_private_assets as assets
    receipts = (load_state().get("assets") or {}).values()
    caption = "".join(part.get("text", {}).get("content", part.get("plain_text", "")) for part in payload.get("caption", []))
    if not caption.startswith("来源附件："):
        # Retain human files by their actual bytes, without claiming a Git source binding.
        if mode == "file_upload":
            raise MirrorHeld("native upload lacks verified private source binding")
        url = payload.get("file", {}).get("url", "")
        cache_key = ("human-native-file", url)
        try:
            if cache_key not in _ASSET_READBACK_CACHE:
                _ASSET_READBACK_CACHE[cache_key] = assets.canonical_native_file(block)
        except assets.AssetHeld:
            raise MirrorHeld("human native file readback held") from None
        return {"asset": _ASSET_READBACK_CACHE[cache_key], "caption": payload.get("caption", []), "name": payload.get("name", "")}
    path = caption[len("来源附件："):]
    matches = [r for r in receipts if r.get("source_path") == path or any(path in versions for versions in r.get("sources", {}).values())]
    if mode == "file_upload":
        matches = [r for r in matches if r.get("upload_id") == payload.get("file_upload", {}).get("id")]
    else:
        url = payload.get("file", {}).get("url", "")
        cache_key = ("native-file", url)
        try:
            if cache_key not in _ASSET_READBACK_CACHE:
                _ASSET_READBACK_CACHE[cache_key] = assets.canonical_native_file(block)
        except assets.AssetHeld:
            raise MirrorHeld("native asset binary readback held") from None
        observed = _ASSET_READBACK_CACHE[cache_key]
        matches = [r for r in matches if r.get("sha256") == observed["sha256"] and r.get("size") == observed["size"]]
    if len(matches) != 1 or matches[0].get("phase") != "verified":
        raise MirrorHeld("native asset binding ambiguous or unavailable")
    receipt = matches[0]
    # This private receipt was pinned to the Git blob before upload; old receipt bytes
    # remain authoritative after a depth-one runner loses earlier source Git objects.
    identity = {"type": kind, "sha256": receipt["sha256"], "size": receipt["size"], "content_type": receipt["content_type"]}
    canonical = {"asset": identity, "caption": payload.get("caption", [])}
    if kind == "file":
        canonical["name"] = payload.get("name", receipt["filename"])
    return canonical


def bundle_references(text: str, suffix: str) -> List[str]:
    """Inventory local HTML/CSS/JSON dependencies; no code or network resources execute."""
    refs: List[str] = []
    if suffix in {".html", ".htm"}:
        from html.parser import HTMLParser
        class References(HTMLParser):
            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == "base":
                    raise ValueError("HTML base requires review")
                for name in ("src", "href", "poster"):
                    if values.get(name):
                        refs.append(values[name])
                if values.get("srcset"):
                    refs.extend(value.strip().split()[0] for value in values["srcset"].split(",") if value.strip())
        parser = References()
        parser.feed(text)
    if suffix in {".html", ".htm", ".css"}:
        refs.extend(match[1].strip().strip("\"'") for match in re.finditer(r"url\(([^)]+)\)", text))
        refs.extend(match[1] for match in re.finditer(r"@import\s+[\"']([^\"']+)[\"']", text))
    if suffix == ".json":
        def strings(value):
            if isinstance(value, str):
                refs.append(value)
            elif isinstance(value, list):
                for item in value:
                    strings(item)
            elif isinstance(value, dict):
                for item in value.values():
                    strings(item)
        strings(json.loads(text))
    return list(dict.fromkeys(ref for ref in refs if ref and not ref.startswith("#") and not urllib.parse.urlsplit(ref).scheme))


def render_native_source(api_key: str, relative: str, markdown: str, snapshot: str, deadline: Optional[float] = None) -> Tuple[List[Dict[str, Any]], str, Dict[str, int]]:
    """Link to original private objects; attach only verified pinned binary uploads."""
    import posixpath
    import notion_private_assets as assets
    statuses: Dict[str, str] = {}
    attachments: List[Dict[str, Any]] = []
    counts = {"assets_ready": 0, "assets_held": 0, "assets_fallback": 0}
    state = load_state()
    known = state.get("entries") or {}
    paths = source_tree_paths(snapshot)
    def resolve(target: str, source: Optional[str] = None) -> Optional[str]:
        value = urllib.parse.unquote(target.split("#", 1)[0].strip().strip("<>"))
        if not value or urllib.parse.urlsplit(value).scheme or value.startswith("/") or "\\" in value:
            return None
        path = posixpath.normpath(posixpath.join(posixpath.dirname(source or relative), value))
        if path.startswith("../") or path == "..":
            return None
        for candidate in [path, value, path + ".md", value + ".md"]:
            if candidate in paths:
                return candidate
        if "/" not in value:
            candidates = [p for p in paths if Path(p).stem == value and p.endswith(".md")]
            if len(candidates) == 1:
                return candidates[0]
        return None
    def link(label: str, target: str, embed: bool = False, source: Optional[str] = None, depth: int = 0) -> str:
        if (deadline is not None and time.monotonic() >= deadline) or depth > 16 or len(statuses) >= 1000:
            raise MirrorHeld("source asset traversal budget exhausted")
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
            return "[" + label + "](" + target + ")"
        path = resolve(target, source)
        if path is None:
            if target.startswith("#"):
                url = assets.private_git_link(ROOT, snapshot, relative)
                return "[" + label + "](" + url + target + ")" if url else label
            counts["assets_held"] += 1
            statuses[target] = "reference_held"
            return label + "（来源引用待核验）"
        if path.endswith(".md"):
            entry = previous_entry_for_path(known, path, state_secret())
            fallback = assets.private_git_link(ROOT, snapshot, path)
            if fallback:
                fallback = fallback.replace("/blob/" + snapshot + "/", "/blob/main/", 1)
            url = "https://www.notion.so/" + entry["page_id"].replace("-", "") if entry.get("page_id") else fallback
            return "[" + label + "](" + url + ")" if url else label
        try:
            asset = assets.prepare(ROOT, snapshot, path)
        except assets.AssetHeld as exc:
            counts["assets_held"] += 1
            statuses[path] = "source_held"
            if exc.code == "source_read_budget":
                fallback = assets.private_git_link(ROOT, snapshot, path)
                if fallback:
                    counts["assets_fallback"] += 1
                    # A source-read budget is not a file-type exclusion or an upload success.
                    return "[" + label + "（原文件）](" + fallback.replace("/blob/" + snapshot + "/", "/blob/main/", 1) + ")"
            return label + "（来源引用待核验）"
        fallback = asset.fallback_url
        if path not in statuses:
            statuses[path] = "processing"
            def checkpoint(receipt: Dict[str, Any]) -> None:
                current = load_state()
                existing = current.setdefault("assets", {}).get(asset.sha256)
                if existing != receipt:
                    current["assets"][asset.sha256] = receipt
                    persist_private_state(current)
            result = assets.publish(asset, api_key, checkpoint, prior_receipt=load_state().get("assets", {}).get(asset.sha256))
            # The module deliberately suppresses callback exceptions; never turn a failed publication into a fallback write.
            require_published_state()
            if result["reason"] == "asset_checkpoint_failed":
                raise MirrorHeld("asset checkpoint publication failed", "checkpoint_publication_failed")
            # Preserve a fixed original-file link even when a quota/size hold occurs before upload.
            checkpoint(result["receipt"])
            if result["status"] == "ready":
                block = result["block"]
                block[block["type"]]["caption"] = text_rich_text("来源附件：" + path)
                attachments.append(block)
                statuses[path] = "ready"
                counts["assets_ready"] += 1
            else:
                statuses[path] = "held:" + result["reason"]
                counts["assets_held"] += 1
                if fallback:
                    counts["assets_fallback"] += 1
            suffix = Path(path).suffix.lower()
            if suffix in {".html", ".htm", ".css", ".json"}:
                if len(asset.data) > 1024 * 1024:
                    counts["assets_held"] += 1
                    statuses[path + "#dependencies"] = "bundle_read_budget"
                else:
                    try:
                        text = asset.data.decode("utf-8")
                        refs = bundle_references(text, suffix)
                    except (UnicodeDecodeError, ValueError):
                        counts["assets_held"] += 1
                        statuses[path + "#dependencies"] = "bundle_unreadable"
                        refs = []
                    for child in refs:
                        if suffix == ".json" and resolve(child, path) is None:
                            continue  # JSON strings are not intrinsically file references.
                        child_label = link(Path(child).name or child, child, source=path, depth=depth + 1)
                        attachments.append(paragraph_block("依赖文件：" + child_label))
        receipt = load_state().get("assets", {}).get(asset.sha256, {})
        pin = receipt.get("source_commit")
        if fallback and isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{40}", pin):
            fallback = fallback.replace("/blob/" + snapshot + "/", "/blob/" + pin + "/", 1)
        return "[" + label + "](" + fallback + ")" if fallback else label + "（附件待核验）"
    # Fenced examples remain literal and do not trigger uploads.
    pieces = re.split(r"(```[\s\S]*?```)", markdown)
    for index in range(0, len(pieces), 2):
        text = re.sub(r"(!?)\[([^\]\n]+)\]\(([^)\n]+)\)", lambda match: link(match[2], match[3], bool(match[1])), pieces[index])
        text = re.sub(r"(!?)\[\[([^\]\n]+)\]\]", lambda match: link(match[2].split("|", 1)[-1], match[2].split("|", 1)[0], bool(match[1])), text)
        if re.search(r"<(?:img|a|video|audio|source|link)\b", text, re.IGNORECASE):
            try:
                references = bundle_references(text, ".html")
            except ValueError:
                references = []
                counts["assets_held"] += 1
                statuses["html-source"] = "bundle_unreadable"
            for target in references:
                attachments.append(paragraph_block("引用文件：" + link(Path(target).name or target, target)))
        pieces[index] = text
    blocks = native_markdown_to_blocks("".join(pieces))
    def rich_links(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        for part in parts:
            content = part.get("text", {}).get("content", "")
            offset = 0
            for match in re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", content):
                output.extend(text_rich_text(content[offset:match.start()]))
                output.extend({"type": "text", "text": {"content": chunk, "link": {"url": match[2]}}} for chunk in chunk_text(match[1]))
                offset = match.end()
            output.extend(text_rich_text(content[offset:]))
        return output
    def convert(block: Dict[str, Any]) -> None:
        kind = block["type"]
        payload = block[kind]
        if kind != "code" and "rich_text" in payload:
            payload["rich_text"] = rich_links(payload["rich_text"])
        if kind == "table_row":
            payload["cells"] = [rich_links(cell) for cell in payload["cells"]]
        for child in payload.get("children", []):
            convert(child)
    for block in [*blocks, *attachments]:
        convert(block)
    rendered = [quote_block("来源路径：" + relative), divider_block(), *blocks, *attachments] if blocks or attachments else [quote_block("来源路径：" + relative)]
    legacy = file_page_blocks(ROOT, relative, markdown)
    rendered_hashes = [block_identity(block) for block in rendered]
    version = "legacy-v1" if rendered_hashes == [block_identity(block) for block in legacy] else "native-v1:" + sha256_text(json.dumps(rendered_hashes))
    return rendered, version, counts


def _canonical_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the established baseline representation unchanged."""
    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        kind = value.get("type")
        if isinstance(kind, str) and isinstance(value.get(kind), dict) and (value.get("object") == "block" or "id" in value):
            return {"type": kind, "content": clean(value[kind])}
        result = {}
        for key, item in value.items():
            if key in {"plain_text", "href"} or (key == "color" and item == "default") or (key == "link" and item is None):
                continue
            if (key == "is_toggleable" and item is False) or (key == "icon" and item is None) or (key == "caption" and item == []):
                continue
            if key == "annotations" and item == {"bold": False, "italic": False, "strikethrough": False, "underline": False, "code": False, "color": "default"}:
                continue
            result[key] = clean(item)
        return result
    kind = block.get("type")
    asset_content = asset_block_identity(block)
    return {"type": kind, "content": clean(asset_content if asset_content is not None else block.get(kind, {}))}


def block_identity(block: Dict[str, Any]) -> str:
    return sha256_text(json.dumps(_canonical_block(block), sort_keys=True, ensure_ascii=False))


def _semantic_block_identity(block: Dict[str, Any]) -> str:
    """Notion may merge equal-attribute text runs; content, links and formatting remain exact."""
    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if not isinstance(value, list):
            return value
        result: List[Any] = []
        def text_run(item: Any) -> bool:
            return isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text", {}).get("content"), str)
        def attributes(item: Dict[str, Any]) -> Dict[str, Any]:
            return {**item, "text": {key: part for key, part in item["text"].items() if key != "content"}}
        for raw in value:
            item = normalize(raw)
            if result and text_run(result[-1]) and text_run(item) and attributes(result[-1]) == attributes(item):
                result[-1]["text"]["content"] += item["text"]["content"]
            else:
                result.append(item)
        return result
    return sha256_text(json.dumps(normalize(_canonical_block(block)), sort_keys=True, ensure_ascii=False))


def intent_expected_matches(intent: Dict[str, Any], observed: Sequence[Dict[str, Any]]) -> bool:
    """Allow API run coalescing only against the remotely checkpointed original request."""
    if [block_identity(block) for block in observed] == intent.get("expected"):
        return True
    payload = (intent.get("context") or {}).get("request", {}).get("children")
    if not isinstance(payload, list) or len(payload) != len(observed) or any(not isinstance(block, dict) for block in payload):
        return False
    # The saved payload must still agree with the original unmodified expected hashes.
    if [block_identity(block) for block in payload] != intent.get("expected"):
        return False
    return [_semantic_block_identity(block) for block in observed] == [_semantic_block_identity(block) for block in payload]


def prepare_intent(operation_id: str, kind: str, parent_id: str, blocks: Sequence[Dict[str, Any]],
                   target_id: Optional[str] = None, baseline: Optional[Sequence[Dict[str, Any]]] = None,
                   context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Checkpoint one explicit batch; this does not enable automatic mirror writes."""
    if kind not in {"create", "append"} or not operation_id or not blocks or len(blocks) > 100:
        raise MirrorHeld("invalid intent")
    state = load_state()
    intents = state.setdefault("write_intents", {})
    if operation_id in intents:
        raise MirrorHeld("intent already exists; readback recovery required")
    assert_intent_fence(state, (context or {}).get("entry_key"), target_id)
    if state.get("write_outcome", {}).get("retry_allowed") is False:
        raise MirrorHeld("legacy unknown write requires review")
    marker = "eddie-mirror-operation:" + operation_id
    if kind == "create" and block_identity(blocks[0]) != block_identity(paragraph_block(marker)):
        raise MirrorHeld("create requires a unique operation marker")
    if kind == "append" and (not target_id or baseline is None or any(not item.get("id") for item in baseline)):
        raise MirrorHeld("append requires managed target identity and complete block baseline")
    if kind == "append":
        expected_baseline = [{"id": block["id"], "hash": block_identity(block)} for block in baseline or []]
        if not any(entry.get("page_id") == target_id and entry.get("managed_block_baseline") == expected_baseline
                   for entry in (state.get("entries") or {}).values()):
            raise MirrorHeld("old target lacks recorded management identity and content baseline")
    intent = {"kind": kind, "parent_id": parent_id, "target_id": target_id, "marker": marker,
              "expected": [block_identity(block) for block in blocks],
              "baseline": [{"id": block["id"], "hash": block_identity(block)} for block in baseline or []],
              "status": "prepared", "retry_allowed": False}
    if context is not None:
        intent["context"] = context
    intents[operation_id] = intent
    persist_private_state(state)
    return intent


def recover_intent(api_key: str, intent: Dict[str, Any]) -> Dict[str, Any]:
    """Read only. Ambiguous, partial, and human-modified targets stay held."""
    if intent.get("kind") == "create":
        candidates = []
        for block in retrieve_block_children(api_key, intent["parent_id"]):
            if block.get("type") != "child_page":
                continue
            target = str(block.get("id") or "")
            children = retrieve_block_tree(api_key, target)
            if children and block_identity(children[0]) == block_identity(paragraph_block(intent["marker"])):
                candidates.append((target, children))
        if len(candidates) != 1:
            return {"status": "held", "reason": "target_not_unique", "retry_allowed": False}
        target, children = candidates[0]
    else:
        target = intent.get("target_id")
        if not target or "baseline" not in intent:
            return {"status": "held", "reason": "missing_managed_baseline", "retry_allowed": False}
        children = retrieve_block_tree(api_key, target)
        baseline = intent["baseline"]
        observed = [{"id": block.get("id"), "hash": block_identity(block)} for block in children[:len(baseline)]]
        if observed != baseline:
            return {"status": "held", "reason": "human_or_target_change", "retry_allowed": False}
        children = children[len(baseline):]
    if not intent_expected_matches(intent, children):
        return {"status": "held", "reason": "partial_or_modified", "retry_allowed": False}
    context = intent.get("context") or {}
    if context.get("title"):
        try:
            verify_page_binding(api_key, target, intent["parent_id"], context["title"])
        except MirrorHeld:
            return {"status": "held", "reason": "target_binding_changed", "retry_allowed": False}
    return {"status": "verified", "target_id": target, "retry_allowed": False}


def recover_intents(api_key: str, persist: bool = False) -> Dict[str, int]:
    if persist:
        require_published_state()
    state = load_state()
    counts = {"verified": 0, "held": int(state.get("write_outcome", {}).get("retry_allowed") is False)}
    for operation, intent in (state.get("write_intents") or {}).items():
        if intent.get("status") == "verified":
            counts["already_verified"] = counts.get("already_verified", 0) + 1
            continue
        outcome = finalize_intent(api_key, operation) if persist else recover_intent(api_key, intent)
        counts[outcome["status"]] += 1
    return counts


def require_published_state() -> None:
    """Do not trust locally finalized state after a failed private publication."""
    try:
        verify_private_checkout(ROOT, os.environ.get("PRIVATE_VAULT_TOKEN", ""))
        relative = STATE_PATH.resolve().relative_to(ROOT.resolve()).as_posix()
        committed = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:" + relative], capture_output=True, timeout=15)
        local = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
        remote = subprocess.run(["git", "-C", str(ROOT), "ls-remote", "origin", "refs/heads/main"], capture_output=True, text=True, timeout=60)
        if committed.returncode or committed.stdout != STATE_PATH.read_bytes() or local.returncode or remote.returncode or remote.stdout.split()[:1] != [local.stdout.strip()]:
            raise ValueError("unpublished state")
    except Exception:
        raise MirrorHeld("private state requires published remote readback") from None


def _send_once(api_key: str, method: str, path: str, payload: Dict[str, Any]) -> None:
    """Only execute_revision calls this, after its dispatched checkpoint is remote."""
    if not ((method == "POST" and path == "/pages") or (method == "PATCH" and re.fullmatch(r"/blocks/[0-9a-f-]{36}/children", path))):
        raise MirrorHeld("unsupported mutation endpoint")
    def has_upload(value: Any) -> bool:
        if isinstance(value, list):
            return any(has_upload(item) for item in value)
        return isinstance(value, dict) and (value.get("type") == "file_upload" or any(has_upload(item) for item in value.values()))
    version = NOTION_VERSION
    if has_upload(payload):
        from notion_private_assets import NOTION_VERSION as upload_version
        version = upload_version
    request = urllib.request.Request(BASE_URL + path, method=method,
        headers={"Authorization": "Bearer " + api_key, "Notion-Version": version,
                 "Content-Type": "application/json"},
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
        if not isinstance(result, dict) or result.get("object") == "error":
            raise ValueError("invalid receipt")
    except (OSError, ValueError) as exc:
        raise UnknownWrite("single attempt outcome requires readback") from exc


def _baseline(blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    def complete(block: Dict[str, Any]) -> bool:
        kind = block.get("type")
        if not block.get("id"):
            return False
        if block.get("has_children") and kind not in {"child_page", "child_database"}:
            children = block.get(kind, {}).get("children")
            return isinstance(children, list) and all(complete(child) for child in children)
        return True
    if not all(complete(block) for block in blocks):
        raise MirrorHeld("managed baseline incomplete or nested content requires review")
    return [{"id": block["id"], "hash": block_identity(block)} for block in blocks]


def finalize_intent(api_key: str, operation_id: str) -> Dict[str, Any]:
    """Read targets again and durably record success; never issue a Notion write."""
    state = load_state()
    intent = state["write_intents"][operation_id]
    if intent.get("status") == "verified" and intent.get("receipt_compacted"):
        return {"status": "verified", "target_id": intent["target_id"], "retry_allowed": False}
    outcome = recover_intent(api_key, intent)
    if outcome["status"] != "verified":
        return outcome
    target = outcome["target_id"]
    observed = retrieve_block_tree(api_key, target)
    baseline = _baseline(observed)
    expected = intent["baseline"] + [{"id": block["id"], "hash": block_identity(block)}
                                    for block in observed[len(intent["baseline"]):]]
    if baseline != expected or not intent_expected_matches(intent, observed[len(intent["baseline"]):]):
        return {"status": "held", "reason": "target_changed_during_readback", "retry_allowed": False}
    context = intent.get("context")
    if not context:
        raise MirrorHeld("legacy intent lacks source binding; readback only")
    if intent.get("status") == "verified":
        return outcome
    intent.update(status="verified", target_id=target, verified_baseline=baseline)
    entry = state.setdefault("entries", {}).setdefault(context["entry_key"], {})
    entry.update(page_id=target, parent_page_id=intent["parent_id"], managed_block_baseline=baseline)
    if context.get("preserved_target_ref"):
        entry["preserved_target_version"] = context["preserved_target_ref"]
    if context["final_batch"]:
        entry.update(signature=context["source_signature"], type=context.get("node_kind", "file"), render_version=context.get("render_version", "legacy-v1"),
                     revision_generation=context.get("revision_generation", 0), source_snapshot=context["source_snapshot"])
    persist_private_state(state)
    return outcome


def execute_revision(api_key: str, relative: str, parent_id: Optional[str], allow_new: bool = False, expected_signature: Optional[str] = None) -> Dict[str, int]:
    """Explicit one-source append-only execution; old signature-only entries stay held."""
    if Path(relative).as_posix() != relative or ".." in Path(relative).parts:
        raise MirrorHeld("source path must be canonical")
    require_published_state()
    source = ROOT / relative
    if source.is_symlink() or not source.resolve().is_relative_to(ROOT.resolve()) or not source.is_file():
        raise MirrorHeld("source path invalid")
    include, exclude = load_patterns(argparse.Namespace(include_json=None, exclude_json=None))
    if not is_included(relative, include, exclude):
        raise MirrorHeld("source outside mirror scope")
    content = source.read_bytes()
    snapshot = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
    original = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:" + relative], capture_output=True, timeout=15)
    if snapshot.returncode or original.returncode or original.stdout != content:
        raise MirrorHeld("source must match the private committed snapshot")
    frozen_markdown = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    signature = sha256_text(frozen_markdown)
    if expected_signature is not None and signature != expected_signature:
        raise MirrorHeld("reviewed source version changed")
    node = Node(relative, None, source.name, "file", signature)
    blocks = file_page_blocks(ROOT, relative, markdown=frozen_markdown)
    return execute_node_blocks(api_key, node, blocks, parent_id, allow_new, snapshot.stdout.strip())


def assert_intent_fence(state: Dict[str, Any], entry_key: Optional[str], target: Optional[str]) -> None:
    """Unknown legacy outcomes fence globally; identified outcomes fence their object."""
    if state.get("write_outcome", {}).get("retry_allowed") is False:
        raise MirrorHeld("legacy unknown write requires review")
    for intent in (state.get("write_intents") or {}).values():
        if intent.get("status") == "verified":
            continue
        context = intent.get("context") or {}
        if not entry_key or not context.get("entry_key") or context["entry_key"] == entry_key or (target and intent.get("target_id") == target):
            raise MirrorHeld("object has an unresolved write; read-only recovery required")


def verify_page_binding(api_key: str, target: str, parent: str, title: str) -> None:
    page = get_page(api_key, target)
    relation = page.get("parent") or {}
    if page.get("archived") or page.get("in_trash") or relation.get("type") != "page_id" or normalize_notion_id(str(relation.get("page_id", ""))) != normalize_notion_id(parent) or page_title_from_page(page) != title:
        raise MirrorHeld("original page identity or parent changed")


def execute_node_blocks(api_key: str, node: Node, blocks: List[Dict[str, Any]], parent_id: Optional[str],
                        allow_new: bool, snapshot: str, budget: Optional[Dict[str, Any]] = None, render_version: str = "legacy-v1") -> Dict[str, int]:
    relative, signature = node.path, node.signature
    secret = state_secret()
    key = state_entry_key(relative, secret)
    state = load_state()
    entry = previous_entry_for_path(state.get("entries", {}), relative, secret)
    has_entry = key in state.get("entries", {}) or relative in state.get("entries", {})
    if has_entry and (not entry.get("page_id") or "managed_block_baseline" not in entry):
        raise MirrorHeld("legacy source lacks managed identity and baseline")
    if state.get("write_outcome", {}).get("retry_allowed") is False:
        raise MirrorHeld("legacy unknown write requires review")
    assert_intent_fence(state, key, entry.get("page_id"))
    require_known_or_protected_binding(state, key, entry)
    if entry.get("signature") == signature and entry.get("render_version", "legacy-v1") == render_version:
        if _baseline(retrieve_block_tree(api_key, entry["page_id"])) != entry["managed_block_baseline"]:
            raise MirrorHeld("human changes retained; baseline review required")
        return {"verified_batches": 0, "skipped": 1}
    owner = os.environ.get("GITHUB_RUN_ID") or os.environ.get("CODEX_THREAD_ID")
    if not owner:
        raise MirrorHeld("write owner unavailable")
    chunks = payload_chunks(blocks)
    generation = entry.get("revision_generation", 0) + 1
    old_contexts = [i.get("context", {}) for i in (state.get("write_intents") or {}).values()
                    if i.get("context", {}).get("entry_key") == key and i.get("context", {}).get("source_signature") == signature
                    and i.get("context", {}).get("render_version", "legacy-v1") == render_version]
    legacy_partial = old_contexts and not any(c.get("final_batch") for c in old_contexts) and all("revision_generation" not in c for c in old_contexts)
    verified = 0
    for index, chunk in enumerate(chunks):
        if budget is not None and (budget["remaining"] <= 0 or time.monotonic() >= budget["deadline"]):
            return {"verified_batches": verified, "skipped": 0, "deferred": 1}
        state = load_state()
        entry = previous_entry_for_path(state.get("entries", {}), relative, secret)
        identity = [key, signature, index] if legacy_partial and render_version == "legacy-v1" else [key, signature, render_version, generation, index]
        operation = sha256_text(json.dumps(identity, separators=(",", ":")))
        existing = (state.get("write_intents") or {}).get(operation)
        if existing:
            if existing.get("status") != "verified":
                raise MirrorHeld("existing attempt requires explicit recovery; no resend")
            verified += 1
            continue
        target = entry.get("page_id")
        parent = entry.get("parent_page_id") or parent_id
        if not parent:
            raise MirrorHeld("explicit parent binding unavailable")
        if not UUID_RE.fullmatch(normalize_notion_id(parent)) or (target and not UUID_RE.fullmatch(normalize_notion_id(target))):
            raise MirrorHeld("target binding requires valid Notion IDs")
        kind = "append" if target else "create"
        if kind == "create":
            if not allow_new or index != 0:
                raise MirrorHeld("new source requires explicit create authorization")
            if node.title in list_child_pages(api_key, parent):
                raise MirrorHeld("existing titled target cannot be adopted or duplicated")
            baseline = []
        else:
            if budget is not None:
                verify_page_binding(api_key, target, parent, node.title)
            baseline = retrieve_block_tree(api_key, target)
            if _baseline(baseline) != entry.get("managed_block_baseline"):
                raise MirrorHeld("human changes retained; baseline review required")
        desired = [paragraph_block("eddie-mirror-operation:" + operation),
                   heading_block(2, "同步版本 " + signature[:12] + f" · {index + 1}/{len(chunks)}"),
                   *chunk, paragraph_block("eddie-mirror-end:" + operation)]
        payload = {"children": desired}
        if kind == "create":
            payload.update(parent={"type": "page_id", "page_id": normalize_notion_id(parent)},
                           properties={"title": {"title": [{"type": "text", "text": {"content": node.title}}]}})
        context = {"entry_key": key, "source_path": relative, "source_signature": signature,
                   "source_snapshot": snapshot, "node_kind": node.kind, "title": node.title if budget is not None else None, "render_version": render_version, "revision_generation": generation, "final_batch": index == len(chunks) - 1,
                   "batch_index": index, "batch_count": len(chunks), "owner": owner,
                   "request": payload, "method": "POST" if kind == "create" else "PATCH",
                   "path": "/pages" if kind == "create" else f"/blocks/{normalize_notion_id(target)}/children"}
        if entry.get("preserve_target_content") is True:
            context["preserved_target"] = preserved_target_snapshot(api_key, relative, entry, baseline)
        prepare_intent(operation, kind, parent, desired, target, baseline, context=context)
        prepared_revision = private_checkpoint_revision() if context.get("preserved_target") else None
        state = load_state()
        intent = state["write_intents"][operation]
        if prepared_revision:
            intent["context"]["preserved_target_ref"] = {"commit": prepared_revision, "operation_id": operation,
                "snapshot_hash": context["preserved_target"]["snapshot_hash"]}
        intent["status"] = "dispatched"
        persist_private_state(state)
        if budget is not None:
            budget["remaining"] -= 1
        try:
            _send_once(api_key, context["method"], context["path"], payload)
        except UnknownWrite:
            state = load_state()
            state["write_intents"][operation]["status"] = "unknown_write"
            persist_private_state(state)
            raise MirrorHeld("unknown outcome retained; explicit recovery required")
        if finalize_intent(api_key, operation)["status"] != "verified":
            raise MirrorHeld("target readback held; no duplicate request permitted")
        verified += 1
    return {"verified_batches": verified, "skipped": 0}

def _bulk_sources(request: Dict[str, Any]) -> Tuple[str, List[Node], Dict[str, str]]:
    """Freeze one committed source revision; private checkpoint commits may then advance HEAD."""
    include, exclude = request.get("include_patterns"), request.get("exclude_patterns")
    if not isinstance(include, list) or not include or not isinstance(exclude, list) or any(not isinstance(v, str) or not v for v in include + exclude):
        raise MirrorHeld("approved bulk scope unavailable")
    revision = "HEAD" if request.get("follow_source_changes") is True else request.get("source_snapshot")
    if not isinstance(revision, str) or (revision != "HEAD" and not re.fullmatch(r"[0-9a-f]{40}", revision)):
        raise MirrorHeld("approved source snapshot unavailable")
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", revision + "^{commit}"], capture_output=True, text=True, timeout=15)
    if result.returncode:
        # A depth-one clean runner may not retain the earlier state checkpoint parent.
        # The exact reviewed per-source signatures below still have to match HEAD.
        result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD^{commit}"], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise MirrorHeld("source snapshot unavailable")
    snapshot = result.stdout.strip()
    tree = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-rz", snapshot], capture_output=True, timeout=30)
    if tree.returncode:
        raise MirrorHeld("source tree unavailable")
    markdown: Dict[str, str] = {}
    for record in tree.stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        path = raw_path.decode("utf-8")
        if not path.endswith(".md") or not is_included(path, include, exclude):
            continue
        if metadata.split()[0] not in {b"100644", b"100755"} or Path(path).as_posix() != path or ".." in Path(path).parts:
            raise MirrorHeld("source is not a regular canonical file")
        local = ROOT / path
        if local.is_symlink() or not local.resolve().is_relative_to(ROOT.resolve()):
            raise MirrorHeld("source path escapes checkout")
        blob = subprocess.run(["git", "-C", str(ROOT), "show", snapshot + ":" + path], capture_output=True, timeout=15)
        if blob.returncode:
            raise MirrorHeld("source bytes unavailable")
        # The existing renderer reads local bytes only after this exact Git comparison.
        if local.exists() and (not local.is_file() or local.read_bytes() != blob.stdout):
            raise MirrorHeld("source checkout differs from committed snapshot")
        markdown[path] = blob.stdout.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not markdown:
        raise MirrorHeld("empty bulk scope cannot imply successful migration")
    if request.get("follow_source_changes") is not True:
        reviewed = request.get("sources")
        actual = [{"path": path, "signature": sha256_text(text)} for path, text in sorted(markdown.items())]
        if reviewed != actual:
            raise MirrorHeld("reviewed bulk source manifest changed")
    nodes = build_nodes(sorted(markdown), ROOT, signatures={path: sha256_text(text) for path, text in markdown.items()})
    return snapshot, nodes, markdown


def require_known_or_protected_binding(state: Dict[str, Any], key: str, entry: Dict[str, Any]) -> None:
    if entry.get("signature") is not None or entry.get("preserve_target_content") is True or not entry.get("page_id"):
        return
    owned = any(intent.get("status") == "verified" and intent.get("target_id") == entry["page_id"]
                and intent.get("context", {}).get("entry_key") == key for intent in (state.get("write_intents") or {}).values())
    if not owned:
        raise MirrorHeld("unknown source binding requires explicit preservation identity")


def verify_protected_original(api_key: str, relative: str, entry: Dict[str, Any], blocks: Sequence[Dict[str, Any]]) -> None:
    """An unknown source signature is not authority; verify the original page identity."""
    if entry.get("preserve_target_content") is not True:
        return
    proof = entry.get("original_identity") or {}
    if (proof.get("source_path") != relative or proof.get("parent_page_id") != entry.get("parent_page_id")
            or proof.get("title") != Path(relative).name or not proof.get("creator_id") or not proof.get("source_quote_id")):
        raise MirrorHeld("protected original identity evidence incomplete")
    state = load_state()
    root = previous_entry_for_path(state.get("entries", {}), ".", state_secret())
    if not root.get("page_id") or root["page_id"] != proof.get("mirror_root_id"):
        raise MirrorHeld("protected original mirror root changed")
    verify_page_binding(api_key, entry["page_id"], entry["parent_page_id"], proof["title"])
    root_meta = get_page(api_key, root["page_id"])
    page = get_page(api_key, entry["page_id"])
    if (root_meta.get("created_by", {}).get("id") != proof["creator_id"] or page.get("created_by", {}).get("id") != proof["creator_id"]
            or root_meta.get("archived") or root_meta.get("in_trash") or root_meta.get("parent", {}).get("page_id") != root.get("parent_page_id")
            or page.get("archived") or page.get("in_trash") or page.get("parent", {}).get("page_id") != entry["parent_page_id"]
            or page_title_from_page(page) != proof["title"]):
        raise MirrorHeld("protected original creator or parent changed")
    quote = next((b for b in blocks if b.get("id") == proof["source_quote_id"] and b.get("type") == "quote"), None)
    text = "".join(part.get("text", {}).get("content", part.get("plain_text", "")) for part in (quote or {}).get("quote", {}).get("rich_text", []))
    if text != "来源路径：" + relative:
        raise MirrorHeld("protected original source quote changed")
    entry_key = state_entry_key(relative, state_secret())
    assert_intent_fence(state, entry_key, entry["page_id"])
    pending = list(blocks)
    has_marker = False
    while pending:
        block = pending.pop()
        kind = block.get("type")
        content = block.get(kind) or {}
        pending.extend(content.get("children", []))
        if kind != "paragraph":
            continue
        value = "".join(part.get("text", {}).get("content", part.get("plain_text", "")) for part in content.get("rich_text", []))
        for prefix in ("eddie-mirror-operation:", "eddie-mirror-end:"):
            if not value.startswith(prefix):
                continue
            has_marker = True
            operation = value[len(prefix):]
            intent = (state.get("write_intents") or {}).get(operation) or {}
            if (intent.get("status") != "verified" or intent.get("target_id") != entry["page_id"]
                    or (intent.get("context") or {}).get("entry_key") != entry_key):
                raise MirrorHeld("protected original has an unbound operation marker")
    if entry.get("signature") is None and not entry.get("revision_generation") and not entry.get("preserved_target_version"):
        original = entry.get("preserved_original") or {}
        original_blocks = original.get("blocks")
        if (has_marker or original.get("target_page_id") != entry["page_id"] or original.get("parent_page_id") != entry["parent_page_id"]
                or original.get("title") != proof["title"] or type(original.get("source_signature_known")) is not bool
                or (original.get("source_signature_known") and not re.fullmatch(r"[0-9a-f]{64}", str(original.get("source_signature", ""))))
                or not isinstance(original_blocks, list) or _baseline(original_blocks) != original.get("baseline")):
            raise MirrorHeld("unknown original version was not fully preserved")


def preserved_target_snapshot(api_key: str, relative: str, entry: Dict[str, Any], blocks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """A private snapshot is actual target evidence, never an invented Git source version."""
    verify_protected_original(api_key, relative, entry, blocks)
    # Native binary URLs expire. Require an existing pinned private-Git blob as well.
    from notion_private_assets import CREDENTIAL_SHAPE, canonical_native_file, prepare, AssetHeld
    encoded = json.dumps(blocks, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if (len(api_key) > 16 and api_key.encode() in encoded) or CREDENTIAL_SHAPE.search(encoded):
        raise MirrorHeld("protected target snapshot contains potential credentials")
    assets = load_state().get("assets") or {}
    binary_refs: Dict[str, Any] = {}
    pending = list(blocks)
    while pending:
        block = pending.pop()
        kind = block.get("type")
        value = block.get(kind) or {}
        pending.extend(value.get("children", []))
        if kind == "unsupported":
            raise MirrorHeld("protected target contains unreadable block content")
        if kind not in {"file", "image", "pdf", "audio", "video"} or value.get("type") != "file":
            continue
        try:
            actual = canonical_native_file(block)
        except AssetHeld:
            raise MirrorHeld("protected native file could not be preserved") from None
        receipt = assets.get(actual["sha256"]) or {}
        if (receipt.get("sha256") != actual["sha256"] or receipt.get("size") != actual["size"]
                or not re.fullmatch(r"[0-9a-f]{40}", str(receipt.get("source_blob", "")))
                or not re.fullmatch(r"[0-9a-f]{40}", str(receipt.get("source_commit", "")))):
            raise MirrorHeld("protected native file lacks durable private blob")
        try:
            pinned = prepare(ROOT, receipt["source_commit"], receipt.get("source_path"))
        except AssetHeld:
            raise MirrorHeld("protected native file private blob unavailable") from None
        if pinned.blob != receipt["source_blob"] or pinned.sha256 != actual["sha256"] or len(pinned.data) != actual["size"]:
            raise MirrorHeld("protected native file private blob mismatch")
        ancestry = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", receipt["source_commit"], "HEAD"], capture_output=True, timeout=15)
        if ancestry.returncode:
            raise MirrorHeld("protected native file private blob not in checkpoint history")
        binary_refs[block["id"]] = {key: receipt[key] for key in ("sha256", "size", "source_blob", "source_commit", "source_path")}
    snapshot = {"kind": "observed_notion_target_version", "source_signature": None,
                "target_page_id": entry["page_id"], "parent_page_id": entry["parent_page_id"], "source_path": relative,
                "blocks": list(blocks), "baseline": _baseline(blocks), "durable_binary_refs": binary_refs}
    snapshot["snapshot_hash"] = sha256_text(json.dumps(snapshot, sort_keys=True, ensure_ascii=False))
    return snapshot


def private_checkpoint_revision() -> str:
    """Call only after publication acknowledgement; point at the committed full snapshot."""
    require_published_state()
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
    if head.returncode or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip()):
        raise MirrorHeld("private preservation checkpoint unavailable")
    return head.stdout.strip()


def _adopt_bulk_target(api_key: str, node: Node, parent: str, desired: List[Dict[str, Any]]) -> bool:
    """Bind only a unique original page with source/body evidence; never rewrite it."""
    state = load_state()
    key = state_entry_key(node.path, state_secret())
    entry = previous_entry_for_path(state.get("entries", {}), node.path, state_secret())
    assert_intent_fence(state, key, entry.get("page_id"))
    # Full parent pagination is mandatory even for a previously managed identity.
    candidates = list_child_pages(api_key, parent)
    target = candidates.get(node.title)
    if entry.get("page_id"):
        require_known_or_protected_binding(state, key, entry)
        if target != entry["page_id"]:
            raise MirrorHeld("original page moved, renamed or duplicated")
        verify_page_binding(api_key, target, parent, node.title)
        if entry.get("preserve_target_content") is True:
            verify_protected_original(api_key, node.path, entry, retrieve_block_tree(api_key, target))
        return True
    if not target:
        # A formerly known source cannot silently be replaced when its page is missing.
        if entry:
            raise MirrorHeld("legacy original page not found")
        return False
    verify_page_binding(api_key, target, parent, node.title)
    observed = retrieve_block_tree(api_key, target)
    body = [b for b in observed if b.get("type") != "child_page"] if node.kind != "file" else observed
    if entry.get("signature") not in {None, node.signature}:
        raise MirrorHeld("legacy source changed before original baseline adoption")
    if [block_identity(b) for b in body[:len(desired)]] != [block_identity(b) for b in desired]:
        raise MirrorHeld("original page source/body evidence incomplete")
    # Additional human blocks are retained as part of the observed baseline.
    adopted = {**entry, "page_id": target, "parent_page_id": parent, "type": node.kind,
               "signature": node.signature, "managed_block_baseline": _baseline(body)}
    state.setdefault("entries", {})[key] = adopted
    persist_private_state(state)
    return True


def published_preservation_matches(ref: Dict[str, Any], snapshot: Dict[str, Any]) -> bool:
    """Only discard a duplicate after reading its exact prepared snapshot from Git."""
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", str(ref.get("commit", ""))):
            return False
        body = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
        digest = sha256_text(json.dumps(body, sort_keys=True, ensure_ascii=False))
        if digest != snapshot.get("snapshot_hash") or digest != ref.get("snapshot_hash"):
            return False
        relative = STATE_PATH.resolve().relative_to(ROOT.resolve()).as_posix()
        receipt = subprocess.run(["git", "-C", str(ROOT), "show", ref["commit"] + ":" + relative], capture_output=True, timeout=30)
        if receipt.returncode:
            return False
        old = json.loads(receipt.stdout)["write_intents"][ref["operation_id"]]
        return old.get("status") == "prepared" and old.get("context", {}).get("preserved_target") == snapshot
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return False


def compact_verified_intents(state: Dict[str, Any], receipt_commit: str) -> None:
    """Remove only redundant, already-verified payload copies; Git retains original receipts."""
    if not re.fullmatch(r"[0-9a-f]{40}", receipt_commit):
        raise MirrorHeld("verified receipt history pin unavailable")
    for intent in (state.get("write_intents") or {}).values():
        if intent.get("status") != "verified" or intent.get("receipt_compacted"):
            continue
        proof = {name: intent.get(name) for name in ("expected", "baseline", "verified_baseline")}
        intent["receipt_hash"] = sha256_text(json.dumps(proof, sort_keys=True, ensure_ascii=False))
        intent["receipt_compacted"] = True
        intent["receipt_commit"] = receipt_commit
        for name in proof:
            intent.pop(name, None)
        if isinstance(intent.get("context"), dict):
            intent["context"].pop("request", None)
            ref = intent["context"].get("preserved_target_ref") or {}
            snapshot = intent["context"].get("preserved_target")
            if snapshot and published_preservation_matches(ref, snapshot):
                intent["context"].pop("preserved_target", None)


def retain_human_append(api_key: str, node: Node) -> None:
    state = load_state()
    key = state_entry_key(node.path, state_secret())
    entry = previous_entry_for_path(state.get("entries", {}), node.path, state_secret())
    observed = retrieve_block_tree(api_key, entry["page_id"])
    if node.kind != "file":
        observed = [block for block in observed if block.get("type") != "child_page"]
    baseline = _baseline(observed)
    previous = entry["managed_block_baseline"]
    if entry.get("preserve_target_content") is True and baseline != previous:
        snapshot = preserved_target_snapshot(api_key, node.path, entry, observed)
        entry["preserved_observation"] = snapshot
        if baseline[:len(previous)] != previous:
            generations = [intent.get("context", {}).get("revision_generation", 0) for intent in (state.get("write_intents") or {}).values()
                           if intent.get("status") == "verified" and intent.get("context", {}).get("entry_key") == key]
            entry.update(signature=None, render_version="unknown-original", revision_generation=max([entry.get("revision_generation", 0), *generations]))
        entry["managed_block_baseline"] = baseline
        state["entries"][key] = entry
        persist_private_state(state)
        return
    if baseline[:len(previous)] != previous:
        raise MirrorHeld("existing content modified; preserved for review")
    if len(baseline) > len(previous):
        entry["managed_block_baseline"] = baseline
        state["entries"][key] = entry
        persist_private_state(state)


def execute_bulk(api_key: str, opaque_id: Optional[str] = None) -> Dict[str, int]:
    """One bounded pass over the approved hierarchy; use the same entry for cron and initial catch-up."""
    require_published_state()
    _ASSET_READBACK_CACHE.clear()
    _SOURCE_TREE_CACHE.clear()
    state = load_state()
    request = state.get("bulk_request") or {}
    try:
        request_id = request["request_id"]
        if uuid.UUID(request_id).version != 4 or request.get("enabled") is not True or (opaque_id is not None and opaque_id != request_id):
            raise ValueError("unapproved request")
        parent = normalize_notion_id(request["parent_page_id"])
        if not UUID_RE.fullmatch(parent):
            raise ValueError("invalid parent")
        # Configuration is private; public callers cannot override scope, parent or budgets.
        max_batches = request.get("max_batches", 100)
        max_seconds = request.get("max_seconds", 900)
        if type(max_batches) is not int or not 1 <= max_batches <= 1000 or type(max_seconds) is not int or not 1 <= max_seconds <= 1200:
            raise ValueError("invalid budget")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise MirrorHeld("approved private bulk request unavailable") from None
    assert_intent_fence(state, "bulk-preflight", None)
    snapshot, nodes, markdown = _bulk_sources(request)
    root_title = state.get("root_title") or DEFAULT_ROOT_TITLE
    nodes[0] = Node(".", None, root_title, "root", ROOT_SIGNATURE)
    budget = {"remaining": max_batches, "deadline": time.monotonic() + max_seconds}
    signatures = {node.path: node.signature for node in nodes}
    old_progress = state.get("bulk_progress") or {}
    continuing = (old_progress.get("request_id") == request_id and old_progress.get("cycle_complete") is False
                  and old_progress.get("renderer_revision") == NATIVE_RENDERER_REVISION)
    fenced_keys = {(intent.get("context") or {}).get("entry_key") for intent in (state.get("write_intents") or {}).values() if intent.get("status") != "verified"}
    fenced_targets = {intent.get("target_id") for intent in (state.get("write_intents") or {}).values() if intent.get("status") != "verified"}
    def still_bound(path: str, signature: str) -> bool:
        entry = previous_entry_for_path(state.get("entries", {}), path, state_secret())
        return bool(entry.get("page_id") and entry.get("parent_page_id") and "managed_block_baseline" in entry
                    and entry.get("signature") == signature and state_entry_key(path, state_secret()) not in fenced_keys
                    and entry["page_id"] not in fenced_targets)
    seen = {path: signature for path, signature in (old_progress.get("cycle_verified") or {}).items()
            if continuing and signatures.get(path) == signature and still_bound(path, signature)}
    held = {path: reason for path, reason in (old_progress.get("held") or {}).items() if continuing and path in signatures}
    if continuing:
        for path in old_progress.get("cycle_verified") or {}:
            entry = previous_entry_for_path(state.get("entries", {}), path, state_secret())
            if path in signatures and (state_entry_key(path, state_secret()) in fenced_keys or entry.get("page_id") in fenced_targets):
                held[path] = "object_held"
    cycle_assets = {path: value for path, value in (old_progress.get("cycle_assets") or {}).items() if path in seen}
    counts = {"total": len(nodes), "verified": 0, "adopted": 0, "skipped": 0, "held": 0, "deferred": 0, "verified_batches": 0, "assets_ready": 0, "assets_held": 0, "assets_fallback": 0}
    resolved: Dict[str, str] = {}
    by_path = {node.path: node for node in nodes}
    def order(node: Node) -> Tuple[int, int, str]:
        return (0, 0, ".") if node.kind == "root" else (len(Path(node.path).parts), 0 if node.kind == "directory" else 1, node.path)
    cursor = old_progress.get("next_order") if continuing else None
    if cursor is not None and (not isinstance(cursor, list) or len(cursor) != 3 or type(cursor[0]) is not int or type(cursor[1]) is not int or not isinstance(cursor[2], str)):
        raise MirrorHeld("private bulk cursor invalid")
    start = next((index for index, node in enumerate(nodes) if cursor is None or order(node) >= tuple(cursor)), len(nodes))
    next_index = len(nodes)
    def mark_held(path: str, reason: str) -> None:
        seen.pop(path, None)
        cycle_assets.pop(path, None)
        held[path] = reason
    def ancestor_id(path: str) -> Optional[str]:
        if path in resolved:
            return resolved[path]
        node = by_path.get(path)
        if not node or node.kind == "file" or path in held:
            return None
        current_state = load_state()
        entry = previous_entry_for_path(current_state.get("entries", {}), path, state_secret())
        try:
            if not entry.get("page_id") or "managed_block_baseline" not in entry or entry.get("type") != node.kind:
                raise MirrorHeld("ancestor lacks verified management binding")
            assert_intent_fence(current_state, state_entry_key(path, state_secret()), entry["page_id"])
            expected_parent = parent if node.kind == "root" else ancestor_id(node.parent_path or ".")
            if not expected_parent or entry.get("parent_page_id") != expected_parent:
                raise MirrorHeld("ancestor parent binding changed")
            verify_page_binding(api_key, entry["page_id"], expected_parent, node.title)
            if list_child_pages(api_key, expected_parent).get(node.title) != entry["page_id"]:
                raise MirrorHeld("ancestor original identity no longer unique")
            resolved[path] = entry["page_id"]
            return entry["page_id"]
        except MirrorHeld:
            mark_held(path, "ancestor_held")
            return None
        except (RuntimeError, urllib.error.URLError):
            mark_held(path, "ancestor_read_held")
            return None
    for index in range(start, len(nodes)):
        node = nodes[index]
        if time.monotonic() >= budget["deadline"] or budget["remaining"] <= 0:
            next_index = index
            break
        parent_id = parent if node.kind == "root" else ancestor_id(node.parent_path or ".")
        if not parent_id:
            mark_held(node.path, "parent_held")
            continue
        state = load_state()
        previous = previous_entry_for_path(state.get("entries", {}), node.path, state_secret())
        blocks = file_page_blocks(ROOT, node.path, markdown[node.path]) if node.kind == "file" else blocks_for_node(ROOT, node, root_title)
        try:
            if node.kind != "root":
                ancestor = previous_entry_for_path(state.get("entries", {}), node.parent_path or ".", state_secret())
                ancestor_title = root_title if node.parent_path == "." else Path(node.parent_path or ".").name
                verify_page_binding(api_key, parent_id, ancestor["parent_page_id"], ancestor_title)
            adopted = _adopt_bulk_target(api_key, node, parent_id, blocks)
            current = previous_entry_for_path(load_state().get("entries", {}), node.path, state_secret())
            if adopted and not previous.get("page_id"):
                counts["adopted"] += 1
            if adopted:
                retain_human_append(api_key, node)
                current = previous_entry_for_path(load_state().get("entries", {}), node.path, state_secret())
            if node.kind != "file" and adopted:
                # Child pages evolve independently of the directory's own retained human header.
                observed = [b for b in retrieve_block_tree(api_key, current["page_id"]) if b.get("type") != "child_page"]
                if _baseline(observed) != current["managed_block_baseline"]:
                    raise MirrorHeld("directory human content changed; retained for review")
                result = {"verified_batches": 0, "skipped": 1}
            else:
                render_version = "legacy-v1"
                if node.kind == "file":
                    blocks, render_version, asset_counts = render_native_source(api_key, node.path, markdown[node.path], snapshot, budget["deadline"])
                result = execute_node_blocks(api_key, node, blocks, parent_id, True, snapshot, budget, render_version)
            counts["verified_batches"] += result["verified_batches"]
            if result.get("deferred"):
                next_index = index
                break
            current = previous_entry_for_path(load_state().get("entries", {}), node.path, state_secret())
            resolved[node.path] = current["page_id"]
            seen[node.path] = node.signature
            held.pop(node.path, None)
            if node.kind == "file":
                cycle_assets[node.path] = asset_counts
            counts["skipped"] += result["skipped"]
        except MirrorHeld as exc:
            require_published_state()
            # A checkpoint failure fences the runner, not just the current source.
            if exc.code != "held":
                raise
            mark_held(node.path, "object_held")
        except (RuntimeError, urllib.error.URLError):
            # Read/pagination failures never justify create or loss of an existing binding.
            mark_held(node.path, "read_held")
    counts["attempted_batches"] = max_batches - budget["remaining"]
    state = load_state()
    counts.update(verified=len(seen), held=len(held), deferred=max(0, len(nodes) - len(seen) - len(held)))
    for name in ("assets_ready", "assets_held", "assets_fallback"):
        counts[name] = sum(value.get(name, 0) for value in cycle_assets.values())
    manifest_hash = sha256_text(json.dumps([[n.path, n.signature] for n in nodes], ensure_ascii=False))
    progress = {"request_id": request_id, "source_snapshot": old_progress.get("source_snapshot", snapshot) if old_progress.get("manifest_hash") == manifest_hash else snapshot,
                "manifest_hash": manifest_hash, "renderer_revision": NATIVE_RENDERER_REVISION, "counts": counts, "held": held,
                "next_order": list(order(nodes[next_index])) if next_index < len(nodes) else None,
                "cycle_complete": next_index == len(nodes), "cycle_verified": seen, "cycle_assets": cycle_assets}
    before_compaction = json.dumps(state, sort_keys=True, ensure_ascii=False)
    require_published_state()
    receipt_head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
    if receipt_head.returncode:
        raise MirrorHeld("verified receipt history pin unavailable")
    compact_verified_intents(state, receipt_head.stdout.strip())
    if old_progress != progress or json.dumps(state, sort_keys=True, ensure_ascii=False) != before_compaction:
        state["bulk_progress"] = progress
        persist_private_state(state)
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-bulk", action="store_true", help="Consume the approved private bulk scope with bounded work.")
    parser.add_argument("--bulk-request", help="Opaque UUID of the approved private bulk request.")
    parser.add_argument("--execute-request", help="Opaque UUID for one reviewed request in private mirror state.")
    parser.add_argument("--execute-revision", help="Explicit source path for guarded append-only execution.")
    parser.add_argument("--allow-new-page", action="store_true", help="Permit a genuinely new source under the explicit parent; never adopt legacy entries.")
    parser.add_argument("--persist-recovery", action="store_true", help="Persist conclusively verified recovery in the private mirror state.")
    parser.add_argument("--recover-intents", action="store_true", help="Read targets for existing private intents; no Notion or state writes.")
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


def _main() -> int:
    args = parse_args()
    if getattr(args, "execute_bulk", False) or getattr(args, "bulk_request", None):
        if args.execute_request or args.execute_revision or args.allow_new_page or args.recover_intents or args.persist_recovery or args.dry_run or args.plan_only or args.full_sync or args.include_json or args.exclude_json or args.path_prefix or args.limit:
            raise MirrorHeld("bulk execution cannot be overridden")
        counts = execute_bulk(load_notion_api_key(), args.bulk_request)
        incomplete = any(counts[name] for name in ("held", "assets_held", "deferred"))
        print(json.dumps({"status": "bulk_incomplete" if incomplete else "bulk_verified", "counts": counts}))
        return 2 if incomplete else 0
    if args.persist_recovery and (not args.recover_intents or args.dry_run or args.plan_only):
        raise MirrorHeld("persist recovery requires explicit recovery mode")
    if (args.execute_request or args.execute_revision) and (args.recover_intents or args.dry_run or args.plan_only or args.full_sync or args.include_json or args.exclude_json):
        raise MirrorHeld("execution cannot be combined with read-only modes")
    if args.execute_request:
        if args.execute_revision or args.allow_new_page:
            raise MirrorHeld("private request cannot be overridden")
        try:
            if uuid.UUID(args.execute_request).version != 4:
                raise ValueError("not an opaque request UUID")
            request = load_state()["execution_request"]
            if request["request_id"] != args.execute_request or type(request["allow_new_page"]) is not bool:
                raise ValueError("request mismatch")
            if not isinstance(request["source_signature"], str) or not re.fullmatch(r"[0-9a-f]{64}", request["source_signature"]):
                raise ValueError("source version missing")
            if not isinstance(request["source_path"], str) or not isinstance(request["parent_page_id"], str):
                raise ValueError("invalid source binding")
        except (ValueError, KeyError, TypeError):
            raise MirrorHeld("reviewed private execution request unavailable") from None
        counts = execute_revision(load_notion_api_key(), request["source_path"], request["parent_page_id"],
                                  request["allow_new_page"], expected_signature=request["source_signature"])
        print(json.dumps({"status": "revision_verified", "counts": counts}))
        return 0
    if args.execute_revision:
        counts = execute_revision(load_notion_api_key(), args.execute_revision, args.parent_page_id, args.allow_new_page)
        print(json.dumps({"status": "revision_verified", "counts": counts}))
        return 0
    if args.recover_intents:
        counts = recover_intents(load_notion_api_key(), persist=args.persist_recovery)
        print(json.dumps({"status": "recovery_readback", "counts": counts}))
        return 2 if counts["held"] else 0
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
    if state.get("write_outcome", {}).get("retry_allowed") is False:
        raise MirrorHeld("needs_review: unresolved unknown write requires target recovery")
    if any(item.get("status") != "verified" for item in (state.get("write_intents") or {}).values()):
        raise MirrorHeld("needs_review: unresolved intent requires target recovery")
    previous_entries = state.get("entries") or {}
    next_entries: Dict[str, Dict[str, Any]] = dict(previous_entries)
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
        # No durable write-intent checkpoint exists in this mirror yet.
        raise MirrorHeld("needs_review: new page creation requires a durable write receipt")

    root_blocks = blocks_for_node(source_root, nodes[0], args.root_title)
    root_page_id, root_created = ensure_page(parent_page_id, args.root_title, icon_for_node("root"), root_blocks if not args.dry_run else None)
    resolved_page_ids["."] = root_page_id
    root_entry_key = state_entry_key(".", state_key_secret)

    if root_created:
        if args.dry_run:
            pass
        else:
            next_entries[root_entry_key] = {**previous_entry_for_path(previous_entries, ".", state_key_secret), "type": "root", "signature": ROOT_SIGNATURE}
    else:
        root_changed = args.full_sync or previous_entry_for_path(previous_entries, ".", state_key_secret).get("signature") != ROOT_SIGNATURE
        if root_changed and not args.dry_run:
            replace_page_content(api_key, root_page_id, root_blocks)
        next_entries[root_entry_key] = {**previous_entry_for_path(previous_entries, ".", state_key_secret), "type": "root", "signature": ROOT_SIGNATURE}

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
        next_entries[state_entry_key(node.path, state_key_secret)] = {**previous_entry_for_path(previous_entries, node.path, state_key_secret), "type": node.kind, "signature": node.signature}

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


def main() -> int:
    try:
        return _main()
    except UnknownWrite:
        if "--recover-intents" in sys.argv and "--persist-recovery" not in sys.argv:
            print(json.dumps({"status": "recovery_held", "retry_allowed": False}))
            return 2
        # Reuse the existing state, preserving source cursors. Persisting this
        # file to the private vault on failure still needs workflow coordination.
        state = load_state()
        state["write_outcome"] = {"status": "unknown_write", "retry_allowed": False}
        persist_private_state(state)
        print(json.dumps({"status": "unknown_write", "retry_allowed": False}))
        return 2
    except MirrorHeld as exc:
        print(json.dumps({"status": "needs_review", "failure_code": exc.code, "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
