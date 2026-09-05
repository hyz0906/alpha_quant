#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""改进方案的**跨窗口稳定性**扫描（实验分支专用）。

背景：单个窗口的结论会互相打架——
  * 2026H1：L2 +0.84pp（赢）、地板几乎无效（+0.07pp）
  * 2025Q1：L2 −0.81pp（输）、地板 +0.90pp（赢）
差异的根因是 regime（QDII 单边上涨 vs 低波震荡），不是参数本身。

本脚本把全样本切成**连续的半年窗口**（2020H1 ~ 2026H1）和**季度窗口**，
逐窗口算「方案 − 现役 BASE」的累计收益差，回答：
  这些改进到底是稳定的，还是只在特定 regime 下有效？

输出：runs/tune_stability.md（半年矩阵 + 季度胜率汇总）

用法：python3 scripts/tune_stability.py
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

from tune_three_layer import BASE, evaluate, load_panel            # noqa: E402
from tune_round3 import build_W                                    # noqa: E402
from tune_window import CANDIDATES, eval_window                    # noqa: E402

MIN_DAYS = 40      # 窗口最少交易日，太短的丢弃（如 2020H1 起始段）


def windows(panel: pd.DataFrame, freq: str) -> list[tuple[str, str, str]]:
    """切分窗口。注意：**不要传 "H"**——pandas 会当成「小时」而非半年，
    半年频率的正写是 "2Q-DEC"。返回 [(标签, 起, 止)]。"""
    s = panel.index.to_series()
    out = []
    for p, grp in s.groupby(s.index.to_period(freq)):
        lab = (f"{p.year}H{1 if p.quarter <= 2 else 2}" if freq == "2Q-DEC"
               else str(p))
        out.append((lab, grp.index.min(), grp.index.max()))
    return out


def main() -> int:
    panel = load_panel()
    print(f"[stab] 样本 {panel.index.min().date()} ~ {panel.index.max().date()}")

    Ws = {name: build_W(panel, cfg) for name, cfg in CANDIDATES.items()}
    Ws["BH300 沪深300"] = None      # 占位，单独处理

    # ---------- 半年矩阵 ----------
    half = [w for w in windows(panel, "2Q-DEC")]
    rows = []
    for name, cfg in CANDIDATES.items():
        W = Ws[name]
        r = {"name": name}
        for tag, a, b in half:
            s = eval_window(panel, W, str(a), str(b))
            r[tag] = s["cum"]
        rows.append(r)
    half_df = pd.DataFrame(rows).set_index("name")
    base = half_df.loc["BASE 现役"]
    diff_h = (half_df - base) * 100        # 超额 pp

    # 对照：BH300
    W300 = pd.DataFrame(0.0, index=panel.index, columns=panel.columns)
    W300["510300.SH"] = 1.0
    bh = {}
    for tag, a, b in half:
        bh[tag] = eval_window(panel, W300, str(a), str(b))["cum"]

    # ---------- 季度胜率 ----------
    quar = [w for w in windows(panel, "Q")]
    qdiff = {}
    for name, cfg in CANDIDATES.items():
        W = Ws[name]
        vals = {}
        for tag, a, b in quar:
            s = eval_window(panel, W, str(a), str(b))
            sb = eval_window(panel, Ws["BASE 现役"], str(a), str(b))
            vals[tag] = (s["cum"] - sb["cum"]) * 100
        qdiff[name] = vals
    q_df = pd.DataFrame(qdiff).T

    # ---------- 控制台 ----------
    print("\n== 半年窗口超额（pp，相对现役 BASE）==")
    print(diff_h.drop(index="BASE 现役").round(2).to_string())
    print("\n== 季度胜率 & 超额统计 ==")
    stat = pd.DataFrame({
        "胜率": (q_df > 0).sum(axis=1) / q_df.shape[1],
        "均值pp": q_df.mean(axis=1),
        "中位pp": q_df.median(axis=1),
        "最差pp": q_df.min(axis=1),
        "最好pp": q_df.max(axis=1),
    })
    print(stat.round(3).to_string())

    # ---------- 报告 ----------
    L = ["# 改进方案跨窗口稳定性扫描（实验分支 exp/new-strategy）", "",
         f"- 样本 {panel.index.min().date()} ~ {panel.index.max().date()}；"
         "数值为**累计收益超额（pp）= 方案 − 现役 BASE**",
         f"- 半年窗口 {len(half)} 个、季度窗口 {len(quar)} 个；"
         "季度胜率 = 超额 >0 的季度占比",
         "- **看胜率与最差值，不要只看均值**——均值会被个别大波动窗口带偏。",
         "", "## 1. 半年窗口超额矩阵（pp）", "",
         "| 方案 | " + " | ".join(t for t, _, _ in half) + " |",
         "| --- | " + " | ".join(["---"] * len(half)) + " |"]
    for name in diff_h.index:
        if name == "BASE 现役":
            continue
        cells = " | ".join(f"{diff_h.loc[name, t]:+.2f}" for t, _, _ in half)
        L.append(f"| {name} | {cells} |")
    L.append("| **BASE 现役（累计收益）** | " + " | ".join(
        f"{base[t]*100:+.2f}%" for t, _, _ in half) + " |")
    L.append("| BH300 沪深300（累计收益） | " + " | ".join(
        f"{bh[t]*100:+.2f}%" for t, _, _ in half) + " |")

    L += ["", "## 2. 季度窗口统计（超额 pp，相对现役）", "",
          "| 方案 | 胜率 | 均值 | 中位数 | 最差季度 | 最好季度 |",
          "|---|---|---|---|---|---|"]
    for name in stat.index:
        if name == "BASE 现役":
            continue
        r = stat.loc[name]
        L.append(f"| {name} | {r['胜率']*100:.0f}% | {r['均值pp']:+.2f}pp "
                 f"| {r['中位pp']:+.2f}pp | {r['最差pp']:+.2f}pp "
                 f"| {r['最好pp']:+.2f}pp |")

    L += ["", "## 3. 季度超额明细（pp）", "",
          "| 方案 | " + " | ".join(q_df.columns) + " |",
          "| --- | " + " | ".join(["---"] * len(q_df.columns)) + " |"]
    for name in q_df.index:
        if name == "BASE 现役":
            continue
        L.append(f"| {name} | " + " | ".join(
            f"{q_df.loc[name, c]:+.2f}" for c in q_df.columns) + " |")

    (ROOT / "runs" / "tune_stability.md").write_text("\n".join(L), encoding="utf-8")
    (ROOT / "runs" / "tune_stability.json").write_text(
        json.dumps({"half_pp": diff_h.round(4).to_dict(),
                    "quarter_pp": q_df.round(4).to_dict(),
                    "stat": stat.round(4).to_dict()}, ensure_ascii=False,
                   indent=2, default=str), encoding="utf-8")
    print("\n[stab] 报告已写出: " + str(ROOT / "runs" / "tune_stability.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
