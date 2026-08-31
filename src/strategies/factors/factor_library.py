#!/usr/bin/env python3
"""AlphaQuant 候选因子库：统一面板接口，供截面筛选与后续轮动策略调用。

设计约定（对齐 factor_screening.py 与 §7.9 诊断标准）：
  * 每个因子是「日期 × 标的」面板（pd.DataFrame），值为该标的在 t 时刻的
    因子暴露，只使用 t 及之前的信息（无前视偏差）。
  * 截面选股时对每行（同一交易日）做 cross-sectional rank 即可，因子量纲
    差异不影响 Spearman IC。
  * 输入面板统一由 build_panels() 产出：close/high/low/volume 四张面板，
    index 为交易日、columns 为标的代码。

因子族：
  动量    mom_20 / mom_60 / mom_120 / mom_240_20(12-1) / mom_60_20(跳过近1月)
  波动    vol_20 / vol_60 / max_dd_60 / downside_vol_60
  趋势    ma_ratio_20 / ma_ratio_60 / ma_slope_60
  量价    volume_ratio_20 / dollar_vol_20 / pv_corr_20
  综合    mom_vol_adj_60（风险调整动量）
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


def build_panels(close: pd.DataFrame, high: pd.DataFrame,
                 low: pd.DataFrame, volume: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """对齐四张面板并返回，同时附加 ret / dollar_volume 派生面板。"""
    panels = {
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "ret": close.pct_change(fill_method=None),
        "dollar_volume": volume * close,  # 成交额（元），量纲可比
    }
    return panels


# --------------------------------------------------------------------------- #
# 动量族
# --------------------------------------------------------------------------- #
def mom(close: pd.DataFrame, window: int) -> pd.DataFrame:
    """t 时点的 window 日动量 = close[t]/close[t-window] - 1。"""
    return close / close.shift(window) - 1.0


def mom_skip(close: pd.DataFrame, lookback: int, skip: int) -> pd.DataFrame:
    """跳过最近 skip 日的动量：close[t-skip]/close[t-lookback] - 1（12-1 型）。"""
    return close.shift(skip) / close.shift(lookback) - 1.0


# --------------------------------------------------------------------------- #
# 波动族
# --------------------------------------------------------------------------- #
def realized_vol(ret: pd.DataFrame, window: int) -> pd.DataFrame:
    """window 日已实现波动率（日收益标准差，未年化）。"""
    return ret.rolling(window, min_periods=int(window * 0.5)).std()


def max_drawdown(close: pd.DataFrame, window: int) -> pd.DataFrame:
    """window 日内最大回撤（负值，越负越差）。"""
    running_max = close.rolling(window, min_periods=1).max()
    dd = close / running_max - 1.0
    return dd.rolling(window, min_periods=1).min()


def downside_vol(ret: pd.DataFrame, window: int) -> pd.DataFrame:
    """window 日下行波动率（semi-deviation，仅负收益）。"""
    neg = ret.where(ret < 0, 0.0)
    return neg.rolling(window, min_periods=int(window * 0.5)).std()


# --------------------------------------------------------------------------- #
# 趋势族
# --------------------------------------------------------------------------- #
def ma_ratio(close: pd.DataFrame, window: int) -> pd.DataFrame:
    """价格相对均线的偏离度 = close/MA(window) - 1。"""
    ma = close.rolling(window, min_periods=int(window * 0.5)).mean()
    return close / ma - 1.0


def ma_slope(close: pd.DataFrame, window: int) -> pd.DataFrame:
    """MA(window) 的归一化线性斜率（slope / MA），衡量趋势方向与强度。

    用 sliding_window_view 向量化 OLS 斜率，避免逐行 rolling apply。
    """
    arr = close.to_numpy(dtype=float)
    if arr.shape[0] < window:
        return pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    w = sliding_window_view(arr, window, axis=0)          # (T-window+1, n_code, window)
    x = np.arange(window, dtype=float) - (window - 1) / 2.0
    x_mean = x.mean()
    x_demean = x - x_mean
    denom = (x_demean ** 2).sum()
    slope = (w * x_demean).sum(axis=2) / denom            # (T-window+1, n_code)
    # 归一化：slope 除以「窗口内 close 均值」，量纲变为「每交易日相对涨幅」
    wmean = w.mean(axis=2)                                 # 每窗口 close 均值
    norm_slope = slope / wmean
    pad = np.full((window - 1, arr.shape[1]), np.nan)
    out = np.concatenate([pad, norm_slope], axis=0)
    return pd.DataFrame(out, index=close.index, columns=close.columns)


# --------------------------------------------------------------------------- #
# 量价 / 流动性族
# --------------------------------------------------------------------------- #
def volume_ratio(volume: pd.DataFrame, window: int) -> pd.DataFrame:
    """量比 = volume / MA(volume, window) - 1（自归一，跨标的可比）。"""
    ma = volume.rolling(window, min_periods=int(window * 0.5)).mean()
    return volume / ma - 1.0


def dollar_volume_log(dollar_volume: pd.DataFrame, window: int) -> pd.DataFrame:
    """window 日均成交额（对数），代理规模/流动性。"""
    return np.log1p(dollar_volume.rolling(window, min_periods=int(window * 0.5)).mean())


def pv_corr(close: pd.DataFrame, volume: pd.DataFrame, window: int) -> pd.DataFrame:
    """价格与成交量的滚动相关性（量价背离：负相关=放量下跌/缩量上涨背离）。"""
    return close.rolling(window, min_periods=int(window * 0.5)).corr(volume)


# --------------------------------------------------------------------------- #
# 综合族
# --------------------------------------------------------------------------- #
def mom_vol_adj(close: pd.DataFrame, ret: pd.DataFrame, window: int) -> pd.DataFrame:
    """风险调整动量 = window 动量 / window 波动率（类夏普动量）。"""
    return mom(close, window) / realized_vol(ret, window).replace(0.0, np.nan)


# --------------------------------------------------------------------------- #
# 因子注册表：name -> (func, 面板依赖, 参数)
# 供 factor_screening.py 批量计算；新因子在此追加即可。
# --------------------------------------------------------------------------- #
FACTOR_REGISTRY: dict[str, dict] = {
    # 动量
    "mom_20":      {"func": mom,          "args": ("close", 20)},
    "mom_60":      {"func": mom,          "args": ("close", 60)},
    "mom_120":     {"func": mom,          "args": ("close", 120)},
    "mom_240_20":  {"func": mom_skip,     "args": ("close", 240, 20)},
    "mom_60_20":   {"func": mom_skip,     "args": ("close", 60, 20)},
    # 波动
    "vol_20":      {"func": realized_vol, "args": ("ret", 20)},
    "vol_60":      {"func": realized_vol, "args": ("ret", 60)},
    "max_dd_60":   {"func": max_drawdown, "args": ("close", 60)},
    "downside_vol_60": {"func": downside_vol, "args": ("ret", 60)},
    # 趋势
    "ma_ratio_20": {"func": ma_ratio,     "args": ("close", 20)},
    "ma_ratio_60": {"func": ma_ratio,     "args": ("close", 60)},
    "ma_slope_60": {"func": ma_slope,     "args": ("close", 60)},
    # 量价 / 流动性
    "volume_ratio_20": {"func": volume_ratio, "args": ("volume", 20)},
    "dollar_vol_20":   {"func": dollar_volume_log, "args": ("dollar_volume", 20)},
    "pv_corr_20":      {"func": pv_corr,   "args": ("close", "volume", 20)},
    # 综合
    "mom_vol_adj_60":  {"func": mom_vol_adj, "args": ("close", "ret", 60)},
}


def compute_factor(name: str, panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """按注册表计算单个因子面板。

    args 中每个元素：若为字符串则视为面板名（panels 的 key）解析为面板；
    否则（如 int 窗口长度）原样透传。
    """
    meta = FACTOR_REGISTRY[name]
    resolved = [panels[a] if isinstance(a, str) else a for a in meta["args"]]
    return meta["func"](*resolved)


def compute_all_factors(panels: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """批量计算全部注册因子。"""
    return {name: compute_factor(name, panels) for name in FACTOR_REGISTRY}


if __name__ == "__main__":
    # 冒烟：合成数据验证各因子可跑通、维度正确
    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(0)
    close = pd.DataFrame(
        10 + np.cumsum(rng.normal(0, 0.1, (300, 3)), axis=0),
        index=idx, columns=["A", "B", "C"],
    )
    high = close * 1.02
    low = close * 0.98
    volume = pd.DataFrame(rng.integers(10_000, 100_000, (300, 3)),
                          index=idx, columns=close.columns)
    p = build_panels(close, high, low, volume)
    fs = compute_all_factors(p)
    for name, f in fs.items():
        assert f.shape == close.shape, f"{name} 维度错误 {f.shape}"
        assert f.index.equals(close.index), f"{name} 索引不对齐"
    print(f"冒烟通过：{len(fs)} 个因子均正常计算")
    for name, f in fs.items():
        print(f"  {name:18s} 非NaN起始 {f.notna().idxmax().min():%Y-%m-%d}  "
              f"末值样本 {f.iloc[-1].notna().sum()}/3")
