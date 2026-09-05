#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三层组合第二轮：参数交互与「月频现金腿」修正版（实验分支专用）。

第一轮（tune_three_layer.py）的三个待验证点：
  1. z_hi=1.5 是唯一 IS/OOS 双升的旋钮 → 能否与 floor=0.5 叠加再进一步？
  2. VOL_FLOOR_ANN 是最干净的「收益档位」旋钮 → 与 z_hi=1.5 叠加是否仍单调？
  3. 现金→货币腿失败（换手 3.15→5.18 吃掉利息）→ 改成**月频调整**能否救回来？
     （日频跟随门控翻转买卖货币腿是败因；月频锁定则只在再平衡日动一次）

另测逆方差底座（夏普 2.87 的类债组合）在目标波动放大下的理论上限，
并显式标注其**需要融资、实盘不可直接执行**。

用法：python3 scripts/tune_round2.py
输出：runs/tune_round2.md / .json
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

from tune_three_layer import (BASE, MMF, KEYS, build_W, evaluate, load_panel,  # noqa: E402
                              fmt_table, row, inv_base)
import risk_parity as rp  # noqa: E402


def build_W_mmf_monthly(panel: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """门控空缺资金→货币腿，但只在月频锁定（月内不变），压降换手。"""
    base = inv_base(panel, cfg.get("lb", 60), cfg.get("vf", 0.025),
                    cfg.get("method", "inverse_vol"))
    W = build_W(panel, {**cfg, "cash_mmf": False})
    freed = (base.sum(axis=1) - W.sum(axis=1)).clip(lower=0.0)
    # 每月首个交易日的释放量，月内保持不变
    freed_m = freed.groupby(freed.index.to_period("M")).transform("first")
    W[MMF] = W[MMF] + freed_m
    return W


def main() -> int:
    panel = load_panel()
    years = len(panel) / rp.TRADING_DAYS
    print(f"[r2] 面板 {panel.index.min().date()} ~ {panel.index.max().date()} "
          f"({years:.2f} 年)")

    sections: list[tuple[str, list[dict]]] = []
    nets: dict[str, pd.Series] = {}

    m_base = evaluate(panel, build_W(panel, BASE))
    base_rows = [row("**BASE 现役**", m_base)]
    nets["BASE 现役"] = m_base["net"]

    # ---------- 1. QDII 双旋钮交互 ----------
    rows = list(base_rows)
    for z in [1.5, 2.0]:
        for f in [0.5, 1.0]:
            cfg = dict(BASE, z_hi=z, q_floor=f)
            rows.append(row(f"z_hi={z} + floor={f}%",
                            evaluate(panel, build_W(panel, cfg))))
    for z, w in [(1.5, 60), (1.5, 90), (1.5, 120), (2.0, 90)]:
        cfg = dict(BASE, z_hi=z, z_win=w, q_floor=0.5)
        rows.append(row(f"z_hi={z} + floor=0.5% + z_win={w}",
                        evaluate(panel, build_W(panel, cfg))))
    sections.append(("1. QDII 层双旋钮交互（z_hi × floor × window）", rows))
    print("[r2] 1/4 QDII 交互 done")

    # ---------- 2. 收益档位（VOL_FLOOR）× QDII z_hi ----------
    rows = list(base_rows)
    for vf in [0.025, 0.04, 0.06, 0.08]:
        for z, tag in [(2.0, "现役z=2.0"), (1.5, "z=1.5")]:
            cfg = dict(BASE, vf=vf, z_hi=z)
            m = evaluate(panel, build_W(panel, cfg))
            rows.append(row(f"地板={vf*100:.1f}% + {tag}", m))
            if vf in (0.04, 0.06) and z == 1.5:
                nets[f"地板{vf*100:.0f}%+z1.5"] = m["net"]
    sections.append(("2. 收益档位（波动率地板）× QDII 灵敏度", rows))
    print("[r2] 2/4 地板×z done")

    # ---------- 3. 现金腿三方案对比 ----------
    rows = list(base_rows)
    m = evaluate(panel, build_W(panel, dict(BASE, cash_mmf=True)))
    rows.append(row("现金→货币腿【日频】(第一轮败案)", m))
    m = evaluate(panel, build_W_mmf_monthly(panel, BASE))
    rows.append(row("现金→货币腿【月频锁定】", m))
    nets["现金→货币腿(月频)"] = m["net"]
    m = evaluate(panel, build_W_mmf_monthly(panel, dict(BASE, z_hi=1.5, q_floor=0.5)))
    rows.append(row("月频货币腿 + z=1.5/floor=0.5", m))
    m = evaluate(panel, build_W_mmf_monthly(panel, dict(BASE, vf=0.06)))
    rows.append(row("月频货币腿 + 地板6%", m))
    sections.append(("3. 门控空缺资金：现金 / 日频货币腿 / 月频货币腿", rows))
    print("[r2] 3/4 现金腿 done")

    # ---------- 4. 逆方差底座 + 目标波动放大（理论，需融资） ----------
    rows = list(base_rows)
    m = evaluate(panel, build_W(panel, dict(BASE, method="inverse_var")))
    rows.append(row("逆方差底座（不加杠杆）", m))
    nets["逆方差底座"] = m["net"]
    for vt in [0.04, 0.06, 0.08]:
        cfg = dict(BASE, method="inverse_var", vol_target=vt, max_lev=8.0)
        m = evaluate(panel, build_W(panel, cfg))
        rows.append(row(f"逆方差 + 目标波动{vt*100:.0f}%（理论/需融资）", m))
        nets[f"逆方差×目标波动{vt*100:.0f}%"] = m["net"]
    sections.append(("4. 逆方差底座（类债、夏普高但收益低）+ 波动放大", rows))
    print("[r2] 4/4 逆方差 done")

    # ---------- 5. 最终候选（IS → OOS 验证） ----------
    cands = {
        "BASE 现役": dict(BASE),
        "P1 仅 z=1.5/floor=0.5": dict(BASE, z_hi=1.5, q_floor=0.5),
        "P2 地板4%（收益档↑）": dict(BASE, vf=0.04),
        "P3 地板6%（收益档↑↑）": dict(BASE, vf=0.06),
        "P4 P1+地板4%": dict(BASE, z_hi=1.5, q_floor=0.5, vf=0.04),
        "P5 P1+地板6%": dict(BASE, z_hi=1.5, q_floor=0.5, vf=0.06),
        "P6 P5+月频货币腿": dict(BASE, z_hi=1.5, q_floor=0.5, vf=0.06,
                            cash_mmf=True),
    }
    rows = []
    for name, cfg in cands.items():
        W = (build_W_mmf_monthly(panel, cfg) if cfg.get("cash_mmf")
             else build_W(panel, cfg))
        m = evaluate(panel, W)
        nets[name] = m["net"]
        rows.append(row(name, m))
        print(f"[cand] {name:22s} 年化 {m['ann_ret']*100:+.2f}% "
              f"夏普 {m['sharpe']:.2f} 回撤 {m['max_dd']*100:.1f}% "
              f"换手 {m['turnover']:.2f} | IS {m['is_sharpe']:.2f} "
              f"OOS {m['oos_sharpe']:.2f}")
    sections.append(("5. 最终候选（IS 选优 → OOS 验证）", rows))

    # ---------- 报告 ----------
    L = ["# 三层组合第二轮：参数交互与月频现金腿（实验分支）", "",
         f"- 样本 {panel.index.min().date()} ~ {panel.index.max().date()}"
         f"（{years:.2f} 年），IS/OOS 切分 2023-07-01，单边成本 0.15%",
         "- 第 1 轮结论复核：z_hi=1.5 唯一 IS/OOS 双升；地板是干净的收益档位旋钮；"
         "日频货币腿换手爆炸。本轮验证交互与月频修正版。", ""]
    for title, rws in sections:
        L += [f"## {title}", ""] + fmt_table(rws, KEYS) + [""]

    L += ["## 6. 候选分年净收益", ""]
    names = list(nets.keys())
    ymat = pd.DataFrame({k: v.groupby(v.index.year).apply(lambda x: (1 + x).prod() - 1)
                         for k, v in nets.items()})
    L += ["| 年份 | " + " | ".join(names) + " |",
          "| --- | " + " | ".join(["---"] * len(names)) + " |"]
    for y in ymat.index:
        L.append(f"| {y} | " + " | ".join(
            (f"{ymat.loc[y, k]*100:+.1f}%" if pd.notna(ymat.loc[y, k]) else "—")
            for k in names) + " |")
    L += ["", "> 注：逆方差系列为类债组合（权益暴露极低），目标波动放大需融资，"
          "实际可得性受融资成本与额度约束，仅作理论上限参考。"]

    (ROOT / "runs" / "tune_round2.md").write_text("\n".join(L), encoding="utf-8")
    (ROOT / "runs" / "tune_round2.json").write_text(
        json.dumps({t: rws for t, rws in sections}, ensure_ascii=False,
                   indent=2, default=str), encoding="utf-8")
    print("\n[r2] 报告已写出: " + str(ROOT / "runs" / "tune_round2.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
