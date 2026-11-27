"""Calibrating the match judge itself: blind pair sampling, label import, Cohen's kappa.

DeepSeek wrote the watch items, extracted the analyst questions and judges the matches — it may
prefer its own phrasing (self-preference). Annotators therefore label
(watch item, analyst question) pairs against the VERBATIM transcript text, never the LLM
paraphrase, and never see the judge's level. The sample is stratified (judge-positive / same
subject but judge-negative / random) because at a ~10% hit prevalence a random sample would be
almost all zeros and kappa would be meaningless.
"""
from __future__ import annotations

import csv
import json
import random
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .db import kv_get
from .roic import load_text
from .segment import segment

CALIB_DIR_NAME = "calib"


def _pairs(con: sqlite3.Connection, run: str) -> List[dict]:
    """All (system watch item, verified analyst question) pairs with the judge's level (0 if unlisted)."""
    out = []
    keys = [r[0] for r in con.execute("SELECT DISTINCT event_key FROM matches WHERE run=? AND arm='system'", (run,))]
    for key in keys:
        rows = [dict(r) for r in con.execute("SELECT * FROM matches WHERE run=? AND event_key=? AND arm='system'", (run, key))]
        tid = rows[0]["transcript_id"]
        items = {}
        lv = defaultdict(int)
        for r in rows:
            items[r["item_id"]] = (r["item_text"], r["item_subject"])
            if r["qid"]:
                lv[(r["item_id"], r["qid"])] = max(lv[(r["item_id"], r["qid"])], r["level"] or 0)
        qs = [dict(q) for q in con.execute("SELECT * FROM questions WHERE transcript_id=? AND quote_ok=1", (tid,))]
        for iid, (itext, isubj) in items.items():
            for q in qs:
                level = lv.get((iid, q["qid"]), 0)
                stratum = "pos" if level >= 1 else ("near" if q["subject"] == isubj else "rand")
                out.append({"event_key": key, "transcript_id": tid, "item_id": iid, "item_text": itext,
                            "item_subject": isubj, "qid": q["qid"], "q_subject": q["subject"], "quote": q["quote"],
                            "exchange_idx": q["exchange_idx"], "judge_level": level, "stratum": stratum})
    return out


def sample(con: sqlite3.Connection, er: sqlite3.Connection, run: str, out_dir: Path, n: int = 50, seed: int = 7,
           shares=(("pos", 0.4), ("near", 0.3), ("rand", 0.3))) -> Tuple[Path, Path]:
    from . import erdb
    pairs = _pairs(con, run)
    if not pairs:
        raise RuntimeError("no match rows for this run — run `match` first")
    rng = random.Random(seed)
    by = defaultdict(lambda: defaultdict(list))
    for p in pairs:
        by[p["stratum"]][p["event_key"]].append(p)
    picked: List[dict] = []
    want = {s: int(round(n * f)) for s, f in shares}
    want["rand"] += n - sum(want.values())

    def draw(stratum: str, k: int) -> int:
        evs = {e: lst[:] for e, lst in by[stratum].items()}
        for lst in evs.values():
            rng.shuffle(lst)
        order = sorted(evs)
        rng.shuffle(order)
        got = 0
        while got < k and any(evs.values()):
            for e in order:                      # round-robin over events: spread the clusters
                if got >= k:
                    break
                if evs[e]:
                    p = evs[e].pop()
                    if p not in picked:
                        picked.append(p)
                        got += 1
        return got

    short = 0
    for s, _ in shares:
        short += want[s] - draw(s, want[s])
    for s in ("rand", "near", "pos"):            # fill shortfalls
        if short <= 0:
            break
        short -= draw(s, short)
    rng.shuffle(picked)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{run}_n{len(picked)}_s{seed}"
    pairs_path, key_path = out_dir / f"pairs_{tag}.csv", out_dir / f"key_{tag}.csv"
    if key_path.exists():
        raise FileExistsError(f"{key_path} exists; use another --seed")
    texts: Dict[str, str] = {}
    why = {}
    for p in picked:
        if p["transcript_id"] not in texts:
            texts[p["transcript_id"]] = load_text(con, p["transcript_id"])
        if p["event_key"] not in why:
            why[p["event_key"]] = {w["id"]: w.get("why") for w in erdb.watch_items(er, p["event_key"])}
    segs = {t: segment(x) for t, x in texts.items()}
    with open(pairs_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["pair_id", "symbol", "watch_item", "watch_item_why", "analyst_quote", "exchange_excerpt", "label", "note"])
        for i, p in enumerate(picked, 1):
            p["pair_id"] = f"P{i:03d}"
            text = texts[p["transcript_id"]]
            ex = next((e for e in segs[p["transcript_id"]].exchanges if e.idx == p["exchange_idx"]), None)
            excerpt = text[ex.start:min(ex.end, ex.start + 900)].strip() if ex else ""
            w.writerow([p["pair_id"], p["event_key"].split(":")[0], p["item_text"], why[p["event_key"]].get(p["item_id"]) or "",
                        p["quote"], excerpt, "", ""])
    with open(key_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pair_id", "event_key", "item_id", "qid", "judge_level", "stratum", "item_subject", "q_subject"])
        for p in picked:
            w.writerow([p["pair_id"], p["event_key"], p["item_id"], p["qid"], p["judge_level"], p["stratum"],
                        p["item_subject"], p["q_subject"]])
    return pairs_path, key_path


# ----------------------------------------------------------------------
# kappa
# ----------------------------------------------------------------------
def cohen(a: List[int], b: List[int], cats: List[int], weights: Optional[str] = None) -> Optional[float]:
    n = len(a)
    if n == 0:
        return None
    k = len(cats)
    ix = {c: i for i, c in enumerate(cats)}
    obs = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b):
        obs[ix[x]][ix[y]] += 1 / n
    pa = [sum(obs[i]) for i in range(k)]
    pb = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    if weights == "linear":
        wt = lambda i, j: abs(i - j) / (k - 1)
    else:
        wt = lambda i, j: 0.0 if i == j else 1.0
    do = sum(wt(i, j) * obs[i][j] for i in range(k) for j in range(k))
    de = sum(wt(i, j) * pa[i] * pb[j] for i in range(k) for j in range(k))
    if de == 0:
        return 1.0 if do == 0 else None
    return round(1 - do / de, 4)


def _boot(clusters: Dict[str, List[Tuple[int, int]]], fn, iters: int = 2000, seed: int = 0):
    keys = sorted(clusters)
    if len(keys) < 3:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(iters):
        a, b = [], []
        for k in (rng.choice(keys) for _ in keys):
            for x, y in clusters[k]:
                a.append(x)
                b.append(y)
        v = fn(a, b)
        if v is not None:
            vals.append(v)
    vals.sort()
    if not vals:
        return None
    return [round(vals[int(0.025 * len(vals))], 4), round(vals[min(len(vals) - 1, int(0.975 * len(vals)))], 4)]


def read_labels(path: Path) -> Dict[str, int]:
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            v = (r.get("label") or "").strip()
            if v in ("0", "1", "2"):
                out[r["pair_id"]] = int(v)
    return out


def agreement(keyrows: List[dict], la: Dict[str, int], lb: Dict[str, int]) -> dict:
    ids = [r["pair_id"] for r in keyrows if r["pair_id"] in la and r["pair_id"] in lb]
    ev = {r["pair_id"]: r["event_key"] for r in keyrows}
    st = {r["pair_id"]: r["stratum"] for r in keyrows}
    a, b = [la[i] for i in ids], [lb[i] for i in ids]
    bin2 = lambda xs: [int(x >= 2) for x in xs]
    bin1 = lambda xs: [int(x >= 1) for x in xs]
    clusters = defaultdict(list)
    for i in ids:
        clusters[ev[i]].append((la[i], lb[i]))
    conf = [[0] * 3 for _ in range(3)]
    for x, y in zip(a, b):
        conf[x][y] += 1
    per_stratum = {}
    for s in ("pos", "near", "rand"):
        sel = [i for i in ids if st.get(i) == s]
        if sel:
            per_stratum[s] = {"n": len(sel), "agree_hit2": round(sum((la[i] >= 2) == (lb[i] >= 2) for i in sel) / len(sel), 4)}
    return {
        "n": len(ids), "n_events": len(clusters),
        "kappa_hit2": cohen(bin2(a), bin2(b), [0, 1]),
        "kappa_hit2_ci95": _boot(clusters, lambda x, y: cohen(bin2(x), bin2(y), [0, 1])),
        "kappa_hit1": cohen(bin1(a), bin1(b), [0, 1]),
        "kappa_weighted_3level": cohen(a, b, [0, 1, 2], "linear"),
        "kappa_weighted_ci95": _boot(clusters, lambda x, y: cohen(x, y, [0, 1, 2], "linear")),
        "pct_agree_3level": round(sum(x == y for x, y in zip(a, b)) / len(a), 4) if a else None,
        "prevalence_hit2": [round(sum(bin2(a)) / len(a), 4), round(sum(bin2(b)) / len(b), 4)] if a else None,
        "confusion_rows_first_cols_second": conf,
        "per_stratum": per_stratum,
    }


def kappa_report(key_path: Path, label_paths: List[Path], gate: float = 0.7, min_n: int = 30,
                 allow_model_reference: bool = False) -> dict:
    with open(key_path, newline="", encoding="utf-8") as f:
        keyrows = list(csv.DictReader(f))
    judge = {r["pair_id"]: int(r["judge_level"]) for r in keyrows}
    labels = {p.stem.replace("labels_", ""): read_labels(p) for p in label_paths}
    rep = {"key": key_path.name, "gate": gate, "min_n": min_n, "annotators": {k: len(v) for k, v in labels.items()},
           "judge_vs": {}, "between_annotators": {}}
    for name, lab in labels.items():
        rep["judge_vs"][name] = agreement(keyrows, judge, lab)
    names = sorted(labels)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            rep["between_annotators"][f"{names[i]}~{names[j]}"] = agreement(keyrows, labels[names[i]], labels[names[j]])
    ref = next((n for n in names if "human" in n or "gold" in n), names[0] if names else None)
    rep["reference"] = ref
    if ref:
        r = rep["judge_vs"][ref]
        human = "human" in ref or "gold" in ref
        ok = r["n"] >= min_n and r["kappa_hit2"] is not None and r["kappa_hit2"] >= gate
        # the gate is a human-vs-judge agreement; model labels (e.g. Claude) can only give a provisional pass
        rep["gate_result"] = {"reference": ref, "kappa_hit2": r["kappa_hit2"], "n": r["n"],
                              "pass": ok and (human or allow_model_reference),
                              "provisional": ok and not human and allow_model_reference,
                              "reference_is_human": human}
    return rep


def report_md(rep: dict) -> str:
    L = [f"# 匹配裁判校准报告（{rep['key']}）", ""]
    g = rep.get("gate_result")
    if g:
        L.append(f"**门槛**：κ(命中=2) ≥ {rep['gate']}，n ≥ {rep['min_n']}；参照标注 = `{g['reference']}`"
                 f"（{'人工' if g['reference_is_human'] else '模型标注，非人工'}）→ "
                 f"κ = {g['kappa_hit2']}，n = {g['n']} → **{'PASS（临时：参照为模型标注）' if g.get('provisional') else 'PASS' if g['pass'] else 'FAIL'}**")
        if not g["reference_is_human"] and not g["pass"]:
            L.append("（参照不是人工标注：门槛按定义只认人工 / 人工裁决的 gold 标注；需要临时结论时加 --allow-model-reference）")
        L.append("")
    L += ["| 对比 | n | 事件数 | κ 命中(=2) | 95% CI | κ ≥1 | 加权 κ 三档 | 95% CI | 三档一致率 | 命中占比 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for title, block in [(f"裁判 vs {k}", v) for k, v in rep["judge_vs"].items()] + \
                        [(k, v) for k, v in rep["between_annotators"].items()]:
        L.append(f"| {title} | {block['n']} | {block['n_events']} | {block['kappa_hit2']} | {block['kappa_hit2_ci95']} | "
                 f"{block['kappa_hit1']} | {block['kappa_weighted_3level']} | {block['kappa_weighted_ci95']} | "
                 f"{block['pct_agree_3level']} | {block['prevalence_hit2']} |")
    L += ["", "混淆矩阵（行 = 前者，列 = 后者，0/1/2）与分层一致率见 JSON。",
          "注意：样本按裁判结果分层抽取（pos/near/rand），κ 描述的是这个分层样本上的一致性，不是全体配对的一致性。"]
    return "\n".join(L)


def calibration_status(calib_dir: Path, run: str) -> dict:
    """Latest kappa report for this match run (key files are named key_<run>_n.._s..)."""
    reps = sorted(calib_dir.glob(f"kappa_{run}_*.json"), key=lambda p: p.stat().st_mtime)
    if not reps:
        return {"status": "uncalibrated", "run": run}
    rep = json.loads(reps[-1].read_text(encoding="utf-8"))
    g = rep.get("gate_result") or {}
    st = "calibrated" if g.get("pass") and not g.get("provisional") else \
        "provisional" if g.get("provisional") else "failed"
    return {"status": st, "run": run, "report": reps[-1].name, "kappa_hit2": g.get("kappa_hit2"), "n": g.get("n"),
            "reference": g.get("reference")}
