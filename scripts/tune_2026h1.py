#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026 上半年（2026-01-01 ~ 06-30）子样本回测对比（实验分支专用）。

前面三轮的参数选择用的是全样本 + IS/OOS(2023-07) 切分。本脚本换一个
**完全没参与任何选择**的时间窗做最终验证：2026 H1（约 118 个交易日）。

回答三个问题：
  1. 各旋钮在 2026H1 是否仍有效（方向是否与全样本一致）？
  2. 叠加方案（L2/L3/L4/L6）在最近半年是否仍跑赢现役？
  3. 逐月看，改进是稳定的还是靠某一个月撑起来的？

用法：python3 scripts/tune_2026h1.py
输出：runs/tune_2026h1.md / .json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import risk_parity as rp                                              # noqa: E402
import portfolio_combined as pc                                       # noqa: E402
from tune_three_layer import (BASE, COST, evaluate, load_panel,       # noqa: E402
                              pb_gate_daily, qdii_gate)
from tune_round3 import build_W, group_expo                           # noqa: E402

TD = rp.TRADING_DAYS
S, E = "2026-01-01", "2026-06-30"

# 现役 + 单因子 + 叠加方案
COMMON = dict(freq="Q", z_hi=1.5, q_floor=0.5, cash_mmf=True)

CANDIDATES = {
    "BASE 现役": dict(BASE),
    "① 仅 z=1.5/floor=0.5": dict(BASE, z_hi=1.5, q_floor=0.5),
    "② 仅 季频再平衡": dict(BASE, freq="Q"),
    "③ 仅 月频货币腿": dict(BASE, cash_mmf=True),
    "④ 仅 地板4%": dict(BASE, vf=0.04),
    "⑤ 仅 地板6%": dict(BASE, vf=0.06),
    "⑥ 仅 p=1.2": dict(BASE, p=1.2),
    "L2 三项免费改进": dict(COMMON, p=1.0),
    "L3 L2+地板4%": dict(COMMON, vf=0.04),
    "L4 L2+地板6%(收益优先)": dict(COMMON, vf=0.06),
    "L6 L2+地板6%+p1.2": dict(COMMON, p=1.2, vf=0.06),
}

# 被动对照
PASSIVE = {
    "EW18 等权月再平衡": "equal",
    "BH300 沪深300买入持有": None,
}


def eval_window(panel: pd.DataFrame, W: pd.DataFrame, start: str, end: str,
                cost: float = COST) -> dict:
    net, _ = pc.backtest_net(panel, W, cost)
    sub = net.loc[start:end]
    m = rp.metrics(sub) if len(sub) >= 30 else {}
    years = len(sub) / TD
    dW = W.loc[start:end].diff().abs().sum(axis=1)
    return {
        "cum": float((1 + sub).prod() - 1),
        "ann_ret": m.get("ann_ret", float("nan")),
        "ann_vol": m.get("ann_vol", float("nan")),
        "sharpe": m.get("sharpe", float("nan")),
        "max_dd": m.get("max_dd", float("nan")),
        "turnover": float(dW.sum() / years),
        "expo": float(W.loc[start:end].sum(axis=1).mean()),
        "net": sub,
    }


def main() -> int:
    panel = load_panel()
    years_all = len(panel) / TD
    sub_idx = panel.loc[S:E].index
    print(f"[h1] 全样本 {panel.index.min().date()} ~ {panel.index.max().date()}"
          f"（{years_all:.2f} 年）")
    print(f"[h1] 子窗口 {sub_idx.min().date()} ~ {sub_idx.max().date()}"
          f"（{len(sub_idx)} 交易日 / {len(sub_idx)/TD:.2f} 年）\n")

    rows, subs, expos = [], {}, {}
    for name, cfg in CANDIDATES.items():
        W = build_W(panel, cfg)
        m = evaluate(panel, W)                    # 全样本（对照用）
        s = eval_window(panel, W, S, E)           # 2026H1
        subs[name] = s["net"]
        expos[name] = group_expo(W.loc[S:E])
        rows.append({
            "name": name,
            "h1_cum": s["cum"], "h1_ann": s["ann_ret"], "h1_vol": s["ann_vol"],
            "h1_sharpe": s["sharpe"], "h1_dd": s["max_dd"],
            "h1_turn": s["turnover"], "h1_expo": s["expo"],
            "full_ann": m["ann_ret"], "full_sharpe": m["sharpe"],
            "oos_sharpe": m["oos_sharpe"],
        })

    base_row = rows[0]

    # ---------- 被动对照 ----------
    prox = panel.loc[S:E]
    p_rows = []
    for label, method in PASSIVE.items():
        if method == "equal":
            W = rp.build_weights(panel, "equal").shift(1).fillna(0.0)
        else:
            W = pd.DataFrame(0.0, index=panel.index, columns=panel.columns)
            W["510300.SH"] = 1.0
        s = eval_window(panel, W, S, E)
        subs[label] = s["net"]
        p_rows.append({"name": label, "h1_cum": s["cum"], "h1_ann": s["ann_ret"],
                       "h1_vol": s["ann_vol"], "h1_sharpe": s["sharpe"],
                       "h1_dd": s["max_dd"], "h1_turn": s["turnover"],
                       "h1_expo": s["expo"], "full_ann": float("nan"),
                       "full_sharpe": float("nan"), "oos_sharpe": float("nan")})

    # ---------- 控制台 ----------
    print(f"{'方案':<24} {'H1累计':>8} {'H1年化':>8} {'H1夏普':>7} {'H1回撤':>8} "
          f"{'H1换手':>7} {'全样本年化':>10} {'全样本夏普':>9} {'vs现役':>8}")
    print("-" * 100)
    for r in rows:
        d = (r["h1_cum"] - base_row["h1_cum"]) * 100
        print(f"{r['name']:<24} {r['h1_cum']*100:>7.2f}% {r['h1_ann']*100:>7.2f}% "
              f"{r['h1_sharpe']:>7.2f} {r['h1_dd']*100:>7.2f}% {r['h1_turn']:>7.2f} "
              f"{r['full_ann']*100:>9.2f}% {r['full_sharpe']:>9.2f} {d:>+7.2f}pp")
    for r in p_rows:
        print(f"{r['name']:<24} {r['h1_cum']*100:>7.2f}% {r['h1_ann']*100:>7.2f}% "
              f"{r['h1_sharpe']:>7.2f} {r['h1_dd']*100:>7.2f}% {r['h1_turn']:>7.2f} "
              f"{'—':>10} {'—':>9} {'—':>8}")

    # ---------- 逐月 ----------
    monthly = pd.DataFrame({k: v.groupby(v.index.to_period("M")).apply(
        lambda x: (1 + x).prod() - 1) for k, v in subs.items()})
    win = {k: int((monthly[k] > monthly["BASE 现役"]).sum()) for k in monthly.columns}
    n_months = len(monthly)

    # ---------- 报告 ----------
    L = ["# 2026 上半年子样本回测对比（实验分支 exp/new-strategy）", "",
         f"- **子窗口**：{sub_idx.min().date()} ~ {sub_idx.max().date()}"
         f"（{len(sub_idx)} 交易日 / {len(sub_idx)/TD:.2f} 年）"
         "——完全没参与前几轮的参数选择，是真正的 fresh 样本",
         f"- 全样本对照：{panel.index.min().date()} ~ {panel.index.max().date()}"
         f"（{years_all:.2f} 年），单边成本 0.15%",
         "- `vs现役` = 2026H1 累计收益 − 现役 BASE 累计收益（pp）", "",
         "## 1. 2026H1 各方案表现", "",
         "| 方案 | H1累计 | H1年化 | H1波动 | H1夏普 | H1回撤 | H1换手/年 | "
         "H1暴露 | vs现役 | 全样本年化 | 全样本夏普 | OOS夏普 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        d = (r["h1_cum"] - base_row["h1_cum"]) * 100
        tag = "" if r["name"] == "BASE 现役" else f"{d:+.2f}pp"
        L.append(f"| {r['name']} | {r['h1_cum']*100:+.2f}% | {r['h1_ann']*100:+.2f}% "
                 f"| {r['h1_vol']*100:.2f}% | {r['h1_sharpe']:.2f} "
                 f"| {r['h1_dd']*100:.2f}% | {r['h1_turn']:.2f} "
                 f"| {r['h1_expo']*100:.1f}% | {tag} | {r['full_ann']*100:+.2f}% "
                 f"| {r['full_sharpe']:.2f} | {r['oos_sharpe']:.2f} |")
    L += ["", "## 2. 被动对照（H1）", "",
          "| 基准 | H1累计 | H1年化 | H1夏普 | H1回撤 |", "|---|---|---|---|---|"]
    for r in p_rows:
        L.append(f"| {r['name']} | {r['h1_cum']*100:+.2f}% | {r['h1_ann']*100:+.2f}% "
                 f"| {r['h1_sharpe']:.2f} | {r['h1_dd']*100:.2f}% |")

    L += ["", "## 3. 2026H1 逐月净收益与胜率", "",
          f"> 胜率 = 该方案跑赢现役 BASE 的月数 / {n_months} 个月", "",
          "| 方案 | " + " | ".join(str(p) for p in monthly.index)
          + " | 跑赢现役月数 |",
          "| --- | " + " | ".join(["---"] * len(monthly)) + " | --- |"]
    for k in monthly.columns:
        cells = " | ".join(f"{monthly.loc[p, k]*100:+.2f}%" for p in monthly.index)
        L.append(f"| {k} | {cells} | {win[k]}/{n_months} |")

    L += ["", "## 4. 2026H1 资产大类平均暴露", "",
          "| 方案 | " + " | ".join(group_expo(panel.loc[:E]).keys()) + " |",
          "| --- | " + " | ".join(["---"] * len(expos["BASE 现役"])) + " |"]
    for k, g in expos.items():
        L.append(f"| {k} | " + " | ".join(f"{v*100:.1f}%" for v in g.values()) + " |")

    (ROOT / "runs" / "tune_2026h1.md").write_text("\n".join(L), encoding="utf-8")
    (ROOT / "runs" / "tune_2026h1.json").write_text(
        json.dumps({"rows": rows, "passive": p_rows,
                    "win_vs_base": win}, ensure_ascii=False, indent=2,
                   default=str), encoding="utf-8")
    print("\n[h1] 报告已写出: " + str(ROOT / "runs" / "tune_2026h1.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
