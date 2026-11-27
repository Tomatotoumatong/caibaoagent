"""Read-only access to the earnings_radar database (cards, scores, companies)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional


def open_ro(path: Path) -> sqlite3.Connection:
    if not Path(path).exists():
        raise FileNotFoundError(f"earnings_radar database not found: {path} (set ER_DB)")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("SELECT 1 FROM cards LIMIT 1")
    except sqlite3.OperationalError:
        con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)   # WAL db on a read-only mount
    con.row_factory = sqlite3.Row
    return con


def latest(con: sqlite3.Connection, key: str, stage: str) -> Optional[dict]:
    r = con.execute("SELECT * FROM cards WHERE event_key=? AND stage=? ORDER BY version DESC LIMIT 1",
                    (key, stage)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["content"] = json.loads(d["content"])
    d["validation"] = json.loads(d["validation"] or "{}")
    d["llm_calls"] = json.loads(d["llm_calls"] or "[]")
    return d


def dev_events(con: sqlite3.Connection) -> List[Dict]:
    """The dev set = every event with a frozen left card in this database."""
    out = []
    keys = [r[0] for r in con.execute("SELECT DISTINCT event_key FROM cards WHERE stage='frozen' ORDER BY event_key")]
    for key in keys:
        fr = latest(con, key, "frozen")
        st = latest(con, key, "static")
        ev = fr["content"].get("event") or {}
        anchors = ev.get("anchors") or {}
        comp = ((st or {}).get("content", {}).get("context") or {}).get("company") or {}
        sym = fr["symbol"]
        sec = con.execute("SELECT gics_sector, gics_sub_industry, cik FROM sp500_members WHERE symbol=?", (sym,)).fetchone()
        exch = None
        cik = comp.get("cik") or (sec["cik"] if sec else None)
        if cik:
            c = con.execute("SELECT exchanges FROM companies WHERE cik=?", (cik,)).fetchone()
            exch = json.loads(c[0]) if c and c[0] else None
        r2 = latest(con, key, "R2")
        out.append({
            "event_key": key, "symbol": sym, "fiscal_period": ev.get("fiscal_period") or key.split(":")[1],
            "announce_date": anchors.get("announce_date") or ev.get("report_date"), "hour": anchors.get("hour"),
            "period_end": (r2 or {}).get("content", {}).get("period_end"),
            "sector": comp.get("gics_sector") or (sec["gics_sector"] if sec else None),
            "sub_industry": comp.get("gics_sub_industry") or (sec["gics_sub_industry"] if sec else None),
            "cik": cik, "exchanges": exch,
        })
    return out


def watch_items(con: sqlite3.Connection, key: str) -> List[Dict]:
    """Watch items as frozen (falls back to the static card when the frozen one has none)."""
    for stage in ("frozen", "static"):
        c = latest(con, key, stage)
        if not c:
            continue
        w = c["content"].get("watch_items")
        if w is None and stage == "static":
            w = (c["content"].get("static") or {}).get("watch_items")
        if w:
            return [{"id": x["id"], "text": x["question"], "why": x.get("why"), "check": x.get("check")} for x in w]
    return []


def company_exchanges(con: sqlite3.Connection, symbol: str) -> Optional[list]:
    r = con.execute("SELECT c.exchanges FROM sp500_members m JOIN companies c ON c.cik=m.cik WHERE m.symbol=?",
                    (symbol,)).fetchone()
    return json.loads(r[0]) if r and r[0] else None


def llm_call_rows(con: sqlite3.Connection, ids: List[int]) -> List[dict]:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return [dict(r) for r in con.execute(f"SELECT id, task, status, attempts FROM llm_calls WHERE id IN ({q})", ids)]
