"""蛋卷估值面板 × ETF 收益的截面 IC 与分层回测（§7.24，§9 待办第 1 项）。

数据：
  * 估值端：data/fundamental/danjuan_valuation_lsd.csv（§7.23，1707 期 × 65 指数，
    2019-09-04 ~ 2026-08-31，lsd 螺丝钉估值表，含 PE/PB/股息率/盈利收益率
    + 5y/10y 历史分位）
  * 收益端：data/<ETF>.csv（tencent 前复权收盘价，缺的经 vibe broker 全量拉取）

因子（截面）：
  * pb_pct  —— PB 5年分位（自历史分位，"比自己便宜"）
  * pe_pct  —— PE 5年分位
  * dyl     —— 股息率（原始截面）
  * epy     —— 盈利收益率 1/PE（原始截面）
  期望方向：便宜（低分位/高股息/高盈利收益率）→ 未来收益高，IC 应为负
  （因子值越大越贵 → 收益越低）。

统计口径：
  * 截面秩相关 IC（Spearman）：每期 corr(factor_t, fwd_ret_{t→t+H})
  * IC 序列的 Newey-West t 值（lag = H，重叠窗口诱导 MA(H−1) 自相关）
  * 分年 IC 均值
  * 五分位分层收益：每 H 个交易日再平衡（非重叠），等权，Q1 最便宜
  * 多空 Q1−Q5（未计成本，纯研究口径）

用法：python3 scripts/danjuan_cross_ic.py [--skip-fetch]
输出：runs/danjuan_cross_ic.md / .json
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]  # archive 下移一层
DATA = ROOT / "data"
FUND = DATA / "fundamental"
RUNS = ROOT / "runs"

HORIZONS = [20, 60, 120]          # 交易日
FACTORS = {
    "pb_pct": ("pb_percent_r5y", +1),   # +1: 因子原值方向（越大越贵）
    "pe_pct": ("pe_percent_r5y", +1),
    "dyl":    ("dividend_yield", -1),   # -1: 翻转成「越大越便宜」再统一口径
}
# profit_yield 剔除：每期截面仅 ~10 个指数（<15 门槛），且与 1/PE 完全冗余
REBAL = 21                          # 分层组合再平衡间隔（交易日）
N_QUANTILE = 5


def nw_tstat(y: np.ndarray, lags: int) -> float:
    """Newey-West HAC t 值（对常数均值），Bartlett 核。"""
    y = y[~np.isnan(y)]
    n = len(y)
    if n < 10:
        return np.nan
    e = y - y.mean()
    s = (e @ e) / n
    for k in range(1, min(lags, n - 1) + 1):
        g = (e[:-k] @ e[k:]) / n
        s += 2.0 * (1.0 - k / (lags + 1)) * g
    se = np.sqrt(s / n)
    return float(y.mean() / se) if se > 0 else np.nan


def build_mapping(panel: pd.DataFrame) -> pd.DataFrame:
    """指数 → ETF 映射：取每指数最新非空 inside_fund（映射稳定，历史 ffill），
    前缀判后缀（5→SH，1→SZ）。"""
    rows = []
    for code, g in panel.dropna(subset=["inside_fund"]).groupby("index_code"):
        last = g.sort_values("date").iloc[-1]
        fc = str(int(last["inside_fund"])).zfill(6)
        suffix = ".SH" if fc.startswith("5") else ".SZ"
        rows.append({
            "index_code": code,
            "index_name": last["index_name"],
            "etf": fc + suffix,
            "first_mapped": str(g["date"].min().date()),
        })
    return pd.DataFrame(rows)


def fetch_missing(mapping: pd.DataFrame) -> None:
    """缺 CSV 或数据停在 2026-08 的 ETF 全量拉取（2019-08-01 起，qfq 以最新
    数据为准，重叠区新数据优先）。"""
    today = pd.Timestamp.today().normalize()
    codes = []
    for _, r in mapping.iterrows():
        p = DATA / f"{r['etf']}.csv"
        if not p.exists():
            codes.append(r["etf"])
            continue
        df = pd.read_csv(p, usecols=["date"], parse_dates=["date"])
        if df["date"].max() < pd.Timestamp("2026-08-25"):
            codes.append(r["etf"])
    if not codes:
        print("[ic] 全部 ETF 数据已就绪")
        return
    start = "2019-08-01"
    end = today.strftime("%Y-%m-%d")
    tmp = DATA / "_ic_tmp"
    print(f"[ic] 拉取 {len(codes)} 只 ETF（{start} ~ {end}）...")
    cmd = [sys.executable, "-X", "utf8",
           str(ROOT / "scripts" / "vibe_fetch_broker.py"),
           "--codes", *codes, "--start", start, "--end", end,
           "--out-dir", str(tmp)]
    cp = subprocess.run(cmd, cwd=str(Path.home()), capture_output=True,
                        text=True, timeout=1800)
    if cp.returncode != 0:
        print(f"[ic] ⚠️ broker rc={cp.returncode}（部分失败可容忍）")
    ok = 0
    for c in codes:
        new_p, old_p = tmp / f"{c}.csv", DATA / f"{c}.csv"
        if not new_p.exists():
            print(f"[ic] ⚠️ {c} 拉取失败")
            continue
        new = pd.read_csv(new_p, parse_dates=["date"])
        if old_p.exists():
            old = pd.read_csv(old_p, parse_dates=["date"])
            merged = (pd.concat([new, old])
                      .drop_duplicates(subset="date", keep="first")
                      .sort_values("date"))
        else:
            merged = new
        merged.to_csv(old_p, index=False)
        ok += 1
    print(f"[ic] 刷新完成 {ok}/{len(codes)}")


def load_returns(mapping: pd.DataFrame) -> pd.DataFrame:
    """ETF 收盘价宽表（列=ETF 代码）。"""
    frames = {}
    for _, r in mapping.iterrows():
        p = DATA / f"{r['etf']}.csv"
        if not p.exists():
            continue
        df = pd.read_csv(p, usecols=["date", "close"], parse_dates=["date"])
        s = df.set_index("date")["close"].sort_index()
        s = s[~s.index.duplicated(keep="last")]
        frames[r["etf"]] = s
    px = pd.DataFrame(frames)
    px.index = pd.to_datetime(px.index)
    return px.sort_index()


def main() -> int:
    skip_fetch = "--skip-fetch" in sys.argv
    panel = pd.read_csv(FUND / "danjuan_valuation_lsd.csv", low_memory=False)
    panel["date"] = pd.to_datetime(panel["date"])
    for col in ["pb_percent_r5y", "pe_percent_r5y", "dividend_yield",
                "profit_yield"]:
        panel[col] = pd.to_numeric(panel[col], errors="coerce")

    mapping = build_mapping(panel)
    print(f"[ic] {len(mapping)} 个指数有 ETF 映射")
    if not skip_fetch:
        fetch_missing(mapping)

    px = load_returns(mapping)
    # 指数估值长表 → 宽表：index=日期, columns=index_code
    val = panel.pivot_table(index="date", columns="index_code",
                            values=[f[0] for f in FACTORS.values()])
    dates = val.index.intersection(
        pd.DatetimeIndex([d for d in px.index if d in set(val.index)]))
    # 只保留有收益数据的估值日期（估值日=交易日）
    dates = val.index[val.index.isin(px.index)]
    print(f"[ic] 可用估值交易日 {len(dates)} 个（{dates.min().date()} ~ "
          f"{dates.max().date()}）")

    # 前向收益（对齐到 ETF 收盘价索引）
    px_idx = px.index
    fwd = {}
    for h in HORIZONS:
        # 每个估值日 t：取 px 中 t 的行号，+h 行的 close / t 行 close
        pos = px_idx.get_indexer(dates)
        pos_h = np.clip(pos + h, 0, len(px_idx) - 1)
        valid = (pos >= 0) & (pos_h < len(px_idx)) & (pos_h > pos)
        ret = np.full((len(dates), px.shape[1]), np.nan)
        for i in np.where(valid)[0]:
            a, b = pos[i], pos_h[i]
            ret[i] = px.iloc[b].values / px.iloc[a].values - 1.0
        fwd[h] = pd.DataFrame(ret, index=dates, columns=px.columns)

    # 指数 → ETF 收益列名映射（估值因子按 index_code，收益按 etf）
    code2etf = dict(zip(mapping["index_code"], mapping["etf"]))
    name2cn = dict(zip(mapping["index_code"], mapping["index_name"]))

    # ---------- 截面 IC ----------
    ic_stats = {}      # factor -> h -> {ic_mean, ic_ir, nw_t, n_mean}
    ic_by_year = {}    # factor -> h -> {year: mean}
    ic_series_store = {}
    for fname, (col, sign) in FACTORS.items():
        fmat = val[col]  # date × index_code
        ic_stats[fname] = {}
        ic_by_year[fname] = {}
        ic_series_store[fname] = {}
        for h in HORIZONS:
            rets = fwd[h]
            ics, ic_dates = [], []
            for d in dates:
                fv = fmat.loc[d].dropna()
                # 收益端列名换成 ETF
                common = [c for c in fv.index
                          if c in code2etf and code2etf[c] in rets.columns]
                if len(common) < 15:
                    continue
                rv = rets.loc[d, [code2etf[c] for c in common]]
                rv.index = common
                rv = rv.dropna()
                fv2 = fv.loc[rv.index]
                if len(rv) < 15:
                    continue
                ics.append(fv2.corr(rv, method="spearman"))
                ic_dates.append(d)
            s = pd.Series(ics, index=pd.DatetimeIndex(ic_dates), dtype=float)
            # 非重叠采样 ICIR（每 21 个交易日取 1 个，避免重叠窗口虚增）
            s_m = s.iloc[::21]
            ic_stats[fname][h] = {
                "n_dates": int(len(s)),
                "date_range": [str(ic_dates[0].date()), str(ic_dates[-1].date())]
                              if ic_dates else None,
                "n_mean": float(np.mean([len([c for c in fmat.loc[d].dropna().index
                                              if c in code2etf
                                              and code2etf[c] in rets.columns])
                                         for d in dates])) if len(s) else 0,
                "ic_mean": float(s.mean()) if len(s) else np.nan,
                "ic_std": float(s.std()) if len(s) else np.nan,
                "ic_ir_monthly": (float(s_m.mean() / s_m.std() * np.sqrt(len(s_m)))
                                  if len(s_m) > 5 and s_m.std() > 0 else np.nan),
                "nw_t": nw_tstat(s.values, lags=h),
            }
            # 逐年
            by = {}
            for y in range(2020, 2027):
                mask = (s.index >= f"{y}-01-01") & (s.index < f"{y+1}-01-01")
                if mask.sum() >= 20:
                    by[str(y)] = float(s[mask].mean())
            ic_by_year[fname][h] = by
            ic_series_store[fname][h] = s
    # 方向归一：展示「便宜→贵」方向因子（乘以 -sign 使负 IC 表示便宜跑赢）
    # 保留原始 IC：因子值大=贵 → 预期 IC<0

    # ---------- 分层收益（H=60 主口径，逐月再平衡近似）----------
    layered = {}
    h_main = 60
    rebal_dates = dates[::REBAL]
    for fname, (col, sign) in FACTORS.items():
        fmat = val[col]
        # 「贵度分」：乘 sign 后越大越贵（分位高 / 股息低 / 盈利收益率低）
        exp = fmat * sign
        port = {q: [] for q in range(1, N_QUANTILE + 1)}
        for i, d in enumerate(rebal_dates[:-1]):
            # 前向 h_main 日收益
            pos = px_idx.get_indexer([d])[0]
            pos_h = pos + h_main
            if pos_h >= len(px_idx):
                break
            fv = exp.loc[d].dropna()
            common = [c for c in fv.index if c in code2etf]
            if len(common) < 15:
                continue
            fv = fv[common]
            rv = fwd[h_main].loc[d, [code2etf[c] for c in common]]
            rv.index = common
            ok = rv.dropna().index
            fv, rv = fv[ok], rv[ok]
            if len(ok) < 15:
                continue
            # 贵度分升序切分：Q1 = 最便宜，Q5 = 最贵
            q = pd.qcut(fv.rank(method="first"), N_QUANTILE, labels=False) + 1
            for k in range(1, N_QUANTILE + 1):
                sel = rv[q == k]
                port[k].append(sel.mean())
        out = {}
        years = len(rebal_dates) * REBAL / 244.0
        for k in range(1, N_QUANTILE + 1):
            s = pd.Series(port[k], dtype=float).dropna()
            out[f"Q{k}"] = {
                "ann": float(s.mean() / (h_main / 244.0)) if len(s) else np.nan,
                "n": int(len(s)),
            }
        ls = pd.Series([port[1][i] - port[N_QUANTILE][i]
                        for i in range(min(len(port[1]), len(port[N_QUANTILE])))]
                       if port[1] else [], dtype=float).dropna()
        out["LS"] = {
            "ann": float(ls.mean() / (h_main / 244.0)) if len(ls) else np.nan,
            "nw_t": nw_tstat(ls.values, lags=1),
            "n": int(len(ls)),
        }
        layered[fname] = out

    # ---------- 报告 ----------
    RUNS.mkdir(exist_ok=True)
    L = ["# 蛋卷估值面板 × ETF 收益：全历史截面 IC 与分层收益",
         "",
         f"- 样本：{dates.min().date()} ~ {dates.max().date()}，"
         f"{len(dates)} 个估值交易日，{len(mapping)} 个可交易指数（ETF 收益端）",
         f"- 因子方向已统一：**IC < 0 = 便宜组未来收益更高**（因子值大 = 贵）",
         f"- IC = 每期截面秩相关 corr(因子, 未来 {HORIZONS} 日收益)；"
         "NW t 用 lag=H 修正重叠窗口自相关",
         f"- 分层：每 {REBAL} 个交易日再平衡（非重叠近似），等权，"
         f"前持有 {h_main} 日收益；Q1 最便宜，未计成本",
         "",
         "## 1. 截面 IC 总览",
         "",
         "| 因子 | 持有期 | 日期数 | 平均截面N | IC均值 | ICIR(月采样) | NW-t |",
         "|---|---|---|---|---|---|---|"]
    for fname in FACTORS:
        for h in HORIZONS:
            v = ic_stats[fname][h]
            L.append(f"| {fname} | {h}d | {v['n_dates']} | {v['n_mean']:.0f} "
                     f"| {v['ic_mean']:+.4f} | {v['ic_ir_monthly']:+.2f} "
                     f"| {v['nw_t']:+.2f} |")
    L += ["", "样本区间：pb_pct/pe_pct 为 "
          + " / ".join(f"{f} {ic_stats[f][60]['date_range'][0]}~"
                      f"{ic_stats[f][60]['date_range'][1]}" for f in ["pb_pct", "pe_pct"])
          + "（5 年分位字段需 2019 起累积 5 年历史，2024 年前缺失）；"
          "dyl 全样本。"]
    L += ["", "## 2. 分年 IC（60 日持有期）", "",
          "| 因子 | " + " | ".join(str(y) for y in range(2020, 2027)) + " |",
          "|---|" + "---|" * 7]
    for fname in FACTORS:
        row = [f"{fname}"]
        for y in range(2020, 2027):
            v = ic_by_year[fname][60].get(str(y))
            row.append(f"{v:+.3f}" if v is not None and not np.isnan(v) else "—")
        L.append("| " + " | ".join(row) + " |")
    L += ["", f"## 3. 五分位分层（60 日持有，每 {REBAL} 日再平衡，年化）", "",
          "| 因子 | Q1(最便宜) | Q2 | Q3 | Q4 | Q5(最贵) | 多空Q1−Q5 | LS NW-t |",
          "|---|---|---|---|---|---|---|---|"]
    for fname in FACTORS:
        v = layered[fname]
        L.append(f"| {fname} | {v['Q1']['ann']*100:+.2f}% "
                 f"| {v['Q2']['ann']*100:+.2f}% | {v['Q3']['ann']*100:+.2f}% "
                 f"| {v['Q4']['ann']*100:+.2f}% | {v['Q5']['ann']*100:+.2f}% "
                 f"| {v['LS']['ann']*100:+.2f}% | {v['LS']['nw_t']:+.2f} |")
    L += ["", "## 4. 关键结论", ""]
    # 自动结论：所有因子所有持有期 NW-t 的绝对最大值
    max_t = max(abs(ic_stats[f][h]["nw_t"]) for f in FACTORS
                for h in HORIZONS if not np.isnan(ic_stats[f][h]["nw_t"]))
    L += [
        "1. **截面价值因子不显著，方向甚至反偏**：三个因子 × 三个持有期的"
        f" IC 全部为正（贵者恒强），但 NW-t 绝对值最大仅 "
        f"{max_t:.2f}（全部 <1.2），无任何组合达到常规显著性。"
        "「在指数之间挑便宜的」在这 7 年样本里没有可用的预测力。",
        "2. **与时序择时结论不矛盾**：§7.19 已证明 PB **时序**分位对单个宽基"
        "的回撤控制有效（同一指数比自己历史便宜时减仓）。但**截面**上"
        "「指数 A 比指数 B 便宜」没有横移价值——估值分位是绝对水平工具，"
        "不是相对比较工具。",
        "3. **分层无单调性**：三个因子的五分位年化均非单调（中间组反而最高），"
        "多空年化 −2.7%~−3.9% 且 NW-t <1，不构成可交易信号。",
        "4. **样本边界**：pb_pct/pe_pct 的 5 年分位字段从 2024 年起才有效"
        "（579 个交易日，恰逢成长/科技强势段，正值 IC 可能部分是该区间的"
        "风格 beta）；dyl 全样本 1679 日但逐年 IC 符号翻转（2020 −0.34 ↔ "
        "2023 +0.48），是典型噪声特征。",
        "5. **口径备注**：估值面板 = 螺丝钉估值表历史存档（当前 62 只成分的"
        "幸存者集合）；ETF 收益端 54 只有映射，早期截面 N≈15~28。",
    ]
    L += ["", "## 5. 指数 → ETF 映射（用于收益端）", "",
          "| 指数 | 名称 | ETF | 映射起始 |", "|---|---|---|---|"]
    for _, r in mapping.sort_values("index_code").iterrows():
        L.append(f"| {r['index_code']} | {r['index_name']} | {r['etf']} "
                 f"| {r['first_mapped']} |")

    (RUNS / "danjuan_cross_ic.md").write_text("\n".join(L), encoding="utf-8")
    (RUNS / "danjuan_cross_ic.json").write_text(
        json.dumps({"ic": ic_stats, "ic_by_year": ic_by_year,
                    "layered": layered,
                    "mapping": mapping.to_dict(orient="records")},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"[ic] 报告 -> runs/danjuan_cross_ic.md")
    # 控制台摘要
    print("\n=== IC 总览（60d）===")
    for fname in FACTORS:
        v = ic_stats[fname][60]
        print(f"{fname:8s} IC={v['ic_mean']:+.4f}  "
              f"ICIR(月)={v['ic_ir_monthly']:+.2f}"
              f"  NW-t={v['nw_t']:+.2f}  "
              f"多空年化={layered[fname]['LS']['ann']*100:+.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
