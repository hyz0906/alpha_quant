# -*- coding: utf-8 -*-
"""三层组合 + 截面动量倾斜层（§7.25）：回答「截面 IC 能否与三策略结合」。

§7.24 截面价值证伪、§7.25 前半证明长周期动量（mom12_1）边缘显著
（IC +0.09~0.15、NW-t ~1.8、2022 年后逐年为正）——不够格做独立第四层，
本脚本测它作为**温和倾斜层**的增量：

  W_final = 组内动量倾斜( W_ERC ) × gate_PB × gate_QDII

倾斜设计（刻意保守，避免掩盖门控效果）：
  * 频率：与 ERC 再平衡同节奏（月频），**不新增调仓事件**；
  * 范围：仅 A股 7 腿 / QDII 6 腿 / 商品 3 腿三组组内倾斜，
    债券 2 腿不倾斜（动量对类现金资产无意义）；
  * 强度：w_i' = w_i × (1 + tilt × z_i)，z_i 为 mom12_1 组内截面 z 分，
    **组内归一**（Σw' = Σw，不改变组敞口，只重分配），tilt ∈ {0.1, 0.2, 0.3}；
  * 时点：再平衡日 t 用 t−1 日动量（与 build_weights shift(1) 同口径，无靠前）。

用法：python3 scripts/portfolio_momentum_tilt.py
输出：runs/portfolio_momentum_tilt.md / .json
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

TILT_GROUPS = {
    "A股": pc.A_STOCK_LEGS,
    "QDII": pc.QDII_LEGS,
    "商品": ["518880.SH", "159985.SZ", "159981.SZ"],
}
MOM_SKIP, MOM_WIN = 20, 250          # mom12_1：过去 250 日、跳过最近 20 日


def momentum_z(panel: pd.DataFrame) -> pd.DataFrame:
    """mom12_1 宽表（列=资产），未标准化。"""
    px = panel
    return px.shift(MOM_SKIP) / px.shift(MOM_WIN) - 1


def apply_tilt(W_erc: pd.DataFrame, mom: pd.DataFrame,
               tilt: float) -> pd.DataFrame:
    """在 ERC 权重变化行（月度再平衡执行日）做组内动量倾斜并归一。"""
    W = W_erc.copy()
    change = W_erc.ne(W_erc.shift()).any(axis=1)
    for t in W_erc.index[change]:
        pos = W_erc.index.get_loc(t)
        if pos == 0:
            continue
        t_sig = W_erc.index[pos - 1]          # 信号日 = 执行日前一交易日
        if t_sig not in mom.index:
            continue
        m = mom.loc[t_sig]
        for legs in TILT_GROUPS.values():
            cols = [c for c in legs if c in W.columns]
            if len(cols) < 3:
                continue
            mv = m[cols].dropna()
            w0 = W_erc.loc[t, cols]
            active = w0[w0 > 0].index.intersection(mv.index)
            if len(active) < 3 or mv[active].std() <= 0:
                continue
            z = (mv[active] - mv[active].mean()) / mv[active].std()
            z = z.clip(-1.5, 1.5)             # 限幅防爆
            w_new = w0[active] * (1.0 + tilt * z)
            w_new = w_new.clip(lower=0.0)
            if w_new.sum() > 0:
                w_new = w_new * (w0[active].sum() / w_new.sum())
            W.loc[t, active] = w_new
    return W


def main() -> int:
    closes = {c: pd.read_csv(ROOT / "data" / f"{c}.csv",
                             parse_dates=["date"]).set_index("date")["close"]
              for c in rp.HETERO_CODES}
    panel = pd.DataFrame(closes).sort_index().dropna()
    print(f"[tilt] 面板 {panel.shape[1]} 只, "
          f"{panel.index.min().date()} ~ {panel.index.max().date()}")

    W_erc = rp.build_weights(panel, "inverse_vol").shift(1).fillna(0.0)
    mom = momentum_z(panel)

    rows = []
    nets = {}
    for tilt in [0.0, 0.10, 0.20, 0.30]:
        W_t = apply_tilt(W_erc, mom, tilt) if tilt > 0 else W_erc
        # 门控（复用 §7.20 口径：PB 门控 A 股腿 + QDII 日频门控）
        W = W_t.copy()
        g = pc.pb_gate_daily(panel.index, "triple")
        for c in pc.A_STOCK_LEGS:
            if c in W.columns:
                W[c] = W[c] * g.reindex(W.index).ffill().fillna(1.0)
        for c in pc.QDII_LEGS:
            if c in W.columns:
                gq = pc.qdii_gate_daily(c, W.index)
                W[c] = W[c] * gq.reindex(W.index).ffill().fillna(1.0)

        net, to = pc.backtest_net(panel, W)
        m = pc.full_metrics(net, W)
        nets[tilt] = net
        rows.append({"tilt": tilt, **m})
        print(f"[tilt] tilt={tilt:.2f} 年化={m['ann_ret']*100:+.2f}% "
              f"夏普={m['sharpe']:.2f} 回撤={m['max_dd']*100:.1f}% "
              f"换手={m['turnover']:.2f}")

    base = rows[0]
    L = ["# 三层组合 + 截面动量倾斜层（§7.25）",
         "",
         f"- 样本：{panel.index.min().date()} ~ {panel.index.max().date()}",
         "- 基线：D 档三层全开（ERC × PB门控 × QDII门控），单边成本 0.15%",
         "- 倾斜：月频、组内（A股/QDII/商品）、mom12_1 z 分限幅 ±1.5、组内归一",
         "",
         "## 1. 倾斜强度敏感性",
         "",
         "| tilt | 年化 | 夏普 | 最大回撤 | 换手/年 | vs 基线超额 |",
         "|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['tilt']:.2f} | {r['ann_ret']*100:+.2f}% "
                 f"| {r['sharpe']:.2f} | {r['max_dd']*100:.1f}% "
                 f"| {r['turnover']:.2f} "
                 f"| {(r['ann_ret']-base['ann_ret'])*100:+.2f}pp |")

    L += ["", "## 2. 分年收益（tilt=0.20 vs 基线）", "",
          "| 年份 | 基线 | tilt=0.20 | 差 |", "|---|---|---|---|"]
    y0 = pc.yearly(nets[0.0])
    y2 = pc.yearly(nets[0.20])
    for y in y0.index:
        L.append(f"| {y} | {y0[y]*100:+.2f}% | {y2[y]*100:+.2f}% "
                 f"| {(y2[y]-y0[y])*100:+.2f}pp |")

    out = ROOT / "runs" / "portfolio_momentum_tilt.md"
    out.write_text("\n".join(L), encoding="utf-8")
    (ROOT / "runs" / "portfolio_momentum_tilt.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"[tilt] 报告已写出: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
