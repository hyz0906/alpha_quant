#!/usr/bin/env python3
"""基本面/估值因子（carry + value）。

与 factor_library.py 的价格因子族互补，这里的因子基于估值/股息等基本面数据。
数据由 fundamental_loader 提供（当前为快照 + 1 年短窗，见其 docstring 的局限）。

因子定义：
  * earnings_yield(pe)      ：盈利收益率 = 1/PE（%），carry 代理（持有权益的现金流回报）
  * valuation_percentile(s) ：序列 s 当前值在其自身历史中的分位（0~1），value 代理
  * equity_risk_premium(ey, r)：盈利收益率 − 无风险利率，股债性价比
  * dividend_yield(dy)      ：股息率（%），carry（分红现金流）
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def earnings_yield(pe: pd.Series | float) -> pd.Series | float:
    """盈利收益率（%） = 100 / PE（TTM）。"""
    return 100.0 / np.asarray(pe, dtype=float)


def dividend_yield(dy: pd.Series | float) -> pd.Series | float:
    """股息率（%），直接透传（单位已是 %）。"""
    return dy


def valuation_percentile(s: pd.Series) -> float:
    """序列当前值在自身历史中的分位（0~1），越低越便宜。"""
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return float("nan")
    return float((s <= s.iloc[-1]).mean())


def rolling_percentile(s: pd.Series, window: int) -> pd.Series:
    """滚动分位（用于估值时间序列 → 因子面板）。window 日内当前值的分位。"""
    return s.rolling(window, min_periods=int(window * 0.5)).apply(
        lambda x: float((x <= x.iloc[-1]).mean()), raw=False
    )


def equity_risk_premium(ey: float, risk_free: float) -> float:
    """股权风险溢价（%）= 盈利收益率 − 无风险利率（如 10 年期国债收益率）。"""
    return float(ey) - float(risk_free)


if __name__ == "__main__":
    # 冒烟
    s = pd.Series([10, 12, 11, 14, 13, 9, 8])
    assert abs(valuation_percentile(s) - 0.0) < 1e-9   # 8 是最低值 → 分位 0
    assert abs(earnings_yield(20.0) - 5.0) < 1e-9
    assert abs(equity_risk_premium(5.0, 2.5) - 2.5) < 1e-9
    print("冒烟通过：估值分位 / 盈利收益率 / 股债性价比 均正常")
