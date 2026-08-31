#!/usr/bin/env python3
"""QDII 门控降频实验：日频 vs 周频（§7.20 换手结构拆解的后续验证）。

§7.20 §5 发现：QDII 门控贡献组合 60% 换手，但单次仅动 ~4% 资金。假设：
把 QDII 门控从日频降频到周频（每周首个交易日按当时信号调一次，周内锁定），
换手应显著下降，而超额收益损失有限——因为溢价飙升回避的收益主要来自
「躲开持续数周的高溢价段」，而非单日择时。

周频口径（无前视）：
  h 为日频持仓序列（T 日信号决定 T+1 持仓，spike_avoid_hold 原版语义）。
  周一（每周首个交易日）的 h 值已由上周五信息决定 → 取每周首个交易日的 h，
  ffill 整周。等价于「周五收盘后决策、下周一执行、周内不动」。

对比档位（均在 D 档三层全开配置下，仅改 QDII 门控频率）：
  D-daily  : QDII 日频门控（现行口径）
  D-weekly : QDII 周频门控
另附 C 档口径（无 PB 门控）同对比做稳健性交叉验证。

用法：python3 scripts/qdii_gate_weekly_test.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import risk_parity as rp
import portfolio_combined as pc

ROOT = Path(__file__).resolve().parents[1]
COST = pc.COST


def qdii_gate_weekly(code: str, panel_index: pd.DatetimeIndex) -> pd.Series:
    """周频 QDII 门控：每周首个交易日取日频门控值，整周 ffill。"""
    h = pc.qdii_gate_daily(code, panel_index)
    week_firsts = panel_index.to_series().groupby(
        panel_index.to_period("W")).first()
    wk = h.reindex(pd.DatetimeIndex(week_firsts.values))
    return wk.reindex(panel_index, method="ffill").fillna(1.0).clip(0.0, 1.0)


def build_weights_qdii_freq(panel: pd.DataFrame, freq: str,
                            use_pb: bool = True) -> pd.DataFrame:
    """D/C 档权重，QDII 门控频率可选 daily/weekly。"""
    w = rp.build_weights(panel, "inverse_vol").shift(1).fillna(0.0)
    W = w.copy()
    if use_pb:
        g = pc.pb_gate_daily(panel.index, "triple")
        for c in pc.A_STOCK_LEGS:
            if c in W.columns:
                W[c] = W[c] * g.reindex(W.index).ffill().fillna(1.0)
    gate_fn = pc.qdii_gate_daily if freq == "daily" else qdii_gate_weekly
    for c in pc.QDII_LEGS:
        if c in W.columns:
            gq = gate_fn(c, W.index)
            W[c] = W[c] * gq.reindex(W.index).ffill().fillna(1.0)
    return W


def qdii_leg_turnover(W_d: pd.DataFrame, W_w: pd.DataFrame,
                      years: float) -> dict:
    """QDII 腿的年均权重变动（日频 vs 周频），验证降频压换手幅度。"""
    out = {}
    for c in pc.QDII_LEGS:
        out[c] = {
            "daily": float(W_d[c].diff().abs().sum() / years),
            "weekly": float(W_w[c].diff().abs().sum() / years),
        }
    return out


def main():
    closes = {c: pd.read_csv(ROOT / "data" / f"{c}.csv",
                             parse_dates=["date"]).set_index("date")["close"]
              for c in rp.HETERO_CODES}
    panel = pd.DataFrame(closes).sort_index().dropna()
    years = len(panel) / rp.TRADING_DAYS
    print(f"共同样本：{panel.index[0].date()} ~ {panel.index[-1].date()}，"
          f"{panel.shape[0]} 个交易日\n")

    rows = {}
    W_store = {}
    for label, freq, use_pb in [
        ("D-daily（现行日频）", "daily", True),
        ("D-weekly（周频降频）", "weekly", True),
        ("C-daily（仅QDII日频）", "daily", False),
        ("C-weekly（仅QDII周频）", "weekly", False),
    ]:
        W = build_weights_qdii_freq(panel, freq, use_pb=use_pb)
        net, _ = pc.backtest_net(panel, W, cost=COST)
        m = pc.full_metrics(net, W)
        rows[label] = {"m": m, "yearly": pc.yearly(net)}
        W_store[label] = W

    # 成本敏感性：周频在更高成本下的优势应更明显
    for c in [0.003, 0.005]:
        for label, freq in [("D-daily", "daily"), ("D-weekly", "weekly")]:
            W = W_store[[k for k in W_store if k.startswith(label)][0]]
            net, _ = pc.backtest_net(panel, W, cost=c)
            rows[f"{label} @成本{c*100:.1f}%"] = {
                "m": pc.full_metrics(net, W), "yearly": pc.yearly(net)}

    leg_to = qdii_leg_turnover(W_store["D-daily（现行日频）"],
                               W_store["D-weekly（周频降频）"], years)

    # 门控状态差异：周频漏掉/延迟了多少次回避
    h_diff = {}
    for c in pc.QDII_LEGS:
        hd = pc.qdii_gate_daily(c, panel.index)
        hw = qdii_gate_weekly(c, panel.index)
        diff_days = int((hd != hw).sum())
        # 周频处于「日频已空仓但周频仍持有」的天数 = 延迟回避暴露
        delayed = int(((hd == 0) & (hw == 1)).sum())
        h_diff[c] = {"状态不一致天数": diff_days, "延迟回避暴露天数": delayed}

    # ---- 控制台 ----
    print(f"{'档位':<26} {'年化%':>8} {'夏普':>6} {'回撤%':>8} "
          f"{'换手/年':>7} {'成本/年%':>8}")
    print("-" * 72)
    for k, v in rows.items():
        m = v["m"]
        print(f"{k:<26} {m['ann_ret']*100:>8.2f} {m['sharpe']:>6.2f} "
              f"{m['max_dd']*100:>8.1f} {m['turnover']:>7.2f} "
              f"{m['turnover']*COST*100:>8.2f}")

    print("\n== QDII 腿年均权重变动（pp/年）==")
    for c, d in leg_to.items():
        print(f"  {c}: 日频 {d['daily']*100:5.1f} → 周频 {d['weekly']*100:5.1f} "
              f"（{(1-d['weekly']/max(d['daily'],1e-9))*100:+.0f}%）")

    print("\n== 周频门控与日频的状态差异 ==")
    for c, d in h_diff.items():
        print(f"  {c}: 不一致 {d['状态不一致天数']} 天，"
              f"其中延迟回避暴露 {d['延迟回避暴露天数']} 天")

    # ---- 落盘 ----
    out = {
        "sample": {"start": str(panel.index[0].date()),
                   "end": str(panel.index[-1].date()),
                   "n_days": int(panel.shape[0])},
        "tiers": {k: {"metrics": {kk: round(vv, 4) for kk, vv in v["m"].items()},
                      "yearly": {str(y): round(float(r), 4)
                                 for y, r in v["yearly"].items()}}
                  for k, v in rows.items()},
        "qdii_leg_turnover": {c: {k: round(v, 4) for k, v in d.items()}
                              for c, d in leg_to.items()},
        "gate_state_diff": h_diff,
    }
    (ROOT / "runs" / "qdii_gate_weekly.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    L = ["# QDII 门控降频实验：日频 vs 周频\n",
         "> 动机（§7.20 §5）：QDII 门控贡献组合 60% 换手，单次仅动 ~4% 资金。",
         "> 周频口径：每周首个交易日取日频门控值（已由上周五信息决定，无靠前），",
         "> 整周 ffill 锁定 = 「周五决策、周一执行、周内不动」。统一单边成本 0.15%。",
         f"> 样本：{panel.index[0].date()} ~ {panel.index[-1].date()}"
         f"（{panel.shape[0]} 个交易日）。\n",
         "## 1. 日频 vs 周频对照\n",
         "| 档位 | 年化 | 夏普 | 最大回撤 | 换手/年 | 年化换手成本 |",
         "|---|---|---|---|---|---|"]
    for k, v in rows.items():
        m = v["m"]
        L.append(f"| {k} | {m['ann_ret']*100:+.2f}% | {m['sharpe']:.2f} "
                 f"| {m['max_dd']*100:.1f}% | {m['turnover']:.2f} "
                 f"| {m['turnover']*COST*100:.2f}% |")

    L.append("\n## 2. 逐年收益（D 档）\n")
    yl = pd.DataFrame({k: rows[k]["yearly"]
                       for k in ["D-daily（现行日频）", "D-weekly（周频降频）"]})
    L.append("| 年份 | D-daily | D-weekly | 差 |")
    L.append("|---|---|---|---|")
    for y in yl.index:
        a, b = yl.loc[y].iloc[0], yl.loc[y].iloc[1]
        L.append(f"| {y} | {a*100:+.2f}% | {b*100:+.2f}% | {(b-a)*100:+.2f}pp |")

    L.append("\n## 3. QDII 腿年均权重变动\n")
    L.append("| 代码 | 日频 | 周频 | 变化 |")
    L.append("|---|---|---|---|")
    for c, d in leg_to.items():
        L.append(f"| {c} | {d['daily']*100:.1f}pp | {d['weekly']*100:.1f}pp "
                 f"| {(1-d['weekly']/max(d['daily'],1e-9))*100:+.0f}% |")

    L.append("\n## 4. 周频门控的状态差异（vs 日频）\n")
    L.append("| 代码 | 状态不一致天数 | 其中延迟回避暴露 |")
    L.append("|---|---|---|")
    for c, d in h_diff.items():
        L.append(f"| {c} | {d['状态不一致天数']} | {d['延迟回避暴露天数']} |")
    L.append("\n> 「延迟回避暴露」= 日频已空仓避险、但周频因周内锁定仍持有的天数，"
             "是降频的主要风险来源。\n")

    md, mw = rows["D-daily（现行日频）"]["m"], rows["D-weekly（周频降频）"]["m"]
    L.append("## 5. 结论\n")
    L.append(f"- 换手：组合年化单边换手 {md['turnover']:.2f} → {mw['turnover']:.2f}"
             f"（{(1-mw['turnover']/md['turnover'])*100:+.0f}%），年化换手成本 "
             f"{md['turnover']*COST*100:.2f}% → {mw['turnover']*COST*100:.2f}%。")
    L.append(f"- 收益：年化 {md['ann_ret']*100:+.2f}% → {mw['ann_ret']*100:+.2f}%"
             f"（{(mw['ann_ret']-md['ann_ret'])*100:+.2f}pp），夏普 {md['sharpe']:.2f} "
             f"→ {mw['sharpe']:.2f}，回撤 {md['max_dd']*100:.1f}% → {mw['max_dd']*100:.1f}%。")
    better = "周频占优" if (mw["sharpe"] >= md["sharpe"]
                           and mw["ann_ret"] >= md["ann_ret"] - 0.002) else \
             "日频占优" if (md["sharpe"] > mw["sharpe"]
                            and md["ann_ret"] > mw["ann_ret"] + 0.002) else "基本打平"
    L.append(f"- **判定：{better}**。")
    (ROOT / "runs" / "qdii_gate_weekly.md").write_text(
        "\n".join(L), encoding="utf-8")
    print("\n已写入: runs/qdii_gate_weekly.md / runs/qdii_gate_weekly.json")


if __name__ == "__main__":
    main()
