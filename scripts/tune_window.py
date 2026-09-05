#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三层组合任意窗口回测对比（实验分支专用）。

前面三轮调参用的是全样本 + IS/OOS(2023-07) 切分；本脚本做「指定窗口」的
fresh 验证——窗口不参与任何参数选择，只看结论能否外推。

换手口径修正：上一版 `W.loc[start:end].diff()` 首行为 NaN，漏掉了窗口首个
交易日的再平衡换手（对季度/年度窗口尤其重要）。本版向前多取一行再 diff。

用法：
    python3 scripts/tune_window.py --start 2025-01-01 --end 2025-03-31 --tag 2025q1
    python3 scripts/tune_window.py --start 2026-01-01 --end 2026-06-30 --tag 2026h1

输出：runs/tune_window_<tag>.md / .json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import risk_parity as rp                                              # noqa: E402
import portfolio_combined as pc                                       # noqa: E402
from tune_three_layer import (BASE, COST, evaluate, load_panel)       # noqa: E402
from tune_round3 import build_W, group_expo                           # noqa: E402

TD = rp.TRADING_DAYS

COMMON = dict(freq="Q", z_hi=1.5, q_floor=0.5, cash_mmf=True)

CANDIDATES = {
    "BASE 现役": dict(BASE),
    "① 仅 z=1.5/floor=0.5": dict(BASE, z_hi=1.5, q_floor=0.5),
    "② 仅 季频再平衡": dict(BASE, freq="Q"),
    "③ 仅 月频货币腿": dict(BASE, cash_mmf=True),
    "④ 仅 地板4%": dict(BASE, vf=0.04),
    "⑤ 仅 地板6%": dict(BASE, vf=0.06),
    "⑥ 仅 p=1.2": dict(BASE, p=1.2),
    "⑦ 仅 PB门控关": dict(BASE, pb_on=False),
    "L2 三项免费改进": dict(COMMON, p=1.0),
    "L3 L2+地板4%": dict(COMMON, vf=0.04),
    "L4 L2+地板6%(收益优先)": dict(COMMON, vf=0.06),
    "L6 L2+地板6%+p1.2": dict(COMMON, p=1.2, vf=0.06),
}

PASSIVE = {"EW18 等权月再平衡": "equal", "BH300 沪深300买入持有": "bh300",
           "债货50/50(511010+511880)": "bondmmf"}


def eval_window(panel: pd.DataFrame, W: pd.DataFrame, start: str, end: str,
                cost: float = COST) -> dict:
    """窗口指标。换手向前多取一行，计入窗口首个交易日的再平衡。"""
    net, _ = pc.backtest_net(panel, W, cost)
    sub = net.loc[start:end]
    m = rp.metrics(sub) if len(sub) >= 30 else {}
    years = len(sub) / TD
    idx = W.loc[start:end].index
    pos = W.index.get_loc(idx[0])
    prev = W.index[max(pos - 1, 0)]                    # 窗口前一交易日
    dW = W.loc[prev:end].diff().abs().sum(axis=1).iloc[1:]
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


def passive_W(panel: pd.DataFrame, kind: str) -> pd.DataFrame:
    if kind == "equal":
        return rp.build_weights(panel, "equal").shift(1).fillna(0.0)
    W = pd.DataFrame(0.0, index=panel.index, columns=panel.columns)
    if kind == "bh300":
        W["510300.SH"] = 1.0
    else:
        W["511010.SH"] = 0.5
        W["511880.SH"] = 0.5
    return W


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--tag", required=True)
    a = ap.parse_args()

    panel = load_panel()
    sub_idx = panel.loc[a.start:a.end].index
    n = len(sub_idx)
    yrs = n / TD
    se = 1 / (yrs ** 0.5)            # 夏普的 1σ 标准误（粗略）
    print(f"[win] 窗口 {sub_idx.min().date()} ~ {sub_idx.max().date()} "
          f"（{n} 交易日 / {yrs:.2f} 年）")
    print(f"[win] 夏普 1σ 标准误 ≈ {se:.2f}（差异小于此值不具统计显著性）\n")

    rows, subs, expos = [], {}, {}
    for name, cfg in CANDIDATES.items():
        W = build_W(panel, cfg)
        m = evaluate(panel, W)
        s = eval_window(panel, W, a.start, a.end)
        subs[name] = s["net"]
        expos[name] = group_expo(W.loc[a.start:a.end])
        rows.append({"name": name, "h1_cum": s["cum"], "h1_ann": s["ann_ret"],
                     "h1_vol": s["ann_vol"], "h1_sharpe": s["sharpe"],
                     "h1_dd": s["max_dd"], "h1_turn": s["turnover"],
                     "h1_expo": s["expo"], "full_ann": m["ann_ret"],
                     "full_sharpe": m["sharpe"], "oos_sharpe": m["oos_sharpe"]})
    base_row = rows[0]

    p_rows = []
    for label, kind in PASSIVE.items():
        s = eval_window(panel, passive_W(panel, kind), a.start, a.end)
        subs[label] = s["net"]
        p_rows.append((label, s))

    print(f"{'方案':<24} {'窗口累计':>9} {'年化':>8} {'夏普':>7} {'回撤':>8} "
          f"{'换手/年':>7} {'vs现役':>9}")
    print("-" * 82)
    for r in rows:
        d = (r["h1_cum"] - base_row["h1_cum"]) * 100
        print(f"{r['name']:<24} {r['h1_cum']*100:>8.2f}% {r['h1_ann']*100:>7.2f}% "
              f"{r['h1_sharpe']:>7.2f} {r['h1_dd']*100:>7.2f}% {r['h1_turn']:>7.2f} "
              f"{d:>+8.2f}pp")
    for label, s in p_rows:
        print(f"{label:<24} {s['cum']*100:>8.2f}% {s['ann_ret']*100:>7.2f}% "
              f"{s['sharpe']:>7.2f} {s['max_dd']*100:>7.2f}% {s['turnover']:>7.2f} "
              f"{'—':>9}")

    monthly = pd.DataFrame({k: v.groupby(v.index.to_period("M")).apply(
        lambda x: (1 + x).prod() - 1) for k, v in subs.items()})
    win = {k: int((monthly[k] > monthly["BASE 现役"]).sum()) for k in monthly.columns}

    L = [f"# 三层组合窗口回测：{a.start} ~ {a.end}（实验分支 exp/new-strategy）", "",
         f"- **窗口**：{sub_idx.min().date()} ~ {sub_idx.max().date()}"
         f"（{n} 交易日 / {yrs:.2f} 年），未参与任何参数选择",
         f"- ⚠️ **统计显著性**：本窗口夏普的 1σ 标准误约 **{se:.2f}**"
         f"（≈ 1/√年数），方案间夏普差值小于该值**不具统计意义**——"
         "本窗口只能验证方向，不能验证幅度。",
         f"- 全样本对照 {panel.index.min().date()} ~ {panel.index.max().date()}，"
         "单边成本 0.15%", "",
         "## 1. 窗口内各方案表现", "",
         "| 方案 | 窗口累计 | 年化 | 波动 | 夏普 | 回撤 | 换手/年 | 暴露 | vs现役 | "
         "全样本年化 | 全样本夏普 | OOS夏普 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        d = (r["h1_cum"] - base_row["h1_cum"]) * 100
        tag = "" if r["name"] == "BASE 现役" else f"{d:+.2f}pp"
        L.append(f"| {r['name']} | {r['h1_cum']*100:+.2f}% | {r['h1_ann']*100:+.2f}% "
                 f"| {r['h1_vol']*100:.2f}% | {r['h1_sharpe']:.2f} "
                 f"| {r['h1_dd']*100:.2f}% | {r['h1_turn']:.2f} "
                 f"| {r['h1_expo']*100:.1f}% | {tag} | {r['full_ann']*100:+.2f}% "
                 f"| {r['full_sharpe']:.2f} | {r['oos_sharpe']:.2f} |")
    L += ["", "## 2. 被动对照", "",
          "| 基准 | 窗口累计 | 年化 | 夏普 | 回撤 |", "|---|---|---|---|---|"]
    for label, s in p_rows:
        L.append(f"| {label} | {s['cum']*100:+.2f}% | {s['ann_ret']*100:+.2f}% "
                 f"| {s['sharpe']:.2f} | {s['max_dd']*100:.2f}% |")
    L += ["", "## 3. 逐月净收益与胜率", "",
          f"> 胜率 = 跑赢现役 BASE 的月数 / {len(monthly)} 个月", "",
          "| 方案 | " + " | ".join(str(p) for p in monthly.index) + " | 胜 |",
          "| --- | " + " | ".join(["---"] * len(monthly)) + " | --- |"]
    for k in monthly.columns:
        cells = " | ".join(f"{monthly.loc[p, k]*100:+.2f}%" for p in monthly.index)
        L.append(f"| {k} | {cells} | {win[k]}/{len(monthly)} |")
    L += ["", "## 4. 窗口内资产大类平均暴露", "",
          "| 方案 | " + " | ".join(group_expo(panel.loc[:a.end]).keys()) + " |",
          "| --- | " + " | ".join(["---"] * len(expos["BASE 现役"])) + " |"]
    for k, g in expos.items():
        L.append(f"| {k} | " + " | ".join(f"{v*100:.1f}%" for v in g.values()) + " |")

    (ROOT / "runs" / f"tune_window_{a.tag}.md").write_text(
        "\n".join(L), encoding="utf-8")
    (ROOT / "runs" / f"tune_window_{a.tag}.json").write_text(
        json.dumps({"window": [a.start, a.end], "rows": rows,
                    "passive": {k: {kk: vv for kk, vv in s.items() if kk != "net"}
                                for k, s in p_rows},
                    "win_vs_base": win}, ensure_ascii=False, indent=2,
                   default=str), encoding="utf-8")
    print(f"\n[win] 报告已写出: runs/tune_window_{a.tag}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
