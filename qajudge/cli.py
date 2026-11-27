"""python -m qajudge <command> — see README.md for the order to run them in."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import calib, erdb, freeze as fz, metrics, pipeline, probe
from .config import ROOT, settings
from .db import connect, kv_get
from .llm import LLMError


def _ctx(need_roic=False, need_llm=False):
    s = settings()
    con = connect(s.qa_db)
    er = erdb.open_ro(s.er_db)
    arch = llm = None
    if need_roic:
        from .roic import Archive, RoicClient
        arch = Archive(con, RoicClient(s.roic_api_key, con=con, min_interval=s.roic_min_interval), s.raw_dir)
    if need_llm:
        from .llm import LLM
        llm = LLM(s.deepseek_api_key, con, model=s.model, base_url=s.deepseek_base_url)
    return s, con, er, arch, llm


def _dev_maps(con, er):
    return [kv_get(con, f"dev_map:{e['event_key']}") or {"event_key": e["event_key"]} for e in erdb.dev_events(er)]


def cmd_probe(a):
    s, con, er, arch, llm = _ctx(need_roic=True, need_llm=a.llm)
    syms = a.symbols.split(",") if a.symbols else probe.DEFAULT_SYMBOLS
    rep = probe.run(con, er, arch, syms, ROOT / a.out, llm=llm)
    print((ROOT / a.out / "probe_report.md").read_text(encoding="utf-8"))
    return 0 if rep else 1


def cmd_seg_report(a):
    """Re-run the segmenter on every archived transcript, offline (no requests). Sealed ones get
    content-free statistics only."""
    from .roic import load_text
    from .segment import segment
    s, con, er, _, _ = _ctx()
    L = ["| 标的 | 季度末 | 封存 | 字符 | Q&A 起点 | 交接段数 | 中位段长 | 截断占比 | 收尾识别 | flags |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in con.execute("SELECT transcript_id, symbol, period_end_date, sealed FROM transcripts ORDER BY symbol, period_end_date"):
        try:
            text = load_text(con, r["transcript_id"], allow_sealed=True)
        except FileNotFoundError:
            L.append(f"| {r['symbol']} | {r['period_end_date']} | {bool(r['sealed'])} | raw file missing |" + " |" * 6)
            continue
        st = segment(text).stats(text)
        L.append(f"| {r['symbol']} | {r['period_end_date']} | {bool(r['sealed'])} | {st['n_chars']} | {st['qa_start_rule']} | "
                 f"{st['n_exchanges']} | {st['median_exchange_chars']} | {st['share_truncated']} | {st['closing_found']} | {st['flags']} |")
    out = ROOT / "probe" / "seg_report.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


def cmd_archive_dev(a):
    s, con, er, arch, _ = _ctx(need_roic=True)
    recs = pipeline.archive_dev(con, er, arch)
    ok = sum(1 for r in recs if r.get("current"))
    print(f"dev events with their own call archived: {ok}/{len(recs)}; with prior call: "
          f"{sum(1 for r in recs if r.get('prior'))}/{len(recs)}")


def cmd_archive(a):
    s, con, er, arch, _ = _ctx(need_roic=True)
    if a.sp500:
        syms = [r[0] for r in er.execute("SELECT symbol FROM sp500_members ORDER BY symbol")]
    elif a.symbols:
        syms = a.symbols.split(",")
    else:
        syms = [l.strip() for l in Path(a.symbols_file).read_text().splitlines() if l.strip()]
    print(f"{len(syms)} symbols; ~{len(syms) * 3 * s.roic_min_interval / 3600:.1f} h at most at {s.roic_min_interval}s/request "
          f"(already archived transcripts are skipped; anything released after 2026-10-17 is stored sealed)")
    pipeline.archive_symbols(con, er, arch, syms)


def cmd_extract(a):
    s, con, er, _, llm = _ctx(need_llm=True)
    tids = []
    for r in _dev_maps(con, er):
        tids += [t for t in (r.get("current"), r.get("prior")) if t]
    tot = {"transcripts": 0, "questions": 0, "quote_ok": 0}
    for tid in dict.fromkeys(tids):
        try:
            res = pipeline.extract_questions(con, llm, tid, force=a.force)
        except (PermissionError, LLMError) as exc:
            print(f"skip {tid}: {exc}")
            continue
        tot["transcripts"] += 1
        tot["questions"] += res.get("n_questions") or 0
        tot["quote_ok"] += res.get("n_quote_ok") or 0
        print(json.dumps(res, ensure_ascii=False))
    print("TOTAL", tot, "llm", llm.calls)


def cmd_match(a):
    s, con, er, _, llm = _ctx(need_llm=True)
    run = pipeline.run_id(s.model)
    print("run", run)
    for ev in erdb.dev_events(er):
        try:
            pipeline.match_event(con, er, llm, run, ev)
        except LLMError as exc:
            print(f"{ev['event_key']}: judge failed, skipped: {exc}")
    print("llm", llm.calls)


def _report(s, con, er, with_value: bool):
    run = pipeline.run_id(s.model) if with_value else None
    if run and not con.execute("SELECT 1 FROM matches WHERE run=? LIMIT 1", (run,)).fetchone():
        print(f"(no match rows for run {run}: value metric omitted — run `match` first)")
        run = None
    rep = metrics.judge(er, con if run else None, run)
    if run:
        cs = calib.calibration_status(ROOT / "calib", run)
        rep["summary"].setdefault("V_value", {})["calibration"] = cs
        if cs["status"] != "calibrated":
            print(f"!! value metric is {cs['status'].upper()} for run {run}: the match judge has not passed the "
                  f"human kappa gate — treat V as diagnostic, not as an evaluation result")
    return rep, run


def cmd_judge(a):
    s, con, er, _, _ = _ctx()
    rep, run = _report(s, con, er, not a.no_value)
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    p = out / f"judge_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    p.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(rep["summary"], ensure_ascii=False, indent=1))
    print("written", p)


def cmd_freeze(a):
    s, con, er, _, _ = _ctx()
    rep, run = _report(s, con, er, not a.no_value)
    cal = (rep["summary"].get("V_value") or {}).get("calibration") or {}
    if run and cal.get("status") != "calibrated" and not a.allow_uncalibrated:
        print("value metric not calibrated -> frozen WITHOUT it (use --allow-uncalibrated to keep it, marked)")
        rep, run = _report(s, con, er, False)
    tids = [t for r in _dev_maps(con, er) for t in (r.get("current"), r.get("prior")) if t] if run else []
    p = fz.freeze(a.name, rep, s.er_db, s.model, run, tids)
    print(json.dumps(rep["summary"], ensure_ascii=False, indent=1))
    print("frozen (read-only):", p)


def cmd_compare(a):
    print(json.dumps(fz.compare(fz.load(a.base), fz.load(a.cand)), ensure_ascii=False, indent=1))


def cmd_calib_sample(a):
    s, con, er, _, _ = _ctx()
    run = pipeline.run_id(s.model)
    pp, kp = calib.sample(con, er, run, ROOT / "calib", n=a.n, seed=a.seed)
    print("pairs to label (blind):", pp)
    print("key (do NOT open before labelling is done):", kp)


def cmd_calib_kappa(a):
    rep = calib.kappa_report(Path(a.key), [Path(p) for p in a.labels], gate=a.gate, min_n=a.min_n,
                             allow_model_reference=a.allow_model_reference)
    stem = Path(a.key).stem.replace("key_", "")
    out = ROOT / "calib" / f"kappa_{stem}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    md = calib.report_md(rep)
    (ROOT / "calib" / f"kappa_{stem}.md").write_text(md, encoding="utf-8")
    print(md)
    print("written", out)


def cmd_status(a):
    s, con, er, _, _ = _ctx()
    q = lambda sql: con.execute(sql).fetchone()[0]
    print(json.dumps({
        "er_db": str(s.er_db), "qa_db": str(s.qa_db), "dev_events": len(erdb.dev_events(er)),
        "transcripts": q("SELECT COUNT(*) FROM transcripts"), "sealed": q("SELECT COUNT(*) FROM transcripts WHERE sealed=1"),
        "questions": q("SELECT COUNT(*) FROM questions"), "questions_quote_ok": q("SELECT COUNT(*) FROM questions WHERE quote_ok=1"),
        "match_runs": [r[0] for r in con.execute("SELECT DISTINCT run FROM matches")],
        "current_run": pipeline.run_id(s.model),
        "http_requests": q("SELECT COUNT(*) FROM http_log"), "http_429": q("SELECT COUNT(*) FROM http_log WHERE status=429"),
        "unseal_ledger_entries": len(kv_get(con, "unseal_ledger", [])),
    }, ensure_ascii=False, indent=1))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="qajudge")
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("probe"); x.add_argument("--symbols"); x.add_argument("--llm", action="store_true"); x.add_argument("--out", default="probe"); x.set_defaults(f=cmd_probe)
    x = sub.add_parser("seg-report"); x.set_defaults(f=cmd_seg_report)
    x = sub.add_parser("archive-dev"); x.set_defaults(f=cmd_archive_dev)
    x = sub.add_parser("archive"); g = x.add_mutually_exclusive_group(required=True); g.add_argument("--symbols"); g.add_argument("--symbols-file")
    g.add_argument("--sp500", action="store_true", help="all current S&P 500 members from the ER database"); x.set_defaults(f=cmd_archive)
    x = sub.add_parser("extract"); x.add_argument("--force", action="store_true"); x.set_defaults(f=cmd_extract)
    x = sub.add_parser("match"); x.set_defaults(f=cmd_match)
    x = sub.add_parser("judge"); x.add_argument("--no-value", action="store_true"); x.set_defaults(f=cmd_judge)
    x = sub.add_parser("freeze"); x.add_argument("--name", required=True); x.add_argument("--no-value", action="store_true")
    x.add_argument("--allow-uncalibrated", action="store_true"); x.set_defaults(f=cmd_freeze)
    x = sub.add_parser("compare"); x.add_argument("--base", required=True); x.add_argument("--cand", required=True); x.set_defaults(f=cmd_compare)
    x = sub.add_parser("calib-sample"); x.add_argument("--n", type=int, default=50); x.add_argument("--seed", type=int, default=7); x.set_defaults(f=cmd_calib_sample)
    x = sub.add_parser("calib-kappa"); x.add_argument("--key", required=True); x.add_argument("--labels", nargs="+", required=True)
    x.add_argument("--gate", type=float, default=0.7); x.add_argument("--min-n", type=int, default=30)
    x.add_argument("--allow-model-reference", action="store_true"); x.set_defaults(f=cmd_calib_kappa)
    x = sub.add_parser("status"); x.set_defaults(f=cmd_status)
    a = p.parse_args(argv)
    return a.f(a) or 0


if __name__ == "__main__":
    sys.exit(main())
