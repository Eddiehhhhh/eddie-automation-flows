#!/usr/bin/env python3
"""Write daily/weekly observatory closeout files from iteration evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import build_observatory_frontend
import iteration_incremental_audit
import iteration_source_discovery
import workspace_intake_report


ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[1]))
RAW_DIR = ROOT / "Raw"
WIKI_DIR = ROOT / "Wiki"
OBS_ROOT = WIKI_DIR / "10 观测站" / "艾迪宇宙观测站"
META_DIR = RAW_DIR / "00 Meta"
CHAT_DIR = RAW_DIR / "05 Chat"
SHANGHAI = timezone(timedelta(hours=8))
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
DATE_RE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
TITLE_LINE_RE = re.compile(r"^#\s+(.+)$", re.M)
NON_WORD_RE = re.compile(r"[\s`#*_\-\|:：，。、“”‘’（）()【】\[\]<>《》!！?？/]+")
LATEST_CLOSEOUT = {
    "daily": META_DIR / "iteration-daily-closeout-latest.json",
    "weekly": META_DIR / "iteration-weekly-closeout-latest.json",
}
TAG_BY_MODE = {"daily": "#daily", "weekly": "#weekly"}
AUTOPROMOTION_SECTION = "迭代升格记录"


@dataclass
class Entry:
    path: Path
    rel_path: str
    title: str
    body: str
    frontmatter: dict[str, str]
    evidence_date: date | None
    note_type: str | None
    source: str


@dataclass
class InsightTheme:
    key: str
    title: str
    seed_line: str
    judgment: str
    aha: str
    lens_title: str
    lens_text: str
    action_title: str
    action_text: str


THEME_RULES: dict[str, dict[str, Any]] = {
    "self_worth": {
        "keywords": ["容貌", "自卑", "焦虑", "固定型思维", "成长型思维", "审美"],
        "title": "自我评价的硬约束感",
        "seed": "当日材料把注意力从“我还可以练什么”推向“我以为自己改不了什么”。",
        "judgment": "这和最近几天偏求职叙事或 AI 系统化的主题不同，它更像在追问：当焦虑落在被感知为不可改变量上时，努力为什么会突然失效。",
        "aha": "一旦问题核心不是能力，而是自我价值的比较坐标，单纯加任务或加系统都不会真正止痛。",
        "lens_title": "Carol Dweck 的固定型/成长型思维",
        "lens": "固定型思维并不只是“相信能力天生固定”，它还会让人把某个维度当成自我价值的硬边界。把边界看得越硬，行动越容易从训练转成回避。",
        "action_title": "列一张可训练 / 不可训练对照卡",
        "action": "用 45 分钟写两列：`我能训练的` 和 `我现在误以为只能承受的`。每列只写 5 条，把容貌、表达、能力、关系感受拆开，避免它们继续混成一个总评价。",
    },
    "indie_product": {
        "keywords": ["独立开发", "App", "盈利", "定价", "定位", "用户", "产品", "付费"],
        "title": "产品价值开始和现实定价绑在一起",
        "seed": "外部材料不再只是讲“做出产品”，而是在讲定位、定价、传播和低频需求如何真实成立。",
        "judgment": "这让外部输入从灵感收藏切成了经营判断。你看的不是励志故事，而是在筛：什么样的产品能靠清晰定位和传播钩子撑住价值。",
        "aha": "当外部材料开始反复讨论定价、壁垒和传播，你关心的已经不是功能新鲜感，而是结构性可持续。",
        "lens_title": "产品定位与价格信号",
        "lens": "价格本身就是定位信号。对低频工具来说，定价、分发和用户心智往往比“再多一个功能”更能决定产品是否成立。",
        "action_title": "拆一页定位信号卡",
        "action": "用 60 分钟把今天命中的独立开发材料拆成一页：`用户是谁 / 价值主张 / 价格信号 / 分发入口 / 不做什么`。只留每栏 1 句最硬判断。",
    },
    "ai_system": {
        "keywords": ["AI", "agent", "skills", "Codex", "Hermes", "工作流", "系统", "Obsidian", "Notion"],
        "title": "AI 材料继续往协作后台收束",
        "seed": "当日新增不是单点工具技巧，而是继续逼近“AI 如何成为长期后台”的问题。",
        "judgment": "如果同主题最近几天已经出现，本轮只有在来源更外部、问题更经营化或更接近真实 workflow 时才值得继续保留。",
        "aha": "真正的新意不在“又多了一个 AI 工具”，而在“这个工具怎样进入你现有的协作边界”。",
        "lens_title": "分布式认知与接口成本",
        "lens": "一个系统越想长期有效，越要把认知负荷外包给稳定接口。判断价值时，接口成本往往比单次生成质量更关键。",
        "action_title": "做一页接口边界图",
        "action": "用 45 分钟画出 `输入源 / 处理层 / 稳定层 / 展示层` 四格，只填今天新增材料真正改变了哪一格，避免把所有问题都丢给“AI 不够聪明”。",
    },
    "job_transition": {
        "keywords": ["求职", "离职", "转岗", "面试", "裁员", "机会", "last day", "冷静期"],
        "title": "过渡期的问题开始从勇气转向排序",
        "seed": "一旦同一窗口里同时出现求职、转岗、机会和生活安排，难点就不再是“有没有选项”，而是“按什么裁掉选项”。",
        "judgment": "这和前几天反复讲“求职叙事”不同，新的地方在于它更强调排序器，而不是继续扩叙事素材。",
        "aha": "机会密度一高，缺的通常不是决心，而是一个能让人停止继续收集的标准。",
        "lens_title": "选择过载",
        "lens": "行为决策研究反复证明，选项越多不一定越自由，反而会放大比较成本、后悔预期和启动拖延。排序器是减压工具，不只是效率工具。",
        "action_title": "做一张机会排序卡",
        "action": "用 45 分钟把当前方向压成四列：`现金流 / 秩序感 / 可迁移能力 / 人脉势能`，只给真实出现的选项打分，最后强制选一个主攻方向。",
    },
    "community_place": {
        "keywords": ["濠联", "文旅", "地方", "社区", "走读", "潮汕", "南头", "城市"],
        "title": "地方实践继续把你拉回长期场域",
        "seed": "当地方、走读和社区材料再次出现时，它提醒的是长期场域，而不是一次性灵感。",
        "judgment": "这类材料如果和最近几天重复，只有在它能改变你对主业、副业或长期角色的判断时才保留。",
        "aha": "真正的增量不在“我还喜欢这件事”，而在“它今天又怎样参与了我的现实排序”。",
        "lens_title": "地方依附与角色场域",
        "lens": "地方感不是怀旧，它常常决定一个人愿意把长期时间投到哪里。稳定场域能给高不确定期提供持续的身份锚点。",
        "action_title": "写一张长期场域卡",
        "action": "用 45 分钟写 1 张卡，回答 `如果文旅不是副线，它需要什么现实支架才成立`，只写资源、时间和现金流三项。",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--target-date")
    parser.add_argument("--window-days", type=int)
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--write-latest", action="store_true")
    parser.add_argument("--state-output")
    return parser.parse_args()


def shanghai_today() -> date:
    return datetime.now(SHANGHAI).date()


def default_daily_target() -> date:
    return shanghai_today() - timedelta(days=1)


def previous_natural_week(today: date) -> tuple[date, date, str]:
    current_week_start = today - timedelta(days=today.weekday())
    prev_week_start = current_week_start - timedelta(days=7)
    prev_week_end = current_week_start - timedelta(days=1)
    iso_year, iso_week, _ = prev_week_start.isocalendar()
    return prev_week_start, prev_week_end, f"{iso_year}-W{iso_week:02d}"


def normalize_target(mode: str, target_date_raw: str | None, window_days: int | None) -> tuple[date, int, dict[str, str]]:
    if mode == "daily":
        target = date.fromisoformat(target_date_raw) if target_date_raw else default_daily_target()
        window = window_days or 3
        return target, window, {
            "label": target.isoformat(),
            "target_date": target.isoformat(),
            "window_start": (target - timedelta(days=max(window - 1, 0))).isoformat(),
            "window_end": target.isoformat(),
        }

    if target_date_raw:
        week_end = date.fromisoformat(target_date_raw)
        week_start = week_end - timedelta(days=6)
        iso_year, iso_week, _ = week_start.isocalendar()
        label = f"{iso_year}-W{iso_week:02d}"
    else:
        week_start, week_end, label = previous_natural_week(shanghai_today())
    window = window_days or 7
    return week_end, window, {
        "label": label,
        "target_date": week_end.isoformat(),
        "window_start": week_start.isoformat(),
        "window_end": week_end.isoformat(),
    }


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_volatile(item)
            for key, item in value.items()
            if key not in {"generated_at", "updated_at", "last_run_at"}
        }
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


def stable_write(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def stable_write_json(path: Path, data: dict[str, Any]) -> bool:
    existing = load_json(path)
    if existing is not None and strip_volatile(existing) == strip_volatile(data):
        return False
    return stable_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def replace_section(text: str, section_title: str, section_body: str, insert_before: str = "## 来源") -> str:
    heading = f"## {section_title}"
    replacement = f"{heading}\n\n{section_body.strip()}\n\n"
    if heading in text:
        start = text.index(heading)
        next_heading = text.find("\n## ", start + len(heading))
        end = next_heading + 1 if next_heading != -1 else len(text)
        return text[:start] + replacement + text[end:]
    anchor = text.find(insert_before)
    if anchor != -1:
        return text[:anchor] + replacement + text[anchor:]
    suffix = "" if text.endswith("\n") else "\n"
    return text + suffix + replacement


def parse_managed_blocks(section_body: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in section_body.splitlines():
        line = raw_line.rstrip()
        if line.startswith("- "):
            if current:
                blocks.append(current)
            current = [line]
            continue
        if current and line.startswith("  - "):
            current.append(line)
            continue
        if current and line.strip():
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def upsert_managed_period_block(
    path: Path,
    period_label: str,
    block_lines: list[str],
    refs: list[str],
) -> bool:
    text = path.read_text(encoding="utf-8")
    heading = f"## {AUTOPROMOTION_SECTION}"
    existing_blocks: list[list[str]] = []
    section_start = text.find(heading)
    if section_start != -1:
        next_heading = text.find("\n## ", section_start + len(heading))
        section_end = next_heading + 1 if next_heading != -1 else len(text)
        section_body = text[section_start + len(heading):section_end].strip()
        existing_blocks = parse_managed_blocks(section_body)
    merged_blocks = [block_lines]
    for block in existing_blocks:
        if block and block[0].startswith(f"- `{period_label}`："):
            continue
        if block:
            merged_blocks.append(block)
    merged_blocks = merged_blocks[:5]
    managed_section = "\n\n".join("\n".join(block) for block in merged_blocks)
    updated = replace_section(text, AUTOPROMOTION_SECTION, managed_section)
    if "## 来源" in updated:
        source_anchor = updated.index("## 来源")
        source_text = updated[source_anchor:]
        missing_refs = [ref for ref in refs if ref not in source_text]
        if missing_refs:
            updated = updated.rstrip() + "\n" + "\n".join(f"- {ref}" for ref in missing_refs) + "\n"
    return stable_write(path, updated)


def read_text(path: Path, limit: int = 20000) -> str:
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        return fh.read(limit)


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def body_without_frontmatter(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return text[match.end():] if match else text


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def extract_title(path: Path, fields: dict[str, str], body: str) -> str:
    if fields.get("title"):
        return fields["title"]
    match = TITLE_LINE_RE.search(body)
    if match:
        return match.group(1).strip()
    return path.stem


def extract_note_type(fields: dict[str, str]) -> str | None:
    return fields.get("note_type") or fields.get("type") or fields.get("kind")


def pick_evidence_date(path: Path, fields: dict[str, str], source: str) -> date | None:
    for key in ("created_at", "updated_at", "date", "收藏时间"):
        found = parse_date(fields.get(key))
        if found:
            return found
    if source == "notion":
        for key in ("created", "updated", "关联日记"):
            found = parse_date(fields.get(key))
            if found:
                return found
    found = parse_date(path.as_posix())
    if found:
        return found
    return None


def iter_markdown_files(base: Path) -> Iterable[Path]:
    if not base.exists():
        return []
    return (path for path in base.rglob("*.md") if path.is_file())


def entry_from_path(path: Path, source: str) -> Entry:
    text = read_text(path)
    fields = parse_frontmatter(text)
    body = body_without_frontmatter(text)
    return Entry(
        path=path,
        rel_path=rel(path),
        title=extract_title(path, fields, body),
        body=body,
        frontmatter=fields,
        evidence_date=pick_evidence_date(path, fields, source),
        note_type=extract_note_type(fields),
        source=source,
    )


def source_root_for_cubox() -> tuple[Path | None, str]:
    cubox = RAW_DIR / "10 Cubox"
    if cubox.exists():
        return cubox, "Cubox"
    xinzhi = RAW_DIR / "10 新枝"
    if xinzhi.exists():
        return xinzhi, "新枝-fallback"
    return None, "missing"


def entries_in_range(base: Path | None, source: str, start: date, end: date) -> list[Entry]:
    if base is None or not base.exists():
        return []
    items: list[Entry] = []
    for path in iter_markdown_files(base):
        entry = entry_from_path(path, source)
        if entry.evidence_date and start <= entry.evidence_date <= end:
            items.append(entry)
    items.sort(key=lambda item: (item.evidence_date or date.min, item.rel_path))
    return items


def get_recent_entries(base: Path | None, source: str, days: int = 7, limit: int = 30) -> list[Entry]:
    if base is None or not base.exists():
        return []
    end = shanghai_today()
    start = end - timedelta(days=max(days - 1, 0))
    items = entries_in_range(base, source, start, end)
    return items[-limit:]


def find_get_daily_summary(target: date, lookback_days: int = 7) -> tuple[Entry | None, int | None]:
    get_base = RAW_DIR / "03 Get"
    candidates: list[tuple[int, int, Entry]] = []
    for path in iter_markdown_files(get_base):
        entry = entry_from_path(path, "get")
        if not entry.evidence_date:
            continue
        age = (target - entry.evidence_date).days
        if 0 <= age <= lookback_days:
            score = get_daily_summary_score(entry)
            if score <= 0:
                continue
            candidates.append((age, -score, entry))
    if not candidates:
        return None, None
    age, _, entry = sorted(candidates, key=lambda item: (item[0], item[1], item[2].rel_path))[0]
    return entry, age


def get_daily_summary_score(entry: Entry) -> int:
    title = entry.title
    tags = str(entry.frontmatter.get("tags", ""))
    body = entry.body
    score = 0
    if "每日总结" in title:
        score += 8
    if "每日总结" in tags:
        score += 8
    for keyword in ("今日状态", "今日评分", "今日主要事项", "情绪", "睡眠", "能量", "身体"):
        if keyword in body:
            score += 2
    if any(token in title for token in ("总结", "状态", "情绪", "日常记录")):
        score += 2
    if entry.note_type == "audio":
        score += 1
    return score


def parse_get_summary_fields(entry: Entry | None) -> dict[str, str]:
    if not entry:
        return {}
    text = entry.body
    fields: dict[str, str] = {}
    patterns = {
        "score": [r"今日评分[:：]\s*([^\n。；;]+)", r"打个([0-9一二三四五六七八九十两]+分)"],
        "mood": [r"(?:心情|状态)[:：]\s*([^\n。；;]+)"],
        "sleep": [r"睡眠[:：]\s*([^\n。；;]+)"],
        "energy": [r"能量[:：]\s*([^\n。；;]+)"],
        "body": [r"身体[:：]\s*([^\n。；;]+)"],
    }
    for key, pattern_list in patterns.items():
        for pattern in pattern_list:
            match = re.search(pattern, text)
            if match:
                fields[key] = match.group(1).strip(" -，,。")
                break
    action_match = re.search(r"今日主要事项[:：]\s*(.+?)(?:\n## |\Z)", text, re.S)
    if action_match:
        action = " ".join(line.strip("- ").strip() for line in action_match.group(1).splitlines() if line.strip())
        if action:
            fields["action_summary"] = action[:180]
    if "action_summary" not in fields:
        summary_match = re.search(r"## 🧠 摘要\s*(.+?)(?:\n## |\Z)", text, re.S)
        if summary_match:
            snippet = " ".join(line.strip() for line in summary_match.group(1).splitlines() if line.strip())
            if snippet:
                fields["action_summary"] = snippet[:180]
    return fields


def normalize_topic_tokens(text: str) -> list[str]:
    cleaned = NON_WORD_RE.sub(" ", text.lower())
    return [token for token in cleaned.split() if len(token) > 1]


def detect_themes(text: str) -> set[str]:
    matched: set[str] = set()
    lowered = text.lower()
    for key, config in THEME_RULES.items():
        for keyword in config["keywords"]:
            lowered_keyword = keyword.lower()
            if keyword.isascii():
                pattern = rf"(?<![a-z0-9]){re.escape(lowered_keyword)}(?![a-z0-9])"
                if re.search(pattern, lowered):
                    matched.add(key)
                    break
            elif lowered_keyword in lowered:
                matched.add(key)
                break
    return matched


def summarize_topics(entries: list[Entry], limit: int = 5) -> list[str]:
    counter: Counter[str] = Counter()
    for entry in entries:
        for theme in detect_themes(f"{entry.title}\n{entry.body[:1000]}"):
            counter[theme] += 1
    result: list[str] = []
    for theme, _ in counter.most_common(limit):
        result.append(THEME_RULES[theme]["title"])
    if result:
        return result
    token_counter: Counter[str] = Counter()
    for entry in entries:
        for token in normalize_topic_tokens(entry.title):
            if len(token) >= 2:
                token_counter[token] += 1
    return [token for token, _ in token_counter.most_common(limit)]


def latest_daily_insight_paths(limit: int = 3, before_target: date | None = None) -> list[Path]:
    daily_root = OBS_ROOT / "daily"
    paths: list[tuple[date, Path]] = []
    for path in daily_root.rglob("启发洞察.md"):
        found = parse_date(path.as_posix())
        if not found:
            continue
        if before_target and found >= before_target:
            continue
        paths.append((found, path))
    return [path for _, path in sorted(paths, key=lambda item: item[0], reverse=True)[:limit]]


def latest_weekly_insight_paths(limit: int = 2, before_target: date | None = None) -> list[Path]:
    weekly_root = OBS_ROOT / "weekly"
    paths: list[tuple[date, Path]] = []
    for path in weekly_root.rglob("启发洞察.md"):
        parts = path.as_posix().split("/")
        week_label = next((part for part in parts if re.fullmatch(r"20\d{2}-W\d{2}", part)), None)
        if not week_label:
            continue
        year, week = week_label.split("-W")
        found = date.fromisocalendar(int(year), int(week), 7)
        if before_target and found >= before_target:
            continue
        paths.append((found, path))
    return [path for _, path in sorted(paths, key=lambda item: item[0], reverse=True)[:limit]]


def load_recent_theme_history(mode: str, target: date) -> Counter[str]:
    counter: Counter[str] = Counter()
    insight_paths = latest_daily_insight_paths(before_target=target)
    if mode == "weekly":
        insight_paths += latest_weekly_insight_paths(before_target=target)
    for path in insight_paths:
        text = read_text(path, 30000)
        for theme in detect_themes(text):
            counter[theme] += 1
    return counter


def render_frontmatter(fields: dict[str, str]) -> str:
    return "---\n" + "\n".join(f"{key}: {value}" for key, value in fields.items()) + "\n---\n\n"


def build_reference_section(mode: str, tab: str, extra_refs: list[str]) -> list[str]:
    base_refs: list[str]
    if tab == "个人状态":
        base_refs = ["[[系统状态]]", "[[启发洞察]]", "[[Wiki/10 观测站/艾迪宇宙观测站/索引]]"]
    elif tab == "系统状态":
        base_refs = ["[[个人状态]]", "[[启发洞察]]", "[[Wiki/10 观测站/艾迪宇宙观测站/索引]]"]
    else:
        base_refs = ["[[个人状态]]", "[[系统状态]]", "[[Wiki/10 观测站/艾迪宇宙观测站/索引]]"]
    if mode == "weekly":
        base_refs.append("[[Wiki/10 观测站/艾迪宇宙观测站/weekly/索引]]")
    deduped: list[str] = []
    for ref in base_refs + extra_refs:
        if ref not in deduped:
            deduped.append(ref)
    return deduped


def quote_path(path: str) -> str:
    return f"`{path}`"


def topic_line(entries: list[Entry], limit: int = 4) -> str:
    topics = summarize_topics(entries, limit=limit)
    return "、".join(topics) if topics else "暂无稳定主题归纳"


def count_by_theme(entries: list[Entry]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for entry in entries:
        for theme in detect_themes(f"{entry.title}\n{entry.body[:1200]}"):
            counter[theme] += 1
    return counter


def theme_source_support(exact_entries: dict[str, list[Entry]]) -> dict[str, set[str]]:
    support: dict[str, set[str]] = defaultdict(set)
    for source, entries in exact_entries.items():
        for theme in count_by_theme(entries):
            support[theme].add(source)
    return support


def theme_exact_support(exact_entries: dict[str, list[Entry]]) -> dict[str, int]:
    support: Counter[str] = Counter()
    for entries in exact_entries.values():
        for theme, count in count_by_theme(entries).items():
            support[theme] += count
    return dict(support)


def select_insight_themes(
    exact_entries: dict[str, list[Entry]],
    recent_history: Counter[str],
    limit: int = 3,
) -> list[InsightTheme]:
    weighted: Counter[str] = Counter()
    exact_counts: Counter[str] = Counter()
    source_support = theme_source_support(exact_entries)
    for source, entries in exact_entries.items():
        theme_counts = count_by_theme(entries)
        multiplier = {"get": 4, "flomo": 3, "cubox": 2, "notion": 2}.get(source, 1)
        for theme, count in theme_counts.items():
            weighted[theme] += count * multiplier
            exact_counts[theme] += count

    selected: list[InsightTheme] = []
    for theme, _ in weighted.most_common():
        if len(selected) >= limit:
            break
        if recent_history.get(theme, 0) >= 1 and exact_counts.get(theme, 0) == 0:
            continue
        if recent_history.get(theme, 0) >= 2 and weighted[theme] < 5:
            continue
        if recent_history.get(theme, 0) >= 1 and len(source_support.get(theme, set())) < 2 and weighted[theme] < 5:
            continue
        if len(selected) >= 2 and exact_counts.get(theme, 0) < 2 and len(source_support.get(theme, set())) < 2:
            continue
        config = THEME_RULES[theme]
        selected.append(
            InsightTheme(
                key=theme,
                title=config["title"],
                seed_line=config["seed"],
                judgment=config["judgment"],
                aha=config["aha"],
                lens_title=config["lens_title"],
                lens_text=config["lens"],
                action_title=config["action_title"],
                action_text=config["action"],
            )
        )
    return selected


def gather_seed_titles(entries: list[Entry], theme_key: str, limit: int = 3) -> list[str]:
    titles: list[str] = []
    for entry in entries:
        if theme_key in detect_themes(f"{entry.title}\n{entry.body[:1200]}"):
            titles.append(entry.title)
    return titles[:limit]


def gather_seed_paths(entries: list[Entry], theme_key: str, limit: int = 3) -> list[str]:
    paths: list[str] = []
    for entry in entries:
        if theme_key in detect_themes(f"{entry.title}\n{entry.body[:1200]}"):
            paths.append(entry.rel_path)
    return paths[:limit]


def build_known_context_lines() -> list[str]:
    lines: list[str] = []
    life_text = read_text(WIKI_DIR / "02 Life" / "近期状态.md", 8000)
    work_text = read_text(WIKI_DIR / "03 Work" / "AI协作与知识系统演进.md", 8000)
    for text in (life_text, work_text):
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("- "):
                lines.append(line[2:].strip())
                break
        if len(lines) >= 2:
            break
    return lines[:2]


def build_workspace_summary() -> tuple[str, dict[str, Any]]:
    items = workspace_intake_report.run_git_status()
    report, data = workspace_intake_report.build_report(items)
    workspace_intake_report.write_if_changed(workspace_intake_report.REPORT_PATH, report + "\n")
    workspace_intake_report.write_if_changed(
        workspace_intake_report.JSON_PATH,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )
    risk_counts = data.get("risk_counts", {})
    high = risk_counts.get("高", 0)
    medium = risk_counts.get("中", 0)
    raw_group = data.get("groups", {}).get("Raw 来源层", {})
    return (
        f"工作区当前共有 {data.get('total', 0)} 项变更，高风险 {high} 项，中风险 {medium} 项；主要仍集中在 Raw 来源层 {raw_group.get('count', 0)} 项。",
        data,
    )


def build_source_snapshot(
    target: date,
    start: date,
    end: date,
    mode: str,
) -> dict[str, Any]:
    flomo_window_entries = entries_in_range(RAW_DIR / "01 Flomo", "flomo", start, end)
    notion_window_entries = entries_in_range(RAW_DIR / "02 Notion", "notion", start, end)
    get_window_entries = entries_in_range(RAW_DIR / "03 Get", "get", start, end)
    cubox_root, cubox_mode = source_root_for_cubox()
    cubox_window_entries = entries_in_range(cubox_root, "cubox", start, end)
    cubox_recent = get_recent_entries(cubox_root, "cubox", days=7)
    get_summary, get_summary_age = find_get_daily_summary(target)
    exact_start = exact_end = target
    return {
        "flomo": {
            "entries": entries_in_range(RAW_DIR / "01 Flomo", "flomo", exact_start, exact_end),
            "window_entries": flomo_window_entries,
        },
        "notion": {
            "entries": entries_in_range(RAW_DIR / "02 Notion", "notion", exact_start, exact_end),
            "window_entries": notion_window_entries,
        },
        "get": {
            "entries": entries_in_range(RAW_DIR / "03 Get", "get", exact_start, exact_end),
            "window_entries": get_window_entries,
            "daily_summary": get_summary,
            "daily_summary_age": get_summary_age,
            "summary_fields": parse_get_summary_fields(get_summary),
        },
        "cubox": {
            "entries": entries_in_range(cubox_root, "cubox", exact_start, exact_end),
            "window_entries": cubox_window_entries,
            "recent_entries": cubox_recent,
            "mode": cubox_mode,
            "root": rel(cubox_root) if cubox_root else None,
        },
        "mode": mode,
    }


def build_personal_status(
    mode: str,
    period: dict[str, str],
    sources: dict[str, Any],
    insights: list[InsightTheme],
) -> tuple[str, list[str]]:
    target_label = period["label"]
    get_daily: Entry | None = sources["get"]["daily_summary"]
    get_age = sources["get"]["daily_summary_age"]
    get_fields = sources["get"]["summary_fields"]
    flomo_entries: list[Entry] = sources["flomo"]["entries"]
    cubox_entries: list[Entry] = sources["cubox"]["entries"]
    notion_entries: list[Entry] = sources["notion"]["entries"]
    get_entries: list[Entry] = sources["get"]["entries"]
    flomo_window_entries: list[Entry] = sources["flomo"]["window_entries"]
    get_window_entries: list[Entry] = sources["get"]["window_entries"]
    cubox_window_entries: list[Entry] = sources["cubox"]["window_entries"]
    refs: list[str] = []

    intro_lines: list[str] = []
    if get_daily and get_age == 0:
        score = get_fields.get("score", "未抽出")
        mood = get_fields.get("mood", "未抽出")
        sleep = get_fields.get("sleep", "未抽出")
        energy = get_fields.get("energy", "未抽出")
        body = get_fields.get("body", "未抽出")
        intro_lines.append(
            f"{target_label} 命中了同日 `Get笔记` 每日总结 bundle，可用字段为：评分 `{score}`、心情 `{mood}`、睡眠 `{sleep}`、能量 `{energy}`、身体 `{body}`。"
        )
        if get_fields.get("action_summary"):
            intro_lines.append(f"行动摘要显示：{get_fields['action_summary']}")
        refs.append(quote_path(get_daily.rel_path))
    else:
        intro_lines.append(
            f"{target_label} 没抓到同日 `Get笔记` 每日总结 bundle，所以首页量化字段仍不能精确填值。"
        )
        if get_daily and get_age is not None:
            intro_lines.append(
                f"最近可核对的同结构 summary 距离目标日 {get_age} 天，来源是 `{get_daily.title}`。"
            )
            refs.append(quote_path(get_daily.rel_path))

    if insights:
        intro_lines.append(
            f"本轮更像一次主题收束：高信号材料把重心推向“{insights[0].title}”，而不是继续重复前几天的求职或 AI 系统老结论。"
        )

    fixed_source_lines = [
        f"- `Flomo`：同日命中 `{len(flomo_entries)}` 条，主题集中在 {topic_line(flomo_entries)}。"
        if flomo_entries
        else f"- `Flomo`：同日未命中 memo；回看窗口共 `{len(flomo_window_entries)}` 条，主题集中在 {topic_line(flomo_window_entries)}。"
    ]
    if flomo_entries:
        refs.extend(quote_path(entry.rel_path) for entry in flomo_entries[:3])
    elif flomo_window_entries:
        refs.extend(quote_path(entry.rel_path) for entry in flomo_window_entries[:2])

    get_topic = topic_line(get_entries or get_window_entries)
    if get_entries:
        fixed_source_lines.append(f"- `Get笔记`：同日命中 `{len(get_entries)}` 条真实笔记；主题以 {get_topic} 为主。")
        refs.extend(quote_path(entry.rel_path) for entry in get_entries[:3])
    else:
        fixed_source_lines.append(
            f"- `Get笔记`：同日未命中正文笔记；回看窗口共 `{len(get_window_entries)}` 条，可作为背景，但不替代 target-date 主轴。"
        )
        refs.extend(quote_path(entry.rel_path) for entry in get_window_entries[:2])

    if notion_entries:
        fixed_source_lines.append(
            f"- `Notion`：同日命中 `{len(notion_entries)}` 条结构化记录，可作为支持证据，不作为首页主导来源。"
        )
        refs.extend(quote_path(entry.rel_path) for entry in notion_entries[:2])
    else:
        fixed_source_lines.append("- `Notion`：没有命中同日结构化生活 bundle。")

    cubox_root = sources["cubox"]["root"]
    cubox_mode = sources["cubox"]["mode"]
    cubox_recent = sources["cubox"]["recent_entries"]
    if cubox_entries:
        fixed_source_lines.append(
            f"- `Cubox`：当前通过 `{cubox_root}` 读取，同日命中 `{len(cubox_entries)}` 条外部材料，主题为 {topic_line(cubox_entries)}。"
        )
        refs.extend(quote_path(entry.rel_path) for entry in cubox_entries[:3])
    elif cubox_recent:
        fixed_source_lines.append(
            f"- `Cubox`：当前通过 `{cubox_root}` 读取，同日未命中；近期有更新、待日级映射。回看窗口 `{len(cubox_window_entries)}` 条、近 7 天 `{len(cubox_recent)}` 条，主题为 {topic_line(cubox_recent)}。"
        )
        refs.extend(quote_path(entry.rel_path) for entry in cubox_recent[:3])
    elif cubox_mode == "missing":
        fixed_source_lines.append("- `Cubox`：当前仓库没有可用落点目录，已作为真实缺口保留。")
    else:
        fixed_source_lines.append(f"- `Cubox`：当前通过 `{cubox_root}` 读取，但目标窗口和近 7 天都未命中新材料。")

    signal_lines: list[str] = []
    if get_daily and get_age == 0:
        signal_lines.append(f"- 精力：以 `Get` 同日字段为主，当前记录为 `{get_fields.get('energy', '未抽出')}`。")
        signal_lines.append(f"- 情绪：以 `Get` 同日字段为主，当前记录为 `{get_fields.get('mood', '未抽出')}`。")
        signal_lines.append(f"- 反馈：从行动摘要看，最强反馈来自 `{get_fields.get('action_summary', '当日行动摘要未抽出')}`。")
    else:
        primary = insights[0].title if insights else "信息仍偏碎"
        signal_lines.append(f"- 精力：更像在做认知收束，主线落在“{primary}”，不是高并发输出日。")
        signal_lines.append("- 情绪：同日量化字段缺失，当前只能根据标题和外部材料判断为“关注点向内收，评价感更强”。")
        signal_lines.append("- 反馈：今天最有用的不是更多建议，而是分清哪些信号真的改变判断。")
    signal_lines.append(
        "- 任务承载：适合 30-90 分钟的一页式判断，不适合再开新的复杂系统分支。"
    )

    frontmatter = render_frontmatter(
        {
            "type": f"observatory-{mode}-tab",
            "tab": "个人状态",
            "date": period["target_date"] if mode == "daily" else period["label"],
            "updated": shanghai_today().isoformat(),
        }
    )
    content = (
        frontmatter
        + "# 个人状态\n\n"
        + "## 今日状态\n\n"
        + "\n\n".join(intro_lines)
        + "\n\n## 固定信息源\n\n"
        + "\n".join(fixed_source_lines)
        + "\n\n## 状态信号\n\n"
        + "\n".join(signal_lines)
        + "\n\n## 标签\n\n"
        + f"- #观测站\n- {TAG_BY_MODE[mode]}\n- #个人状态\n\n"
        + "## 引用文章\n\n"
        + "\n".join(f"- {ref}" for ref in build_reference_section(mode, "个人状态", refs))
        + "\n"
    )
    return content, refs


def build_system_status(
    mode: str,
    period: dict[str, str],
    sources: dict[str, Any],
    discovery: dict[str, Any],
    audit: dict[str, Any],
    workspace_summary: str,
    written_paths: list[str],
    actual_wiki_updates: list[str],
    candidate_skill_items: list[str],
) -> tuple[str, list[str]]:
    refs: list[str] = [
        "[[Wiki/06 Systems/Notion 检索索引]]",
        "[[Wiki/06 Systems/每日隐藏链接发现报告]]",
        "[[Wiki/06 Systems/迭代系统]]",
    ]
    discovery_delta = discovery.get("delta", {})
    run_lines = [
        f"{period['label']} 的 {mode} 迭代已先执行 `来源自发现` 与 `增量输入审计`，当前候选数 `{audit['counts']['total_candidates']}`，高信号候选 `{audit['counts']['high_priority_candidates']}`。",
        workspace_summary,
    ]
    if discovery_delta.get("added_sources") or discovery_delta.get("removed_sources"):
        run_lines.append(
            f"来源 delta 显示新增一级来源 `{len(discovery_delta.get('added_sources', []))}` 个、移除 `{len(discovery_delta.get('removed_sources', []))}` 个。"
        )
    else:
        run_lines.append("本轮一级来源没有新增或移除；系统重点落在同源目录递归吸纳与最终写回。")

    flomo_entries = sources["flomo"]["entries"]
    get_entries = sources["get"]["entries"]
    notion_entries = sources["notion"]["entries"]
    cubox_entries = sources["cubox"]["entries"]
    cubox_recent = sources["cubox"]["recent_entries"]
    cubox_root = sources["cubox"]["root"] or "无"
    data_lines = [
        f"- `Flomo`：同日 `{len(flomo_entries)}` 条，回看窗口 `{len(sources['flomo']['window_entries'])}` 条。",
        f"- `Get笔记`：同日 `{len(get_entries)}` 条，回看窗口 `{len(sources['get']['window_entries'])}` 条；同日 daily summary {'命中' if sources['get']['daily_summary_age'] == 0 else '未命中'}。",
        f"- `Notion`：同日 `{len(notion_entries)}` 条；{'可作为支持证据' if notion_entries else '同周期 bundle 缺失'}。",
        (
            f"- `Cubox`：当前读取 `{cubox_root}`；同日 `{len(cubox_entries)}` 条，回看窗口 `{len(sources['cubox']['window_entries'])}` 条。"
            if cubox_entries
            else f"- `Cubox`：当前读取 `{cubox_root}`；{'近期有更新、待日级映射' if cubox_recent else '近期无命中'}。"
        ),
        "- `GitHub 运行痕迹`：当前仍按执行层处理，证据落在 `.github/state/`、workflow 和 `Raw/05 Chat/`，不假设存在 `Raw/GitHub`。",
    ]
    if discovery["summary"].get("unregistered_existing"):
        data_lines.append(
            f"- `其他 Raw 来源`：发现未注册但存在目录 `{len(discovery['summary']['unregistered_existing'])}` 个，仍需检索接线。"
        )

    writeback_lines = [
        f"- 前台页面：`{rel(build_observatory_frontend.HTML_OUTPUT)}` 会在后台三文件写回后重建。",
        "- 后台三文件：" + "、".join(f"`{path}`" for path in written_paths),
        "- 规则模板：继续复用 daily / weekly 固定模板，不再按天漂移标题结构。",
        f"- 每日 / 每周迭代：当前通过 `scripts/run_iteration_pipeline.py --mode {mode} --finalize` 进入完整 closeout。",
        f"- 升格链路：候选 Wiki `{len(actual_wiki_updates)}` 项已写入、候选 Skill `{len(candidate_skill_items)}` 项待观察。",
    ]

    gap_lines: list[str] = []
    if sources["get"]["daily_summary_age"] != 0:
        gap_lines.append("- `Get笔记` 同日 daily summary 缺失，首页量化字段仍可能断档。")
    if not notion_entries:
        gap_lines.append("- `Notion` 同周期结构化 bundle 不稳定，生活指标仍不能稳定进入观测站首页。")
    if not cubox_entries and cubox_recent:
        gap_lines.append("- `Cubox` 目前更多是“近期有更新、待日级映射”，外部材料到日级判断的证据链仍偏弱。")
    if discovery["summary"].get("registered_missing"):
        gap_lines.append("- 注册表仍存在磁盘缺失来源，说明历史入口和真实目录还没有完全收敛。")
    if not gap_lines:
        gap_lines.append("- 本轮没有发现会直接破坏可信度的新缺口，后续重点在提升同日 Get/Notion/Cubox 命中率。")

    frontmatter = render_frontmatter(
        {
            "type": f"observatory-{mode}-tab",
            "tab": "系统状态",
            "date": period["target_date"] if mode == "daily" else period["label"],
            "updated": shanghai_today().isoformat(),
        }
    )
    content = (
        frontmatter
        + "# 系统状态\n\n"
        + "## 运行状态\n\n"
        + "\n\n".join(run_lines)
        + "\n\n## 数据接入\n\n"
        + "\n".join(data_lines)
        + "\n\n## 自动化写回\n\n"
        + "\n".join(writeback_lines)
        + "\n\n## 当前缺口\n\n"
        + "\n".join(gap_lines)
        + "\n\n## 标签\n\n"
        + f"- #观测站\n- {TAG_BY_MODE[mode]}\n- #系统状态\n\n"
        + "## 引用文章\n\n"
        + "\n".join(f"- {ref}" for ref in build_reference_section(mode, "系统状态", refs))
        + "\n"
    )
    return content, refs


def build_insight_tab(
    mode: str,
    period: dict[str, str],
    sources: dict[str, Any],
    selected_themes: list[InsightTheme],
    recent_history: Counter[str],
) -> tuple[str, list[str], dict[str, list[str]]]:
    refs: list[str] = [
        "[[Wiki/01 People/Eddie]]",
        "[[Wiki/02 Life/近期状态]]",
        "[[Wiki/03 Work/AI协作与知识系统演进]]",
    ]
    theme_refs: dict[str, list[str]] = defaultdict(list)

    exact_entries = {
        "flomo": sources["flomo"]["entries"],
        "get": sources["get"]["entries"],
        "notion": sources["notion"]["entries"],
        "cubox": sources["cubox"]["entries"],
    }
    fallback_entries = {
        "flomo": sources["flomo"]["window_entries"],
        "get": sources["get"]["window_entries"],
        "notion": sources["notion"]["window_entries"],
        "cubox": sources["cubox"]["window_entries"] or sources["cubox"]["recent_entries"],
    }

    insight_lines = ["## 洞察", ""]
    if not selected_themes:
        insight_lines.extend(["### 证据不足", "", "- 种子：当前窗口没有形成足够强的新主题。"])
    for theme in selected_themes[:3]:
        seed_titles: list[str] = []
        for source_entries in exact_entries.values():
            seed_titles.extend(gather_seed_titles(source_entries, theme.key, limit=2))
        if not seed_titles:
            for source_entries in fallback_entries.values():
                seed_titles.extend(gather_seed_titles(source_entries, theme.key, limit=2))
        seed_titles = seed_titles[:3]
        refs.extend(
            quote_path(entry.rel_path)
            for source_entries in list(exact_entries.values()) + list(fallback_entries.values())
            for entry in source_entries
            if entry.title in seed_titles
        )
        theme_refs[theme.key] = seed_titles
        judgment = theme.judgment
        if recent_history.get(theme.key, 0) >= 1:
            judgment = "和最近 3 次输出相比，本轮的新意在于来源组合和问题角度都发生了变化。 " + judgment
        insight_lines.extend(
            [
                f"### {theme.title}",
                f"- 种子：{'；'.join(seed_titles) if seed_titles else theme.seed_line}",
                f"- 判断：{judgment}",
                f"- Aha：{theme.aha}",
                "",
            ]
        )

    sprout_lines = ["## 发芽", ""]
    for theme in selected_themes[: min(len(selected_themes), 3)]:
        sprout_lines.extend(
            [
                f"### {theme.lens_title}",
                f"- 种子：{'；'.join(theme_refs.get(theme.key, [])) or theme.seed_line}",
                f"- 判断或外部镜头：{theme.lens_text}",
                f"- Aha：{theme.aha}",
                "",
            ]
        )
    if not selected_themes:
        sprout_lines.extend(["### 证据不足", "- 判断或外部镜头：今天不足以引入新的外部镜头。", ""])

    action_theme = selected_themes[0] if selected_themes else None
    action_lines = ["## 行动建议", ""]
    if action_theme:
        action_lines.extend([f"### {action_theme.action_title}", f"- {action_theme.action_text}", ""])
    else:
        action_lines.extend(["### 证据不足", "- 今天不足以给出不重复的行动建议。", ""])

    random_lines = ["## 随机洞察", ""]
    cubox_entries = sources["cubox"]["entries"]
    if cubox_entries:
        random_lines.append(f"- 外部材料今天更偏 `{topic_line(cubox_entries)}`，说明你的关注面没有只困在内部状态。")
    if sources["get"]["daily_summary_age"] != 0:
        random_lines.append("- 同日 `Get` 总结缺失时，系统更容易误把外部材料或 Flomo 当首页主轴；这轮已经显式压住了这个倾向。")
    if not cubox_entries and not sources["cubox"]["recent_entries"]:
        random_lines.append("- `Cubox` 没有命中时，不应该用别的来源假装填满外部视角。")
    if len(random_lines) == 2:
        random_lines.append("- 本轮更值得看的不是素材数量，而是哪些素材真的改变了判断。")
    random_lines.append("")

    frontmatter = render_frontmatter(
        {
            "type": f"observatory-{mode}-tab",
            "tab": "启发洞察",
            "date": period["target_date"] if mode == "daily" else period["label"],
            "updated": shanghai_today().isoformat(),
        }
    )
    content = (
        frontmatter
        + "# 启发洞察\n\n"
        + "\n".join(insight_lines)
        + "\n"
        + "\n".join(sprout_lines)
        + "\n"
        + "\n".join(action_lines)
        + "\n"
        + "\n".join(random_lines)
        + "\n## 标签\n\n"
        + f"- #观测站\n- {TAG_BY_MODE[mode]}\n- #启发洞察\n\n"
        + "## 引用文章\n\n"
        + "\n".join(f"- {ref}" for ref in build_reference_section(mode, "启发洞察", refs))
        + "\n"
    )
    return content, refs, theme_refs


def build_hidden_links(
    selected_themes: list[InsightTheme],
    sources: dict[str, Any],
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    source_groups = [
        sources["flomo"]["entries"] or sources["flomo"]["window_entries"],
        sources["get"]["entries"] or sources["get"]["window_entries"],
        sources["cubox"]["entries"] or sources["cubox"]["window_entries"] or sources["cubox"]["recent_entries"],
    ]
    for theme in selected_themes[:3]:
        seed_titles: list[str] = []
        for source_entries in source_groups:
            seed_titles.extend(gather_seed_titles(source_entries, theme.key, limit=2))
        items.append(
            {
                "title": theme.title,
                "evidence": "；".join(seed_titles[:3]) or theme.seed_line,
                "inference": theme.judgment + " " + theme.aha,
            }
        )
    return items


def build_candidate_lists(
    discovery: dict[str, Any],
    sources: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    wiki_candidates: list[str] = []
    skill_candidates: list[str] = []
    follow_ups: list[str] = []

    cubox_mode = sources["cubox"]["mode"]
    if cubox_mode == "新枝-fallback":
        wiki_candidates.append("在 `Schema/来源目录注册.md` 和相关系统页明确：当前 `Cubox` 逻辑源由 `Raw/10 新枝/` 承接，直到独立 `Raw/10 Cubox/` 出现。")
    if discovery["summary"].get("unregistered_existing"):
        wiki_candidates.append("把 `来源自发现` 标出的未注册来源补进 `Schema/来源目录注册.md` 与索引接线。")
    if sources["get"]["daily_summary_age"] != 0:
        skill_candidates.append("为 `Get笔记` 增加同日 daily summary 命中检查，避免首页长期依赖旧 summary 回退。")
        follow_ups.append("检查 Get 同日 daily summary 是否同步延迟，必要时修正同步窗口或命名规则。")
    if not sources["notion"]["entries"]:
        follow_ups.append("补查 Notion 同日结构化生活 bundle，确认是镜像滞后还是源侧没有记录。")
    if not sources["cubox"]["entries"] and sources["cubox"]["recent_entries"]:
        skill_candidates.append("给 Cubox-like 外部材料补一个日级映射器，把 `近期有更新` 缩成可核对的 target-date bundle。")
        follow_ups.append("为 `Raw/10 新枝/` 增加 target-date 映射字段或索引，减少“近期有更新、待日级映射”的模糊状态。")
    return wiki_candidates, skill_candidates, follow_ups


def build_promotion_block(
    mode: str,
    period: dict[str, str],
    theme: InsightTheme,
    seed_paths: list[str],
    observatory_ref: str,
) -> tuple[Path, list[str]] | None:
    if mode != "weekly":
        return None
    seed_refs = [quote_path(path) for path in seed_paths[:3]]
    if theme.key == "ai_system":
        return (
            WIKI_DIR / "03 Work" / "AI协作与知识系统演进.md",
            [
                f"- `{period['label']}`：本周多源材料继续把 AI 的价值从“能回答”推向“协作接口与后台整合”。这说明当前主线不是再找一个更强工具，而是让 AI 进入既有协作边界与长期后台。",
                f"  - 来源：{'、'.join(seed_refs + [observatory_ref])}",
            ],
        )
    if theme.key == "job_transition":
        return (
            WIKI_DIR / "02 Life" / "近期状态.md",
            [
                f"- `{period['label']}`：离职过渡期的核心矛盾开始从“有没有机会”转向“按什么排序机会”，说明当前更需要决策标准，而不是继续同时维持所有选项。",
                f"  - 来源：{'、'.join(seed_refs + [observatory_ref])}",
            ],
        )
    return None


def promote_selected_themes(
    mode: str,
    period: dict[str, str],
    selected_themes: list[InsightTheme],
    exact_entries: dict[str, list[Entry]],
    observatory_paths: list[str],
) -> list[str]:
    if not selected_themes:
        return []
    source_support = theme_source_support(exact_entries)
    exact_support = theme_exact_support(exact_entries)
    observatory_ref = quote_path(observatory_paths[-1])
    all_exact_entries = [entry for entries in exact_entries.values() for entry in entries]
    written: list[str] = []
    for theme in selected_themes:
        if len(source_support.get(theme.key, set())) < 2:
            continue
        if exact_support.get(theme.key, 0) < 2:
            continue
        seed_paths = gather_seed_paths(all_exact_entries, theme.key, limit=3)
        block = build_promotion_block(mode, period, theme, seed_paths, observatory_ref)
        if not block:
            continue
        page_path, lines = block
        refs = [quote_path(path) for path in seed_paths] + [observatory_ref]
        upsert_managed_period_block(page_path, period["label"], lines, refs)
        written.append(rel(page_path))
    return written


def chat_path_for(mode: str, period: dict[str, str]) -> Path:
    if mode == "daily":
        return CHAT_DIR / f"{period['target_date']}-Hermes每日自动化-{period['target_date']}.md"
    return CHAT_DIR / f"{shanghai_today().isoformat()}-Hermes每周自动化-{period['label']}.md"


def build_chat_record(
    mode: str,
    period: dict[str, str],
    known_context: list[str],
    workspace_summary: str,
    hidden_links: list[dict[str, str]],
    wiki_candidates: list[str],
    skill_candidates: list[str],
    follow_ups: list[str],
    written_paths: list[str],
) -> str:
    title = f"Hermes{'每日' if mode == 'daily' else '每周'}自动化-{period['label']}"
    frontmatter = render_frontmatter(
        {
            "date": shanghai_today().isoformat(),
            "type": "chat",
            "source": "hermes",
            "topic": title,
        }
    )
    lines = [
        frontmatter.rstrip(),
        "",
        f"# {title}",
        "",
        "## 核心要点",
        "",
        f"- 目标周期：`{period['label']}`",
        f"- 已知上下文：{'；'.join(known_context) if known_context else '本轮未抽到已知上下文短句'}",
        f"- 工作区：{workspace_summary}",
        f"- 后台三文件：{'、'.join(f'`{path}`' for path in written_paths)}",
        "",
        "## 隐藏链接",
        "",
    ]
    for item in hidden_links:
        lines.extend(
            [
                f"### {item['title']}",
                f"- 证据：{item['evidence']}",
                f"- 推断：{item['inference']}",
                "",
            ]
        )
    lines.extend(["## 候选 Wiki 更新", ""])
    if wiki_candidates:
        lines.extend(f"- {item}" for item in wiki_candidates)
    else:
        lines.append("- 暂无高置信稳定页写回。")
    lines.extend(["", "## 候选 Skill 升格", ""])
    if skill_candidates:
        lines.extend(f"- {item}" for item in skill_candidates)
    else:
        lines.append("- 暂无满足升格门槛的新 skill。")
    lines.extend(["", "## Follow-up", ""])
    if follow_ups:
        lines.extend(f"- {item}" for item in follow_ups)
    else:
        lines.append("- 暂无额外 follow-up。")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_observatory_tabs(mode: str, period: dict[str, str], personal: str, system: str, insight: str) -> list[str]:
    if mode == "daily":
        folder = OBS_ROOT / "daily" / period["target_date"][:4] / period["target_date"][:7] / period["target_date"]
    else:
        folder = OBS_ROOT / "weekly" / period["label"][:4] / period["label"]
    files = {
        folder / "个人状态.md": personal,
        folder / "系统状态.md": system,
        folder / "启发洞察.md": insight,
    }
    changed_paths: list[str] = []
    for path, content in files.items():
        changed = stable_write(path, content)
        if changed:
            changed_paths.append(rel(path))
    return list(files.keys())


def observatory_tab_paths(mode: str, period: dict[str, str]) -> list[str]:
    if mode == "daily":
        folder = OBS_ROOT / "daily" / period["target_date"][:4] / period["target_date"][:7] / period["target_date"]
    else:
        folder = OBS_ROOT / "weekly" / period["label"][:4] / period["label"]
    return [
        rel(folder / "个人状态.md"),
        rel(folder / "系统状态.md"),
        rel(folder / "启发洞察.md"),
    ]


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# {summary['mode']} iteration closeout",
        "",
        f"- period: `{summary['period']['label']}`",
        f"- observatory_folder: `{summary['observatory_folder']}`",
        f"- chat_record: `{summary['chat_record']}`",
        f"- actual_wiki_updates: {len(summary.get('actual_wiki_updates', []))}",
        f"- wiki_candidates: {len(summary['candidate_wiki_updates'])}",
        f"- skill_candidates: {len(summary['candidate_skill_promotions'])}",
        f"- hidden_links: {len(summary['hidden_links'])}",
        "",
        "## Written Tabs",
        "",
    ]
    lines.extend(f"- `{path}`" for path in summary["written_tabs"])
    return "\n".join(lines) + "\n"


def closeout(
    mode: str,
    target_date_raw: str | None = None,
    window_days: int | None = None,
) -> dict[str, Any]:
    target, window, period = normalize_target(mode, target_date_raw, window_days)
    start = date.fromisoformat(period["window_start"])
    end = date.fromisoformat(period["window_end"])
    previous_discovery = load_json(iteration_source_discovery.DEFAULT_OUTPUT)
    discovery = iteration_source_discovery.discover(previous_discovery)
    audit = iteration_incremental_audit.audit(target, window, 120)

    known_context = build_known_context_lines()
    workspace_summary, workspace_data = build_workspace_summary()
    sources = build_source_snapshot(target, start, end, mode)
    recent_history = load_recent_theme_history(mode, target)
    exact_theme_entries = {
        "flomo": sources["flomo"]["entries"],
        "get": sources["get"]["entries"],
        "notion": sources["notion"]["entries"],
        "cubox": sources["cubox"]["entries"],
    }
    fallback_theme_entries = {
        "flomo": sources["flomo"]["window_entries"],
        "get": sources["get"]["window_entries"],
        "notion": sources["notion"]["window_entries"],
        "cubox": sources["cubox"]["window_entries"] or sources["cubox"]["recent_entries"],
    }
    selected_themes = select_insight_themes(exact_theme_entries, recent_history)
    if not selected_themes:
        selected_themes = select_insight_themes(fallback_theme_entries, Counter(), limit=2)
    personal_text, personal_refs = build_personal_status(mode, period, sources, selected_themes)
    wiki_candidates, skill_candidates, follow_ups = build_candidate_lists(discovery, sources)
    written_rel_paths = observatory_tab_paths(mode, period)
    insight_text, insight_refs, theme_refs = build_insight_tab(mode, period, sources, selected_themes, recent_history)
    actual_wiki_updates = promote_selected_themes(
        mode,
        period,
        selected_themes,
        exact_theme_entries,
        written_rel_paths,
    )
    system_text, system_refs = build_system_status(
        mode,
        period,
        sources,
        discovery,
        audit,
        workspace_summary,
        written_rel_paths,
        actual_wiki_updates,
        skill_candidates,
    )
    written_paths = write_observatory_tabs(mode, period, personal_text, system_text, insight_text)
    written_rel_paths = [rel(path) for path in written_paths]

    hidden_links = build_hidden_links(selected_themes, sources)
    chat_text = build_chat_record(
        mode,
        period,
        known_context,
        workspace_summary,
        hidden_links,
        wiki_candidates,
        skill_candidates,
        follow_ups,
        written_rel_paths,
    )
    chat_path = chat_path_for(mode, period)
    stable_write(chat_path, chat_text)

    frontend_snapshot = build_observatory_frontend.build_snapshot()
    frontend_changed = build_observatory_frontend.write_outputs(frontend_snapshot)
    observatory_folder = str(written_paths[0].parent.relative_to(ROOT))
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "period": period,
        "known_context": known_context,
        "workspace": {
            "summary": workspace_summary,
            "path": rel(workspace_intake_report.REPORT_PATH),
            "json_path": rel(workspace_intake_report.JSON_PATH),
            "risk_counts": workspace_data.get("risk_counts", {}),
        },
        "discovery": discovery["summary"],
        "audit": audit["counts"],
        "sources": {
            "flomo_count": len(sources["flomo"]["entries"]),
            "get_count": len(sources["get"]["entries"]),
            "get_daily_summary_age": sources["get"]["daily_summary_age"],
            "notion_count": len(sources["notion"]["entries"]),
            "cubox_count": len(sources["cubox"]["entries"]),
            "cubox_recent_count": len(sources["cubox"]["recent_entries"]),
            "cubox_root": sources["cubox"]["root"],
            "cubox_mode": sources["cubox"]["mode"],
        },
        "hidden_links": hidden_links,
        "actual_wiki_updates": actual_wiki_updates,
        "candidate_wiki_updates": wiki_candidates,
        "candidate_skill_promotions": skill_candidates,
        "follow_ups": follow_ups,
        "written_tabs": written_rel_paths,
        "chat_record": rel(chat_path),
        "observatory_folder": observatory_folder,
        "frontend": {
            "html_path": rel(build_observatory_frontend.HTML_OUTPUT),
            "json_path": rel(build_observatory_frontend.JSON_OUTPUT),
            "changed": frontend_changed,
            "views": {
                name: info["current_period"] for name, info in frontend_snapshot["views"].items()
            },
        },
    }
    return summary


def main() -> int:
    args = parse_args()
    summary = closeout(args.mode, args.target_date, args.window_days)
    if args.write_latest:
        stable_write_json(LATEST_CLOSEOUT[args.mode], summary)
    if args.state_output:
        state_path = Path(args.state_output)
        if not state_path.is_absolute():
            state_path = ROOT / state_path
        stable_write_json(state_path, summary)
    if args.format == "markdown":
        print(render_markdown(summary), end="")
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
