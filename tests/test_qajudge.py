import csv
import json
import os
import stat

import pytest

from conftest import EVENTS, FakeLLM, FakeRoic, make_transcript
from qajudge import calib, erdb, freeze as fz, metrics, pipeline
from qajudge.align import align_event, derive_fiscal, label_convention
from qajudge.db import connect, kv_get
from qajudge.llm import LLM
from qajudge.roic import Archive, RoicClient, identifier_candidates, load_text, should_seal, unseal
from qajudge.segment import quote_in, segment


# ---------------------------------------------------------------- segmentation
def test_segment_finds_exchanges_and_closing():
    text = make_transcript()
    seg = segment(text)
    assert seg.qa_start_rule in ("qa_session", "take_questions")
    assert len(seg.exchanges) == 3
    assert seg.closing_at is not None and seg.closing_at > seg.exchanges[-1].start
    st = seg.stats(text)
    assert st["share_go_ahead"] == 1.0 and st["share_cue_names_firm"] == 1.0
    assert "too_short" not in seg.flags and "no_qa_found" not in seg.flags
    first = text[seg.exchanges[0].start:seg.exchanges[0].end]
    assert "Ann Lee" in first and "Bob Tan" not in first


def test_segment_intro_mention_of_questions_is_not_the_qa():
    text = "We will take questions at the end. " + "Prepared remarks. " * 400
    assert segment(text).exchanges == []


def test_quote_check_is_whitespace_and_quote_insensitive():
    src = "Thanks. How should we think about “gross margin”   into Q4?"
    assert quote_in('How should we think about "gross margin" into Q4?', src)
    assert not quote_in("How should we think about operating margin into Q4?", src)
    assert not quote_in("short", src)


# ---------------------------------------------------------------- ROIC client
def test_client_identifier_fallback_429_and_no_key_leak(tmp_path):
    con = connect(tmp_path / "qa.sqlite")
    fake = FakeRoic(fail_first_429=True)
    sleeps = []
    cl = RoicClient("sk-secret-123", con=con, transport=fake, min_interval=12.5, sleep=sleeps.append)
    arch = Archive(con, cl, tmp_path / "raw")
    ident, events, attempts = arch.resolve("EXM", ["Nasdaq"])
    assert ident == "NASDAQ:EXM" and len(events) == 2
    assert fake.calls[0][1]["Authorization"] == "Bearer sk-secret-123"
    assert all("sk-secret" not in json.dumps(c[2]) and "sk-secret" not in c[0] for c in fake.calls)
    logged = json.dumps([dict(r) for r in con.execute("SELECT * FROM http_log")])
    assert "sk-secret" not in logged
    assert cl.stats["429"] == 1 and any(s >= 1 for s in sleeps)
    # cached identifier is tried first next time
    fake.calls.clear()
    arch.resolve("EXM", None)
    assert fake.calls[0][2]["identifier"] == "NASDAQ:EXM"


def test_identifier_candidates_share_class():
    c = identifier_candidates("BF.B", None)
    assert c[:4] == ["NYSE:BF.B", "NYSE:BF-B", "NASDAQ:BF.B", "NASDAQ:BF-B"] and "BF.B" in c
    assert identifier_candidates("NVDA", ["Nasdaq"])[0] == "NASDAQ:NVDA"


def test_bad_key_rejected():
    with pytest.raises(ValueError):
        RoicClient("sk-你的key")


# ---------------------------------------------------------------- alignment / sealing
def test_derive_fiscal_and_labels():
    assert derive_fiscal("2026-10-26", "0125") == (2027, 3)      # NVDA
    assert derive_fiscal("2026-09-30", "0630") == (2027, 1)      # MSFT
    assert derive_fiscal("2026-09-30", "1231") == (2026, 3)      # TXN
    assert derive_fiscal("2026-11-01", "1101") == (2026, 4)      # ADI-like 52/53 week: first days count as October
    assert label_convention(EVENTS[0], "0125") == "fiscal"
    assert label_convention({**EVENTS[0], "fiscal_year": 2026, "fiscal_quarter": 4}, "0125") == "calendar"


def test_align_by_dates_and_prior():
    er = {"event_key": "EXM:2026-10", "fiscal_period": "2026-10", "announce_date": "2026-11-18", "period_end": "2026-10-26"}
    cur, prior, d = align_event(er, EVENTS)
    assert cur["id"] == "evt_cur" and prior["id"] == "evt_pri" and d["method"] == "period_end_date"
    assert d["prior_gap_days"] == 91 and d["release_vs_announce_days"] == 0
    cur, _, d = align_event({**er, "period_end": None}, EVENTS)
    assert cur["id"] == "evt_cur" and d["method"] == "period_month"


def test_seal_policy_and_ledger(tmp_path):
    assert should_seal(EVENTS[0], is_dev_call=True) is False
    assert should_seal(EVENTS[0], is_dev_call=False) is True          # Q3 season call, not dev -> clean sample
    assert should_seal(EVENTS[1], is_dev_call=False) is False         # released 2026-08-26 < cutoff -> history
    con = connect(tmp_path / "qa.sqlite")
    arch = Archive(con, RoicClient("k" * 10, con=con, transport=FakeRoic(), min_interval=0), tmp_path / "raw")
    arch.fetch("EXM", EVENTS[0], sealed=True)
    with pytest.raises(PermissionError):
        load_text(con, "trraw_cur")
    with pytest.raises(ValueError):
        unseal(con, ["trraw_cur"], "  ")
    assert unseal(con, ["trraw_cur"], "test") == 1
    assert "question-and-answer" in load_text(con, "trraw_cur")
    assert kv_get(con, "unseal_ledger")[0]["reason"] == "test"


# ---------------------------------------------------------------- end to end
@pytest.fixture
def pipe(tmp_path, er_db):
    con = connect(tmp_path / "qa.sqlite")
    er = erdb.open_ro(er_db)
    arch = Archive(con, RoicClient("k" * 10, con=con, transport=FakeRoic(), min_interval=0), tmp_path / "raw")
    llm = LLM("sk-test-key", con, transport=FakeLLM())
    recs = pipeline.archive_dev(con, er, arch, log=lambda *a: None)
    return con, er, llm, recs


def test_archive_dev_unseals_only_the_dev_call(pipe):
    con, er, llm, recs = pipe
    assert recs[0]["current"] == "trraw_cur" and recs[0]["prior"] == "trraw_pri"
    seals = dict(con.execute("SELECT transcript_id, sealed FROM transcripts"))
    assert seals == {"trraw_cur": 0, "trraw_pri": 0}
    assert kv_get(con, "unseal_ledger", []) == []          # fetched unsealed directly: nothing to lift


def test_extract_match_value_and_cache(pipe):
    con, er, llm, recs = pipe
    r = pipeline.extract_questions(con, llm, "trraw_cur")
    assert r["n_exchanges"] == 3 and r["n_questions"] == 3 and r["n_quote_ok"] == 3
    pipeline.extract_questions(con, llm, "trraw_pri")
    n_before = llm.transport.n
    assert pipeline.extract_questions(con, llm, "trraw_cur").get("cached")
    assert llm.transport.n == n_before
    run = pipeline.run_id("deepseek-flash")
    ev = erdb.dev_events(er)[0]
    out = pipeline.match_event(con, er, llm, run, ev, log=lambda *a: None)
    assert out["status"] == "ok" and out["system"] == 3 and out["template"] == 3
    rep = metrics.judge(er, con, run)
    v = rep["events"]["EXM:2026-10"]["V"]
    assert v["system"]["hit2"] == pytest.approx(2 / 3, abs=1e-3)       # gross margin + data center
    assert v["system"]["q_coverage2"] == pytest.approx(2 / 3, abs=1e-3)
    assert v["prior_q"]["k"] == 2                                        # only 2 prior questions exist
    assert v["template"]["hit2"] == pytest.approx(1 / 3, abs=1e-3)       # 毛利率 template line
    assert rep["summary"]["V_value"]["system_minus_template_hit2"]["mean"] == pytest.approx(1 / 3, abs=1e-3)


def test_abc_pairs(pipe):
    con, er, llm, recs = pipe
    rep = metrics.judge(er, None, None)
    e = rep["events"]["EXM:2026-10"]
    A = e["A"]
    assert A["fields"]["eps_gaap"]["match"] is None and A["n_triage"] == 1          # approx XBRL gap -> triage
    assert A["fields"]["gross_margin_pct"]["match"] is True                         # 60.05 vs 60.0 within 0.1pp
    assert A["fields"]["operating_margin_pct"]["match"] is False                    # 30 vs 25 -> real mismatch
    assert A["correctness"] == pytest.approx(2 / 3, abs=1e-3) and A["coverage"] == 1.0
    B = e["B"]
    assert B["n_cards"] == 5 and B["n_clean"] == 4 and B["core_ok"] == 3 and B["llm_calls_retried"] == 1
    C = e["C"]
    assert C["coverage"] == pytest.approx(2 / 3, abs=1e-3) and C["correctness"] == 1.0 and C["n_checkable"] == 1


# ---------------------------------------------------------------- calibration
def test_kappa_values():
    assert calib.cohen([1, 1, 0, 0], [1, 0, 0, 0], [0, 1]) == 0.5
    assert calib.cohen([0, 1, 2], [0, 1, 2], [0, 1, 2], "linear") == 1.0
    # linear weights: one-step disagreements cost half
    k = calib.cohen([0, 0, 1, 2, 2, 1], [0, 1, 1, 2, 1, 1], [0, 1, 2], "linear")
    assert 0 < k < 1


def test_calib_sample_blind_and_kappa(pipe, tmp_path):
    con, er, llm, recs = pipe
    for t in ("trraw_cur", "trraw_pri"):
        pipeline.extract_questions(con, llm, t)
    run = pipeline.run_id("deepseek-flash")
    pipeline.match_event(con, er, llm, run, erdb.dev_events(er)[0], log=lambda *a: None)
    pp, kp = calib.sample(con, er, run, tmp_path / "calib", n=6, seed=1)
    with open(pp, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 6 and "judge_level" not in rows[0] and "stratum" not in rows[0]
    assert all(r["analyst_quote"] and r["exchange_excerpt"] for r in rows)
    with pytest.raises(FileExistsError):
        calib.sample(con, er, run, tmp_path / "calib", n=6, seed=1)
    with open(kp, encoding="utf-8") as f:
        key = list(csv.DictReader(f))
    # an annotator who agrees with the judge everywhere
    lab = tmp_path / "calib" / "labels_human.csv"
    with open(lab, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["pair_id", "label"])
        for r in key:
            w.writerow([r["pair_id"], r["judge_level"]])
    rep = calib.kappa_report(kp, [lab], gate=0.7, min_n=5)
    assert rep["judge_vs"]["human"]["pct_agree_3level"] == 1.0
    assert rep["gate_result"]["pass"] and rep["gate_result"]["reference_is_human"]
    assert "PASS" in calib.report_md(rep)


# ---------------------------------------------------------------- freeze
def test_freeze_is_write_once_and_compare(pipe, tmp_path, monkeypatch, er_db):
    con, er, llm, recs = pipe
    monkeypatch.setattr(fz, "BASE_DIR", tmp_path / "baselines")
    rep = metrics.judge(er, None, None)
    p = fz.freeze("base1", rep, er_db, "deepseek-flash", None, [])
    assert not os.stat(p).st_mode & stat.S_IWUSR
    with pytest.raises(FileExistsError):
        fz.freeze("base1", rep, er_db, "deepseek-flash", None, [])
    fz.freeze("cand1", rep, er_db, "deepseek-flash", None, [])
    c = fz.compare(fz.load(str(p)), fz.load(str(tmp_path / "baselines" / "cand1.json")))
    assert c["metrics"]["A.correctness"]["mean_delta"] == 0 and not c["warnings"]
    meta = fz.load(str(p))["meta"]
    assert meta["er_db_sha256"] and meta["materials"]["rubric"]


def test_gate_needs_human_reference(pipe, tmp_path):
    con, er, llm, recs = pipe
    for t in ("trraw_cur", "trraw_pri"):
        pipeline.extract_questions(con, llm, t)
    run = pipeline.run_id("deepseek-flash")
    pipeline.match_event(con, er, llm, run, erdb.dev_events(er)[0], log=lambda *a: None)
    cdir = tmp_path / "calib"
    pp, kp = calib.sample(con, er, run, cdir, n=6, seed=2)
    with open(kp, encoding="utf-8") as f:
        key = list(csv.DictReader(f))
    lab = cdir / "labels_claude.csv"
    with open(lab, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["pair_id", "label"])
        for r in key:
            w.writerow([r["pair_id"], r["judge_level"]])
    rep = calib.kappa_report(kp, [lab], min_n=5)
    assert rep["gate_result"]["pass"] is False and not rep["gate_result"]["reference_is_human"]
    rep2 = calib.kappa_report(kp, [lab], min_n=5, allow_model_reference=True)
    assert rep2["gate_result"]["pass"] and rep2["gate_result"]["provisional"]
    (cdir / f"kappa_{kp.stem.replace('key_', '')}.json").write_text(json.dumps(rep2))
    assert calib.calibration_status(cdir, run)["status"] == "provisional"


def test_probe_report(tmp_path, er_db):
    from qajudge import probe
    con = connect(tmp_path / "qa.sqlite")
    er = erdb.open_ro(er_db)
    arch = Archive(con, RoicClient("k" * 10, con=con, transport=FakeRoic(), min_interval=0), tmp_path / "raw")
    rep = probe.run(con, er, arch, ["EXM", "ZZZ"], tmp_path / "probe", log=lambda *a: None)
    e = rep["symbols"]["EXM"]
    assert e["identifier"] == "NASDAQ:EXM" and e["alignment"]["status"] == "matched"
    assert {t["sealed"] for t in e["transcripts"]} == {False} and e["coverage_latest2"] == 1.0
    assert all(t["n_exchanges"] >= 2 for t in e["transcripts"])
    assert rep["symbols"]["ZZZ"]["identifier"] is None and rep["symbols"]["ZZZ"]["n_listed"] == 0
    assert len(list((tmp_path / "probe" / "samples").glob("*.md"))) == 2
    md = (tmp_path / "probe" / "probe_report.md").read_text(encoding="utf-8")
    assert "NASDAQ:EXM" in md and "fiscal" in md


# ---------------------------------------------------------------- probe 2026-11-26 regressions
def _call(qa_body, closing="That wraps up the Q&A portion of today's earnings call. This concludes today's conference."):
    return "Welcome to the call. " + "Prepared remarks about the quarter. " * 300 + qa_body + closing


def test_late_qa_phrase_does_not_discard_cues():
    # MSFT: no opening Q&A phrase, only "wraps up the Q&A portion" at the very end
    body = ("Our first question comes from the line of Adam Wood with Morgan Stanley. Please proceed. What about Azure margins? "
            + "Answer. " * 200 + "Operator, next question, please. The next question comes from the line of Karl Keirstead with UBS. "
            "Please proceed. How is capex trending? " + "Answer. " * 200 + "Operator, next question, please. Great, thanks for "
            "taking my question. What about security revenue? " + "Answer. " * 200)
    seg = segment(_call(body))
    assert [e.rule for e in seg.exchanges] == ["question_from", "next_please", "next_please"]
    assert seg.qa_start_rule == "first_cue" and seg.closing_at is not None


def test_more_handoff_phrasings_and_closings():
    body = ("One moment for our next question. Our first question is going to come from the line of Drew Levine with JPMorgan. "
            "Your line is open. What about pricing? " + "Answer. " * 150 +
            "With that, looks like we have Keith up for the next question. Keith? Can you talk about bookings? " + "Answer. " * 150 +
            "Gabriela, welcome. We'll take your question now. What about headcount? " + "Answer. " * 150)
    seg = segment(_call(body, "Ladies and gentlemen, this does conclude today's Example Corp conference call. You may now disconnect."))
    assert [e.rule for e in seg.exchanges] == ["question_from", "up_for_next", "take_your_question"]
    assert seg.closing_at is not None and "no_closing" not in seg.flags


def test_queue_stub_is_merged_into_first_exchange():
    body = ("With that, operator, we'll take the first question. We will now begin Q&A. Please raise your hand. "
            "We will now pause a moment to assemble the queue. Your first question will come from Brent Thill with Jefferies. "
            "Please go ahead. What else excites you? " + "Answer. " * 150 +
            "Your next question will come from Brad Zelnick with Deutsche Bank. How do usage metrics translate? " + "Answer. " * 150)
    seg = segment(_call(body))
    assert len(seg.exchanges) == 2


def test_long_exchange_keeps_late_followups_and_quotes_check_full_text():
    from qajudge.segment import exchange_full, exchange_text
    body = ("Our first question comes from the line of Ann Lee with UBS. Please go ahead. How is demand? " + "Long answer. " * 400 +
            "And just a quick follow-up on gross margin into next year? " + "Answer. " * 50)
    text = _call(body)
    seg = segment(text)
    ex = seg.exchanges[0]
    short = exchange_text(text, ex)
    assert "follow-up on gross margin into next year?" in short and "[...]" in short
    assert quote_in("And just a quick follow-up on gross margin into next year?", exchange_full(text, ex))


def test_known_identifier_without_calls_stops_the_search(tmp_path):
    class NoCalls(FakeRoic):
        def __call__(self, url, headers, params, timeout):
            self.calls.append((url, dict(headers), dict(params)))
            if params.get("identifier") == "NYSE:BRK.B":
                return 200, {"data": [], "next_page_url": None}, {}, ""
            return 404, {"error": {"type": "invalid_request_error", "code": "resource_missing"}}, {}, ""
    con = connect(tmp_path / "qa.sqlite")
    fake = NoCalls()
    arch = Archive(con, RoicClient("k" * 10, con=con, transport=fake, min_interval=0), tmp_path / "raw")
    ident, events, attempts = arch.resolve("BRK.B", None)
    assert ident == "NYSE:BRK.B" and events == [] and len(fake.calls) == 1
    assert kv_get(con, "roic_id:BRK.B")["no_calls"] is True
