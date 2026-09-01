#!/usr/bin/env python3
"""大类资产轮动测试：股/债/金互转 vs 被动等权基准。

§7 已证伪截面价量因子与快速时序动量；时序诊断进一步显示「多/空现金」的
MA200 趋势过滤只能降回撤、不加夏普（空仓=0 收益低估了择时价值）。本脚本
检验最后一条时序路线——**风险资产趋势走弱时，轮动到债券/黄金等避险资产**，
而非拿现金。

策略（日频、信号滞后 1 日）：
  * equity_bond  ：510300 > MA200 则持 510300，否则持 511010（国债）
  * equity_gold  ：510300 > MA200 则持 510300，否则持 518880（黄金）
  * mom3         ：510300/518880/511010 三资产，持 12 月动量最高者；全负持债
  * mom4         ：+513100(纳指) 四资产，同上

基准：
  * equal_weight_18：异构池 18 只等权（被动基线）
  * buyhold_510300 ：纯沪深300

用法：python3 scripts/rotation_test.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]  # archive 下移一层
DATA_DIR = ROOT / "data"
TRADING_DAYS = 252

HETERO_CODES = [
    "510300.SH", "510500.SH", "159915.SZ", "512010.SH", "159928.SZ",
    "512880.SH", "512660.SH", "511010.SH", "511180.SH", "518880.SH",
    "159985.SZ", "159981.SZ", "513100.SH", "513500.SH", "513050.SH",
    "513880.SH", "513030.SH", "159920.SZ",
]


def metrics(daily_ret: pd.Series) -> dict:
    r = daily_ret.dropna()
    total = float((1 + r).prod() - 1)
    ann_ret = float((1 + total) ** (TRADING_DAYS / len(r)) - 1) if len(r) else float("nan")
    ann_vol = float(r.std() * np.sqrt(TRADING_DAYS))
    sharpe = float(ann_ret / ann_vol) if ann_vol > 0 else float("nan")
    eq = (1 + r).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())
    return {"ann_ret": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe,
            "max_dd": mdd, "total": total}


def load(code: str) -> pd.Series:
    df = pd.read_csv(DATA_DIR / f"{code}.csv", parse_dates=["date"]).set_index("date")
    return df["close"].sort_index()


def ret_of(close: pd.Series) -> pd.Series:
    return close.pct_change(fill_method=None)


def main():
    closes = {c: load(c) for c in HETERO_CODES}
    rets = {c: ret_of(closes[c]) for c in HETERO_CODES}

    # 对齐到共同交易日
    panel = pd.DataFrame(rets).sort_index()

    results = {}

    # ---- 基准 1：等权 18 ----
    ew = panel.mean(axis=1)
    results["equal_weight_18"] = metrics(ew)

    # ---- 基准 2：纯沪深300 ----
    results["buyhold_510300"] = metrics(rets["510300.SH"])

    # ---- 基准 3：纯黄金 ----
    results["buyhold_518880"] = metrics(rets["518880.SH"])

    # ---- 基准 4：纯国债 ----
    results["buyhold_511010"] = metrics(rets["511010.SH"])

    # ---- 策略：股债/股金轮动（MA200） ----
    eq_close = closes["510300.SH"]
    ma200 = eq_close.rolling(200).mean()
    up = (eq_close > ma200).astype(float).shift(1)  # 1=持权益, 0=持避险

    r_eq = rets["510300.SH"]
    r_bond = rets["511010.SH"]
    r_gold = rets["518880.SH"]
    common = up.dropna().index

    eb = (up * r_eq + (1 - up) * r_bond)[common]
    eg = (up * r_eq + (1 - up) * r_gold)[common]
    results["equity_bond"] = metrics(eb)
    results["equity_gold"] = metrics(eg)

    # ---- 策略：多资产动量轮动（top-1 by 12月动量，全负持债） ----
    def momentum_rotation(assets: list[str], name: str):
        closes_p = pd.DataFrame({c: closes[c] for c in assets})
        mom = closes_p / closes_p.shift(240) - 1.0  # 12 月动量
        rank = mom.rank(axis=1, ascending=False)     # 1=最高动量
        # 滞后 1 日：t 日持仓由 t-1 日收盘动量决定，避免前视（同 equity_bond 的 up.shift(1)）
        pick = (rank == 1).astype(float).shift(1)    # 选动量最高
        all_neg = (mom < 0).all(axis=1).shift(1, fill_value=False)  # 全负则持债
        ret_panel = pd.DataFrame({c: rets[c] for c in assets})
        strat = (pick * ret_panel).sum(axis=1)
        # 全负时切换到债券收益
        strat = strat.where(~all_neg, rets["511010.SH"])
        results[name] = metrics(strat)

    momentum_rotation(["510300.SH", "518880.SH", "511010.SH"], "mom3_egb")
    momentum_rotation(["510300.SH", "518880.SH", "511010.SH", "513100.SH"], "mom4_egbn")

    # ---- 打印 ----
    print("=" * 96)
    print("大类资产轮动测试 vs 被动基准（全区间，信号滞后 1 日）")
    print("=" * 96)
    print(f"  {'策略':<18} {'年化收益':>9} {'年化波动':>9} {'夏普':>7} {'最大回撤':>9} {'累计收益':>9}")
    print("-" * 96)
    order = ["equal_weight_18", "buyhold_510300", "buyhold_518880", "buyhold_511010",
             "equity_bond", "equity_gold", "mom3_egb", "mom4_egbn"]
    for name in order:
        m = results[name]
        print(f"  {name:<18} {m['ann_ret']*100:>+8.2f}% {m['ann_vol']*100:>8.2f}% "
              f"{m['sharpe']:>7.2f} {m['max_dd']*100:>8.1f}% {m['total']*100:>+8.1f}%")

    # 落盘
    out = {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
               for kk, vv in v.items()} for k, v in results.items()}
    (ROOT / "runs/rotation_test.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    md = ["# 大类资产轮动测试报告\n",
          "> 信号滞后 1 日，未计摩擦（月内日频调仓、换手低）。\n",
          "## 结果\n",
          "| 策略 | 年化收益 | 年化波动 | 夏普 | 最大回撤 | 累计收益 |",
          "|---|---|---|---|---|---|"]
    for name in order:
        m = results[name]
        md.append(f"| {name} | {m['ann_ret']*100:+.2f}% | {m['ann_vol']*100:.2f}% "
                  f"| {m['sharpe']:.2f} | {m['max_dd']*100:.1f}% | {m['total']*100:+.1f}% |")
    md += ["\n## 说明\n",
           "- equity_bond/equity_gold：510300 上穿/下穿 MA200 决定持权益还是避险腿。",
           "- mom3/mom4：12 月动量最高者持有，全负时切国债。",
           "- 关注「夏普是否超过 equal_weight_18」与「最大回撤是否显著收窄」。\n"]
    (ROOT / "runs/rotation_test.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\nJSON 已写入: runs/rotation_test.json")
    print(f"Markdown 已写入: runs/rotation_test.md")


if __name__ == "__main__":
    main()
