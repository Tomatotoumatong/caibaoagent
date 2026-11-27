"""Frozen judging material: the match rubric (rubric/MATCH_RUBRIC.md), the subject vocabulary,
the sector templates and the two LLM prompts. Their hashes go into every match row and every
frozen baseline, so a changed rubric can never be silently compared with an old result."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from .config import ROOT

RUBRIC_PATH = ROOT / "rubric" / "MATCH_RUBRIC.md"
TEMPLATES_PATH = ROOT / "rubric" / "sector_templates.json"

SUBJECTS = {
    "revenue_demand": "total revenue / overall demand (any horizon, incl. revenue guidance)",
    "segment_product": "revenue or demand of one segment, product line or customer group",
    "geography": "exposure to one country or region (e.g. China)",
    "gross_margin": "gross margin / gross profit",
    "opex_operating_margin": "operating expenses, operating margin, unit costs",
    "eps_net_income": "EPS, net income, FFO, full-year profit outlook",
    "cash_flow_capex": "the company's own capex, free cash flow",
    "capital_return": "buybacks, dividends",
    "balance_sheet": "debt, liquidity, financing, credit quality, provisions",
    "pricing": "prices the company charges",
    "volume_units": "units shipped, users, subscribers, occupancy, production volume",
    "backlog_bookings": "orders, backlog, RPO, ARR, bookings, renewals",
    "inventory_supply": "inventory, supply chain, capacity, utilization",
    "cost_inputs_tariffs": "input costs, tariffs, FX effect on costs",
    "ai": "AI products, AI demand, AI investment",
    "pipeline_launch": "new products, R&D pipeline, roadmap",
    "competition_share": "competition, market share",
    "regulation_legal": "regulation, litigation, policy",
    "macro_end_market": "macro conditions, end-market health",
    "m_and_a": "acquisitions, divestitures",
    "guidance_method": "how the company guides and what it assumes (no specific quantity)",
    "strategy_management": "strategy, management, organisation",
    "other": "anything else",
}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def rubric_text() -> str:
    return RUBRIC_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def templates() -> dict:
    d = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in d.items() if not k.startswith("_")}


def template_items(sector: str, k: int) -> list:
    rows = templates().get(sector) or []
    return [{"id": f"T{i + 1}", "text": t, "subject": s} for i, (t, s) in enumerate(rows[:k])]


_SUBJ = "\n".join(f"  {k}: {v}" for k, v in SUBJECTS.items())

EXTRACT_SYSTEM = f"""You read the Q&A part of an earnings-call transcript that has NO speaker labels.
Each numbered exchange starts where the operator hands over to an analyst; it then contains the
analyst's question(s), management's answer(s) and possibly follow-ups by the same analyst.
For every exchange, extract the ANALYST's questions only — never management's statements.
Rules:
1. text: a faithful English paraphrase of one question (max 35 words) that names the quantity and the
   business scope asked about (e.g. "gross margin outlook for Q4", "data center demand visibility in 2027").
2. quote: copy, character for character, 30-250 characters of the exchange where the analyst asks it.
   Code checks that the quote appears verbatim; a question whose quote is not found is discarded.
3. A follow-up question by the same analyst is a separate entry. Multi-part questions may be split.
4. subject: exactly one of
{_SUBJ}
   choosing the most specific.
5. analyst / firm: as introduced by the operator, or null.
6. If an exchange contains no analyst question (operator housekeeping, closing remarks), return an
   empty questions list for it.
Return one JSON object only."""

EXTRACT_SCHEMA = {"type": "object", "required": ["exchanges"]}


def extract_user(exchanges: list) -> str:
    parts = ["Exchanges (each truncated to its first characters; the analyst speaks first):"]
    for x in exchanges:
        parts.append(f"\n### exchange {x['idx']}\n{x['text']}")
    parts.append('\nReturn JSON: {"exchanges": [{"x": <exchange number>, "analyst": str|null, "firm": str|null, '
                 '"questions": [{"text": str, "quote": str, "subject": str}]}]}')
    return "\n".join(parts)


def match_system() -> str:
    return ("你是研究评估系统里的匹配裁判。下面是冻结的判定规则，必须严格照此执行：\n\n" + rubric_text() +
            "\n\n## 输出\n只输出一个 JSON 对象："
            '{"items": [{"id": 观察项 id, "subject": 主题词, "matches": [{"q": 问题 id, "level": 1 或 2, "why": "≤25 字理由"}]}]}。\n'
            "每个观察项都要出现一次；只列出等级为 1 或 2 的问题，没列出的一律视为 0。subject 从下列主题词中选一个最具体的：\n" + _SUBJ)


MATCH_SCHEMA = {"type": "object", "required": ["items"]}


def match_user(items: list, questions: list) -> str:
    it = [{"id": i["id"], "text": i["text"]} for i in items]
    qs = [{"id": q["qid"], "question": q["text"], "quote": q.get("quote")} for q in questions]
    return ("观察项：\n" + json.dumps(it, ensure_ascii=False, indent=1) +
            "\n\n分析师问题（question 为转述，quote 为原文摘录；以原文为准）：\n" +
            json.dumps(qs, ensure_ascii=False, indent=1) + "\n\n按规则输出 JSON。")


def material_hashes() -> dict:
    return {"rubric": sha(rubric_text()), "templates": sha(TEMPLATES_PATH.read_text(encoding="utf-8")),
            "extract_prompt": sha(EXTRACT_SYSTEM), "match_prompt": sha(match_system())}
