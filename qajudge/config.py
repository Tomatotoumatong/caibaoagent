"""Paths and settings. Everything comes from environment variables (optionally loaded from
`.env.qa` in the project root; values already in the environment win).

  ROIC_API_KEY         roic.ai key (free plan: 5 requests/min, latest 2 calls per company,
                       raw transcripts only — no speaker labels)
  ROIC_MIN_INTERVAL    seconds between ROIC requests (default 12.5 = 4.8/min, under the 5/min cap)
  DEEPSEEK_API_KEY     DeepSeek key (question extraction + matching judge)
  DEEPSEEK_BASE_URL    default https://api.deepseek.com
  QA_MODEL             default deepseek-flash
  ER_DB                earnings_radar database to judge (read-only), default
                       ../earnings_radar_loop/data_q3v5_r05/radar.sqlite
  QA_HOME              data directory of this project, default ./data
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    """KEY=VALUE lines; '#' comments; existing environment variables are not overridden."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


@dataclass
class Settings:
    roic_api_key: str
    roic_min_interval: float
    deepseek_api_key: str
    deepseek_base_url: str
    model: str
    er_db: Path
    home: Path

    @property
    def qa_db(self) -> Path:
        return self.home / "qa.sqlite"

    @property
    def raw_dir(self) -> Path:
        return self.home / "raw" / "roic"


def settings() -> Settings:
    load_env_file(ROOT / ".env.qa")
    e = os.environ.get
    return Settings(
        roic_api_key=e("ROIC_API_KEY", ""),
        roic_min_interval=float(e("ROIC_MIN_INTERVAL", "12.5")),
        deepseek_api_key=e("DEEPSEEK_API_KEY", ""),
        deepseek_base_url=e("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=e("QA_MODEL", "deepseek-flash"),
        er_db=Path(e("ER_DB", str(ROOT.parent / "earnings_radar_loop" / "data_q3v5_r05" / "radar.sqlite"))),
        home=Path(e("QA_HOME", str(ROOT / "data"))),
    )
