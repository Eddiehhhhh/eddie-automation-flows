#!/usr/bin/env python3
"""Audit Raw filenames for naming compliance.

Checks all Raw source directories against AGENTS.md Sync Filename Rules:
  - No date/time prefixes in filenames
  - Format: {title}.md (conflict suffix: -{id}.md)

Uses — exit code = number of violations found (CI-friendly).

Usage:
  python scripts/audit_raw_filenames.py           # scan & report
  python scripts/audit_raw_filenames.py --json     # machine-readable output
  python scripts/audit_raw_filenames.py --fix      # auto-rename violations
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))

# Source directories to audit — (relative_path, label)
SOURCE_DIRS: list[tuple[str, str]] = [
    ("Raw/01 Flomo",          "flomo"),
    ("Raw/03 Get",            "get"),
    ("Raw/06 TencentDocs",    "tencent_docs"),
]

# Old full-timestamp pattern: YYYY-MM-DD-HHMMSS-id-title.md
OLD_FULL_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}-[A-Za-z0-9+/=_-]+-.*\.md$")

# YYYY-MM-DD prefix (without time/memo_id): YYYY-MM-DD-title.md
YYYYMMDD_PREFIX_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-.*\.md$")

# MM-DD prefix pattern: MM-DD-title.md
MMDD_PREFIX_PATTERN = re.compile(r"^\d{2}-\d{2}-.*\.md$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def frontmatter_value(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value


def safe_slug(text: str, fallback: str) -> str:
    """Slugify text for use as a filename."""
    # Remove control characters (U+0000-U+001F)
    value = "".join(c for c in (text or "") if ord(c) >= 32)
    # Replace filename-unsafe characters
    value = re.sub(r'[\\/:*?"<>|]+', "-", value)
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-. ")
    return value or fallback



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


# Leading-date regex — mirrors sync_flomo_to_raw.py's clean_title()
LEADING_DATE_RE = re.compile(
    r"""^\s*
    (?:
        \d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?
        (?:\s*[Tt]\s*\d{1,2}:\d{2}(?::\d{2})?)?
        |\d{1,2}[-/.]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?
    )
    """,
    re.VERBOSE,
)


def strip_leading_date(text: str) -> str:
    """Strip leading date/time prefix from text, like clean_title()."""
    value = re.sub(r"\s+", " ", (text or "").strip())
    match = LEADING_DATE_RE.match(value)
    if match:
        remainder = value[match.end():].lstrip(" -—_:：·|#.")
        value = remainder
    value = re.sub(r"\s+", " ", value).strip()
    return value


def expected_clean_name(path: Path) -> str | None:
    """Generate the expected clean filename (stem only, no path).

    Strips leading date/time prefixes from the stored title, consistent
    with sync_flomo_to_raw.py's clean_title() behavior.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    title = frontmatter_value(text, "title")
    if not title:
        return None
    cleaned = strip_leading_date(title)
    if not cleaned:
        source_id = (
            frontmatter_value(text, "memo_id")
            or frontmatter_value(text, "note_id")
            or frontmatter_value(text, "doc_id")
            or "source"
        )
        cleaned = f"untitled-{source_id}"
    slug = truncate_utf8_bytes(safe_slug(cleaned, "untitled"), 160)
    return f"{slug}.md"


def is_old_format(name: str) -> bool:
    return bool(OLD_FULL_PATTERN.match(name))


def is_yyyymmdd_format(name: str) -> bool:
    return bool(YYYYMMDD_PREFIX_PATTERN.match(name))


def is_mmdd_format(name: str) -> bool:
    return bool(MMDD_PREFIX_PATTERN.match(name))


# ---------------------------------------------------------------------------
# Violation record
# ---------------------------------------------------------------------------

class Violation:
    def __init__(self, path: Path, source: str, reason: str, expected: str | None = None):
        self.path = path
        self.source = source
        self.reason = reason
        self.expected = expected

    def rel(self) -> str:
        return str(self.path.relative_to(ROOT))

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.rel(),
            "source": self.source,
            "reason": self.reason,
            "expected_name": self.expected,
        }

    def __str__(self) -> str:
        msg = f"[{self.source}] {self.rel()}: {self.reason}"
        if self.expected:
            msg += f"\n  expected: {self.expected}"
        return msg


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def scan_directory(base: Path, source: str) -> list[Violation]:
    """Scan a source directory and return naming violations."""
    violations: list[Violation] = []
    for path in sorted(base.rglob("*.md")):
        if path.is_symlink():
            continue
        name = path.name

        if is_old_format(name):
            expected = expected_clean_name(path)
            if expected is not None and expected == name:
                continue
            violations.append(Violation(
                path, source,
                "完整时间戳格式 (YYYY-MM-DD-HHMMSS-memo_id-标题.md)", expected
            ))
        elif is_yyyymmdd_format(name):
            expected = expected_clean_name(path)
            if expected is not None and expected == name:
                continue
            violations.append(Violation(
                path, source,
                "YYYY-MM-DD- 前缀格式", expected
            ))
        elif is_mmdd_format(name):
            expected = expected_clean_name(path)
            if expected is not None and expected == name:
                continue
            violations.append(Violation(
                path, source,
                "MM-DD- 前缀格式", expected
            ))
    return violations


# ---------------------------------------------------------------------------
# Fix
# ---------------------------------------------------------------------------

def fix_violation(v: Violation) -> bool:
    """Rename a single file to its expected clean name. Returns True if changed."""
    if not v.expected:
        return False

    parent = v.path.parent
    new_path = parent / v.expected

    # Handle name collision
    counter = 1
    while new_path.exists() and new_path.resolve() != v.path.resolve():
        stem = Path(v.expected).stem
        new_path = parent / f"{stem}-{counter}.md"
        counter += 1
        if counter > 100:
            print(f"  ERROR: too many collisions for {v.rel()}", file=sys.stderr)
            return False

    if new_path.resolve() == v.path.resolve():
        return False  # already at correct name

    v.path.rename(new_path)
    print(f"  FIXED: {v.rel()} -> {new_path.name}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON (machine-readable)")
    parser.add_argument("--fix", action="store_true",
                        help="Auto-rename violating files")
    args = parser.parse_args()

    # Scan
    all_violations: list[Violation] = []
    for rel_dir, label in SOURCE_DIRS:
        base = ROOT / rel_dir
        if not base.exists():
            print(f"SKIP (not found): {rel_dir}", file=sys.stderr)
            continue
        violations = scan_directory(base, label)
        all_violations.extend(violations)

    total = len(all_violations)

    # JSON output
    if args.json:
        result = {
            "total": total,
            "violations": [v.to_dict() for v in all_violations],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return total  # exit code = violation count

    # Human-readable output
    print(f"\n{'=' * 60}")
    print(f"  文件名合规审计 — Raw 来源目录")
    print(f"{'=' * 60}")
    print(f"  扫描范围: {', '.join(d for d, _ in SOURCE_DIRS)}")
    print(f"  违规总数: {total}")
    print()

    if total == 0:
        print("  ✓ 所有文件名合规，未发现违规。")
        return 0

    # Group by source
    by_source: dict[str, list[Violation]] = {}
    for v in all_violations:
        by_source.setdefault(v.source, []).append(v)

    for source, violations in sorted(by_source.items()):
        print(f"  [{source}] {len(violations)} 个违规:")
        for v in violations:
            print(f"    - {v.path.name}")
            print(f"      {v.reason}")
            if v.expected:
                print(f"      expected: {v.expected}")
        print()

    if args.fix:
        print(f"{'=' * 60}")
        print(f"  开始自动修复...")
        fixed = 0
        for v in all_violations:
            if fix_violation(v):
                fixed += 1
        print(f"  修复完成: {fixed}/{total} 个文件已重命名")
    else:
        print(f"  提示: 使用 --fix 参数自动重命名违规文件。")

    return total  # exit code = violation count


if __name__ == "__main__":
    sys.exit(main())
