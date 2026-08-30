"""
信号生成层 [P3-03 融合 + P2 轮动]。

两类接口：
  1. generate_signal(ts_code, trade_date)
     单标的融合信号：RSRS 修正分 + 研报情感 -> STRONG_BUY / DIVERGENCE_WATCH / HOLD
     （原有接口，向后保留；供执行网关 / 监控使用）
  2. generate_rotation_signals(codes, top_k)
     批量轮动信号：全池 RSRS 修正分截面排名 -> top_k 等权组合，
     输出可直接被回测 / 执行消费的目标权重。
"""
import json
from datetime import date
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session
from loguru import logger

from src.database.connection import engine
from src.database.models import FactorData, ReportSentiment

# 本文件位于 <root>/src/strategies/，故 root = parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SignalGenerator:
    def __init__(self):
        pass

    # ------------------------------------------------------------------
    # 接口 1：单标的融合信号（原有逻辑，语义对齐修正分）
    # ------------------------------------------------------------------
    def generate_signal(self, ts_code: str, trade_date: str):
        """Fusion logic: RSRS + Sentiment"""
        with Session(engine) as session:
            rsrs_rec = session.query(FactorData).filter(
                FactorData.ts_code == ts_code
            ).order_by(FactorData.trade_date.desc()).first()

            sent_rec = session.query(ReportSentiment).filter(
                ReportSentiment.ts_code == ts_code
            ).order_by(ReportSentiment.publish_date.desc()).first()

            if not rsrs_rec:
                return "NO_DATA"

            rsrs_score = rsrs_rec.rsrs_zscore  # 修正分 z*r2（见 factors/rsrs.py）
            sentiment = sent_rec.sentiment_score if sent_rec else 0.0

            # Fusion Logic [P3-03]
            signal = "HOLD/SELL"
            if rsrs_score is not None and rsrs_score > 0.7 and sentiment > 0.2:
                signal = "STRONG_BUY"
            elif rsrs_score is not None and rsrs_score > 0.7 and sentiment < -0.2:
                signal = "DIVERGENCE_WATCH"

            return {
                "ts_code": ts_code,
                "date": trade_date,
                "signal": signal,
                "rsrs": rsrs_score,
                "sentiment": sentiment,
            }

    # ------------------------------------------------------------------
    # 接口 2：批量轮动信号（新增）
    # ------------------------------------------------------------------
    def latest_factor_snapshot(self, codes: list[str]) -> list[dict]:
        """每个 code 取 factor_data 里最新一天的因子记录。"""
        with Session(engine) as session:
            snapshot = []
            for code in codes:
                rec = session.query(FactorData).filter(
                    FactorData.ts_code == code
                ).order_by(FactorData.trade_date.desc()).first()
                if rec and rec.rsrs_zscore is not None:
                    snapshot.append({
                        "ts_code": code,
                        "trade_date": str(rec.trade_date),
                        "rsrs_zscore": float(rec.rsrs_zscore),
                        "rsrs_r2": float(rec.rsrs_r2) if rec.rsrs_r2 is not None else None,
                    })
                else:
                    logger.warning(f"[signals] no factor data for {code}")
            return snapshot

    def generate_rotation_signals(
        self,
        codes: list[str],
        top_k: int = 2,
        min_score: float = 0.0,
        as_of: str = None,
        out_path: Path = None,
    ) -> dict:
        """截面轮动：RSRS 修正分排名 top_k 等权。

        Args:
            codes:       候选池（带交易所后缀，如 512480.SH）
            top_k:       持仓数量
            min_score:   入选最低修正分（默认 0，即仅多头排序）
            as_of:       信号日期标签（默认今天）
            out_path:    可选，结果落盘 JSON

        Returns: {date, top_k, holdings: [{code, weight, rsrs}], full_rank: [...]}
        """
        snapshot = self.latest_factor_snapshot(codes)
        eligible = [s for s in snapshot if s["rsrs_zscore"] >= min_score]
        eligible.sort(key=lambda s: s["rsrs_zscore"], reverse=True)
        picked = eligible[:top_k]

        holdings = [
            {
                "code": s["ts_code"],
                "weight": round(1.0 / len(picked), 6) if picked else 0.0,
                "rsrs": round(s["rsrs_zscore"], 4),
            }
            for s in picked
        ]
        result = {
            "date": as_of or str(date.today()),
            "top_k": top_k,
            "min_score": min_score,
            "holdings": holdings,
            "full_rank": [
                {"code": s["ts_code"], "rsrs": round(s["rsrs_zscore"], 4)}
                for s in snapshot
            ],
        }

        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.success(f"[signals] rotation signals -> {out_path}")

        for h in holdings:
            logger.info(f"[signals] {result['date']} HOLD {h['code']} "
                        f"w={h['weight']} rsrs={h['rsrs']}")
        if not holdings:
            logger.warning(f"[signals] {result['date']} 空仓（无标的满足 min_score={min_score}）")
        return result
