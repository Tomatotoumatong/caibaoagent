"""roic.ai v3 client (earnings-call listing + raw transcripts) and the transcript archive.

Facts from roic.ai/api/docs (checked 2026-11-26) the code relies on:
  GET /v3.0.0/company-events?identifier=NASDAQ:AAPL&content=transcript&order=desc&limit=..
      -> {data: [{id: evt_.., symbol, fiscal_year, fiscal_quarter, period_end_date, release_date,
                  transcripts: [{id: trraw_.., type, ..}, {id: tredit_..}], ...}], next_page_url}
  GET /v3.0.0/documents/transcripts/{trraw_id}/raw -> {id, event, symbol, fiscal_year, fiscal_quarter,
      period_end_date, created_at, updated_at, edition: "raw", text}
  auth: exactly one of `Authorization: Bearer <key>` or ?apikey= — we use the header, so the key
  never appears in a URL or a log line.
  Free plan: 5 requests / sliding 60 s, listing + raw transcript for the latest 2 calls per company,
  no edited (speaker-labelled) transcript. 429 -> wait Retry-After; 402 -> not in plan.
Anything the docs did not pin down (identifier spelling for odd tickers, label convention of
fiscal_year/quarter) is what `probe` measures.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import random
import sqlite3
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

BASE = "https://api.roic.ai"
EVENTS = "/v3.0.0/company-events"
RAW = "/v3.0.0/documents/transcripts/{id}/raw"

# transport(url, headers, params, timeout) -> (status, json-or-None, response headers, text)
Transport = Callable[[str, dict, dict, float], Tuple[int, Optional[dict], dict, str]]


def requests_transport() -> Transport:
    import requests

    s = requests.Session()

    def get(url, headers, params, timeout):
        r = s.get(url, headers=headers, params=params, timeout=timeout)
        try:
            body = r.json()
        except ValueError:
            body = None
        return r.status_code, body, dict(r.headers), r.text[:500]

    return get


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class RoicError(RuntimeError):
    def __init__(self, status: int, etype: str = "", code: str = "", message: str = ""):
        super().__init__(f"HTTP {status} {etype}/{code}: {message[:200]}")
        self.status, self.etype, self.code = status, etype, code


class RoicClient:
    def __init__(self, api_key: str, con: Optional[sqlite3.Connection] = None,
                 transport: Optional[Transport] = None, min_interval: float = 12.5,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 max_429: int = 3, timeout: float = 60.0):
        key = (api_key or "").strip()
        if not key or any(ord(c) > 126 or ord(c) < 33 for c in key):
            raise ValueError("ROIC_API_KEY missing or contains non-ASCII/whitespace characters")
        self._key = key
        self.con = con
        self.transport = transport or requests_transport()
        self.min_interval = min_interval
        self._sleep, self._clock = sleep, clock
        self._last = None
        self.max_429 = max_429
        self.timeout = timeout
        self.stats = {"requests": 0, "429": 0, "402": 0, "errors": 0, "latency_ms": [], "rl_headers": {}}

    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _log(self, path, params, status, latency, h, etype=None, code=None) -> None:
        g = lambda k: h.get(k) or h.get(k.lower())
        rl = {k: g(k) for k in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After")}
        for k, v in rl.items():
            if v is not None:
                self.stats["rl_headers"][k] = v
        if self.con is None:
            return
        self.con.execute(
            "INSERT INTO http_log (at, path, params, status, latency_ms, rl_limit, rl_remaining, rl_reset, "
            "retry_after, error_type, error_code) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), path, json.dumps(params, sort_keys=True), status, latency, rl["X-RateLimit-Limit"],
             rl["X-RateLimit-Remaining"], rl["X-RateLimit-Reset"], rl["Retry-After"], etype, code))
        self.con.commit()

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        n429, nerr, backoff = 0, 0, 5.0
        while True:
            self._throttle()
            t0 = time.monotonic()
            try:
                status, body, h, text = self.transport(BASE + path, headers, params, self.timeout)
            except Exception as exc:  # network
                status, body, h, text = -1, None, {}, str(exc)
            lat = int((time.monotonic() - t0) * 1000)
            self.stats["requests"] += 1
            self.stats["latency_ms"].append(lat)
            err = (body or {}).get("error") or {}
            self._log(path, params, status, lat, h, err.get("type"), err.get("code"))
            if status == 200 and isinstance(body, dict):
                return body
            if status == 429 and n429 < self.max_429:
                n429 += 1
                self.stats["429"] += 1
                ra = h.get("Retry-After") or h.get("retry-after")
                wait = float(ra) if ra and str(ra).replace(".", "", 1).isdigit() else 60.0
                self._sleep(min(wait * (1.5 ** (n429 - 1)), 180.0) + random.uniform(0, 2))
                continue
            if status in (-1, 500, 502, 503, 504) and nerr < 2:
                nerr += 1
                self.stats["errors"] += 1
                self._sleep(backoff)
                backoff *= 2
                continue
            if status == 402:
                self.stats["402"] += 1
            raise RoicError(status, err.get("type", ""), err.get("code", ""), err.get("message", text or ""))

    def list_events(self, identifier: str, limit: int = 10) -> List[dict]:
        body = self.get(EVENTS, {"identifier": identifier, "content": "transcript", "order": "desc", "limit": limit})
        return list(body.get("data") or [])

    def raw_transcript(self, transcript_id: str) -> dict:
        if not transcript_id.startswith("trraw_"):
            raise ValueError(f"not a raw transcript id: {transcript_id}")
        return self.get(RAW.format(id=transcript_id))


# ----------------------------------------------------------------------
# identifiers
# ----------------------------------------------------------------------
def identifier_candidates(symbol: str, exchanges_hint: Optional[List[str]] = None) -> List[str]:
    """ROIC wants EXCHANGE:TICKER (docs example NASDAQ:AAPL). Exchange order from the SEC
    company record when we have it; share classes (BF.B) are tried with '.' and '-'."""
    hint = " ".join(exchanges_hint or []).upper()
    order = ["NASDAQ", "NYSE"] if "NASDAQ" in hint else ["NYSE", "NASDAQ"]
    variants = [symbol] + ([symbol.replace(".", "-")] if "." in symbol else [])
    out = [f"{ex}:{v}" for ex in order for v in variants] + variants
    return list(dict.fromkeys(out))


def raw_transcript_id(event: dict) -> Optional[str]:
    for t in event.get("transcripts") or []:
        tid = t.get("id") or ""
        if tid.startswith("trraw_"):
            return tid
    return None


def write_gz(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    tmp.replace(path)


def read_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


class Archive:
    """Raw responses are written to disk before the database row (the options-snapshot rule):
    a crash can leave an orphan file, never a row without its file."""

    def __init__(self, con: sqlite3.Connection, client: Optional[RoicClient], raw_dir: Path):
        self.con, self.client, self.raw_dir = con, client, raw_dir

    def resolve(self, symbol: str, exchanges_hint: Optional[List[str]] = None, limit: int = 10
                ) -> Tuple[Optional[str], List[dict], List[dict]]:
        """-> (identifier, events, attempts). Cached per symbol; a cached miss is retried only
        when called with a different hint."""
        from .db import kv_get, kv_set
        cached = kv_get(self.con, f"roic_id:{symbol}")
        cands = identifier_candidates(symbol, exchanges_hint)
        if cached and cached.get("identifier"):
            cands = [cached["identifier"]] + [c for c in cands if c != cached["identifier"]]
        attempts = []
        for ident in cands:
            try:
                events = self.client.list_events(ident, limit=limit)
            except RoicError as exc:
                attempts.append({"identifier": ident, "status": exc.status, "error": f"{exc.etype}/{exc.code}"})
                if exc.status in (401, 403):
                    raise
                continue
            attempts.append({"identifier": ident, "status": 200, "n_events": len(events)})
            if not events:
                # unknown identifiers answer 404 (probe 2026-11-26: NYSE:BRK-B 404, NYSE:BRK.B 200 + []),
                # so 200 with no events = the company is known and holds no calls: stop trying.
                kv_set(self.con, f"roic_id:{symbol}", {"identifier": ident, "at": now_iso(), "no_calls": True})
                return ident, [], attempts
            if events:
                self._store_events(symbol, ident, events)
                kv_set(self.con, f"roic_id:{symbol}", {"identifier": ident, "at": now_iso()})
                return ident, events, attempts
        kv_set(self.con, f"roic_id:{symbol}", {"identifier": None, "at": now_iso(), "attempts": attempts})
        return None, [], attempts

    def _store_events(self, symbol: str, ident: str, events: List[dict]) -> None:
        at = now_iso()
        write_gz(self.raw_dir / symbol.replace("/", "_") / f"events_{at.replace(':', '')}.json.gz",
                 {"identifier": ident, "listed_at": at, "data": events})
        for e in events:
            self.con.execute(
                "INSERT INTO roic_events (symbol, event_id, identifier, fiscal_year, fiscal_quarter, period_end_date, "
                "release_date, raw_transcript_id, listed_at, payload) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol, event_id) DO UPDATE SET raw_transcript_id=excluded.raw_transcript_id, "
                "listed_at=excluded.listed_at, payload=excluded.payload, release_date=excluded.release_date",
                (symbol, e.get("id"), ident, e.get("fiscal_year"), e.get("fiscal_quarter"), e.get("period_end_date"),
                 e.get("release_date"), raw_transcript_id(e), at, json.dumps(e, ensure_ascii=False)))
        self.con.commit()

    def have(self, transcript_id: str) -> bool:
        r = self.con.execute("SELECT raw_path FROM transcripts WHERE transcript_id=?", (transcript_id,)).fetchone()
        return bool(r and Path(r[0]).exists())

    def fetch(self, symbol: str, event: dict, sealed: bool) -> Optional[str]:
        """Archive one raw transcript. `sealed=False` only for dev-set events; an existing row
        keeps the stricter of its old and new seal (unsealing is a separate, logged act)."""
        tid = raw_transcript_id(event)
        if not tid:
            return None
        if self.have(tid):
            return tid  # an existing seal is never lifted here; see unseal()
        body = self.client.raw_transcript(tid)
        text = body.get("text") or ""
        path = self.raw_dir / symbol.replace("/", "_") / f"{tid}.json.gz"
        write_gz(path, body)
        self.con.execute(
            "INSERT INTO transcripts (transcript_id, symbol, event_id, fiscal_year, fiscal_quarter, period_end_date, "
            "provider_created_at, fetched_at, n_chars, text_sha256, raw_path, sealed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, symbol, body.get("event") or event.get("id"), body.get("fiscal_year"), body.get("fiscal_quarter"),
             body.get("period_end_date") or event.get("period_end_date"), body.get("created_at"), now_iso(),
             len(text), hashlib.sha256(text.encode()).hexdigest(), str(path), int(bool(sealed))))
        self.con.commit()
        return tid


CUTOFF = "2026-10-17"   # same clean-sample line as the PEAD study: earlier calls are history


def should_seal(event: dict, is_dev_call: bool) -> bool:
    """Unsealed: the call aligned to a dev-set event (see align.align_event), and any call released
    before CUTOFF (history, e.g. the 'prior quarter' baseline). Everything else — the untouched
    Q3 events and the forward sample — is sealed."""
    if is_dev_call:
        return False
    rel = event.get("release_date")
    if not rel and event.get("period_end_date"):
        rel = (dt.date.fromisoformat(event["period_end_date"]) + dt.timedelta(days=45)).isoformat()
    return not (rel and rel < CUTOFF)


def load_text(con: sqlite3.Connection, transcript_id: str, allow_sealed: bool = False) -> str:
    r = con.execute("SELECT raw_path, sealed FROM transcripts WHERE transcript_id=?", (transcript_id,)).fetchone()
    if r is None:
        raise KeyError(transcript_id)
    if r["sealed"] and not allow_sealed:
        raise PermissionError(f"{transcript_id} belongs to the sealed clean sample; unseal it explicitly first")
    return read_gz(Path(r["raw_path"])).get("text") or ""


def unseal(con: sqlite3.Connection, transcript_ids: List[str], reason: str) -> int:
    """Unsealing is recorded in kv `unseal_ledger` with the reason — the clean-sample ledger."""
    from .db import kv_get, kv_set
    if not reason.strip():
        raise ValueError("a reason is required to unseal")
    n = 0
    for tid in transcript_ids:
        n += con.execute("UPDATE transcripts SET sealed=0 WHERE transcript_id=? AND sealed=1", (tid,)).rowcount
    if n:
        led = kv_get(con, "unseal_ledger", [])
        led.append({"at": now_iso(), "transcripts": transcript_ids, "reason": reason, "n": n})
        kv_set(con, "unseal_ledger", led)
    con.commit()
    return n
