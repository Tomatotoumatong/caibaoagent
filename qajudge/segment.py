"""Q&A segmentation of a raw (speaker-less) call transcript — code only, no LLM.

The free ROIC plan gives one plain-text string per call, no speaker labels. Conference-call
operators are formulaic, so the Q&A is cut at their hand-offs ("Our next question comes from the
line of X with Y"). Each exchange = operator intro + analyst question(s) + management answers.
Separating question from answer inside an exchange is left to the extraction step (LLM, with a
verbatim-quote check); this module only finds the boundaries and reports how sure it is.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

I = re.IGNORECASE

QA_START = [
    ("qa_session", re.compile(r"question[\s-]*(?:and|&)[\s-]*answer\s+(?:session|portion|period|segment|part)", I)),
    ("qa_abbrev", re.compile(r"\bQ\s?&\s?A\s+(?:session|portion|period)", I)),
    ("open_for_questions", re.compile(r"open (?:up )?(?:the )?(?:call|line|lines|floor|it) (?:up )?(?:for|to) (?:your |any )?questions", I)),
    ("take_questions", re.compile(r"(?:begin|start|move to|turn to|conduct|take) (?:the |our |your )?(?:question|questions|Q&A)", I)),
]

# operator / IR hand-off to an analyst: start of an exchange. Phrasings seen in the 2026-11-26
# probe: "Our first question comes from the line of X with Y" (MSFT), "Our next question is going to
# come from the line of" (BF.B), "Your first question will come from" / "We'll take our first
# question from" / "looks like we have Keith up for the next question" (CRM video format).
_COME = r"(?:comes|is coming|will come|will be coming|is going to come|is|has come|now comes|we have|we'll have) from"
CUES = [
    ("question_from", re.compile(r"(?:our|the|your|and our|and the) (?:first|next|following|final|last) question,? "
                                 r"(?:today |this morning |this afternoon |this evening |then )?" + _COME, I)),
    ("first_next_from", re.compile(r"\b(?:first|next|last) question,? from\b", I)),
    ("line_of", re.compile(r"(?:comes|coming|come|is) from the line of", I)),
    ("move_to_next", re.compile(r"(?:we'll|we will|let's|let us|i'll|i will) (?:now )?(?:go|move|take|turn|proceed)"
                                r"(?: (?:to|on to|over to|with))? (?:the |our )?(?:next|first|last|final) (?:question|caller)", I)),
    ("next_caller", re.compile(r"(?:the|our) next (?:caller|questioner) (?:is|comes from)", I)),
    ("next_we_have", re.compile(r"\b(?:next|first),? we (?:have|will hear from|'ll hear from|go to)\b", I)),
    ("up_for_next", re.compile(r"\bup (?:for|with) the next question\b", I)),
    ("take_your_question", re.compile(r"\b(?:we'll|we will) take your question(?: now)?\b", I)),
    # IR asks for the next question and the operator intro is missing from the ASR text (MSFT 2026-06)
    ("next_please", re.compile(r"\bnext question,? please\b", I)),
]
GO_AHEAD = re.compile(r"(?:please go ahead|please proceed|your line is (?:now )?(?:open|live)|go ahead,? please|you may (?:now )?(?:ask|proceed|unmute)"
                      r"|please unmute (?:your line|your audio|yourself)(?: and ask your question)?)[.,!]?", I)
CLOSING = [
    re.compile(r"(?:this|that) (?:does |will )?(?:concludes?|completes?|ends)\b[^.?!]{0,60}?"
               r"(?:question[\s-]*(?:and|&)[\s-]*answer|Q\s?&\s?A|conference|call|session|program|presentation|portion)", I),
    re.compile(r"(?:wraps?|wrapping) up (?:the |our |today's )?(?:Q\s?&\s?A|question|call)", I),
    re.compile(r"(?:there are|we have|i'm showing|i am showing|showing) no (?:further|more) questions", I),
    re.compile(r"(?:we have|we've) (?:reached|come to) the end of (?:our|the|today's) (?:time|call|question)", I),
    re.compile(r"turn (?:the (?:call|conference|floor)|it|things) back (?:over )?to\b", I),
    re.compile(r"(?:closing|concluding) (?:remarks|comments)", I),
    re.compile(r"you may (?:now )?disconnect", I),
]
# a Q&A-start phrase that is really the end ("That wraps up the Q&A portion", MSFT) is not a start
_CLOSING_CONTEXT = re.compile(r"(?:wrap|conclud|end of|ends|that's (?:all|it)|close out|reached)[^.?!]{0,40}$", I)
FIRM_AFTER_CUE = re.compile(r"^[^.?!]{0,140}?\b(?:with|from|of|at)\s+[A-Z][\w&.'-]+", re.S)

MERGE_GAP = 150          # cues closer than this are one hand-off ("next question ... comes from the line of")
MAX_EXCHANGE_CHARS = 3500  # head passed to extraction; the analyst speaks first, answers come later
STUB_GAP = 700            # two hand-offs this close with no "?" between = one hand-off (queue announcements)


@dataclass
class Exchange:
    idx: int
    start: int
    end: int
    rule: str
    question_start: int     # after "please go ahead" when found, else start


@dataclass
class Segmentation:
    n_chars: int
    n_newlines: int
    qa_start: Optional[int]
    qa_start_rule: Optional[str]
    closing_at: Optional[int]
    exchanges: List[Exchange] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)

    def stats(self, text: str) -> dict:
        lens = [e.end - e.start for e in self.exchanges]
        with_firm = sum(1 for e in self.exchanges if FIRM_AFTER_CUE.match(text[e.start:e.start + 220] or ""))
        return {
            "n_chars": self.n_chars, "n_newlines": self.n_newlines,
            "qa_start_rule": self.qa_start_rule,
            "qa_start_frac": round(self.qa_start / self.n_chars, 3) if self.qa_start is not None and self.n_chars else None,
            "closing_found": self.closing_at is not None,
            "n_exchanges": len(self.exchanges),
            "median_exchange_chars": int(statistics.median(lens)) if lens else None,
            "max_exchange_chars": max(lens) if lens else None,
            "share_truncated": round(sum(l > MAX_EXCHANGE_CHARS for l in lens) / len(lens), 3) if lens else None,
            "share_cue_names_firm": round(with_firm / len(lens), 3) if lens else None,
            "share_go_ahead": round(sum(e.question_start > e.start for e in self.exchanges) / len(lens), 3) if lens else None,
            "cue_rules": sorted({e.rule for e in self.exchanges}),
            "flags": list(self.flags),
        }


def _first(patterns, text: str, lo: int = 0) -> Tuple[Optional[int], Optional[str]]:
    best = (None, None)
    for name, rx in patterns:
        for m in rx.finditer(text, lo):
            if _CLOSING_CONTEXT.search(text[max(0, m.start() - 60):m.start()]):
                continue
            if best[0] is None or m.start() < best[0]:
                best = (m.start(), name)
            break
    return best


def segment(text: str) -> Segmentation:
    n = len(text)
    seg = Segmentation(n_chars=n, n_newlines=text.count("\n"), qa_start=None, qa_start_rule=None, closing_at=None)
    if n < 2000:
        seg.flags.append("too_short")
        return seg
    lo = int(n * 0.10)  # "we will take questions after the prepared remarks" in the intro is not the Q&A
    qs, rule = _first(QA_START, text, lo)
    cues = []
    for name, rx in CUES:                      # hand-offs never occur in prepared remarks: search from 2%
        for m in rx.finditer(text, int(n * 0.02)):
            cues.append((m.start(), name))
    cues.sort()
    merged: List[Tuple[int, str]] = []
    for pos, name in cues:
        if merged and (pos - merged[-1][0] < MERGE_GAP or
                       (pos - merged[-1][0] < STUB_GAP and "?" not in text[merged[-1][0]:pos])):
            continue
        merged.append((pos, name))
    # Q&A starts at the earlier of a start phrase and the first hand-off. (Before 2026-11-26 a
    # start phrase found late — MSFT's closing "wraps up the Q&A portion" — discarded every cue.)
    if merged and (qs is None or merged[0][0] < qs):
        qs, rule = merged[0][0], "first_cue"
    if qs is None:
        seg.flags.append("no_qa_found")
        return seg
    seg.qa_start, seg.qa_start_rule = qs, rule
    merged = [c for c in merged if c[0] >= qs - MERGE_GAP]
    if not merged:
        seg.flags.append("qa_start_but_no_cues")
        return seg
    last = merged[-1][0]
    closing = None
    for rx in CLOSING:
        m = rx.search(text, last + 200)
        if m and (closing is None or m.start() < closing):
            closing = m.start()
    seg.closing_at = closing
    end_all = closing if closing is not None else n
    for i, (pos, name) in enumerate(merged):
        end = merged[i + 1][0] if i + 1 < len(merged) else end_all
        ga = GO_AHEAD.search(text, pos, min(pos + 400, end))
        seg.exchanges.append(Exchange(idx=i, start=pos, end=end, rule=name, question_start=ga.end() if ga else pos))
    k = len(seg.exchanges)
    if k < 3:
        seg.flags.append("few_exchanges")
    if k > 40:
        seg.flags.append("many_exchanges")
    if closing is None:
        seg.flags.append("no_closing")
    lens = [e.end - e.start for e in seg.exchanges]
    if lens and max(lens) > 6 * max(1, statistics.median(lens)):
        seg.flags.append("long_outlier_exchange")   # likely a missed hand-off
    return seg


_QSENT = re.compile(r"[^.?!]{15,300}\?")


def exchange_text(text: str, ex: Exchange, limit: int = MAX_EXCHANGE_CHARS) -> str:
    """Head of the exchange; when it is cut, the later sentences ending in '?' are appended so a
    follow-up deep in a long answer is not lost (rhetorical questions by management come too —
    the extraction step must tell them apart)."""
    full = text[ex.start:ex.end].strip()
    if len(full) <= limit:
        return full
    tail = [m.group(0).strip() for m in _QSENT.finditer(full, limit)][:8]
    return full[:limit] + (" [...] later in this exchange: " + " | ".join(tail) if tail else " [...]")


def exchange_full(text: str, ex: Exchange) -> str:
    return text[ex.start:ex.end]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[‘’]", "'", re.sub(r"[“”]", '"', s or ""))).strip().lower()


def quote_in(quote: str, source: str) -> bool:
    q = norm(quote)
    return len(q) >= 20 and q in norm(source)
