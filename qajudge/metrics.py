"""The minimal new judge: three (correctness, coverage) pairs + one value metric.

  A  extraction vs XBRL     correctness = matched / compared       coverage = compared / XBRL-available
     fields: revenue, GAAP EPS (as R2 already did) + gross margin %, operating margin % (new)
  B  number & citation      correctness = LLM cards with no number  coverage = core stages present
     discipline              violation and no citation error          with status ok / 4
  C  watch-item settling    correctness = LLM agrees with the code   coverage = items the release
                              verdict (coded items)                    settled / all items
  V  value: analyst-Q&A hit rate of the frozen watch items vs two baselines (previous call's
     questions, sector template), strict level 2, plus a rarity-weighted version.

Every pair is reported together: a correctness number alone rewards abstaining.
"""
from __future__ import annotations

import math
import random
import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional

from . import erdb

CORE_STAGES = ("static", "frozen", "R0", "reflection")
LLM_STAGES = ("static", "event", "frozen", "R0", "R2", "reflection")


def _ratio(a, b):
    return round(a / b, 4) if b else None


# ----------------------------------------------------------------------
# A, B, C — from the earnings_radar database only
# ----------------------------------------------------------------------
def extraction_vs_xbrl(er: sqlite3.Connection, key: str) -> dict:
    r0, r2 = erdb.latest(er, key, "R0"), erdb.latest(er, key, "R2")
    if not r0 or not r2:
        return {"computable": False, "reason": "no R0" if not r0 else "no R2 (10-Q/10-K not in yet)"}
    xb = r2["content"].get("xbrl") or {}
    a = r0["content"].get("actuals") or {}
    xv = lambda m: (xb.get(m) or {}).get("value")
    approx = lambda m: bool((xb.get(m) or {}).get("approx"))
    rev = xv("revenue")
    eps_g = a.get("eps_gaap")
    if eps_g is None and a.get("eps_basis") == "gaap":
        eps_g = a.get("eps_diluted")
    fields = {
        "revenue": (rev, a.get("revenue"), lambda v, x: abs(v - x) <= 0.005 * abs(x)),
        "eps_gaap": (xv("eps_diluted"), eps_g,
                     (lambda v, x: abs(v - x) <= max(0.02 * abs(x), 0.03)) if approx("eps_diluted")
                     else (lambda v, x: abs(v - x) <= max(0.005 * abs(x), 0.011))),
        "gross_margin_pct": (xv("gross_profit") / rev * 100 if xv("gross_profit") is not None and rev else None,
                             a.get("gross_margin_pct"),
                             (lambda v, x: abs(v - x) <= (0.3 if approx("gross_profit") else 0.1))),
        "operating_margin_pct": (xv("operating_income") / rev * 100 if xv("operating_income") is not None and rev else None,
                                 a.get("operating_margin_pct"),
                                 (lambda v, x: abs(v - x) <= (0.3 if approx("operating_income") else 0.1))),
    }
    src = {"revenue": "revenue", "eps_gaap": "eps_diluted", "gross_margin_pct": "gross_profit",
           "operating_margin_pct": "operating_income"}
    out = {"computable": True, "fields": {}, "n_available": 0, "n_compared": 0, "n_match": 0, "n_triage": 0,
           "mismatches": []}
    for f, (x, v, ok) in fields.items():
        d = {"xbrl": x, "extracted": v, "available": x is not None, "compared": x is not None and v is not None,
             "xbrl_approx": approx(src[f]) or (approx("revenue") and f.endswith("_pct"))}
        if d["compared"]:
            d["match"] = bool(ok(float(v), float(x)))
            if not d["match"] and d["xbrl_approx"]:
                # fiscal-Q4 values derived as FY - 9M are approximate (share counts move): a gap is
                # not evidence of a wrong extraction. Excluded from correctness, listed for triage.
                d["match"] = None
                out["n_triage"] += 1
            else:
                out["n_match"] += d["match"]
            if d["match"] is not True:
                out["mismatches"].append({"field": f, "extracted": round(float(v), 4), "xbrl": round(float(x), 4),
                                          "kind": "triage_approx_xbrl" if d["match"] is None else "mismatch"})
        out["n_available"] += d["available"]
        out["n_compared"] += d["compared"]
        out["fields"][f] = d
    out["correctness"] = _ratio(out["n_match"], out["n_compared"] - out["n_triage"])
    out["coverage"] = _ratio(out["n_compared"], out["n_available"])
    return out


def discipline(er: sqlite3.Connection, key: str) -> dict:
    present, clean, ok_core, retried = 0, 0, 0, 0
    detail = {}
    for st in LLM_STAGES:
        c = erdb.latest(er, key, st)
        if not c:
            continue
        v = c["validation"]
        is_clean = not v.get("number_violations") and not v.get("citation_errors")
        present += 1
        clean += is_clean
        if st in CORE_STAGES and c["status"] == "ok":
            ok_core += 1
        calls = erdb.llm_call_rows(er, c["llm_calls"])
        r = sum(1 for x in calls if (x.get("attempts") or 1) > 1)
        retried += r
        detail[st] = {"status": c["status"], "clean": is_clean, "llm_retried": r}
    return {"n_cards": present, "n_clean": clean, "core_ok": ok_core, "core_expected": len(CORE_STAGES),
            "correctness": _ratio(clean, present), "coverage": _ratio(ok_core, len(CORE_STAGES)),
            "llm_calls_retried": retried, "stages": detail}


def watch_settling(er: sqlite3.Connection, key: str) -> dict:
    r0 = erdb.latest(er, key, "R0")
    items = erdb.watch_items(er, key)
    if not r0:
        return {"computable": False}
    ans = ((r0["content"].get("answers") or {}).get("answers")) or []
    settled = sum(1 for x in ans if x.get("status") != "not_disclosed")
    coded = [x for x in ans if x.get("source") == "code"]
    agree = sum(1 for x in coded if x.get("agree"))
    return {"computable": True, "n_items": len(ans), "n_settled": settled, "n_coded": len(coded), "n_agree": agree,
            "n_checkable": sum(1 for w in items if w.get("check")), "n_watch": len(items),
            "coverage": _ratio(settled, len(ans)), "correctness": _ratio(agree, len(coded))}


# ----------------------------------------------------------------------
# V — value, from qa.sqlite match rows
# ----------------------------------------------------------------------
def subject_idf(con: sqlite3.Connection, run: str) -> Dict[str, float]:
    """idf over the dev pool: df(s) = number of calls whose analyst questions include subject s."""
    rows = con.execute(
        "SELECT DISTINCT m.event_key, q.subject FROM matches m JOIN questions q ON q.transcript_id=m.transcript_id "
        "WHERE m.run=? AND q.quote_ok=1", (run,)).fetchall()
    events = {r[0] for r in rows}
    df = defaultdict(int)
    for _, s in rows:
        df[s] += 1
    n = len(events)
    idf = {s: math.log((1 + n) / (1 + c)) + 1 for s, c in df.items()}
    idf["__unseen__"] = math.log(1 + n) + 1
    return idf


def value(con: sqlite3.Connection, run: str, key: str, idf: Dict[str, float]) -> dict:
    rows = [dict(r) for r in con.execute("SELECT * FROM matches WHERE run=? AND event_key=?", (run, key))]
    if not rows:
        return {"computable": False}
    tid = rows[0]["transcript_id"]
    nq = con.execute("SELECT COUNT(*) FROM questions WHERE transcript_id=? AND quote_ok=1", (tid,)).fetchone()[0]
    out = {"computable": True, "n_questions": nq}
    for arm in ("system", "prior_q", "template"):
        items = defaultdict(lambda: {"best": 0, "subject": None})
        qs2 = set()
        for r in rows:
            if r["arm"] != arm:
                continue
            it = items[r["item_id"]]
            it["subject"] = r["item_subject"]
            it["best"] = max(it["best"], r["level"] or 0)
            if (r["level"] or 0) >= 2:
                qs2.add(r["qid"])
        k = len(items)
        if not k:
            out[arm] = None
            continue
        w = lambda s: idf.get(s or "other", idf["__unseen__"])
        out[arm] = {
            "k": k,
            "hit2": round(sum(i["best"] >= 2 for i in items.values()) / k, 4),
            "hit1": round(sum(i["best"] >= 1 for i in items.values()) / k, 4),
            "rw_hit2": round(sum(w(i["subject"]) * (i["best"] >= 2) for i in items.values()) / k, 4),
            "q_coverage2": round(len(qs2) / nq, 4) if nq else None,
        }
    return out


# ----------------------------------------------------------------------
def boot_ci(diffs: List[float], iters: int = 2000, seed: int = 0) -> Optional[List[float]]:
    if len(diffs) < 3:
        return None
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(iters))
    return [round(means[int(0.025 * iters)], 4), round(means[int(0.975 * iters) - 1], 4)]


def judge(er: sqlite3.Connection, con: Optional[sqlite3.Connection], run: Optional[str]) -> dict:
    events = erdb.dev_events(er)
    idf = subject_idf(con, run) if con is not None and run else {}
    per = {}
    for ev in events:
        k = ev["event_key"]
        per[k] = {"symbol": ev["symbol"], "A": extraction_vs_xbrl(er, k), "B": discipline(er, k), "C": watch_settling(er, k)}
        if idf:
            per[k]["V"] = value(con, run, k, idf)
    return {"events": per, "summary": summarize(per), "idf": {s: round(v, 3) for s, v in idf.items()}}


def summarize(per: Dict[str, dict]) -> dict:
    s: dict = {"n_events": len(per)}
    A = [e["A"] for e in per.values() if e["A"].get("computable")]
    na, nc, nm, nt = (sum(x[f] for x in A) for f in ("n_available", "n_compared", "n_match", "n_triage"))
    byf = {}
    for f in ("revenue", "eps_gaap", "gross_margin_pct", "operating_margin_pct"):
        av = sum(x["fields"][f]["available"] for x in A)
        cp = sum(x["fields"][f]["compared"] for x in A)
        mt = sum(x["fields"][f].get("match") is True for x in A)
        tr = sum(x["fields"][f]["compared"] and x["fields"][f].get("match") is None for x in A)
        byf[f] = {"correctness": _ratio(mt, cp - tr), "coverage": _ratio(cp, av),
                  "match/compared/available/triage": f"{mt}/{cp}/{av}/{tr}"}
    s["A_extraction_vs_xbrl"] = {"correctness": _ratio(nm, nc - nt), "coverage": _ratio(nc, na), "triage": nt, "events": len(A),
                                 "events_not_computable": len(per) - len(A), "by_field": byf,
                                 "mismatches": {k: e["A"]["mismatches"] for k, e in per.items()
                                                if e["A"].get("mismatches")}}
    B = [e["B"] for e in per.values()]
    s["B_discipline"] = {"correctness": _ratio(sum(x["n_clean"] for x in B), sum(x["n_cards"] for x in B)),
                         "coverage": _ratio(sum(x["core_ok"] for x in B), sum(x["core_expected"] for x in B)),
                         "llm_calls_retried": sum(x["llm_calls_retried"] for x in B)}
    C = [e["C"] for e in per.values() if e["C"].get("computable")]
    s["C_watch_settling"] = {"correctness": _ratio(sum(x["n_agree"] for x in C), sum(x["n_coded"] for x in C)),
                             "coverage": _ratio(sum(x["n_settled"] for x in C), sum(x["n_items"] for x in C)),
                             "checkable_share": _ratio(sum(x["n_checkable"] for x in C), sum(x["n_watch"] for x in C))}
    V = {k: e["V"] for k, e in per.items() if e.get("V", {}).get("computable")}
    if V:
        # V's own coverage: the free ROIC plan cannot backfill, so an event whose call was not archived
        # in time is permanently outside V — report it instead of silently shrinking the denominator
        v: dict = {"events": len(V), "coverage_events": _ratio(len(V), len(per))}
        for arm in ("system", "prior_q", "template"):
            xs = [x[arm] for x in V.values() if x.get(arm)]
            v[arm] = {m: (round(sum(x[m] for x in xs if x[m] is not None) / len(xs), 4) if xs else None)
                      for m in ("hit2", "hit1", "rw_hit2", "q_coverage2")}
            v[arm]["n_events"] = len(xs)
        for base in ("prior_q", "template"):
            for m in ("hit2", "rw_hit2"):
                d = [x["system"][m] - x[base][m] for x in V.values() if x.get("system") and x.get(base)]
                v[f"system_minus_{base}_{m}"] = {"mean": round(sum(d) / len(d), 4) if d else None,
                                                 "ci95": boot_ci(d), "n": len(d)}
        s["V_value"] = v
    return s
