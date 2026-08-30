"""
AlphaQuant CLI —— 完整工作流入口。

标准链路（一条龙 `run_all`）：
  fetch_data   vibe-trading 多源回退链(tencent 免 token, 前复权)拉日线 -> data/*.csv + DB
  calc_factors RSRS 向量化计算 -> 回写 market_data.rsrs_* + factor_data
  gen_signals  截面轮动信号(top_k 等权) + RSRS/情感融合 -> output/signals_*.json
  backtest     注入 vibe loader cache -> ChinaAEngine(T+1/整手/涨跌停, ETF 免印花税)
               跑 RSRS 轮动回测 -> runs/<name>/
  monitor      QDII 实时溢价监控（独立环节，akshare）

所有策略计算在 WSL 内运行：cd ~/workspace/alpha_quant && python3 main.py <cmd>
"""
import argparse
import time
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from config.logging_config import setup_logging
from config.settings import settings
from src.database.connection import engine
from src.database.models import MarketData, FactorData
from src.strategies.factors.rsrs import RSRSCalculator

logger = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "output"


def parse_codes(s: str) -> list[str]:
    return [c.strip() for c in s.split(",") if c.strip()]


# ----------------------------------------------------------------------
# 1. 数据获取：vibe-trading 免 token 链替换 tushare
# ----------------------------------------------------------------------
def cmd_fetch_data(args):
    from src.data_engine.vibe_market_loader import VibeMarketLoader

    codes = parse_codes(args.codes)
    loader = VibeMarketLoader(source=args.source)
    written = loader.fetch_and_sync(codes, args.start, args.end)
    logger.success(f"fetch_data done: {written}")


def cmd_update_data(args):
    """旧命令保留为兼容别名，同样走 vibe 免 token 链。"""
    args.codes = args.codes or "512480.SH,513100.SH"
    args.start = args.start or "2020-01-01"
    args.end = args.end or str(date.today())
    args.source = "auto"
    cmd_fetch_data(args)


# ----------------------------------------------------------------------
# 2. 因子计算：实装（原为 stub）
# ----------------------------------------------------------------------
def _calc_one(code: str, n: int, m: int) -> int:
    """对单个标的：DB 读行情 -> RSRS -> 回写 market_data + factor_data。"""
    with Session(engine) as session:
        rows = (
            session.query(MarketData)
            .filter(MarketData.ts_code == code)
            .order_by(MarketData.trade_date)
            .all()
        )
        if not rows:
            logger.warning(f"[factors] no market data for {code}, skip")
            return 0

        df = pd.DataFrame([{
            "trade_date": r.trade_date,
            "open": r.open, "high": r.high, "low": r.low,
            "close": r.close, "vol": r.vol,
        } for r in rows]).set_index("trade_date")

        calc = RSRSCalculator()
        df = calc.calculate_rsrs_vectorized(df, N=n, M=m)

        # 回写行情表
        for r in rows:
            val = df.loc[r.trade_date]
            r.rsrs_beta = None if pd.isna(val["rsrs_beta"]) else float(val["rsrs_beta"])
            r.rsrs_r2 = None if pd.isna(val["rsrs_r2"]) else float(val["rsrs_r2"])
            r.rsrs_zscore = None if pd.isna(val["rsrs_zscore"]) else float(val["rsrs_zscore"])

        # upsert 因子表
        for idx, val in df.iterrows():
            session.merge(FactorData(
                ts_code=code,
                trade_date=idx,
                rsrs_beta=val["rsrs_beta"] if not pd.isna(val["rsrs_beta"]) else None,
                rsrs_zscore=val["rsrs_zscore"] if not pd.isna(val["rsrs_zscore"]) else None,
                rsrs_r2=val["rsrs_r2"] if not pd.isna(val["rsrs_r2"]) else None,
            ))
        session.commit()

    valid = int(df["rsrs_zscore"].notna().sum())
    logger.success(f"[factors] {code}: {len(df)} bars, {valid} valid rsrs points")
    return valid


def cmd_calc_factors(args):
    logger.info("Calculating RSRS factors...")
    with Session(engine) as session:
        if args.codes:
            codes = parse_codes(args.codes)
        else:
            codes = [
                r[0] for r in
                session.query(MarketData.ts_code).distinct().all()
            ]
    if not codes:
        logger.warning("market_data 为空；请先运行 fetch_data")
        return
    total = 0
    for code in codes:
        total += _calc_one(code, n=args.n, m=args.m)
    logger.success(f"Factor calculation complete: {total} points over {len(codes)} codes.")


# ----------------------------------------------------------------------
# 3. 信号生成：截面轮动 + 融合
# ----------------------------------------------------------------------
def cmd_gen_signals(args):
    from src.strategies.signal_generator import SignalGenerator

    codes = parse_codes(args.codes)
    if not codes:
        with Session(engine) as session:
            codes = [
                r[0] for r in
                session.query(FactorData.ts_code).distinct().all()
            ]
    if not codes:
        logger.warning("factor_data 为空；请先运行 calc_factors")
        return

    gen = SignalGenerator()
    out_path = OUTPUT_DIR / f"signals_{date.today():%Y%m%d}.json"
    result = gen.generate_rotation_signals(
        codes, top_k=args.top_k, min_score=args.min_score, out_path=out_path
    )

    # 可选：单标的融合信号（研报情感参与时），情感缺失时自动退化为纯 RSRS
    if args.fusion:
        for h in result["holdings"]:
            fused = gen.generate_signal(h["code"], str(date.today()))
            logger.info(f"[fusion] {h['code']} -> {fused}")


# ----------------------------------------------------------------------
# 4. 回测：vibe-trading runner + ChinaAEngine
# ----------------------------------------------------------------------
def cmd_backtest(args):
    from src.backtest.vibe_exporter import VibeBacktestExporter

    codes = parse_codes(args.codes)
    exporter = VibeBacktestExporter(
        run_name=args.name,
        codes=codes,
        eval_start=args.start,
        eval_end=args.end,
        initial_cash=args.cash,
        top_k=args.top_k,
        n=args.n,
        m=args.m,
        min_score=args.min_score,
    )
    rc = exporter.run(ensure_data_first=not args.no_fetch)
    if rc == 0:
        logger.success(f"回测完成，结果见 runs/{args.name}/")


# ----------------------------------------------------------------------
# 5. 一条龙
# ----------------------------------------------------------------------
def cmd_run_all(args):
    codes = parse_codes(args.codes)
    today = str(date.today())
    fetch_args = argparse.Namespace(
        codes=args.codes, start=args.start_fetch, end=today, source="auto")
    cmd_fetch_data(fetch_args)

    calc_args = argparse.Namespace(codes=args.codes, n=args.n, m=args.m)
    cmd_calc_factors(calc_args)

    sig_args = argparse.Namespace(
        codes=args.codes, top_k=args.top_k,
        min_score=args.min_score, fusion=False)
    cmd_gen_signals(sig_args)

    bt_args = argparse.Namespace(
        name=args.name, codes=args.codes, start=args.start, end=args.end,
        cash=args.cash, top_k=args.top_k, n=args.n, m=args.m,
        min_score=args.min_score, no_fetch=False)
    cmd_backtest(bt_args)


# ----------------------------------------------------------------------
# 6. QDII 监控（原有逻辑，redis 惰性导入）
# ----------------------------------------------------------------------
def cmd_monitor(args):
    import json

    from src.data_engine.qdii_calc import QDIICalculator

    logger.info("Starting QDII Monitor...")
    calc = QDIICalculator()
    etf_code = "513100"  # Nasdaq ETF

    r = None
    try:
        import redis
        r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        logger.warning("Redis not connected! Alerts will only be logged.")

    while True:
        try:
            premium = calc.get_realtime_premium(etf_code)
            logger.info(f"ETF: {etf_code} Premium: {premium:.4f}")

            if abs(premium) > 0.03:
                msg = f"High Premium Alert! {etf_code}: {premium:.2%}"
                logger.warning(msg)
                if r:
                    r.publish("alert_queue", json.dumps({
                        "type": "PREMIUM_ALERT",
                        "code": etf_code,
                        "value": premium,
                        "timestamp": time.time()
                    }))
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user.")
            break
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")

        time.sleep(3)


# ----------------------------------------------------------------------
# CLI 装配
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="AlphaQuant CLI（vibe-trading 集成版）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # 注意：--codes 由各子命令自行注册（默认值不同），此处只放 RSRS 窗口参数，
    # 否则 argparse 会因重复注册同一 option string 抛 ArgumentError。
    rsrs_window_args = [
        (["-n", "--n"], {"type": int, "default": 18, "help": "RSRS 回归窗口"}),
        (["-m", "--m"], {"type": int, "default": 600, "help": "RSRS 标准化窗口"}),
    ]

    p = sub.add_parser("fetch_data", help="vibe 链拉日线(免token)->csv+DB")
    p.add_argument("--codes", default="512480.SH,513100.SH")
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default=str(date.today()))
    p.add_argument("--source", default="auto")
    p.set_defaults(func=cmd_fetch_data)

    p = sub.add_parser("update_data", help="(兼容别名) 同 fetch_data")
    p.add_argument("--codes", default=None)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.set_defaults(func=cmd_update_data)

    p = sub.add_parser("calc_factors", help="计算 RSRS 因子并入库")
    p.add_argument("--codes", default=None, help="逗号分隔，如 512480.SH,513100.SH")
    for a, kw in rsrs_window_args:
        p.add_argument(*a, **kw)
    p.set_defaults(func=cmd_calc_factors)

    p = sub.add_parser("gen_signals", help="生成轮动/融合信号")
    p.add_argument("--codes", default=None)
    p.add_argument("--top-k", dest="top_k", type=int, default=2)
    p.add_argument("--min-score", dest="min_score", type=float, default=0.0)
    p.add_argument("--fusion", action="store_true",
                   help="附加单标的 RSRS+情感融合信号")
    p.set_defaults(func=cmd_gen_signals)

    p = sub.add_parser("backtest", help="vibe ChinaAEngine 回测 RSRS 轮动")
    p.add_argument("--name", default=f"rsrs_rotation_{date.today():%Y%m%d}")
    p.add_argument("--codes", default="512480.SH,513100.SH,588000.SH")
    p.add_argument("--start", default="2024-01-01", help="评估起点")
    p.add_argument("--end", default=str(date.fromordinal(date.today().toordinal() - 1)),
                   help="评估终点(须早于今天)")
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--top-k", dest="top_k", type=int, default=2)
    p.add_argument("--min-score", dest="min_score", type=float, default=0.0)
    p.add_argument("--no-fetch", action="store_true", help="跳过数据拉取(用现有csv)")
    for a, kw in rsrs_window_args:
        p.add_argument(*a, **kw)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("run_all", help="fetch->factors->signals->backtest 一条龙")
    p.add_argument("--codes", default="512480.SH,513100.SH,588000.SH")
    p.add_argument("--start", default="2024-01-01", help="评估起点")
    p.add_argument("--end", default=str(date.fromordinal(date.today().toordinal() - 1)))
    p.add_argument("--start-fetch", dest="start_fetch", default="2020-01-01",
                   help="数据拉取起点(含 warmup)")
    p.add_argument("--name", default=f"rsrs_rotation_{date.today():%Y%m%d}")
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--top-k", dest="top_k", type=int, default=2)
    p.add_argument("--min-score", dest="min_score", type=float, default=0.0)
    for a, kw in rsrs_window_args:
        p.add_argument(*a, **kw)
    p.set_defaults(func=cmd_run_all)

    p = sub.add_parser("monitor", help="QDII 实时溢价监控")
    p.set_defaults(func=cmd_monitor)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
