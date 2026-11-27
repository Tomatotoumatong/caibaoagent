"""10-symbol ROIC probe: coverage, fiscal-key alignment, rate limits, Q&A split quality.

Sealed (clean-sample) transcripts are fetched and archived like any other, but only content-free
structure statistics are computed for them (lengths, hand-off counts); no excerpt is written."""
from __future__ import annotations

import datetime as dt
import json
import statistics
from pathlib import Path
from typing import List, Optional

from . import erdb
from .align import align_event, label_convention
from .roic import Archive, RoicError, load_text, raw_transcript_id, should_seal, unseal
from .segment import exchange_text, segment

DEFAULT_SYMBOLS = ["NVDA", "CRM", "MSFT", "FICO", "TYL", "ADI", "CRWD", "BF.B", "BRK.B", "JKHY"]
WHY = {"NVDA": "FY ends late Jan (52/53-week), dev event", "CRM": "FY ends Jan 31, not a dev symbol",
       "MSFT": "FY ends Jun 30, dev event", "FICO": "FY ends Sep 30, cold name, dev event",
       "TYL": "cold name, dev event", "ADI": "FY ends ~Nov 1, bmo 11-24 (newest call: availability lag)",
       "CRWD": "FY ends Jan 31, bmo, dev event", "BF.B": "share-class ticker (identifier spelling), FY ends Apr 30",
       "BRK.B": "share-class ticker; Berkshire holds no earnings call (expected: no transcript)",
       "JKHY": "cold name, FY ends Jun 30"}
FYE_FALLBACK = {"CRM": "0131", "BF.B": "0430", "BRK.B": "1231", "JKHY": "0630"}


def _fye(er, sym: str) -> Optional[str]:
    if er is not None:
        r = er.execute("SELECT c.fiscal_year_end FROM sp500_members m JOIN companies c ON c.cik=m.cik WHERE m.symbol=?",
                       (sym,)).fetchone()
        if r and r[0]:
            return r[0]
    return FYE_FALLBACK.get(sym)


def run(con, er, arch: Archive, symbols: List[str], out_dir: Path, llm=None, log=print) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "samples").mkdir(exist_ok=True)
    dev = {e["symbol"]: e for e in erdb.dev_events(er)} if er is not None else {}
    rep = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "symbols": {}}
    for sym in symbols:
        log(f"--- {sym}")
        s = {"why_in_probe": WHY.get(sym)}
        rep["symbols"][sym] = s
        hint = erdb.company_exchanges(er, sym) if er is not None else None
        try:
            ident, events, attempts = arch.resolve(sym, hint)
        except RoicError as exc:
            s["error"] = str(exc)
            log(f"  error: {exc}")
            if exc.status in (401, 403):
                break
            continue
        s.update({"identifier": ident, "identifier_attempts": attempts, "n_listed": len(events)})
        fye = _fye(er, sym)
        s["events"] = [{"period_end_date": e.get("period_end_date"), "release_date": e.get("release_date"),
                        "fiscal_year": e.get("fiscal_year"), "fiscal_quarter": e.get("fiscal_quarter"),
                        "label_convention": label_convention(e, fye), "raw_transcript": raw_transcript_id(e),
                        "n_transcript_entries": len(e.get("transcripts") or [])} for e in events]
        dev_call = None
        if sym in dev:
            cur, prior, diag = align_event(dev[sym], events)
            s["dev_event"] = dev[sym]["event_key"]
            s["alignment"] = diag
            dev_call = cur
        s["transcripts"] = []
        for e in events:
            tid = raw_transcript_id(e)
            if not tid:
                continue
            is_dev = dev_call is not None and e.get("id") == dev_call.get("id")
            sealed = should_seal(e, is_dev)
            try:
                arch.fetch(sym, e, sealed=sealed)
            except RoicError as exc:
                s["transcripts"].append({"transcript_id": tid, "error": str(exc)})
                continue
            if is_dev:
                unseal(con, [tid], f"dev-set event {s.get('dev_event')} (probe)")
            row = con.execute("SELECT * FROM transcripts WHERE transcript_id=?", (tid,)).fetchone()
            text = load_text(con, tid, allow_sealed=True)          # structure statistics only below
            seg = segment(text)
            t = {"transcript_id": tid, "period_end_date": e.get("period_end_date"), "sealed": bool(row["sealed"]),
                 "provider_created_at": row["provider_created_at"], "release_date": e.get("release_date"),
                 **seg.stats(text)}
            if row["provider_created_at"] and e.get("release_date"):
                t["available_days_after_release"] = (dt.date.fromisoformat(row["provider_created_at"][:10]) -
                                                     dt.date.fromisoformat(e["release_date"][:10])).days
            if not row["sealed"]:
                _sample(out_dir / "samples" / f"{sym.replace('.', '_')}_{tid}.md", sym, e, text, seg)
                if llm is not None:
                    from .pipeline import extract_questions
                    t["extraction"] = extract_questions(con, llm, tid)
            s["transcripts"].append(t)
            log(f"  {tid} pe={e.get('period_end_date')} sealed={bool(row['sealed'])} chars={t['n_chars']} "
                f"exch={t['n_exchanges']} flags={t['flags']}")
        s["coverage_latest2"] = sum(1 for t in s["transcripts"] if "error" not in t) / 2
    st = arch.client.stats
    lat = st["latency_ms"]
    rep["rate_limit"] = {"requests": st["requests"], "http_429": st["429"], "http_402": st["402"], "retried_errors": st["errors"],
                         "min_interval_s": arch.client.min_interval,
                         "latency_ms_median": int(statistics.median(lat)) if lat else None,
                         "last_headers_seen": st["rl_headers"]}
    rep["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    (out_dir / "probe_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out_dir / "probe_report.md").write_text(report_md(rep), encoding="utf-8")
    return rep


def _sample(path: Path, sym, e, text, seg) -> None:
    L = [f"# {sym} {e.get('period_end_date')} — segmentation sample (unsealed)", "",
         f"chars={len(text)} newlines={text.count(chr(10))} qa_start={seg.qa_start} ({seg.qa_start_rule}) "
         f"closing={seg.closing_at} exchanges={len(seg.exchanges)} flags={seg.flags}", ""]
    if seg.qa_start:
        L += ["## 200 chars before / after the Q&A start", "```", text[max(0, seg.qa_start - 200):seg.qa_start + 200], "```", ""]
    for ex in seg.exchanges[:3]:
        L += [f"## exchange {ex.idx} (rule {ex.rule}, {ex.end - ex.start} chars, question starts +{ex.question_start - ex.start})",
              "```", exchange_text(text, ex, 700), "```", ""]
    if seg.exchanges:
        last = seg.exchanges[-1]
        L += ["## last exchange tail (closing detection)", "```", text[max(last.start, last.end - 400):last.end + 300], "```"]
    path.write_text("\n".join(L), encoding="utf-8")


def report_md(rep: dict) -> str:
    L = ["# ROIC probe report", "", f"{rep['started_at']} → {rep.get('finished_at')}", "",
         "| 标的 | identifier | 列表事件 | 可用逐字稿 | 标签口径 | 对齐(dev) | 字符 | 换行 | Q&A 起点 | 交接段数 | 中位段长 | 截断占比 | 收尾识别 | flags |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for sym, s in rep["symbols"].items():
        if "error" in s:
            L.append(f"| {sym} | ERROR {s['error'][:60]} |" + " |" * 12)
            continue
        conv = ",".join(sorted({e["label_convention"] for e in s.get("events", [])})) or "-"
        al = s.get("alignment") or {}
        al_s = f"{al.get('status')}/{al.get('method')} Δpe={al.get('period_end_diff_days')} Δrel={al.get('release_vs_announce_days')}" if al else "-"
        ts = [t for t in s.get("transcripts", []) if "error" not in t] or [{}]
        for i, t in enumerate(ts):
            head = f"| {sym} | {s.get('identifier')} | {s.get('n_listed')} | {len([x for x in s.get('transcripts', []) if 'error' not in x])} | {conv} | {al_s} |" \
                if i == 0 else "| | | | | | |"
            L.append(head + f" {t.get('n_chars')} | {t.get('n_newlines')} | {t.get('qa_start_rule')} | {t.get('n_exchanges')} | "
                     f"{t.get('median_exchange_chars')} | {t.get('share_truncated')} | {t.get('closing_found')} | {t.get('flags')} |")
    r = rep["rate_limit"]
    L += ["", f"**限流**：{r['requests']} 次请求，429 × {r['http_429']}，402 × {r['http_402']}，间隔 {r['min_interval_s']} s，"
              f"中位延迟 {r['latency_ms_median']} ms；最后看到的限流头 {r['last_headers_seen']}", "",
          "抽查：`probe/samples/*.md`（只为未封存的逐字稿生成）。"]
    return "\n".join(L)
