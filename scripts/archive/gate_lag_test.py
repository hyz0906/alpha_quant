"""QDII 门控滞后敏感性测试：模拟 15:30 时净值滞后 1~2 个交易日的实盘代价。

复用 strategy_matrix 的 THREE 口径（INV 底仓 × PB 门控 × QDII_REAL 门控），
仅把 QDII 门控序列整体后移 lag 天。shift(0) 应复现夏普 1.31（口径校验）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import risk_parity as rp
import portfolio_combined as pc
import strategy_matrix as sm

COST = 0.0015
panel = sm.load_panel()
rets = panel.pct_change(fill_method=None)
years = len(panel) / rp.TRADING_DAYS
g_pb = pc.pb_gate_daily(panel.index, "triple")

print(f"面板 {panel.index.min().date()} ~ {panel.index.max().date()} ({len(panel)} 交易日 / {years:.2f} 年)")
print(f"{'lag':>4} {'净年化':>8} {'毛年化':>8} {'夏普':>6} {'回撤':>8} {'换手/年':>7} {'成本pp':>7} {'暴露QDII':>9}")
print("-" * 70)

results = {}
for lag in [0, 1, 2]:
    W = sm.inv_base(panel)
    for c in pc.A_STOCK_LEGS:
        W[c] = W[c] * g_pb.reindex(W.index).ffill().fillna(1.0)
    for c in pc.QDII_LEGS:
        gq = sm.qdii_gate(c, panel, "real")
        if lag:
            gq = gq.shift(lag).fillna(1.0)  # 无信号期视为持有（实盘开盘拿不到门控）
        W[c] = W[c] * gq.reindex(W.index).ffill().fillna(1.0)
    net, to = pc.backtest_net(panel, W, cost=COST)
    m = pc.full_metrics(net, W)
    gross = (W * rets).sum(axis=1)
    g_ann = (1 + gross).prod() ** (1 / years) - 1
    cost_drag = g_ann - m["ann_ret"]
    qdii_exp = float(W[pc.QDII_LEGS].sum(axis=1).mean())
    results[lag] = (m, qdii_exp)
    print(f"  {lag}  {m['ann_ret']*100:+7.2f}% {g_ann*100:+7.2f}% "
          f"{m['sharpe']:6.2f} {m['max_dd']*100:7.1f}% {m['turnover']:7.2f} "
          f"{cost_drag*100:7.2f} {qdii_exp*100:8.1f}%")

m0 = results[0][0]
print("-" * 70)
for lag in [1, 2]:
    m, exp = results[lag]
    print(f"lag={lag}: 夏普 {m0['sharpe']:.2f}→{m['sharpe']:.2f} "
          f"({(m['sharpe']/m0['sharpe']-1)*100:+.1f}%), "
          f"净年化 {m0['ann_ret']*100:+.2f}→{m['ann_ret']*100:+.2f}pp, "
          f"回撤 {m0['max_dd']*100:.1f}→{m['max_dd']*100:.1f}%, "
          f"换手 {m0['turnover']:.2f}→{m['turnover']:.2f}")
