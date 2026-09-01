# -*- coding: utf-8 -*-
"""截面动量 IC 检验（§7.25）：价值被证伪后的方向反查。

背景：§7.24 证明截面价值（pb/pe 分位、股息率）全持有期不显著且方向反偏
（贵者恒强）——反偏本身就是动量信号。本脚本在同一面板（51 只场内 ETF）
上检验截面动量因子：

  mom20 / mom60 / mom120 / mom250  —— 过去 N 日收益
  mom12_1                          —— 经典 12-1：过去 250 日、跳过最近 20 日
  rev20                            —— 短期反转（-mom20，A 股文献常见为正）

方法学与 §7.24 完全一致：截面 Spearman IC（20/60/120 日前向收益）、
NW-t（非重叠月采样防虚增）、五分位分层多空年化。样本 2019-09 ~ 2026-08。

用法：python3 scripts/danjuan_momentum_ic.py
输出：runs/danjuan_momentum_ic.md / .json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]  # archive 下移一层
sys.path.insert(0, str(ROOT / "scripts"))
import danjuan_cross_ic as base  # 复用 mapping/returns/nw_tstat

HORIZONS = [20, 60, 120]
N_QUANTILE = 5
MIN_CROSS = 15           # 截面最少标的数


def build_momentum_factors(px: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """动量因子宽表（行=交易日，列=ETF）。mom12_1 跳过最近 20 日。"""
    f = {
        "mom20": px / px.shift(20) - 1,
        "mom60": px / px.shift(60) - 1,
        "mom120": px / px.shift(120) - 1,
        "mom250": px / px.shift(250) - 1,
    }
    f["mom12_1"] = px.shift(20) / px.shift(250) - 1
    f["rev20"] = -f["mom20"]
    return f


def cross_sectional_ic(fmat: pd.DataFrame, fwd: pd.DataFrame,
                       dates: pd.DatetimeIndex) -> pd.Series:
    """逐日截面 Spearman IC（因子 vs 前向收益）。"""
    ics, ic_dates = [], []
    for d in dates:
        fv = fmat.loc[d].dropna()
        rv = fwd.loc[d, fv.index].dropna()
        common = fv.index.intersection(rv.index)
        if len(common) < MIN_CROSS:
            continue
        ics.append(fv[common].corr(rv[common], method="spearman"))
        ic_dates.append(d)
    return pd.Series(ics, index=pd.DatetimeIndex(ic_dates), dtype=float)


def layered_returns(fmat: pd.DataFrame, fwd: pd.DataFrame,
                    rebal_dates: pd.DatetimeIndex,
                    years: float) -> dict[int, dict]:
    """五分位分层：Q1=动量最弱组，Q5=动量最强组（等权、按调仓日快照）。"""
    port = {q: [] for q in range(1, N_QUANTILE + 1)}
    for d in rebal_dates:
        fv = fmat.loc[d].dropna()
        rv = fwd.loc[d, fv.index].dropna()
        common = fv.index.intersection(rv.index)
        if len(common) < MIN_CROSS:
            continue
        q = pd.qcut(fv[common].rank(method="first"), N_QUANTILE,
                    labels=False) + 1
        for qi in range(1, N_QUANTILE + 1):
            sel = q[q == qi].index
            port[qi].append(float(rv[sel].mean()))
    out = {}
    for qi, rs in port.items():
        s = pd.Series(rs, dtype=float).dropna()
        # 调仓间隔 ≈ 持有期 h 日 → 年化
        h = int(fwd.attrs.get("h", 60))
        ann = float(s.mean() * (244 / h)) if len(s) else np.nan
        out[qi] = {"n": int(len(s)), "ann": ann}
    ls = pd.Series(port[N_QUANTILE], dtype=float) - \
        pd.Series(port[1], dtype=float)
    h = int(fwd.attrs.get("h", 60))
    out["LS"] = {"n": int(ls.dropna().shape[0]),
                 "ann": float(ls.mean() * (244 / h))}
    return out


def main() -> int:
    panel = pd.read_csv(base.FUND / "danjuan_valuation_lsd.csv",
                        low_memory=False, parse_dates=["date"])
    mapping = base.build_mapping(panel)
    px = base.load_returns(mapping)
    px = px.loc["2018-06-01":]          # 留足 mom250 预热
    print(f"[mom] 价格面板 {px.shape[1]} 只, {px.index.min().date()} ~ "
          f"{px.index.max().date()}")

    factors = build_momentum_factors(px)
    dates_all = px.index[px.index >= "2019-09-04"]

    # 前向收益（T 日收盘算因子 → T+1 起持有 h 日，无靠前）
    fwd = {h: (px.shift(-(h + 1)) / px.shift(-1) - 1) for h in HORIZONS}
    for h in HORIZONS:
        fwd[h].attrs["h"] = h

    ic_stats: dict = {}
    ic_series: dict = {}
    for fname, fmat in factors.items():
        ic_stats[fname] = {}
        ic_series[fname] = {}
        for h in HORIZONS:
            valid = dates_all[fwd[h].loc[dates_all].notna().any(axis=1)]
            s = cross_sectional_ic(fmat, fwd[h], valid)
            ic_series[fname][h] = s
            s_m = s.iloc[::21]           # 非重叠月采样
            ic_stats[fname][h] = {
                "n_dates": int(len(s)),
                "ic_mean": float(s.mean()) if len(s) else np.nan,
                "ic_ir_monthly": float(s_m.mean() / s_m.std())
                if len(s_m) > 3 and s_m.std() > 0 else np.nan,
                "nw_t": base.nw_tstat(s_m.values, lags=6)
                if len(s_m) > 10 else np.nan,
            }
        print(f"[mom] {fname} done")

    # 分层（60d 持有、每 20 日调仓）
    h_main = 60
    rebal = dates_all[::20]
    rebal = rebal[fwd[h_main].loc[rebal].notna().any(axis=1)]
    years = (dates_all[-1] - dates_all[0]).days / 365.25
    layered = {fn: layered_returns(fm, fwd[h_main], rebal, years)
               for fn, fm in factors.items()}
    print("[mom] 分层完成")

    # 分年 IC（mom60, 60d 前向）
    yearly = {}
    for fname in ["mom60", "mom12_1"]:
        s = ic_series[fname][60]
        yearly[fname] = {str(y): float(g.mean())
                         for y, g in s.groupby(s.index.year)}

    # ---------- 报告 ----------
    L = ["# 截面动量 IC 检验报告（§7.25）",
         "",
         f"- 样本：{dates_all[0].date()} ~ {dates_all[-1].date()}，"
         f"{px.shape[1]} 只场内 ETF",
         "- 方法：截面 Spearman IC；NW-t 用非重叠月采样（§7.24 同款）",
         "- 因子口径：T 日收盘算动量 → T+1 起持有 h 日（无靠前）",
         "",
         "## 1. 截面 IC 总览",
         "",
         "| 因子 | 持有期 | 日期数 | IC均值 | ICIR(月采样) | NW-t |",
         "|---|---|---|---|---|---|"]
    for fname in factors:
        for h in HORIZONS:
            v = ic_stats[fname][h]
            L.append(f"| {fname} | {h}d | {v['n_dates']} "
                     f"| {v['ic_mean']:+.4f} | {v['ic_ir_monthly']:+.2f} "
                     f"| {v['nw_t']:+.2f} |")

    L += ["", "## 2. 五分位分层（60d 持有期，年化）", "",
          "| 因子 | Q1(最弱) | Q2 | Q3 | Q4 | Q5(最强) | 多空(Q5-Q1) |",
          "|---|---|---|---|---|---|---|"]
    for fname, qs in layered.items():
        L.append(f"| {fname} "
                 + " ".join(f"| {qs[q]['ann']*100:+.2f}% " for q in range(1, 6))
                 + f"| {qs['LS']['ann']*100:+.2f}% |")

    L += ["", "## 3. 分年 IC 均值（60d 前向）", "",
          "| 年份 | mom60 | mom12_1 |", "|---|---|---|"]
    years_all = sorted(set(yearly["mom60"]) | set(yearly["mom12_1"]))
    for y in years_all:
        L.append(f"| {y} | {yearly['mom60'].get(y, float('nan')):+.4f} "
                 f"| {yearly['mom12_1'].get(y, float('nan')):+.4f} |")

    L += ["", "## 4. 与三层组合的结合判断", "",
          "（见 WORKFLOW.md §7.25 结论）"]

    out_md = ROOT / "runs" / "danjuan_momentum_ic.md"
    out_md.write_text("\n".join(L), encoding="utf-8")
    (ROOT / "runs" / "danjuan_momentum_ic.json").write_text(
        json.dumps({"ic": ic_stats, "layered": layered, "yearly": yearly},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"[mom] 报告已写出: {out_md}")

    print("\n=== IC 总览（60d）===")
    for fname in factors:
        v = ic_stats[fname][60]
        print(f"{fname:8s} IC={v['ic_mean']:+.4f}  ICIR={v['ic_ir_monthly']:+.2f}"
              f"  NW-t={v['nw_t']:+.2f}  "
              f"多空年化={layered[fname]['LS']['ann']*100:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
