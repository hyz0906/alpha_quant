#!/usr/bin/env python3
"""RSRS 因子有效性诊断：截面 IC + 分层收益 + 多空收益（扩展池 12 只）。

回答 §9 的核心问题：RSRS 截面轮动在扩展池上跑输等权，是「因子失效」还是
「池子同质化稀释截面差异」？用三个标准因子检验给出证据：

  1. 截面 IC（Spearman rank IC）：每交易日，RSRS 修正分与未来 k 日收益的
     截面秩相关。IC 均值 / ICIR(均值÷标准差) / 正 IC 占比衡量区分度。
  2. 分层收益：每交易日按修正分分 5 组（quintile），看各组未来收益是否
     单调递增（高分组收益 > 低分组）。
  3. 多空收益：Q5−Q1 分层收益差（多头 top 组 减 空头 bottom 组）。

RSRS 修正分复刻 vibe_exporter 模板的语义：N=18 OLS beta → M=600 zscore → ×R2。
未来收益用 next-k-day close 收益，k ∈ {5,10,20}，对齐周频(≈5日)/月频(≈20日)
调仓周期。

用法：python3 scripts/factor_diagnosis.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]  # archive 下移一层
DATA_DIR = ROOT / "data"

CODES = [
    "510300.SH", "510500.SH", "159915.SZ", "512010.SH", "159928.SZ",
    "512880.SH", "512660.SH", "512400.SH", "513100.SH", "513500.SH",
    "513050.SH", "518880.SH",
]
N = 18
M = 600
MIN_PERIODS = 60
HORIZONS = [5, 10, 20]
N_QUANTILES = 5


def rsrs_score(df: pd.DataFrame) -> pd.Series:
    """复刻 vibe_exporter 模板的 RSRS 修正分（z*R2）。"""
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    if len(df) < N + 2:
        return pd.Series(float("nan"), index=df.index)

    x_win = np.lib.stride_tricks.sliding_window_view(low, N)
    y_win = np.lib.stride_tricks.sliding_window_view(high, N)
    x_mean = x_win.mean(axis=1, keepdims=True)
    y_mean = y_win.mean(axis=1, keepdims=True)
    numerator = ((x_win - x_mean) * (y_win - y_mean)).sum(axis=1)
    denominator = ((x_win - x_mean) ** 2).sum(axis=1)
    beta = np.divide(
        numerator, denominator,
        out=np.full_like(numerator, float("nan")), where=denominator != 0,
    )
    x_std = x_win.std(axis=1)
    y_std = y_win.std(axis=1)
    corr = np.divide(
        numerator / N, x_std * y_std,
        out=np.full_like(numerator, float("nan")), where=(x_std * y_std) != 0,
    )
    r2 = corr ** 2

    pad = np.full(N - 1, float("nan"))
    beta_s = pd.Series(np.concatenate([pad, beta]), index=df.index)
    r2_s = pd.Series(np.concatenate([pad, r2]), index=df.index)
    roll_mean = beta_s.rolling(M, min_periods=MIN_PERIODS).mean()
    roll_std = beta_s.rolling(M, min_periods=MIN_PERIODS).std()
    z = (beta_s - roll_mean) / roll_std.replace(0, float("nan"))
    return z * r2_s


def spearman_ic(x: pd.Series, y: pd.Series) -> float:
    """截面秩相关（Spearman）。只算两者都非 NaN 的标的。"""
    mask = x.notna() & y.notna()
    if mask.sum() < 3:
        return float("nan")
    return float(x[mask].rank().corr(y[mask].rank()))


def main():
    # ---- 1. 读数据 + 算 RSRS 修正分 ----
    scores = {}
    closes = {}
    for code in CODES:
        df = pd.read_csv(DATA_DIR / f"{code}.csv", parse_dates=["date"]).set_index("date")
        df = df.sort_index()
        closes[code] = df["close"]
        scores[code] = rsrs_score(df)

    close_panel = pd.DataFrame(closes).sort_index()
    score_panel = pd.DataFrame(scores).sort_index()

    # ---- 2. 未来 k 日收益（前视，用于 IC 的 y） ----
    fwd = {k: close_panel.shift(-k) / close_panel - 1.0 for k in HORIZONS}

    # ---- 3. 截面 IC ----
    print("=" * 78)
    print("RSRS 因子诊断：截面 IC（扩展池 12 只，共同样本 2017-09 起）")
    print("=" * 78)
    ic_stats = {}
    for k in HORIZONS:
        ics = []
        for t in score_panel.index:
            ic = spearman_ic(score_panel.loc[t], fwd[k].loc[t])
            if not np.isnan(ic):
                ics.append(ic)
        ics = np.array(ics)
        mean_ic = float(ics.mean())
        icir = float(mean_ic / ics.std()) if ics.std() > 0 else 0.0
        pos_rate = float((ics > 0).mean())
        ic_stats[k] = {
            "mean_ic": round(mean_ic, 4),
            "icir": round(icir, 4),
            "ic_std": round(float(ics.std()), 4),
            "positive_ic_rate": round(pos_rate, 4),
            "n_obs": int(len(ics)),
        }
        print(f"  horizon={k:>2}d  IC均值 {mean_ic:+.4f} | ICIR {icir:+.3f} | "
              f"IC标准差 {ics.std():.4f} | 正IC占比 {pos_rate:.1%} | 观测 {len(ics)}")

    # ---- 4. 分层收益（quintile） ----
    print("\n" + "=" * 78)
    print("分层收益：每交易日按修正分分 5 组，各组未来收益均值（Q1低→Q5高）")
    print("=" * 78)
    layered = {}
    for k in HORIZONS:
        q_ret = {q: [] for q in range(1, N_QUANTILES + 1)}
        for t in score_panel.index:
            s = score_panel.loc[t]
            r = fwd[k].loc[t]
            mask = s.notna() & r.notna()
            if mask.sum() < N_QUANTILES:
                continue
            ss = s[mask]
            rr = r[mask]
            try:
                labels = pd.qcut(ss.rank(method="first"), N_QUANTILES,
                                 labels=False) + 1
            except ValueError:
                continue
            for q in range(1, N_QUANTILES + 1):
                q_ret[q].append(float(rr[labels == q].mean()))
        layered[k] = {
            q: round(float(np.mean(q_ret[q])), 6) for q in q_ret
        }
        qs = layered[k]
        ls = list(qs.values())
        mono = all(ls[i] <= ls[i + 1] for i in range(len(ls) - 1))
        spread = ls[-1] - ls[0]
        print(f"  horizon={k:>2}d  Q1 {qs[1]*100:+.2f}%  Q2 {qs[2]*100:+.2f}%  "
              f"Q3 {qs[3]*100:+.2f}%  Q4 {qs[4]*100:+.2f}%  Q5 {qs[5]*100:+.2f}%"
              f"  | 多空(Q5-Q1) {spread*100:+.2f}%  | 单调 {'是' if mono else '否'}")

    # ---- 5. 输出 JSON ----
    out = {
        "codes": CODES,
        "n": N, "m": M, "min_periods": MIN_PERIODS,
        "ic": ic_stats,
        "layered": layered,
    }
    (ROOT / "runs/factor_diagnosis.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nJSON 已写入: runs/factor_diagnosis.json")


if __name__ == "__main__":
    main()
