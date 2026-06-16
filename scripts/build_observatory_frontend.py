#!/usr/bin/env python3
"""Build the observatory front-end from markdown archives.

The back-end source of truth remains the three sibling markdown files under
daily/weekly/monthly period folders. This script builds a durable, local HTML
front-end plus a JSON snapshot for agents and humans.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
OBS_ROOT = ROOT / "Wiki" / "10 观测站" / "艾迪宇宙观测站"
HTML_OUTPUT = OBS_ROOT / "index.html"
JSON_OUTPUT = OBS_ROOT / "metrics" / "observatory-data.json"
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
INLINE_RE = re.compile(
    r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]|\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`"
)
ORDERED_ITEM_RE = re.compile(r"^\d+\.\s+")
PATHISH_PREFIXES = ("Wiki/", "Raw/", "Schema/", "scripts/")
VIEW_CONFIG = {
    "daily": {
        "label": "每日迭代",
        "default_tab": "个人状态",
        "display_path": "daily/YYYY/YYYY-MM/YYYY-MM-DD/",
    },
    "weekly": {
        "label": "每周迭代",
        "default_tab": "系统状态",
        "display_path": "weekly/YYYY/YYYY-Www/",
    },
    "monthly": {
        "label": "每月观测",
        "default_tab": "启发洞察",
        "display_path": "monthly/YYYY/YYYY-MM/",
    },
}
TAB_DISPLAY = {
    "个人状态": "个人状态",
    "系统状态": "系统状态",
    "启发洞察": "洞察发芽",
}
SECTION_EXCLUDE = {"标签", "引用文章"}


@dataclass
class Reference:
    raw: str
    label: str
    uri: str | None


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def strip_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, text[match.end():]


def parse_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
            continue
        if line.startswith("# "):
            continue
        if current is not None:
            sections[current].append(raw_line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def list_items(text: str) -> list[str]:
    items: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif ORDERED_ITEM_RE.match(line):
            items.append(ORDERED_ITEM_RE.sub("", line).strip())
    return items


def resolve_path(target: str, current_dir: Path) -> Path | None:
    target = target.strip()
    if not target:
        return None
    if re.match(r"^[a-zA-Z]+://", target):
        return None
    path = Path(target)
    if target.startswith(PATHISH_PREFIXES):
        path = ROOT / target
    elif path.is_absolute():
        path = path
    else:
        path = current_dir / target

    if path.exists():
        return path
    if path.suffix == "":
        md_path = path.with_suffix(".md")
        if md_path.exists():
            return md_path
        index_path = path / "索引.md"
        if index_path.exists():
            return index_path
    return path if path.exists() else None


def path_to_uri(path: Path | None) -> str | None:
    return path.as_uri() if path and path.exists() else None


def parse_reference_item(raw: str, current_dir: Path) -> Reference:
    raw = raw.strip()
    wiki_match = re.fullmatch(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", raw)
    if wiki_match:
        target = wiki_match.group(1)
        label = wiki_match.group(2) or Path(target).name
        return Reference(raw=raw, label=label, uri=path_to_uri(resolve_path(target, current_dir)))
    md_match = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", raw)
    if md_match:
        label = md_match.group(1)
        target = md_match.group(2)
        if re.match(r"^[a-zA-Z]+://", target):
            return Reference(raw=raw, label=label, uri=target)
        return Reference(raw=raw, label=label, uri=path_to_uri(resolve_path(target, current_dir)))
    code_match = re.fullmatch(r"`([^`]+)`", raw)
    if code_match:
        target = code_match.group(1)
        return Reference(raw=raw, label=target, uri=path_to_uri(resolve_path(target, current_dir)))
    return Reference(raw=raw, label=raw, uri=path_to_uri(resolve_path(raw, current_dir)))


def inline_html(text: str, current_dir: Path) -> str:
    parts: list[str] = []
    cursor = 0
    for match in INLINE_RE.finditer(text):
        parts.append(escape(text[cursor:match.start()]))
        if match.group(1):
            target = match.group(1)
            label = match.group(2) or Path(target).name
            uri = path_to_uri(resolve_path(target, current_dir))
            if uri:
                parts.append(
                    f'<a href="{escape(uri, quote=True)}" class="inline-link">{escape(label)}</a>'
                )
            else:
                parts.append(escape(label))
        elif match.group(3):
            label = match.group(3)
            target = match.group(4)
            if re.match(r"^[a-zA-Z]+://", target):
                parts.append(
                    f'<a href="{escape(target, quote=True)}" class="inline-link">{escape(label)}</a>'
                )
            else:
                uri = path_to_uri(resolve_path(target, current_dir))
                if uri:
                    parts.append(
                        f'<a href="{escape(uri, quote=True)}" class="inline-link">{escape(label)}</a>'
                    )
                else:
                    parts.append(escape(label))
        elif match.group(5):
            code_text = match.group(5)
            uri = path_to_uri(resolve_path(code_text, current_dir))
            code_html = f"<code>{escape(code_text)}</code>"
            if uri and (code_text.startswith(PATHISH_PREFIXES) or "/" in code_text):
                parts.append(
                    f'<a href="{escape(uri, quote=True)}" class="code-link">{code_html}</a>'
                )
            else:
                parts.append(code_html)
        cursor = match.end()
    parts.append(escape(text[cursor:]))
    return "".join(parts)


def markdown_to_html(text: str, current_dir: Path) -> str:
    lines = text.strip().splitlines()
    html_parts: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("### "):
            html_parts.append(f"<h4>{inline_html(stripped[4:].strip(), current_dir)}</h4>")
            i += 1
            continue
        if stripped.startswith("#### "):
            html_parts.append(f"<h5>{inline_html(stripped[5:].strip(), current_dir)}</h5>")
            i += 1
            continue
        if stripped.startswith("- "):
            items: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:].strip())
                i += 1
            html_parts.append(
                "<ul>" + "".join(f"<li>{inline_html(item, current_dir)}</li>" for item in items) + "</ul>"
            )
            continue
        if ORDERED_ITEM_RE.match(stripped):
            items = []
            while i < len(lines) and ORDERED_ITEM_RE.match(lines[i].strip()):
                items.append(ORDERED_ITEM_RE.sub("", lines[i].strip()).strip())
                i += 1
            html_parts.append(
                "<ol>" + "".join(f"<li>{inline_html(item, current_dir)}</li>" for item in items) + "</ol>"
            )
            continue
        para_lines = [stripped]
        i += 1
        while i < len(lines):
            next_stripped = lines[i].strip()
            if not next_stripped or next_stripped.startswith(("### ", "#### ", "- ")) or ORDERED_ITEM_RE.match(next_stripped):
                break
            para_lines.append(next_stripped)
            i += 1
        html_parts.append(f"<p>{inline_html(' '.join(para_lines), current_dir)}</p>")
    return "\n".join(html_parts)


def first_paragraph(text: str) -> str:
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].startswith("### "):
            continue
        return " ".join(lines)
    return ""


def collect_entry(view: str, entry_dir: Path) -> dict:
    tabs = {}
    for tab_name in ("个人状态", "系统状态", "启发洞察"):
        file_path = entry_dir / f"{tab_name}.md"
        text = file_path.read_text(encoding="utf-8")
        frontmatter, body = strip_frontmatter(text)
        sections = parse_sections(body)
        tags = [item for item in list_items(sections.get("标签", "")) if item]
        references = [
            parse_reference_item(item, entry_dir)
            for item in list_items(sections.get("引用文章", ""))
        ]
        main_sections = []
        for name, content in sections.items():
            if name in SECTION_EXCLUDE or not content.strip():
                continue
            main_sections.append(
                {
                    "title": name,
                    "html": markdown_to_html(content, entry_dir),
                    "summary": first_paragraph(content),
                }
            )
        summary = next((section["summary"] for section in main_sections if section["summary"]), "")
        tabs[tab_name] = {
            "title": tab_name,
            "display_title": TAB_DISPLAY[tab_name],
            "path": rel(file_path),
            "uri": file_path.as_uri(),
            "frontmatter": frontmatter,
            "summary": summary,
            "sections": main_sections,
            "tags": tags,
            "references": [
                {"raw": ref.raw, "label": ref.label, "uri": ref.uri}
                for ref in references
            ],
            "reference_count": len(references),
        }
    return {
        "id": entry_dir.name,
        "label": entry_dir.name,
        "path": rel(entry_dir),
        "uri": entry_dir.as_uri(),
        "updated_at": datetime.fromtimestamp(
            max((entry_dir / "个人状态.md").stat().st_mtime,
                (entry_dir / "系统状态.md").stat().st_mtime,
                (entry_dir / "启发洞察.md").stat().st_mtime),
            tz=timezone.utc,
        ).isoformat(),
        "tabs": tabs,
    }


def find_entries(view: str) -> list[dict]:
    view_root = OBS_ROOT / view
    entries: list[dict] = []
    for person_file in view_root.rglob("个人状态.md"):
        if "_template" in person_file.parts:
            continue
        entry_dir = person_file.parent
        if all((entry_dir / name).exists() for name in ("个人状态.md", "系统状态.md", "启发洞察.md")):
            entries.append(collect_entry(view, entry_dir))
    entries.sort(key=lambda item: item["id"], reverse=True)
    return entries


def derive_generated_at(views: dict) -> str:
    timestamps: list[str] = []
    for data in views.values():
        for entry in data.get("entries", []):
            updated_at = entry.get("updated_at")
            if updated_at:
                timestamps.append(updated_at)
    return max(timestamps) if timestamps else datetime.now(timezone.utc).isoformat()


def build_snapshot() -> dict:
    views = {}
    for view, config in VIEW_CONFIG.items():
        entries = find_entries(view)
        if not entries:
            continue
        views[view] = {
            "label": config["label"],
            "default_tab": config["default_tab"],
            "display_path": config["display_path"],
            "current_period": entries[0]["id"],
            "current_uri": entries[0]["uri"],
            "entry_count": len(entries),
            "entries": entries[:16],
        }
    return {
        "generated_at": derive_generated_at(views),
        "workspace": str(ROOT),
        "html_path": rel(HTML_OUTPUT),
        "views": views,
    }


def render_html(snapshot: dict) -> str:
    data_json = json.dumps(snapshot, ensure_ascii=False)
    html_text = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>艾迪宇宙观测站</title>
  <style>
    :root {{
      --bg: #f6f1e7;
      --bg-strong: #efe4d1;
      --surface: rgba(255, 252, 247, 0.78);
      --surface-strong: rgba(255, 251, 243, 0.94);
      --ink: #172033;
      --muted: #5f6676;
      --accent: #d85d2f;
      --accent-alt: #0f6c6d;
      --line: rgba(23, 32, 51, 0.14);
      --shadow: 0 30px 80px rgba(23, 32, 51, 0.12);
      --radius-xl: 28px;
      --radius-lg: 18px;
      --radius-md: 12px;
      --font-display: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      --font-body: "Avenir Next", "Helvetica Neue", "PingFang SC", sans-serif;
      --font-mono: "SFMono-Regular", "JetBrains Mono", monospace;
    }}

    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      font-family: var(--font-body);
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(216, 93, 47, 0.18), transparent 36%),
        radial-gradient(circle at top right, rgba(15, 108, 109, 0.16), transparent 28%),
        linear-gradient(180deg, rgba(246, 241, 231, 0.9), rgba(241, 233, 220, 1));
      min-height: 100vh;
    }}

    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      background-image:
        linear-gradient(rgba(23, 32, 51, 0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(23, 32, 51, 0.03) 1px, transparent 1px);
      background-size: 26px 26px;
      pointer-events: none;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.3), rgba(0,0,0,0.8));
    }}

    a {{
      color: inherit;
      text-decoration: none;
    }}

    .page {{
      width: min(1380px, calc(100vw - 48px));
      margin: 0 auto;
      padding: 40px 0 56px;
      position: relative;
      z-index: 1;
    }}

    .fade-in {{
      opacity: 0;
      animation: fadeUp 0.72s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    }}

    .delay-1 {{ animation-delay: 0.06s; }}
    .delay-2 {{ animation-delay: 0.12s; }}
    .delay-3 {{ animation-delay: 0.18s; }}
    .delay-4 {{ animation-delay: 0.24s; }}

    @keyframes fadeUp {{
      from {{ opacity: 0; transform: translateY(18px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}

    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.6fr) minmax(280px, 0.9fr);
      gap: 20px;
      margin-bottom: 24px;
    }}

    .panel {{
      background: var(--surface);
      backdrop-filter: blur(18px);
      border: 1px solid var(--line);
      border-radius: var(--radius-xl);
      box-shadow: var(--shadow);
    }}

    .hero-main {{
      padding: 28px 30px 26px;
      position: relative;
      overflow: hidden;
    }}

    .hero-main::after {{
      content: "";
      position: absolute;
      inset: auto -30px -34px auto;
      width: 180px;
      height: 180px;
      background: radial-gradient(circle, rgba(216, 93, 47, 0.25), transparent 68%);
      pointer-events: none;
    }}

    .eyebrow {{
      display: inline-flex;
      gap: 8px;
      align-items: center;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.7);
      border: 1px solid rgba(23, 32, 51, 0.08);
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .hero h1 {{
      margin: 18px 0 12px;
      font-family: var(--font-display);
      font-size: clamp(40px, 6vw, 64px);
      line-height: 0.95;
      letter-spacing: -0.04em;
      font-weight: 700;
    }}

    .hero-copy {{
      margin: 0;
      max-width: 62ch;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.7;
    }}

    .hero-meta {{
      padding: 24px;
      display: grid;
      gap: 14px;
      align-content: start;
    }}

    .hero-stat {{
      padding: 16px 18px;
      border-radius: var(--radius-lg);
      background: var(--surface-strong);
      border: 1px solid rgba(23, 32, 51, 0.08);
    }}

    .hero-stat-label {{
      display: block;
      font-size: 11px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 8px;
    }}

    .hero-stat strong {{
      display: block;
      font-size: 24px;
      line-height: 1.15;
    }}

    .overview-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 22px;
    }}

    .overview-card {{
      padding: 18px 18px 16px;
      border-radius: var(--radius-lg);
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.72);
      min-height: 170px;
      transition: transform 180ms ease, border-color 180ms ease, box-shadow 180ms ease;
    }}

    .overview-card.active {{
      border-color: rgba(216, 93, 47, 0.36);
      box-shadow: 0 16px 36px rgba(216, 93, 47, 0.14);
      transform: translateY(-2px);
    }}

    .overview-label {{
      font-size: 11px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 10px;
    }}

    .overview-card h2 {{
      margin: 0 0 8px;
      font-size: 26px;
      line-height: 1.06;
      letter-spacing: -0.04em;
    }}

    .overview-card p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
      font-size: 14px;
    }}

    .layout {{
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      gap: 20px;
    }}

    .sidebar {{
      padding: 20px 18px;
      display: grid;
      gap: 18px;
      align-content: start;
      height: fit-content;
      position: sticky;
      top: 20px;
    }}

    .sidebar h3, .content-card h3 {{
      margin: 0 0 10px;
      font-size: 12px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 700;
    }}

    .toggle-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}

    .pill {{
      border: 1px solid rgba(23, 32, 51, 0.1);
      background: rgba(255,255,255,0.72);
      color: var(--ink);
      border-radius: 999px;
      padding: 10px 14px;
      font: inherit;
      font-size: 13px;
      cursor: pointer;
      transition: transform 160ms ease, background 160ms ease, border-color 160ms ease;
    }}

    .pill:hover {{
      transform: translateY(-1px);
      border-color: rgba(216, 93, 47, 0.22);
    }}

    .pill.active {{
      background: var(--ink);
      color: #fff8ef;
      border-color: var(--ink);
    }}

    .period-list {{
      display: grid;
      gap: 8px;
    }}

    .period-item {{
      padding: 14px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(23, 32, 51, 0.08);
      background: rgba(255,255,255,0.72);
      cursor: pointer;
      transition: transform 160ms ease, border-color 160ms ease, background 160ms ease;
    }}

    .period-item:hover {{
      transform: translateX(3px);
      border-color: rgba(15, 108, 109, 0.28);
    }}

    .period-item.active {{
      background: rgba(15, 108, 109, 0.08);
      border-color: rgba(15, 108, 109, 0.28);
    }}

    .period-item strong {{
      display: block;
      font-size: 16px;
      line-height: 1.2;
      margin-bottom: 6px;
    }}

    .period-item span {{
      color: var(--muted);
      font-size: 13px;
    }}

    .main {{
      display: grid;
      gap: 18px;
    }}

    .content-card {{
      padding: 22px 22px 26px;
    }}

    .content-head {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 14px;
      align-items: start;
      margin-bottom: 18px;
    }}

    .content-head h2 {{
      margin: 8px 0 0;
      font-size: clamp(26px, 4vw, 40px);
      line-height: 1.02;
      letter-spacing: -0.05em;
    }}

    .content-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}

    .meta-chip {{
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid rgba(23, 32, 51, 0.08);
      background: rgba(255,255,255,0.72);
      font-size: 12px;
      color: var(--muted);
    }}

    .section-card {{
      padding: 18px 18px 6px;
      border-radius: var(--radius-lg);
      border: 1px solid rgba(23, 32, 51, 0.08);
      background: rgba(255,255,255,0.74);
      margin-bottom: 14px;
    }}

    .section-card h4 {{
      margin: 0 0 10px;
      font-family: var(--font-display);
      font-size: 25px;
      letter-spacing: -0.03em;
    }}

    .section-card h5 {{
      margin: 18px 0 10px;
      font-size: 16px;
      letter-spacing: -0.02em;
    }}

    .section-card p,
    .section-card li {{
      color: var(--ink);
      line-height: 1.78;
      font-size: 15px;
    }}

    .section-card ul,
    .section-card ol {{
      margin: 0 0 14px 18px;
      padding: 0;
    }}

    .section-card p {{
      margin: 0 0 14px;
    }}

    .inline-link {{
      text-decoration: underline;
      text-decoration-thickness: 1px;
      text-underline-offset: 2px;
    }}

    .code-link code,
    code {{
      font-family: var(--font-mono);
      font-size: 0.92em;
      background: rgba(23, 32, 51, 0.06);
      padding: 0.18em 0.42em;
      border-radius: 6px;
    }}

    .chip-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}

    .tag-chip {{
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(216, 93, 47, 0.1);
      color: #8a3c1d;
      font-size: 12px;
    }}

    .ref-list {{
      display: grid;
      gap: 10px;
    }}

    .ref-item {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
      padding: 12px 0;
      border-top: 1px solid rgba(23, 32, 51, 0.08);
      font-size: 14px;
    }}

    .ref-item:first-child {{
      border-top: 0;
      padding-top: 0;
    }}

    .ref-item span {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}

    .empty {{
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
    }}

    @media (max-width: 1100px) {{
      .hero, .layout {{
        grid-template-columns: 1fr;
      }}

      .sidebar {{
        position: static;
      }}
    }}

    @media (max-width: 760px) {{
      .page {{
        width: min(100vw - 24px, 1380px);
        padding-top: 20px;
      }}

      .overview-grid {{
        grid-template-columns: 1fr;
      }}

      .hero-main,
      .hero-meta,
      .content-card,
      .sidebar {{
        padding: 18px;
      }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero fade-in">
      <article class="panel hero-main">
        <div class="eyebrow">观测站前台 · 后端仍以三 sibling files 为准</div>
        <h1>艾迪宇宙观测站</h1>
        <p class="hero-copy">
          这是当前仓库里真实存在的前台页面。它直接从 <code>daily / weekly / monthly</code> 的后台归档渲染，
          用来让每日迭代和每周迭代的增量、系统缺口和升格结果变得可见。
        </p>
      </article>
      <aside class="panel hero-meta fade-in delay-1" id="hero-meta"></aside>
    </section>

    <section class="overview-grid fade-in delay-2" id="overview-grid"></section>

    <section class="layout fade-in delay-3">
      <aside class="panel sidebar">
        <div>
          <h3>时间维度</h3>
          <div class="toggle-row" id="view-toggle"></div>
        </div>
        <div>
          <h3>周期列表</h3>
          <div class="period-list" id="period-list"></div>
        </div>
        <div>
          <h3>内容维度</h3>
          <div class="toggle-row" id="tab-toggle"></div>
        </div>
      </aside>

      <section class="main">
        <article class="panel content-card" id="content-card"></article>
        <article class="panel content-card">
          <h3>引用文章</h3>
          <div class="ref-list" id="reference-list"></div>
        </article>
      </section>
    </section>
  </main>

  <script id="observatory-data" type="application/json">__DATA_JSON__</script>
  <script>
    const snapshot = JSON.parse(document.getElementById("observatory-data").textContent);
    const views = snapshot.views;
    const viewNames = Object.keys(views);

    function parseHash() {{
      const cleaned = window.location.hash.replace(/^#/, "");
      const [view, period, tab] = cleaned.split("/");
      return {{ view, period, tab }};
    }}

    function defaultState() {{
      const daily = views.daily || views[viewNames[0]];
      return {{
        view: daily ? (views.daily ? "daily" : viewNames[0]) : "",
        period: daily ? daily.current_period : "",
        tab: daily ? daily.default_tab : "",
      }};
    }}

    function normalizeState(input) {{
      const base = defaultState();
      const view = views[input.view] ? input.view : base.view;
      const viewData = views[view];
      const period = viewData.entries.some((entry) => entry.id === input.period)
        ? input.period
        : viewData.current_period;
      const allowedTabs = Object.keys(viewData.entries[0].tabs);
      const tab = allowedTabs.includes(input.tab) ? input.tab : viewData.default_tab;
      return {{ view, period, tab }};
    }}

    function writeHash(state) {{
      const nextHash = `#${{state.view}}/${{encodeURIComponent(state.period)}}/${{encodeURIComponent(state.tab)}}`;
      if (window.location.hash !== nextHash) {{
        history.replaceState(null, "", nextHash);
      }}
    }}

    function entryFor(state) {{
      return views[state.view].entries.find((entry) => entry.id === state.period);
    }}

    function renderHeroMeta(state) {{
      const blocks = viewNames.map((view) => {{
        const data = views[view];
        return `
          <div class="hero-stat">
            <span class="hero-stat-label">${{data.label}}</span>
            <strong>${{data.current_period}}</strong>
            <span>${{data.entry_count}} 个周期归档</span>
          </div>
        `;
      }}).join("");
      document.getElementById("hero-meta").innerHTML = `
        ${blocks}
        <div class="hero-stat">
          <span class="hero-stat-label">生成时间</span>
          <strong>${{snapshot.generated_at.slice(0, 16).replace("T", " ")}}</strong>
          <span>当前页面由本地脚本重建</span>
        </div>
      `;
    }}

    function renderOverview(state) {{
      const grid = document.getElementById("overview-grid");
      grid.innerHTML = viewNames.map((view) => {{
        const data = views[view];
        const entry = data.entries[0];
        const summary = entry.tabs[data.default_tab].summary || "当前周期暂无摘要。";
        return `
          <button class="overview-card ${view === state.view ? "active" : ""}" data-view="${{view}}">
            <div class="overview-label">${{data.label}}</div>
            <h2>${{entry.id}}</h2>
            <p>${{summary}}</p>
          </button>
        `;
      }}).join("");
      grid.querySelectorAll("[data-view]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const next = normalizeState({{ view: button.dataset.view }});
          update(next);
        }});
      }});
    }}

    function renderViewToggle(state) {{
      const container = document.getElementById("view-toggle");
      container.innerHTML = viewNames.map((view) => `
        <button class="pill ${view === state.view ? "active" : ""}" data-view="${{view}}">
          ${{views[view].label}}
        </button>
      `).join("");
      container.querySelectorAll("[data-view]").forEach((button) => {{
        button.addEventListener("click", () => update(normalizeState({{ view: button.dataset.view }})));
      }});
    }}

    function renderPeriodList(state) {{
      const data = views[state.view];
      const container = document.getElementById("period-list");
      container.innerHTML = data.entries.map((entry) => {{
        const summary = entry.tabs[data.default_tab].summary || "暂无摘要";
        return `
          <button class="period-item ${entry.id === state.period ? "active" : ""}" data-period="${{entry.id}}">
            <strong>${{entry.id}}</strong>
            <span>${{summary}}</span>
          </button>
        `;
      }}).join("");
      container.querySelectorAll("[data-period]").forEach((button) => {{
        button.addEventListener("click", () => update(normalizeState({{ ...state, period: button.dataset.period }})));
      }});
    }}

    function renderTabToggle(state) {{
      const entry = entryFor(state);
      const container = document.getElementById("tab-toggle");
      container.innerHTML = Object.keys(entry.tabs).map((tab) => `
        <button class="pill ${tab === state.tab ? "active" : ""}" data-tab="${{tab}}">
          ${{entry.tabs[tab].display_title}}
        </button>
      `).join("");
      container.querySelectorAll("[data-tab]").forEach((button) => {{
        button.addEventListener("click", () => update(normalizeState({{ ...state, tab: button.dataset.tab }})));
      }});
    }}

    function renderContent(state) {{
      const data = views[state.view];
      const entry = entryFor(state);
      const tab = entry.tabs[state.tab];
      const contentCard = document.getElementById("content-card");
      const sectionHtml = tab.sections.length
        ? tab.sections.map((section) => `
            <section class="section-card">
              <h4>${{section.title}}</h4>
              ${{section.html}}
            </section>
          `).join("")
        : `<p class="empty">当前 tab 暂无内容。</p>`;

      const tags = tab.tags.length
        ? `<div class="chip-list">${{tab.tags.map((tag) => `<span class="tag-chip">${{tag}}</span>`).join("")}}</div>`
        : `<p class="empty">暂无标签。</p>`;

      contentCard.innerHTML = `
        <div class="content-head">
          <div>
            <div class="eyebrow">${{data.label}} · ${{entry.id}}</div>
            <h2>${{tab.display_title}}</h2>
          </div>
          <div class="content-meta">
            <a class="meta-chip" href="${{tab.uri}}">打开后台文件</a>
            <a class="meta-chip" href="${{entry.uri}}">打开周期目录</a>
            <span class="meta-chip">${{tab.reference_count}} 条引用</span>
          </div>
        </div>
        ${sectionHtml}
        <section class="section-card">
          <h4>标签</h4>
          ${tags}
        </section>
      `;
    }}

    function renderReferences(state) {{
      const tab = entryFor(state).tabs[state.tab];
      const container = document.getElementById("reference-list");
      if (!tab.references.length) {{
        container.innerHTML = `<p class="empty">当前 tab 暂无引用文章。</p>`;
        return;
      }}
      container.innerHTML = tab.references.map((ref) => {{
        const label = ref.uri
          ? `<a href="${{ref.uri}}">${{ref.label}}</a>`
          : `<span>${{ref.label}}</span>`;
        return `
          <div class="ref-item">
            ${label}
            <span>${{ref.uri ? "可打开" : "仅文本"}}</span>
          </div>
        `;
      }}).join("");
    }}

    function update(nextState) {{
      const state = normalizeState(nextState);
      writeHash(state);
      renderHeroMeta(state);
      renderOverview(state);
      renderViewToggle(state);
      renderPeriodList(state);
      renderTabToggle(state);
      renderContent(state);
      renderReferences(state);
    }}

    const initial = normalizeState(parseHash());
    update(initial);
    window.addEventListener("hashchange", () => update(normalizeState(parseHash())));
  </script>
</body>
</html>
"""
    html_text = html_text.replace("__DATA_JSON__", escape(data_json))
    return html_text.replace("{{", "{").replace("}}", "}")


def stable_write_text(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def write_outputs(snapshot: dict) -> dict[str, bool]:
    json_text = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    html_text = render_html(snapshot)
    return {
        "json_changed": stable_write_text(JSON_OUTPUT, json_text),
        "html_changed": stable_write_text(HTML_OUTPUT, html_text),
    }


def main() -> int:
    snapshot = build_snapshot()
    changed = write_outputs(snapshot)
    print(json.dumps({
        "html": rel(HTML_OUTPUT),
        "json": rel(JSON_OUTPUT),
        "views": {name: info["current_period"] for name, info in snapshot["views"].items()},
        "changed": changed,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
