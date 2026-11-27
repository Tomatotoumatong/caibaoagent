import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_transcript(analysts=(("Ann Lee", "Morgan Stanley", "How should we think about gross margin into the fourth quarter?"),
                              ("Bob Tan", "Goldman Sachs", "Can you talk about data center demand visibility next year?"),
                              ("Cy Wu", "UBS", "What is your capex plan for next year?"))):
    intro = ("Good afternoon and welcome to the Example Corp third quarter fiscal 2027 earnings conference call. "
             "We will take questions after the prepared remarks. ") + "Revenue grew strongly this quarter. " * 80
    qa = "At this time, we will now begin the question-and-answer session. "
    for i, (name, firm, q) in enumerate(analysts):
        lead = "Our first question comes from the line of" if i == 0 else "Our next question comes from the line of"
        qa += (f"{lead} {name} with {firm}. Please go ahead. Thanks for taking my question. {q} "
               f"Thank you. So the answer is that we see continued strength and we remain disciplined. " * 1
               + "Management explains at length. " * 15)
    qa += "There are no further questions at this time. I would like to turn the call back to management for closing remarks. Thank you."
    return intro + qa


ER_SCHEMA = """
CREATE TABLE cards (card_id TEXT PRIMARY KEY, event_key TEXT, symbol TEXT, side TEXT, stage TEXT, version INTEGER,
  as_of TEXT, created_at TEXT, status TEXT, content TEXT, evidence_ids TEXT, validation TEXT, llm_calls TEXT);
CREATE TABLE scores (event_key TEXT PRIMARY KEY, symbol TEXT, scored_at TEXT, mode TEXT, metrics TEXT);
CREATE TABLE sp500_members (symbol TEXT PRIMARY KEY, cik TEXT, security TEXT, gics_sector TEXT, gics_sub_industry TEXT,
  date_added TEXT, snapshot_at TEXT);
CREATE TABLE companies (cik TEXT PRIMARY KEY, name TEXT, tickers TEXT, exchanges TEXT, sic TEXT, fiscal_year_end TEXT, updated_at TEXT);
CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, created_at TEXT, task TEXT, model TEXT, thinking INTEGER, status TEXT, attempts INTEGER);
"""


def add_card(con, key, stage, content, status="ok", validation=None, calls=()):
    sym = key.split(":")[0]
    side = "left" if stage in ("static", "frozen", "numbers") else "right"
    con.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{key}:{stage}:v1", key, sym, side, stage, 1, "2026-11-01", "2026-11-01", status,
                 json.dumps(content, ensure_ascii=False), "[]",
                 json.dumps(validation or {"status": status, "number_violations": [], "citation_errors": []}),
                 json.dumps(list(calls))))


@pytest.fixture
def er_db(tmp_path):
    p = tmp_path / "radar.sqlite"
    con = sqlite3.connect(p)
    con.executescript(ER_SCHEMA)
    con.execute("INSERT INTO sp500_members VALUES ('EXM','0000000001','Example','Information Technology','Semiconductors','','')")
    con.execute("INSERT INTO companies VALUES ('0000000001','EXAMPLE','[\"EXM\"]','[\"Nasdaq\"]','','0125','')")
    key = "EXM:2026-10"
    wi = [{"id": "W1", "question": "第三季度毛利率是否会维持在 60% 及以上？", "why": "稼动率", "check": {"metric": "gross_margin_pct", "op": ">=", "value": 60}},
          {"id": "W2", "question": "数据中心营收是否环比增长？", "why": "需求", "check": None},
          {"id": "W3", "question": "公司是否宣布新的回购计划？", "why": "资本回报", "check": None}]
    add_card(con, key, "static", {"context": {"company": {"cik": "0000000001", "gics_sector": "Information Technology",
                                                          "gics_sub_industry": "Semiconductors"}},
                                  "static": {"watch_items": wi}})
    add_card(con, key, "frozen", {"event": {"fiscal_period": "2026-10", "anchors": {"announce_date": "2026-11-18", "hour": "amc"}},
                                  "watch_items": wi}, status="degraded",
             validation={"status": "degraded", "number_violations": [{"number": "37.5%"}], "citation_errors": []}, calls=[1])
    add_card(con, key, "R0", {"actuals": {"revenue": 100.0e6, "eps_gaap": 1.44, "eps_diluted": 2.0, "eps_basis": "non_gaap",
                                          "gross_margin_pct": 60.0, "operating_margin_pct": 30.0},
                              "answers": {"answers": [{"watch_id": "W1", "status": "yes", "source": "code", "agree": True},
                                                      {"watch_id": "W2", "status": "yes", "source": "llm"},
                                                      {"watch_id": "W3", "status": "not_disclosed", "source": "llm"}]}}, calls=[2])
    add_card(con, key, "R2", {"period_end": "2026-10-26",
                              "xbrl": {"revenue": {"value": 100.0e6, "approx": False}, "eps_diluted": {"value": 1.50, "approx": True},
                                       "gross_profit": {"value": 60.05e6, "approx": False},
                                       "operating_income": {"value": 25.0e6, "approx": False}}})
    add_card(con, key, "reflection", {"lessons": []})
    con.execute("INSERT INTO llm_calls VALUES (1,'', 'left_freeze','m',1,'ok',2)")
    con.execute("INSERT INTO llm_calls VALUES (2,'', 'extract_release','m',0,'ok',1)")
    con.commit()
    con.close()
    return p


EVENTS = [
    {"id": "evt_cur", "symbol": "NASDAQ:EXM", "fiscal_year": 2027, "fiscal_quarter": 3, "period_end_date": "2026-10-26",
     "release_date": "2026-11-18", "transcripts": [{"id": "trraw_cur"}, {"id": "tredit_cur"}]},
    {"id": "evt_pri", "symbol": "NASDAQ:EXM", "fiscal_year": 2027, "fiscal_quarter": 2, "period_end_date": "2026-07-27",
     "release_date": "2026-08-26", "transcripts": [{"id": "trraw_pri"}]},
]


class FakeRoic:
    """transport(url, headers, params, timeout) for RoicClient."""

    def __init__(self, texts=None, fail_first_429=False):
        self.calls = []
        self.texts = texts or {"trraw_cur": make_transcript(),
                               "trraw_pri": make_transcript((("Dee Ray", "Citi", "What drove the gross margin decline this quarter?"),
                                                             ("Eve Ko", "BofA", "How is China demand trending?")))}
        self.fail_first_429 = fail_first_429

    def __call__(self, url, headers, params, timeout):
        self.calls.append((url, dict(headers), dict(params)))
        if self.fail_first_429:
            self.fail_first_429 = False
            return 429, {"error": {"type": "rate_limit_error", "code": "rate_limited"}}, {"Retry-After": "1"}, ""
        if url.endswith("/company-events"):
            if params.get("identifier") != "NASDAQ:EXM":
                return 400, {"error": {"type": "invalid_request_error", "code": "bad_identifier"}}, {}, ""
            return 200, {"data": EVENTS, "next_page_url": None}, {"X-RateLimit-Limit": "5", "X-RateLimit-Remaining": "4"}, ""
        m = re.search(r"/transcripts/(trraw_\w+)/raw", url)
        if m:
            tid = m.group(1)
            ev = EVENTS[0] if tid == "trraw_cur" else EVENTS[1]
            return 200, {"id": tid, "event": ev["id"], "symbol": "NASDAQ:EXM", "fiscal_year": ev["fiscal_year"],
                         "fiscal_quarter": ev["fiscal_quarter"], "period_end_date": ev["period_end_date"],
                         "created_at": ev["release_date"] + "T23:00:00Z", "edition": "raw", "text": self.texts[tid]}, {}, ""
        return 404, {"error": {"type": "not_found"}}, {}, ""


class FakeLLM:
    """transport(url, headers, body) for qajudge.llm.LLM — deterministic toy extraction + judge."""

    def __init__(self):
        self.n = 0

    def __call__(self, url, headers, body):
        self.n += 1
        sys_, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if "ANALYST" in sys_:
            out = []
            for m in re.finditer(r"### exchange (\d+)\n(.*?)(?=\n### exchange|\nReturn JSON)", user, re.S):
                idx, txt = int(m.group(1)), m.group(2)
                q = re.search(r"Thanks for taking my question\. ([^?]+\?)", txt)
                qs = []
                if q:
                    t = q.group(1)
                    subj = "gross_margin" if "margin" in t else "segment_product" if "data center" in t else \
                        "geography" if "China" in t else "cash_flow_capex"
                    qs.append({"text": t, "quote": t, "subject": subj})
                out.append({"x": idx, "analyst": None, "firm": None, "questions": qs})
            obj = {"exchanges": out}
        else:
            items = json.loads(re.search(r"观察项：\n(.*?)\n\n分析师问题", user, re.S).group(1))
            qs = json.loads(re.search(r"以原文为准）：\n(.*?)\n\n按规则", user, re.S).group(1))
            res = []
            for it in items:
                ms = []
                for q in qs:
                    t = q["question"].lower()
                    if ("毛利" in it["text"] or "gross margin" in it["text"].lower()) and "margin" in t:
                        ms.append({"q": q["id"], "level": 2, "why": "同一个量"})
                    elif ("数据中心" in it["text"] or "data center" in it["text"].lower()) and "data center" in t:
                        ms.append({"q": q["id"], "level": 2, "why": "同一分部"})
                    elif "营收" in it["text"] and "demand" in t:
                        ms.append({"q": q["id"], "level": 1, "why": "总量 vs 分部"})
                res.append({"id": it["id"], "subject": "gross_margin" if "毛利" in it["text"] else "other", "matches": ms})
            obj = {"items": res}
        return 200, {"choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
