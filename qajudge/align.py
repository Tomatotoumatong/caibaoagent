"""Aligning earnings_radar events with ROIC call listings, by dates (never by labels).

earnings_radar keys events by fiscal-quarter-end month (NVDA:2026-10); ROIC lists
(fiscal_year, fiscal_quarter, period_end_date, release_date). We match on period_end_date and
report separately whether ROIC's year/quarter labels are fiscal or calendar — the 'previous
quarter' baseline is the preceding call by date, so it does not depend on the labels either."""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional, Tuple


def _d(s: Optional[str]) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def derive_fiscal(period_end: str, fye_mmdd: Optional[str]) -> Optional[Tuple[int, int]]:
    """(fiscal_year, fiscal_quarter) from a quarter-end date and the fiscal-year-end MMDD.
    52/53-week years end within a week of a month end: a date in the first 7 days counts as the
    previous month."""
    pe = _d(period_end)
    if not pe or not fye_mmdd or len(fye_mmdd) != 4:
        return None
    m_e, d_e = int(fye_mmdd[:2]), int(fye_mmdd[2:])
    if d_e <= 7:
        m_e = 12 if m_e == 1 else m_e - 1
    y, m = pe.year, pe.month
    if pe.day <= 7:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    q = ((m - m_e - 1) % 12) // 3 + 1
    fy = y if m <= m_e else y + 1
    return fy, q


def label_convention(ev: dict, fye_mmdd: Optional[str]) -> str:
    fy, fq = ev.get("fiscal_year"), ev.get("fiscal_quarter")
    pe = ev.get("period_end_date")
    if fy is None or not pe:
        return "unknown"
    fis = derive_fiscal(pe, fye_mmdd)
    if fis and (fy, fq) == fis:
        return "fiscal"
    p = _d(pe)
    if p and (fy, fq) == (p.year, (p.month - 1) // 3 + 1):
        return "calendar"
    if fq is None:
        return "annual_null_quarter"
    return "other"


def align_event(er: Dict, roic_events: List[dict], tol_days: int = 10) -> Tuple[Optional[dict], Optional[dict], dict]:
    """-> (call for this event, the call before it, diagnostics)."""
    with_tx = [e for e in roic_events if any((t.get("id") or "").startswith("trraw_") for t in e.get("transcripts") or [])]
    ann = _d(er.get("announce_date"))
    pe_exact = _d(er.get("period_end"))
    month = er.get("fiscal_period")
    cand = []
    for e in roic_events:
        pe = _d(e.get("period_end_date"))
        if pe is None:
            continue
        if pe_exact is not None:
            ok, how, diff = abs((pe - pe_exact).days) <= tol_days, "period_end_date", (pe - pe_exact).days
        else:
            ok, how, diff = e["period_end_date"][:7] == month, "period_month", None
        if not ok and ann and e.get("release_date"):
            rd = _d(e["release_date"])
            if rd and abs((rd - ann).days) <= 3:
                ok, how, diff = True, "release_date", None
        if ok:
            cand.append((e, how, diff))
    diag = {"n_listed": len(roic_events), "n_with_transcript": len(with_tx)}
    if not cand:
        diag["status"] = "no_match"
        return None, None, diag
    cur, how, diff = sorted(cand, key=lambda c: (c[1] != "period_end_date", abs(c[2] or 0)))[0]
    rd = _d(cur.get("release_date"))
    diag.update({"status": "matched", "method": how, "period_end_diff_days": diff,
                 "release_vs_announce_days": (rd - ann).days if rd and ann else None,
                 "has_transcript": cur in with_tx,
                 "roic_period_end": cur.get("period_end_date"), "roic_fy_fq": [cur.get("fiscal_year"), cur.get("fiscal_quarter")]})
    cpe = _d(cur.get("period_end_date"))
    prior = None
    for e in sorted(with_tx, key=lambda e: e.get("period_end_date") or "", reverse=True):
        pe = _d(e.get("period_end_date"))
        if pe and cpe and pe < cpe:
            prior = e
            diag["prior_gap_days"] = (cpe - pe).days
            break
    diag["prior_found"] = prior is not None
    return cur, prior, diag
