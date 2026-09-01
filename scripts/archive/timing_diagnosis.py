#!/usr/bin/env python3
"""时序择时（趋势跟踪）诊断：逐标的「多/空仓」vs buy-hold。

背景（§7 收口结论）：截面价量因子在 ETF 宇宙上全部证伪（同质池+异构池
16+RSRS 全灭，最高 |ICIR|=0.289）。下一步检验「时序择时」——它不依赖截面
区分度，只依赖单标的自身历史趋势。本脚本回答：**趋势跟踪（长仓/空仓）在
异构池 18 只标的上，能否相对 buy-hold 改善风险调整后收益？**

信号（全部只用 t 及之前数据，滞后 1 日应用，杜绝前视）：
  * mom_60/120/240   ：N 日动量 = close[t]/close[t-N]-1，>0 做多
  * ma_200           ：close[t] > 200 日均线做多
  * ma_50_200        ：50 日 > 200 日均线（金叉）做多
  * vol_target_inv   ：按 20 日已实现波动率倒数缩放仓位（波动率目标，风险预算）

策略：信号>0 则持有该标的（多），否则空仓（现金，0 收益）；日频、滞后 1 日。
对照：buy-hold（始终满仓）。指标：年化收益/年化波动/夏普/最大回撤/多头暴露。

用法：python3 scripts/timing_diagnosis.py [--codes ...] [--start YYYY-MM-DD]
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]  # archive 下移一层
DATA_DIR = ROOT / "data"

HETERO_CODES = [
    "510300.SH", "510500.SH", "159915.SZ", "512010.SH", "159928.SZ",
    "512880.SH", "512660.SH", "511010.SH", "511180.SH", "518880.SH",
    "159985.SZ", "159981.SZ", "513100.SH", "513500.SH", "513050.SH",
    "513880.SH", "513030.SH", "159920.SZ",
]
TRADING_DAYS = 252


def annualized_return(daily_ret: pd.Series) -> float:
    ret = daily_ret.dropna()
    if len(ret) == 0:
        return float("nan")
    total = float((1 + ret).prod() - 1)
    return float((1 + total) ** (TRADING_DAYS / len(ret)) - 1)


def annualized_vol(daily_ret: pd.Series) -> float:
    return float(daily_ret.dropna().std() * np.sqrt(TRADING_DAYS))


def sharpe(daily_ret: pd.Series) -> float:
    ar = annualized_return(daily_ret)
    av = annualized_vol(daily_ret)
    return float(ar / av) if av > 0 else float("nan")


def max_drawdown(daily_ret: pd.Series) -> float:
    """从日收益序列算最大回撤（负值）。"""
    eq = (1 + daily_ret.fillna(0)).cumprod()
    return float((eq / eq.cummax() - 1).min())


def build_signals(close: pd.Series) -> dict[str, pd.Series]:
    s = {}
    s["mom_60"] = close / close.shift(60) - 1.0
    s["mom_120"] = close / close.shift(120) - 1.0
    s["mom_240"] = close / close.shift(240) - 1.0
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()
    s["ma_200"] = close / ma200 - 1.0
    s["ma_50_200"] = ma50 / ma200 - 1.0
    # 波动率目标：仓位 = 目标年化20% / 20日已实现年化波动，截断[0,1]
    rv20 = close.pct_change(fill_method=None).rolling(20).std() * np.sqrt(TRADING_DAYS)
    s["vol_target"] = (0.20 / rv20).clip(0.0, 1.0)
    return s


def evaluate(close: pd.Series, signal: pd.Series, long_flat: bool = True) -> dict:
    """返回某信号下该标的的择时 vs buy-hold 指标。

    long_flat=True 时信号>0 做多、否则空仓（现金）；信号可为连续仓位
    （vol_target）时视为「仓位权重」而非开关。
    """
    ret = close.pct_change(fill_method=None)
    if long_flat:
        pos = (signal > 0).astype(float).shift(1)
    else:
        pos = signal.clip(0.0, 1.0).shift(1)  # 连续仓位（vol_target）
    strat_ret = ret * pos
    bh = ret

    return {
        "strat": {
            "ann_ret": round(annualized_return(strat_ret), 4),
            "ann_vol": round(annualized_vol(strat_ret), 4),
            "sharpe": round(sharpe(strat_ret), 4),
            "max_dd": round(max_drawdown(strat_ret), 4),
            "exposure": round(float(pos.mean()), 4),
        },
        "buyhold": {
            "ann_ret": round(annualized_return(bh), 4),
            "ann_vol": round(annualized_vol(bh), 4),
            "sharpe": round(sharpe(bh), 4),
            "max_dd": round(max_drawdown(bh), 4),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*", default=None)
    ap.add_argument("--start", default=None, help="YYYY-MM-DD")
    args = ap.parse_args()
    codes = args.codes or HETERO_CODES

    closes = {}
    for code in codes:
        df = pd.read_csv(DATA_DIR / f"{code}.csv", parse_dates=["date"]).set_index("date")
        df = df.sort_index()
        if args.start:
            df = df[df.index >= pd.Timestamp(args.start)]
        closes[code] = df["close"]

    signals = ["mom_60", "mom_120", "mom_240", "ma_200", "ma_50_200", "vol_target"]
    long_flat_map = {s: (s != "vol_target") for s in signals}

    # 汇总结构：signal -> {asset: {strat, buyhold}}
    all_res = {s: {} for s in signals}
    for code, close in closes.items():
        sigs = build_signals(close)
        for s in signals:
            all_res[s][code] = evaluate(close, sigs[s], long_flat_map[s])

    # 打印：每个信号的池化平均 + 逐标的夏普改善
    print("=" * 110)
    print(f"时序择时诊断：趋势跟踪(多/空) vs buy-hold（异构池 {len(codes)} 只）")
    print("=" * 110)
    summary = {}
    for s in signals:
        rows = all_res[s]
        dsharpe = [rows[c]["strat"]["sharpe"] - rows[c]["buyhold"]["sharpe"]
                   for c in rows]
        dsharpe = [x for x in dsharpe if not np.isnan(x)]
        win = sum(1 for x in dsharpe if x > 0)
        avg_ds = float(np.mean(dsharpe)) if dsharpe else float("nan")
        # 多头侧收益 vs 空仓侧（纯 timing alpha，仅开关型信号）
        long_ret, flat_ret = [], []
        if long_flat_map[s]:
            for c in rows:
                close = closes[c]
                sig = build_signals(close)[s]
                ret = close.pct_change(fill_method=None)
                pos = (sig > 0).astype(float).shift(1)
                lr = ret[pos == 1].mean()
                fr = ret[pos == 0].mean()
                if not np.isnan(lr):
                    long_ret.append(lr)
                if not np.isnan(fr):
                    flat_ret.append(fr)
        lr_avg = float(np.mean(long_ret)) if long_ret else float("nan")
        fr_avg = float(np.mean(flat_ret)) if flat_ret else float("nan")
        summary[s] = {
            "avg_dsharpe": round(avg_ds, 4),
            "win_rate": round(win / len(dsharpe), 4) if dsharpe else 0.0,
            "long_daily": round(lr_avg, 6) if not np.isnan(lr_avg) else None,
            "flat_daily": round(fr_avg, 6) if not np.isnan(fr_avg) else None,
        }
        print(f"\n【{s}】 池化 Δ夏普(择时−buyhold) {avg_ds:+.3f} | "
              f"改善标的占比 {win}/{len(dsharpe)} ({summary[s]['win_rate']:.0%})")
        if long_flat_map[s]:
            print(f"   多头日收益均值 {lr_avg*100:+.3f}% vs 空仓日收益均值 {fr_avg*100:+.3f}%"
                  f" | 时序溢价(多−空) {(lr_avg-fr_avg)*100:+.3f}%")
        print(f"   {'标的':<12} {'BH夏普':>8} {'择时夏普':>9} {'Δ夏普':>8} "
              f"{'BH回撤':>8} {'择时回撤':>9} {'暴露':>6}")
        for c in sorted(rows, key=lambda x: -(rows[x]["strat"]["sharpe"] - rows[x]["buyhold"]["sharpe"])):
            r = rows[c]
            ds = r["strat"]["sharpe"] - r["buyhold"]["sharpe"]
            print(f"   {c:<12} {r['buyhold']['sharpe']:>8.2f} {r['strat']['sharpe']:>9.2f} "
                  f"{ds:>+8.2f} {r['buyhold']['max_dd']*100:>7.1f}% "
                  f"{r['strat']['max_dd']*100:>8.1f}% {r['strat']['exposure']:>6.0%}")

    # 落盘
    out = {"codes": codes, "start": args.start, "signals": summary,
           "per_asset": {s: {c: all_res[s][c] for c in codes} for s in signals}}
    (ROOT / "runs/timing_diagnosis.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    md = ["# 时序择时（趋势跟踪）诊断报告\n",
          f"> 异构池 {len(codes)} 只，多/空仓 vs buy-hold。信号滞后 1 日应用，无前视。\n",
          "## 1. 池化汇总\n",
          "| 信号 | 平均Δ夏普 | 改善标的占比 | 多头日收益 | 空仓日收益 | 时序溢价 |",
          "|---|---|---|---|---|---|"]
    for s in signals:
        m = summary[s]
        lr = f"{m['long_daily']*100:+.3f}%" if m["long_daily"] is not None else "—"
        fr = f"{m['flat_daily']*100:+.3f}%" if m["flat_daily"] is not None else "—"
        prem = f"{(m['long_daily']-m['flat_daily'])*100:+.3f}%" if m["long_daily"] is not None else "—"
        md.append(f"| {s} | {m['avg_dsharpe']:+.3f} | {m['win_rate']:.0%} | {lr} | {fr} | {prem} |")
    md += ["\n## 2. 逐标的 Δ夏普（择时 − buy-hold）\n"]
    for s in signals:
        md.append(f"\n### {s}\n")
        md.append("| 标的 | BH夏普 | 择时夏普 | Δ夏普 | BH回撤 | 择时回撤 | 暴露 |")
        md.append("|---|---|---|---|---|---|---|")
        rows = all_res[s]
        for c in sorted(rows, key=lambda x: -(rows[x]["strat"]["sharpe"] - rows[x]["buyhold"]["sharpe"])):
            r = rows[c]
            ds = r["strat"]["sharpe"] - r["buyhold"]["sharpe"]
            md.append(f"| {c} | {r['buyhold']['sharpe']:.2f} | {r['strat']['sharpe']:.2f} "
                      f"| {ds:+.2f} | {r['buyhold']['max_dd']*100:.1f}% "
                      f"| {r['strat']['max_dd']*100:.1f}% | {r['strat']['exposure']:.0%} |")
    md += ["\n## 3. 说明\n",
           "- 空仓=现金（0 收益），未计摩擦；月内日频调仓、换手成本低。",
           "- 时序溢价 = 多头日收益均值 − 空仓日收益均值，衡量「趋势信号能否挑出上涨段」。",
           "- 波动率目标（vol_target）是风险预算而非择时，仓位随波动率连续缩放，"
           "天然降波动、降回撤，但非 alpha 来源。\n"]
    (ROOT / "runs/timing_diagnosis.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\nJSON 已写入: runs/timing_diagnosis.json")
    print(f"Markdown 已写入: runs/timing_diagnosis.md")


if __name__ == "__main__":
    main()
