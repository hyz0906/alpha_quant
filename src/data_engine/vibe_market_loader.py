"""
VibeMarketLoader：通过 vibe-trading 的多源回退链获取 A 股/ETF 日线，
落盘 CSV 并 upsert 进 alphaquant.db 的 market_data 表。

替代关系（Design.md [P1-02] 的实现调整）：
  原：TushareLoader（需要 TUSHARE_TOKEN）
  新：vibe-trading fetch_market_data（默认 tencent 源，免 token，前复权）
  TushareLoader 保留为备用，未删除。

调用链：
  main.py fetch_data
    -> VibeMarketLoader.fetch_and_sync(codes, start, end)
      -> subprocess(cwd=$HOME): python scripts/vibe_fetch_broker.py ...
      -> 读 data/<code>.csv -> upsert market_data
"""
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from config.logging_config import setup_logging
from src.database.connection import engine
from src.database.models import MarketData

logger = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class VibeMarketLoader:
    """用 vibe-trading 数据层替代 tushare（免 token）。"""

    def __init__(self, source: str = "auto", data_dir: Path = None):
        self.source = source
        self.data_dir = data_dir or PROJECT_ROOT / "data"

    # ------------------------------------------------------------------
    # 对外主接口
    # ------------------------------------------------------------------
    def fetch_and_sync(self, codes: list[str], start: str, end: str) -> dict:
        """拉取日线并同步进 DB。日期格式 YYYY-MM-DD。

        Returns: {code: rows_written}
        """
        manifest = self.fetch_csvs(codes, start, end)
        written = {}
        for code in manifest.get("files", {}):
            df = self.read_csv(code)
            if df is not None and not df.empty:
                written[code] = self.upsert_market_data(code, df)
        return written

    # ------------------------------------------------------------------
    # 步骤 1：子进程调 vibe-trading 拉数据 -> CSV
    # ------------------------------------------------------------------
    def fetch_csvs(self, codes: list[str], start: str, end: str) -> dict:
        broker = PROJECT_ROOT / "scripts" / "vibe_fetch_broker.py"
        if not broker.exists():
            raise FileNotFoundError(f"broker script missing: {broker}")

        cmd = [
            sys.executable, "-X", "utf8", str(broker),
            "--codes", *codes,
            "--start", start,
            "--end", end,
            "--out-dir", str(self.data_dir),
            "--source", self.source,
        ]
        # 关键：cwd 必须是 $HOME（不含 src/ 目录），否则 import src
        # 会解析到本项目的 src 包而不是 vibe-trading 的。
        logger.info(f"[vibe-loader] launching broker: {' '.join(cmd[:6])} ...")
        proc = subprocess.run(
            cmd,
            cwd=str(Path.home()),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.stdout:
            for line in proc.stdout.strip().splitlines():
                logger.info(f"[broker] {line}")
        if proc.returncode != 0:
            logger.error(f"[vibe-loader] broker stderr: {proc.stderr[-2000:]}")
            raise RuntimeError(
                f"vibe fetch broker failed (exit={proc.returncode}); "
                f"see stderr above. partial failures are tolerated only "
                f"when manifest exists."
            )
        manifest_path = self.data_dir / "manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("broker produced no manifest.json")
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # 步骤 2：CSV -> DataFrame
    # ------------------------------------------------------------------
    def read_csv(self, code: str) -> pd.DataFrame | None:
        path = self.data_dir / f"{code}.csv"
        if not path.exists():
            logger.warning(f"[vibe-loader] csv not found: {path}")
            return None
        df = pd.read_csv(path, parse_dates=["date"])
        if df.empty:
            return df
        df = df.sort_values("date").set_index("date")
        needed = {"open", "high", "low", "close"}
        missing = needed - set(df.columns)
        if missing:
            logger.error(f"[vibe-loader] {code} missing columns: {missing}")
            return None
        if "volume" not in df.columns:
            df["volume"] = 0.0
        return df

    # ------------------------------------------------------------------
    # 步骤 3：DataFrame -> DB upsert
    # ------------------------------------------------------------------
    def upsert_market_data(self, ts_code: str, df: pd.DataFrame) -> int:
        records = [
            MarketData(
                ts_code=ts_code,
                trade_date=idx.date(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                vol=float(row.get("volume", 0.0) or 0.0),
            )
            for idx, row in df.iterrows()
        ]
        with Session(engine) as session:
            for rec in records:
                session.merge(rec)  # (ts_code, trade_date) 联合主键幂等
            session.commit()
        logger.success(f"[vibe-loader] upserted {len(records)} rows for {ts_code}")
        return len(records)
