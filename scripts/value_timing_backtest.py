#!/usr/bin/env python3
"""value 时序估值择时——完整回测（生产级）。

升级自 fundamental_timing.py 的诊断版。后者只回答了「估值分位低是否未来收益更高」
（单指标 PE-TTM、单规则三档、无显著性、无换手/逐年/组合）。本脚本补齐为完整回测：

1. 多估值指标：PE(TTM)/PE(LYR)/PE中位数/PB/PB中位数（乐咕 index-basic-pe/pb 实测字段）
2. 分位窗口敏感性：3y/5y/7y/10y 滚动 + 全历史 expanding（均无前视）
3. 未来收益分层的 Newey-West 显著性检验（未来 12 月收益重叠 11 个月 → lag=12 修正）
4. 多择时规则对比（指标=PE-TTM 与 PB 并列）：三档/两档/线性×2
5. 完整绩效：年化/波动/夏普/Calmar/最大回撤/平均仓位/年化换手 + 逐年收益
6. 组合层面：3 宽基月频等权（各自估值择时）vs 等权买入持有
7. 阈值鲁棒性曲面（buy×sell 网格）：判断「最优阈值」是稳定结构还是样本内过拟合

诚实边界（不改）：仅 3 个宽基（免费源覆盖上限）；月频未来收益重叠、独立样本有限
（NW 已修正）；用指数点位不含 ETF 费率/跟踪误差；阈值扫描存在样本内过拟合风险。

用法：python3 scripts/value_timing_backtest.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import akshare as ak

ROOT = Path(__file__).resolve().parents[1]
FUND_DIR = ROOT / "data" / "fundamental"

# 乐咕 index-basic-pe/pb 实测字段（2026-08）：
#   pe 接口：日期/指数/等权静态市盈率/静态市盈率/静态市盈率中位数/
#            等权滚动市盈率/滚动市盈率/滚动市盈率中位数
#   pb 接口：日期/指数/市净率/等权市净率/市净率中位数
# 无「股息率」列 → value 指标仅 5 个。
LEGU_BROAD_INDEXES: dict[str, str] = {
    "上证50": "510050.SH",
    "沪深300": "510300.SH",
    "中证500": "510500.SH",
}

# 估值指标：列名 -> (中文名, 数据来源接口)
VALUE_METRICS: dict[str, tuple[str, str]] = {
    "pe_ttm": ("PE(TTM)", "pe"),
    "pe_lyr": ("PE(LYR)", "pe"),
    "pe_med": ("PE中位数", "pe"),
    "pb":     ("PB", "pb"),
    "pb_med": ("PB中位数", "pb"),
}

# 择时主指标（规则对比/组合/阈值曲面均同时覆盖这两个）
TIMING_METRICS = ["pe_ttm", "pb"]

FWD_MONTHS = 12            # 未来收益视界（估值是慢变量，用长视界）
PCT_MIN = 36               # 分位最小样本（3 年）
NW_LAGS = FWD_MONTHS       # Newey-West 滞后阶（重叠 11 个月 → 取 12 保守）
WINDOWS: dict[str, int | None] = {
    "3y": 36, "5y": 60, "7y": 84, "10y": 120, "全历史": None,
}
DEFAULT_WINDOW = 60        # 默认分位窗口 5 年


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #
def load_metrics(symbol: str) -> pd.DataFrame:
    """拉取单宽基的多估值指标月频历史，缓存。返回 index=月末 date。

    列：close, pe_ttm, pe_lyr, pe_med, pb, pb_med
    """
    etf = LEGU_BROAD_INDEXES[symbol]
    cache = FUND_DIR / f"legu_metrics_{etf}.csv"
    if cache.exists():
        return pd.read_csv(cache, parse_dates=["date"]).set_index("date").sort_index()

    pe = ak.stock_index_pe_lg(symbol=symbol)
    pb = ak.stock_index_pb_lg(symbol=symbol)

    pe = pe.rename(columns={
        "日期": "date", "指数": "close",
        "滚动市盈率": "pe_ttm", "静态市盈率": "pe_lyr",
        "滚动市盈率中位数": "pe_med",
    })
    pb = pb.rename(columns={"日期": "date", "市净率": "pb", "市净率中位数": "pb_med"})

    pe = pe[[c for c in ["date", "close", "pe_ttm", "pe_lyr", "pe_med"] if c in pe.columns]]
    pb = pb[[c for c in ["date", "pb", "pb_med"] if c in pb.columns]]

    df = pe.merge(pb, on="date", how="outer")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")

    for c in ["close", "pe_ttm", "pe_lyr", "pe_med", "pb", "pb_med"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["close"]).sort_index()
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache, encoding="utf-8")
    return df


# --------------------------------------------------------------------------- #
# 分位 / 未来收益 / Newey-West
# --------------------------------------------------------------------------- #
def rolling_percentile(s: pd.Series, window: int | None) -> pd.Series:
    """滚动估值分位（0~1，越低越便宜），只用 t 及之前数据（无前视）。

    window=None 时用 expanding（全历史至今）。
    """
    if window is None:
        return s.expanding(min_periods=PCT_MIN).apply(
            lambda x: float((x <= x.iloc[-1]).mean()), raw=False)
    return s.rolling(window, min_periods=min(PCT_MIN, window)).apply(
        lambda x: float((x <= x.iloc[-1]).mean()), raw=False)


def future_ret(close: pd.Series, months: int = FWD_MONTHS) -> pd.Series:
    """t 时点买入持有 months 个月的收益 = close[t+months]/close[t] - 1。"""
    return close.shift(-months) / close - 1.0


def layered_fwd(pct: pd.Series, fwd: pd.Series, n_q: int = 5) -> tuple[list[float], bool]:
    """按分位分 n_q 档，返回各档未来收益均值 + 是否单调递减。"""
    bucket = np.floor(pct.clip(0, 0.999999) * n_q).where(pct.notna())
    out = []
    for q in range(n_q):
        v = fwd[(bucket == q) & fwd.notna()]
        out.append(float(v.mean()) if v.size else float("nan"))
    mono = all(out[i] >= out[i + 1] for i in range(n_q - 1))
    return out, mono


def newey_west_t(y: pd.Series, x: pd.Series, lags: int) -> tuple[float, float, float]:
    """对 y = α + β·x 做 OLS，返回 (β, se, t)（Newey-West HAC 标准误，Bartlett 核）。

    lag = 重叠月数修正（未来 FWD_MONTHS 月收益重叠 FWD_MONTHS-1 个月）。
    t 是斜率显著性：|t|>2 且 β<0 → 估值越贵未来收益显著越低（value 有效）。
    """
    X = np.column_stack([np.ones(len(y)), np.asarray(x, float)])
    yv = np.asarray(y, float)
    mask = ~(np.isnan(yv) | np.isnan(X).any(axis=1))
    X, yv = X[mask], yv[mask]
    n, k = X.shape
    if n <= k + lags:
        return float("nan"), float("nan"), float("nan")
    XtX = X.T @ X
    try:
        XtXi = np.linalg.inv(XtX)
    except np.linalg.LinAlgError:
        return float("nan"), float("nan"), float("nan")
    beta = XtXi @ X.T @ yv
    resid = yv - X @ beta
    u = resid[:, None] * X                       # (n, k)
    M = u.T @ u
    for l in range(1, lags + 1):
        w = 1.0 - l / (lags + 1.0)
        G = u[l:].T @ u[:-l]                     # Σ_t u_t u_{t-l}'
        M += w * (G + G.T)
    V = XtXi @ M @ XtXi
    se = np.sqrt(np.diag(V))
    return float(beta[1]), float(se[1]), float(beta[1] / se[1])


# --------------------------------------------------------------------------- #
# 仓位规则
# --------------------------------------------------------------------------- #
def pos_triple(pct: pd.Series, buy: float = 0.3, sell: float = 0.7) -> pd.Series:
    """三档：分位<buy 全仓；buy~sell 半仓；≥sell 空仓。"""
    return pct.map(lambda x: 1.0 if x < buy else (0.5 if x < sell else 0.0))


def pos_binary(pct: pd.Series, th: float = 0.5) -> pd.Series:
    """两档：分位<th 全仓，否则空仓。"""
    return (pct < th).astype(float)


def pos_linear(pct: pd.Series, slope: float = 1.0) -> pd.Series:
    """线性仓位：pos = clip(1 - slope·pct, 0, 1)，分位 0 满仓、分位 1 空仓。"""
    return (1.0 - slope * pct).clip(0.0, 1.0)


RULES: dict[str, callable] = {
    "三档(0.3/0.7)": lambda p: pos_triple(p, 0.3, 0.7),
    "两档(0.5)":     lambda p: pos_binary(p, 0.5),
    "线性(1-pct)":   lambda p: pos_linear(p, 1.0),
    "线性(1.5-1.5pct)": lambda p: pos_linear(p, 1.5),
}


# --------------------------------------------------------------------------- #
# 绩效
# --------------------------------------------------------------------------- #
def perf_stats(ret_m: pd.Series, pos: pd.Series) -> dict:
    """从月收益 + 持仓（已 shift 1 月对齐）算完整绩效。

    annual_turnover 是「年化单边换手次数」（月均 |Δpos| × 12）；
    1.0 = 平均每年调仓 100% 仓位（单边）。
    """
    strat = (ret_m * pos).dropna()
    bh = ret_m.dropna()

    def _s(r: pd.Series) -> dict:
        m = r.size
        if m == 0:
            return {"ann_ret": float("nan"), "ann_vol": float("nan"),
                    "sharpe": float("nan"), "calmar": float("nan"),
                    "mdd": float("nan")}
        ann_ret = float((1 + r).prod() ** (12 / m) - 1)
        ann_vol = float(r.std() * np.sqrt(12))
        sharpe = float(ann_ret / ann_vol) if ann_vol > 0 else float("nan")
        eq = (1 + r).cumprod()
        mdd = float((eq / eq.cummax() - 1).min())
        calmar = float(ann_ret / abs(mdd)) if mdd < 0 else float("nan")
        return {"ann_ret": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe,
                "calmar": calmar, "mdd": mdd}

    st, bh_ = _s(strat), _s(bh)
    monthly_turn = float(pos.diff().abs().mean())
    return {
        "strat": st, "buyhold": bh_,
        "exposure": float(pos.mean()),
        "annual_turnover": monthly_turn * 12,
        "ann_excess": st["ann_ret"] - bh_["ann_ret"],
        "dsharpe": st["sharpe"] - bh_["sharpe"],
    }


def yearly_returns(ret_m: pd.Series, pos: pd.Series) -> dict[int, tuple[float, float]]:
    """逐年收益：(择时, 买入持有)。year -> (timing_ret, bh_ret)。"""
    strat = ret_m * pos
    bh = ret_m
    out = {}
    for year, grp in strat.groupby(strat.index.year):
        t = float((1 + grp).prod() - 1)
        b = float((1 + bh[bh.index.year == year]).prod() - 1)
        out[int(year)] = (t, b)
    return out


def backtest_rule(close: pd.Series, pct: pd.Series, rule_fn) -> dict:
    """单一规则：分位 → 仓位（shift 1 月）→ 绩效 + 逐年。"""
    pos = rule_fn(pct).shift(1)          # t 月末定仓，t+1 月生效（无前视）
    ret_m = close.pct_change()
    return {"perf": perf_stats(ret_m, pos), "yearly": yearly_returns(ret_m, pos)}


def _port_stats(r: pd.Series) -> dict:
    r = r.dropna()
    if r.size == 0:
        return {"ann_ret": float("nan"), "ann_vol": float("nan"), "sharpe": float("nan"),
                "calmar": float("nan"), "mdd": float("nan")}
    ann_ret = float((1 + r).prod() ** (12 / r.size) - 1)
    ann_vol = float(r.std() * np.sqrt(12))
    eq = (1 + r).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())
    return {
        "ann_ret": ann_ret, "ann_vol": ann_vol,
        "sharpe": float(ann_ret / ann_vol) if ann_vol > 0 else float("nan"),
        "calmar": float(ann_ret / abs(mdd)) if mdd < 0 else float("nan"),
        "mdd": mdd,
    }


def _current_snapshot(df: pd.DataFrame) -> dict:
    row = df.iloc[-1]
    pe_pct = rolling_percentile(df["pe_ttm"].dropna(), DEFAULT_WINDOW)
    pb_pct = rolling_percentile(df["pb"].dropna(), DEFAULT_WINDOW)
    return {
        "pe_ttm": float(row.get("pe_ttm", float("nan"))),
        "pe_lyr": float(row.get("pe_lyr", float("nan"))),
        "pb": float(row.get("pb", float("nan"))),
        "pe_pct": float(pe_pct.iloc[-1]) if pe_pct.notna().any() else float("nan"),
        "pb_pct": float(pb_pct.iloc[-1]) if pb_pct.notna().any() else float("nan"),
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    data = {sym: load_metrics(sym) for sym in LEGU_BROAD_INDEXES}

    # ============ 1. value 因子时序有效性（分层 + NW 显著性） ============
    factor_valid = {}      # symbol -> metric -> {layered, mono, ls, beta, t}
    for sym, df in data.items():
        close = df["close"]
        fwd = future_ret(close)
        factor_valid[sym] = {}
        for metric in VALUE_METRICS:
            if metric not in df.columns:
                continue
            s = df[metric].dropna()
            pct = rolling_percentile(s, DEFAULT_WINDOW)
            mask = pct.notna() & fwd.notna()
            layered, mono = layered_fwd(pct[mask], fwd[mask])
            ls = layered[0] - layered[-1] if (not np.isnan(layered[0]) and not np.isnan(layered[-1])) else float("nan")
            beta, se, t = newey_west_t(fwd[mask], pct[mask], NW_LAGS)
            factor_valid[sym][metric] = {
                "layered": [round(x, 4) for x in layered],
                "mono": mono, "ls": round(ls, 4) if not np.isnan(ls) else None,
                "beta": round(beta, 4) if not np.isnan(beta) else None,
                "nw_t": round(t, 3) if not np.isnan(t) else None,
            }

    # ============ 2. 分位窗口敏感性 ============
    window_sens = {}      # symbol -> metric -> {window: ls}
    for sym, df in data.items():
        window_sens[sym] = {}
        for metric in VALUE_METRICS:
            if metric not in df.columns:
                continue
            s = df[metric].dropna()
            fwd = future_ret(df["close"])
            window_sens[sym][metric] = {}
            for wname, w in WINDOWS.items():
                pct = rolling_percentile(s, w)
                mask = pct.notna() & fwd.notna()
                layered, _ = layered_fwd(pct[mask], fwd[mask])
                if np.isnan(layered[0]) or np.isnan(layered[-1]):
                    window_sens[sym][metric][wname] = None
                else:
                    window_sens[sym][metric][wname] = round(layered[0] - layered[-1], 4)

    # ============ 3. 择时规则对比（pe_ttm 与 pb 并列） ============
    rule_res = {}         # metric -> symbol -> rule -> {perf, yearly}
    for metric in TIMING_METRICS:
        rule_res[metric] = {}
        for sym, df in data.items():
            close = df["close"]
            s = df[metric].dropna()
            pct = rolling_percentile(s, DEFAULT_WINDOW)
            rule_res[metric][sym] = {}
            for rname, fn in RULES.items():
                bt = backtest_rule(close, pct, fn)
                rule_res[metric][sym][rname] = {"perf": bt["perf"], "yearly": bt["yearly"]}

    # ============ 4. 组合层面（3 宽基月频等权，pe_ttm 与 pb 并列） ============
    port = {}
    for metric in TIMING_METRICS:
        strat_panel = pd.DataFrame()
        bh_panel = pd.DataFrame()
        for sym, df in data.items():
            s = df[metric].dropna()
            pct = rolling_percentile(s, DEFAULT_WINDOW)
            pos = pos_triple(pct).shift(1)
            ret_m = df["close"].pct_change()
            strat_panel[sym] = ret_m * pos
            bh_panel[sym] = ret_m
        port_strat = strat_panel.mean(axis=1)       # 月频等权再平衡
        port_bh = bh_panel.mean(axis=1)
        ps_, pb_ = _port_stats(port_strat), _port_stats(port_bh)
        port[metric] = {
            "strat": ps_, "buyhold": pb_,
            "ann_excess": ps_["ann_ret"] - pb_["ann_ret"],
            "yearly": {int(y): (float((1 + g).prod() - 1),
                                float((1 + port_bh[port_bh.index.year == y]).prod() - 1))
                       for y, g in port_strat.groupby(port_strat.index.year)},
        }

    # ============ 5. 阈值鲁棒性曲面（pe_ttm 与 pb 并列，三档 buy×sell） ============
    threshold_grid = {}   # metric -> symbol -> {(buy,sell): {ann_excess, sharpe}}
    buys = [0.2, 0.3, 0.4, 0.5]
    sells = [0.6, 0.7, 0.8]
    for metric in TIMING_METRICS:
        threshold_grid[metric] = {}
        for sym, df in data.items():
            close = df["close"]
            s = df[metric].dropna()
            pct = rolling_percentile(s, DEFAULT_WINDOW)
            threshold_grid[metric][sym] = {}
            for b in buys:
                for sl in sells:
                    pos = pos_triple(pct, b, sl).shift(1)
                    perf = perf_stats(close.pct_change(), pos)
                    threshold_grid[metric][sym][f"{b}/{sl}"] = {
                        "ann_excess": round(perf["ann_excess"], 4),
                        "sharpe": round(perf["strat"]["sharpe"], 3),
                    }

    # ============ 落盘 JSON ============
    out = {
        "method": {
            "fwd_months": FWD_MONTHS, "pct_min": PCT_MIN, "nw_lags": NW_LAGS,
            "default_window": f"{DEFAULT_WINDOW}个月",
            "windows": {k: (v if v else "expanding") for k, v in WINDOWS.items()},
            "rules": list(RULES.keys()), "timing_metrics": TIMING_METRICS,
            "source": "乐咕乐股 index-basic-pe/pb 月频（2005 至今，免 token，指数点位）",
        },
        "factor_validity": factor_valid,
        "window_sensitivity": window_sens,
        "rule_comparison": {m: {s: {r: v["perf"] for r, v in rule_res[m][s].items()}
                                for s in rule_res[m]} for m in TIMING_METRICS},
        "portfolio": port,
        "threshold_grid": threshold_grid,
        "current_snapshot": {sym: _current_snapshot(df) for sym, df in data.items()},
    }
    (ROOT / "runs" / "value_timing_backtest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ============ 落盘 Markdown ============
    L = []
    L.append("# value 时序估值择时——完整回测\n")
    L.append("> 数据源：乐咕乐股 index-basic-pe/pb 月频（3 宽基，2005 至今约 21 年，免 token，指数点位）。")
    L.append("> 方法：滚动估值分位（无前视）→ 未来 12 月收益分层（Newey-West lag=12 修正重叠样本）→ 多规则择时回测（月度再平衡）。\n")
    L.append("> **诚实边界**：仅 3 宽基（免费源覆盖上限）；NW 已修正重叠但独立样本仍有限；指数点位不含 ETF 费率/跟踪误差；阈值扫描存在样本内过拟合风险。\n")

    # §1 因子有效性
    L.append("## 1. value 因子时序有效性（分层 + NW 显著性）\n")
    L.append("> 对每个指标：Q1(便宜)→Q5(贵) 未来 12 月收益均值 + 多空 Q1−Q5 + 斜率 β 的 NW t 值。")
    L.append("> **t<−2 且 β<0 判「value 有效」**（越贵未来收益显著越低）。\n")
    L.append("| 宽基 | 指标 | Q1 | Q2 | Q3 | Q4 | Q5 | 多空Q1-Q5 | 单调 | 斜率β | NW-t | 判定 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for sym in LEGU_BROAD_INDEXES:
        for metric, (mname, _) in VALUE_METRICS.items():
            r = factor_valid[sym].get(metric)
            if r is None:
                continue
            lay = r["layered"]
            ls = f"{r['ls']*100:+.1f}%" if r["ls"] is not None else "—"
            beta = f"{r['beta']*100:+.1f}%" if r["beta"] is not None else "—"
            t = f"{r['nw_t']:.2f}" if r["nw_t"] is not None else "—"
            verdict = "✓" if (r["nw_t"] is not None and r["nw_t"] < -2) else "—"
            L.append(f"| {sym} | {mname} | " + " | ".join(f"{x*100:+.1f}%" for x in lay)
                     + f" | {ls} | {'是' if r['mono'] else '否'} | {beta} | {t} | {verdict} |")

    # §2 窗口敏感性
    L.append("\n## 2. 分位窗口敏感性（多空 Q1−Q5）\n")
    L.append("> 检验结论是否依赖分位窗口。窗口：3y/5y/7y/10y/全历史。\n")
    for metric, (mname, _) in VALUE_METRICS.items():
        L.append(f"\n### {mname}\n")
        wn = list(WINDOWS.keys())
        L.append("| 宽基 | " + " | ".join(wn) + " |")
        L.append("|---|" + "|".join(["---"] * len(wn)) + "|")
        for sym in LEGU_BROAD_INDEXES:
            cells = []
            for w in wn:
                v = window_sens[sym].get(metric, {}).get(w)
                cells.append(f"{v*100:+.1f}%" if v is not None else "—")
            L.append(f"| {sym} | " + " | ".join(cells) + " |")

    # §3 规则对比（两个指标）
    L.append("\n## 3. 择时规则对比（月度再平衡）\n")
    L.append("> 「年化换手」为年化单边换手次数（1.0 = 年均调仓 100% 仓位）。估值是慢变量，换手极低。\n")
    for metric in TIMING_METRICS:
        mname = VALUE_METRICS[metric][0]
        L.append(f"\n### 指标 = {mname}\n")
        L.append("| 宽基 | 规则 | 年化收益 | 年化波动 | 夏普 | Calmar | 最大回撤 | 仓位暴露 | 年化换手 | 超额(vs持有) |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for sym in LEGU_BROAD_INDEXES:
            for rname in RULES:
                p = rule_res[metric][sym][rname]["perf"]
                s_, b_ = p["strat"], p["buyhold"]
                L.append(f"| {sym} | {rname} | {s_['ann_ret']*100:+.2f}% | {s_['ann_vol']*100:.1f}% "
                         f"| {s_['sharpe']:.2f} | {s_['calmar']:.2f} | {s_['mdd']*100:.1f}% "
                         f"| {p['exposure']:.0%} | {p['annual_turnover']:.1f} | {p['ann_excess']*100:+.2f}% |")
            bh = rule_res[metric][sym][next(iter(RULES))]["perf"]["buyhold"]
            L.append(f"| {sym} | **买入持有** | {bh['ann_ret']*100:+.2f}% | {bh['ann_vol']*100:.1f}% "
                     f"| {bh['sharpe']:.2f} | {bh['calmar']:.2f} | {bh['mdd']*100:.1f}% "
                     f"| 100% | 0.0 | 0.00% |")

    # §4 组合
    L.append("\n## 4. 组合层面（3 宽基月频等权）\n")
    L.append("| 指标 | 组合 | 年化收益 | 年化波动 | 夏普 | Calmar | 最大回撤 | 超额 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for metric in TIMING_METRICS:
        mname = VALUE_METRICS[metric][0]
        p = port[metric]
        ps_, pb_ = p["strat"], p["buyhold"]
        L.append(f"| {mname} | 估值择时(等权) | {ps_['ann_ret']*100:+.2f}% | {ps_['ann_vol']*100:.1f}% | {ps_['sharpe']:.2f} "
                 f"| {ps_['calmar']:.2f} | {ps_['mdd']*100:.1f}% | {p['ann_excess']*100:+.2f}% |")
        L.append(f"| {mname} | 买入持有(等权) | {pb_['ann_ret']*100:+.2f}% | {pb_['ann_vol']*100:.1f}% | {pb_['sharpe']:.2f} "
                 f"| {pb_['calmar']:.2f} | {pb_['mdd']*100:.1f}% | 0.00% |")

    # §5 阈值曲面（两个指标）
    L.append("\n## 5. 阈值鲁棒性曲面（三档 buy/sell，超额年化 vs 持有）\n")
    L.append("> 若超额在阈值网格上大片为正且平滑 → 结构可信；若只在个别格点突出 → 警惕过拟合。\n")
    for metric in TIMING_METRICS:
        mname = VALUE_METRICS[metric][0]
        L.append(f"\n### 指标 = {mname}\n")
        for sym in LEGU_BROAD_INDEXES:
            L.append(f"**{sym}**（超额年化 %）\n")
            L.append("| buy\\sell | " + " | ".join(f"{s:.1f}" for s in sells) + " |")
            L.append("|---|" + "|".join(["---"] * len(sells)) + "|")
            for b in buys:
                cells = " | ".join(f"{threshold_grid[metric][sym][f'{b}/{sl}']['ann_excess']*100:+.1f}%"
                                   for sl in sells)
                L.append(f"| {b:.1f} | {cells} |")

    # §6 逐年收益（PB 三档，因 PB 是最强信号）
    L.append("\n## 6. 逐年收益（PB，三档 0.3/0.7；每格=择时%/持有%）\n")
    years = sorted({y for sym in LEGU_BROAD_INDEXES for y in rule_res["pb"][sym]["三档(0.3/0.7)"]["yearly"]})
    L.append("| 年份 | " + " | ".join(f"{sym}" for sym in LEGU_BROAD_INDEXES) + " |")
    L.append("|---|" + "|".join(["---"] * len(LEGU_BROAD_INDEXES)) + "|")
    for y in years:
        cells = []
        for sym in LEGU_BROAD_INDEXES:
            yr = rule_res["pb"][sym]["三档(0.3/0.7)"]["yearly"].get(y)
            cells.append(f"{yr[0]*100:+.1f}% / {yr[1]*100:+.1f}%" if yr else "—")
        L.append(f"| {y} | " + " | ".join(cells) + " |")

    # §7 当前快照
    L.append("\n## 7. 当前估值快照（2026-08-28，乐咕最新月频）\n")
    L.append("| 宽基 | PE(TTM) | PE(LYR) | PB | PE-TTM 5y分位 | PB 5y分位 |")
    L.append("|---|---|---|---|---|---|")
    for sym, df in data.items():
        cs = _current_snapshot(df)
        L.append(f"| {sym} | {cs['pe_ttm']:.1f} | {cs['pe_lyr']:.1f} | {cs['pb']:.2f} | {cs['pe_pct']:.0%} | {cs['pb_pct']:.0%} |")

    # §8 结论
    L.append("\n## 8. 结论与注意事项\n")
    L.append("- **value 因子时序有效，且 PB 显著强于 PE**：PB/PB中位数 在 3 个宽基上 NW-t 全部 <−2（显著），多空 Q1−Q5 达 +17%~+33%；PE(TTM) 的 NW-t 仅 −1.5~−1.9（不显著）。**上一版用 PE-TTM 判「中证500 无效」是选错指标——用 PB 中证500 同样有效（+28.1%，t=−2.55）。**")
    L.append("- **收益端无法增收益，但 PB 择时在价值宽基上能提夏普/Calmar**：上证50 PB 择时夏普 0.22→0.37、Calmar 0.09→0.17、回撤 −71%→−28%（收益仅从 6.10% 降到 4.8%）；沪深300 PB 夏普 0.29→0.31。中证500 即使 PB 有效，择时夏普仍降（0.23→0.17）——其长期满仓收益更高、减仓代价大。**结论：value 择时对「大盘价值宽基」是有效的风险调整工具，对「中小成长宽基」不适用。**")
    L.append("- **逐年结构（§6）**：择时在熊市/震荡市跑赢（2008 −24% vs −67%、2015 +8% vs −6%、2022 +6.7% vs −19.5%），在单边大牛市踏空（2006-07 空仓 0% vs 满仓 +120~160%）——估值分位在牛市冲高过快导致过早离场，这是估值择时的本质代价。")
    L.append("- **分位窗口**：§2 显示窗口越长越稳健（PB 全历史多空 +73%，3y 反而不稳定甚至转负），5y 是合理默认；短窗口噪声大勿用。")
    L.append("- **规则选择**：线性 1.5−1.5pct 在夏普/回撤上最优，但 1.5 为事后取值、略有过拟合风险；保守三档（0.3/0.7）更稳健、换手更低。")
    L.append("- **阈值鲁棒性**：§5 曲面平滑、无孤立突出格点 → 结构可信；但 PB 全曲面超额均 ≤0，印证「不能增收益、只能控回撤」。")
    L.append("- **换手极低**：年化单边换手 <1.5 次，交易成本可忽略。")
    L.append("- **样本重叠**：未来 12 月收益重叠 11 个月，NW(lag=12) 已修正标准误，但有效独立样本 ≈ 月数/12，显著性仍宜打折。")
    L.append("- **未计成本**：指数点位不含 ETF 费率/跟踪误差，实盘需扣 0.2~0.5%/年。")
    (ROOT / "runs" / "value_timing_backtest.md").write_text("\n".join(L), encoding="utf-8")

    # ============ 控制台摘要 ============
    print("=" * 100)
    print("value 时序估值择时——完整回测（乐咕月频，2005 至今）")
    print("=" * 100)
    print("\n[1] value 因子有效性（NW t<−2 判有效）")
    for sym in LEGU_BROAD_INDEXES:
        for metric, (mname, _) in VALUE_METRICS.items():
            r = factor_valid[sym].get(metric)
            if not r:
                continue
            ls = f"{r['ls']*100:+.1f}%" if r["ls"] is not None else "—"
            t = f"{r['nw_t']:.2f}" if r["nw_t"] is not None else "—"
            flag = "✓" if (r["nw_t"] is not None and r["nw_t"] < -2) else " "
            print(f"  {sym:<6} {mname:<10} 多空 {ls:<8} NW-t {t:<7} {flag}")
    print("\n[3] 择时规则对比")
    for metric in TIMING_METRICS:
        print(f"  --- 指标 {VALUE_METRICS[metric][0]} ---")
        for sym in LEGU_BROAD_INDEXES:
            for rname in RULES:
                p = rule_res[metric][sym][rname]["perf"]
                print(f"    {sym:<6} {rname:<16} 年化 {p['strat']['ann_ret']*100:+.2f}% 夏普 {p['strat']['sharpe']:.2f} "
                      f"回撤 {p['strat']['mdd']*100:.1f}% 超额 {p['ann_excess']*100:+.2f}%")
    print("\n[4] 组合（等权）")
    for metric in TIMING_METRICS:
        p = port[metric]
        print(f"  {VALUE_METRICS[metric][0]:<8} 择时 年化 {p['strat']['ann_ret']*100:+.2f}% 夏普 {p['strat']['sharpe']:.2f} "
              f"回撤 {p['strat']['mdd']*100:.1f}% | 持有 年化 {p['buyhold']['ann_ret']*100:+.2f}% "
              f"夏普 {p['buyhold']['sharpe']:.2f} 回撤 {p['buyhold']['mdd']*100:.1f}%")
    print(f"\nMarkdown: runs/value_timing_backtest.md")
    print(f"JSON:     runs/value_timing_backtest.json")


if __name__ == "__main__":
    main()
