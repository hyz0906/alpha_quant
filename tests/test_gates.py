"""三层组合核心函数冒烟测试：无前视语义与状态机行为。

覆盖 portfolio_live 每日信号直接依赖的两个关键组件：
  * qdii_relchange_realistic.spike_avoid_hold — z 飙升回避状态机（QDII 门控）
  * risk_parity.build_weights — 月末定权、次月持有（逆波动底仓）
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import risk_parity as rp  # noqa: E402
from qdii_relchange_realistic import spike_avoid_hold  # noqa: E402


def _panel(n=400, cols=("A", "B", "C", "D"), seed=42):
    idx = pd.bdate_range("2023-01-02", periods=n)
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {c: (1 + rng.normal(0.0003, 0.01, n)).cumprod() for c in cols}, index=idx)


# ---------------------------------------------------------------- spike_avoid_hold
def test_spike_avoid_no_lookahead():
    """信号 T 日触发 → 空仓最早出现在 T+1 日（不允许当日生效）。"""
    idx = pd.bdate_range("2024-01-01", periods=60)
    z = pd.Series(0.0, index=idx)
    prem = pd.Series(0.02, index=idx)  # > floor=1%
    z.iloc[10] = 3.0                   # T=10 飙升
    h = spike_avoid_hold(z, prem)
    assert h.iloc[10] == 1.0           # 触发当日仍持有
    assert h.iloc[11] == 0.0           # T+1 才空仓
    assert (h.iloc[11:16] == 0.0).all()  # min_hold=5：空仓至少 5 日


def test_spike_avoid_floor_blocks_low_premium():
    """溢价低于 floor 时，z 再高也不触发（过滤低溢价噪声）。"""
    idx = pd.bdate_range("2024-01-01", periods=60)
    z = pd.Series(0.0, index=idx)
    prem = pd.Series(0.005, index=idx)  # < floor=1%
    z.iloc[10] = 5.0
    h = spike_avoid_hold(z, prem)
    assert (h == 1.0).all()


def test_spike_avoid_reentry_after_min_hold():
    """z 回落且空仓满 min_hold 后回补。"""
    idx = pd.bdate_range("2024-01-01", periods=60)
    z = pd.Series(0.0, index=idx)
    prem = pd.Series(0.02, index=idx)
    z.iloc[10] = 3.0
    h = spike_avoid_hold(z, prem)
    # 空仓自 T=11 起；T=16 时空仓第 6 日（≥min_hold）且 z 已回落 → 回补
    assert h.iloc[15] == 0.0
    assert h.iloc[16] == 1.0


# ---------------------------------------------------------------- build_weights
def test_build_weights_no_lookahead():
    """截断未来数据后，历史区间的权重必须完全不变（无前视）。"""
    panel = _panel()
    w_full = rp.build_weights(panel, "inverse_vol")
    w_part = rp.build_weights(panel.iloc[:300], "inverse_vol")
    # 截断面板的最后一行是「假性月末」（完整面板中该月尚未结束），排除后比较
    cmp_idx = w_part.index[:-1]
    pd.testing.assert_frame_equal(w_part.loc[cmp_idx], w_full.loc[cmp_idx])


def test_build_weights_sum_to_one():
    """已赋权的行权重和为 1（首个非零月末之后）。"""
    panel = _panel(n=200, cols=("A", "B", "C"), seed=1)
    w = rp.build_weights(panel, "inverse_vol")
    w = w[w.sum(axis=1) > 0]
    assert len(w) > 0
    assert ((w.sum(axis=1) - 1.0).abs() < 1e-9).all()
