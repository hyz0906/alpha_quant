#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三层组合第三轮：底仓波动惩罚指数 p 与再平衡频率（实验分支专用）。

前两轮线索：
  * method=inverse_vol (w ∝ 1/σ¹) → 三层后夏普 1.63
  * method=inverse_var (w ∝ 1/σ²) → 三层后夏普 2.87，但年化仅 3.73%（类债）
两者其实是同一个公式的两个端点：**w ∝ 1/σ^p**。本轮把 p 连续化扫描，
找「风险调整收益 vs 收益水平」的真实甜点，并检验再平衡频率（2W/M/Q）。

同时诊断各 p 下的**资产大类权重分布**，回答「夏普高是不是因为退化成债券」。

用法：python3 scripts/tune_round3.py
输出：runs/tune_round3.md / .json
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

import risk_parity as rp                                        # noqa: E402
from tune_three_layer import (BASE, MMF, KEYS, evaluate, load_panel,  # noqa: E402
                              fmt_table, row, pb_gate_daily, qdii_gate)
import portfolio_combined as pc                                 # noqa: E402

TD = rp.TRADING_DAYS
A_LEGS = list(pc.A_STOCK_LEGS)
Q_LEGS = list(pc.QDII_LEGS)

GROUPS = {
    "A股权益": A_LEGS,
    "QDII海外": Q_LEGS,
    "债券": ["511010.SH"],
    "货币": [MMF],
    "商品": ["518880.SH", "159985.SZ", "159981.SZ"],
}


def base_weights_p(panel: pd.DataFrame, lookback: int = 60, floor_ann: float = 0.025,
                   p: float = 1.0, freq: str = "M") -> pd.DataFrame:
    """w ∝ 1/σ^p 底仓，freq ∈ {M 月末, Q 季末, 2W 每10交易日}。"""
    rets = panel.pct_change(fill_method=None)
    vol = rets.rolling(lookback, min_periods=int(lookback * 0.5)).std()
    vol_f = vol.clip(lower=floor_ann / np.sqrt(TD))
    w = pd.DataFrame(0.0, index=panel.index, columns=panel.columns)

    s = panel.index.to_series()
    if freq == "M":
        reb = list(s.groupby(s.index.to_period("M")).last())
    elif freq == "Q":
        reb = list(s.groupby(s.index.to_period("Q")).last())
    elif freq == "2W":
        reb = list(panel.index[::10])
    else:
        raise ValueError(freq)

    for d in reb:
        r = vol_f.loc[d].dropna()
        if r.empty:
            continue
        inv = 1.0 / (r ** p)
        w.loc[d, r.index] = (inv / inv.sum()).values
    w = w.replace(0.0, np.nan).ffill().fillna(0.0)
    return w.shift(1).fillna(0.0)


def build_W(panel: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """三层组合（底仓用 p 参数化版本）。"""
    base = base_weights_p(panel, cfg.get("lb", 60), cfg.get("vf", 0.025),
                          cfg.get("p", 1.0), cfg.get("freq", "M"))
    W = base.copy()
    if cfg.get("pb_on", True):
        g = pb_gate_daily(panel.index, window=cfg.get("pb_win", 60),
                          rule=cfg.get("pb_rule", "triple"),
                          lo=cfg.get("pb_lo", 0.3), hi=cfg.get("pb_hi", 0.7),
                          source=cfg.get("pb_src", "510300.SH"))
        for c in cfg.get("pb_legs", A_LEGS):
            if c in W.columns:
                W[c] = W[c] * g
    if cfg.get("qdii_on", True):
        for c in Q_LEGS:
            g = qdii_gate(c, panel.index, cfg.get("z_win", 60), cfg.get("z_hi", 2.0),
                          cfg.get("q_floor", 1.0), cfg.get("min_hold", 5))
            W[c] = W[c] * g
    if cfg.get("cash_mmf", False):
        freed = (base.sum(axis=1) - W.sum(axis=1)).clip(lower=0.0)
        freed_m = freed.groupby(freed.index.to_period("M")).transform("first")
        W[MMF] = W[MMF] + freed_m
    return W


def group_expo(W: pd.DataFrame) -> dict:
    return {g: float(W[cols].sum(axis=1).mean()) for g, cols in GROUPS.items()}


def main() -> int:
    panel = load_panel()
    years = len(panel) / TD
    print(f"[r3] 面板 {panel.index.min().date()} ~ {panel.index.max().date()} "
          f"({years:.2f} 年)")

    # 一致性校验：p=1 / M 应等价于旧口径的 risk_parity inverse_vol
    # （L6 落地后模块默认已是 p=1.2/季频，须临时钉回旧值再比对）
    saved = (rp.VOL_LOOKBACK, rp.VOL_FLOOR_ANN, rp.VOL_P, rp.REBAL_FREQ)
    rp.VOL_P, rp.REBAL_FREQ = 1.0, "M"
    try:
        w_ref = rp.build_weights(panel, "inverse_vol").shift(1).fillna(0.0)
    finally:
        rp.VOL_LOOKBACK, rp.VOL_FLOOR_ANN, rp.VOL_P, rp.REBAL_FREQ = saved
    w_p1 = base_weights_p(panel, 60, 0.025, 1.0, "M")
    diff = float((w_ref - w_p1).abs().max().max())
    print(f"[r3] 一致性校验 p=1 vs inverse_vol: 最大差 {diff:.2e}")
    assert diff < 1e-12, "p=1 应与 inverse_vol 完全一致"

    sections: list[tuple[str, list[dict]]] = []
    nets: dict[str, pd.Series] = {}
    expos: dict[str, dict] = {}

    m_base = evaluate(panel, build_W(panel, BASE))
    base_rows = [row("**BASE 现役 (p=1.0)**", m_base)]
    nets["BASE p=1.0"] = m_base["net"]
    expos["BASE p=1.0"] = group_expo(build_W(panel, BASE))

    # ---------- 1. 波动惩罚指数 p ----------
    rows = list(base_rows)
    for p in [1.0, 1.2, 1.4, 1.6, 1.8, 2.0]:
        cfg = dict(BASE, p=p)
        m = evaluate(panel, build_W(panel, cfg))
        rows.append(row(f"p={p:.1f}", m))
        nets[f"p={p:.1f}"] = m["net"]
        expos[f"p={p:.1f}"] = group_expo(build_W(panel, cfg))
        print(f"[r3] p={p:.1f} 年化 {m['ann_ret']*100:+.2f}% 夏普 {m['sharpe']:.2f} "
              f"回撤 {m['max_dd']*100:.1f}% 换手 {m['turnover']:.2f} "
              f"| IS {m['is_sharpe']:.2f} OOS {m['oos_sharpe']:.2f}")
    sections.append(("1. 底仓波动惩罚指数 p（w ∝ 1/σ^p）", rows))
    print("[r3] 1/4 p 扫描 done")

    # ---------- 2. p × QDII 灵敏度（第二轮最优 z=1.5/floor=0.5） ----------
    rows = list(base_rows)
    for p in [1.0, 1.2, 1.4, 1.6]:
        cfg = dict(BASE, p=p, z_hi=1.5, q_floor=0.5)
        m = evaluate(panel, build_W(panel, cfg))
        rows.append(row(f"p={p:.1f} + z=1.5/floor=0.5", m))
        nets[f"p={p:.1f}+z1.5"] = m["net"]
        expos[f"p={p:.1f}+z1.5"] = group_expo(build_W(panel, cfg))
    sections.append(("2. p × QDII 灵敏度叠加", rows))
    print("[r3] 2/4 p×z done")

    # ---------- 3. 再平衡频率 ----------
    rows = list(base_rows)
    for freq in ["2W", "M", "Q"]:
        for p in [1.0, 1.4]:
            cfg = dict(BASE, p=p, freq=freq)
            m = evaluate(panel, build_W(panel, cfg))
            rows.append(row(f"freq={freq} + p={p:.1f}", m))
    sections.append(("3. 再平衡频率（2W 每10日 / M 月末 / Q 季末）", rows))
    print("[r3] 3/4 频率 done")

    # ---------- 4. 最终候选 ----------
    cands = {
        "BASE 现役": dict(BASE),
        "Q1 p=1.2": dict(BASE, p=1.2),
        "Q2 p=1.4": dict(BASE, p=1.4),
        "Q3 p=1.2 + z1.5/f0.5": dict(BASE, p=1.2, z_hi=1.5, q_floor=0.5),
        "Q4 p=1.4 + z1.5/f0.5": dict(BASE, p=1.4, z_hi=1.5, q_floor=0.5),
        "Q5 Q4 + 月频货币腿": dict(BASE, p=1.4, z_hi=1.5, q_floor=0.5, cash_mmf=True),
        "Q6 Q5 + 地板4%": dict(BASE, p=1.4, z_hi=1.5, q_floor=0.5, cash_mmf=True,
                           vf=0.04),
        "Q7 Q5 + 地板6%": dict(BASE, p=1.4, z_hi=1.5, q_floor=0.5, cash_mmf=True,
                           vf=0.06),
    }
    rows = []
    for name, cfg in cands.items():
        m = evaluate(panel, build_W(panel, cfg))
        nets[name] = m["net"]
        expos[name] = group_expo(build_W(panel, cfg))
        rows.append(row(name, m))
        print(f"[cand] {name:22s} 年化 {m['ann_ret']*100:+.2f}% "
              f"夏普 {m['sharpe']:.2f} 回撤 {m['max_dd']*100:.1f}% "
              f"换手 {m['turnover']:.2f} | IS {m['is_sharpe']:.2f} "
              f"OOS {m['oos_sharpe']:.2f}")
    sections.append(("4. 最终候选（IS 选优 → OOS 验证）", rows))

    # ---------- 报告 ----------
    L = ["# 三层组合第三轮：波动惩罚指数 p 与再平衡频率（实验分支）", "",
         f"- 样本 {panel.index.min().date()} ~ {panel.index.max().date()}"
         f"（{years:.2f} 年），IS/OOS 切分 2023-07-01，单边成本 0.15%",
         "- 底仓通式 **w ∝ 1/σ^p**：p=1 即现役逆波动，p=2 即逆方差（类债）。"
         "本轮把 p 连续化，找甜点。",
         f"- 一致性校验：p=1/月频 与原 inverse_vol 权重最大差 {diff:.1e}（等价）。", ""]
    for title, rws in sections:
        L += [f"## {title}", ""] + fmt_table(rws, KEYS) + [""]

    # 资产大类暴露诊断
    L += ["## 5. 资产大类平均暴露（诊断：夏普高是否=退化成债券）", "",
          "| 变体 | " + " | ".join(GROUPS.keys()) + " |",
          "| --- | " + " | ".join(["---"] * len(GROUPS)) + " |"]
    for k, g in expos.items():
        L.append(f"| {k} | " + " | ".join(f"{g[x]*100:.1f}%" for x in GROUPS) + " |")

    # 分年收益
    L += ["", "## 6. 候选分年净收益", ""]
    names = list(nets.keys())
    ymat = pd.DataFrame({k: v.groupby(v.index.year).apply(lambda x: (1 + x).prod() - 1)
                         for k, v in nets.items()})
    L += ["| 年份 | " + " | ".join(names) + " |",
          "| --- | " + " | ".join(["---"] * len(names)) + " |"]
    for y in ymat.index:
        L.append(f"| {y} | " + " | ".join(
            (f"{ymat.loc[y, k]*100:+.1f}%" if pd.notna(ymat.loc[y, k]) else "—")
            for k in names) + " |")

    (ROOT / "runs" / "tune_round3.md").write_text("\n".join(L), encoding="utf-8")
    (ROOT / "runs" / "tune_round3.json").write_text(
        json.dumps({"sections": {t: rws for t, rws in sections},
                    "exposure": expos}, ensure_ascii=False, indent=2,
                   default=str), encoding="utf-8")
    print("\n[r3] 报告已写出: " + str(ROOT / "runs" / "tune_round3.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
