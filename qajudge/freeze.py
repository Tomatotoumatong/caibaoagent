"""Frozen baselines: a judge report plus everything needed to know what it was computed from.
A frozen file is written once, made read-only, and never overwritten."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Optional

from . import rubric
from .config import ROOT
from .metrics import boot_ci

BASE_DIR = ROOT / "baselines"


def file_sha(path: Path) -> Optional[str]:
    if not Path(path).exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_sha() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / "qajudge").glob("*.py")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def freeze(name: str, report: dict, er_db: Path, model: str, run: Optional[str], transcripts: list) -> Path:
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("baseline name: letters, digits, '-' and '_' only")
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    path = BASE_DIR / f"{name}.json"
    if path.exists():
        raise FileExistsError(f"{path} exists — frozen baselines are never overwritten; pick a new name")
    meta = {
        "name": name, "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "er_db": str(er_db), "er_db_sha256": file_sha(er_db), "er_db_bytes": os.path.getsize(er_db) if Path(er_db).exists() else None,
        "qajudge_code": code_sha(), "materials": rubric.material_hashes(), "model": model, "match_run": run,
        "transcripts_sha": hashlib.sha256(json.dumps(sorted(transcripts)).encode()).hexdigest()[:16] if transcripts else None,
        "n_transcripts": len(transcripts),
        "value_included": bool(report["summary"].get("V_value")),
    }
    path.write_text(json.dumps({"meta": meta, **report}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return path


def load(name_or_path: str) -> dict:
    p = Path(name_or_path)
    if not p.exists():
        p = BASE_DIR / f"{name_or_path}.json"
    return json.loads(p.read_text(encoding="utf-8"))


EVENT_METRICS = {
    "A.correctness": lambda e: e["A"].get("correctness"),
    "A.coverage": lambda e: e["A"].get("coverage"),
    "B.correctness": lambda e: e["B"].get("correctness"),
    "B.coverage": lambda e: e["B"].get("coverage"),
    "C.correctness": lambda e: e["C"].get("correctness"),
    "C.coverage": lambda e: e["C"].get("coverage"),
    "V.system.hit2": lambda e: ((e.get("V") or {}).get("system") or {}).get("hit2"),
    "V.system.rw_hit2": lambda e: ((e.get("V") or {}).get("system") or {}).get("rw_hit2"),
}


def compare(base: dict, cand: dict) -> dict:
    """Paired by event_key; CI is a bootstrap over events of the per-event differences."""
    warn = []
    for k in ("materials", "model"):
        if base["meta"].get(k) != cand["meta"].get(k):
            warn.append(f"{k} differs — value metrics are not comparable: {base['meta'].get(k)} vs {cand['meta'].get(k)}")
    out = {"base": base["meta"]["name"], "cand": cand["meta"]["name"], "warnings": warn, "metrics": {}}
    common = sorted(set(base["events"]) & set(cand["events"]))
    out["n_common_events"] = len(common)
    for m, f in EVENT_METRICS.items():
        pairs = []
        for k in common:
            try:
                a, b = f(base["events"][k]), f(cand["events"][k])
            except (KeyError, TypeError):
                continue
            if a is not None and b is not None:
                pairs.append(b - a)
        out["metrics"][m] = {"n": len(pairs), "mean_delta": round(sum(pairs) / len(pairs), 4) if pairs else None,
                             "ci95": boot_ci(pairs)}
    return out
