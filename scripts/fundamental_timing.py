#!/usr/bin/env python3
"""基本面因子时序诊断：估值分位（value）择时有效性检验。

背景：§9 的「基本面全历史数据接入」原计划走 Tushare index_dailybasic 做
「多年 × 多标的」截面 IC，但实测 token 无权限（40203，需 2000 积分），且免费源
（中证官网 20 日快照 / 乐咕）均无法提供多标的多年估值历史。因此本节退而求其次，
用乐咕唯一可得的「3 宽基月频估值（2005 至今，约 21 年）」做**时序估值择时**检验——
即验证 value 因子的核心假设：估值分位低（便宜）时买入、分位高（贵）时回避，未来
收益是否更高。这是估值因子最贴合实际用法的形态（比截面 rank 更接近 FED 模型择时）。

诚实边界：
  * 仅 3 个宽基（上证50/沪深300/中证500），无法做多标的截面 Spearman IC。
  * 月频未来 12 月收益高度重叠（相邻月重叠 11 个月），点估计有效，但有效独立样本
    ≈ 总月数/12，显著性有限，结论须在「择时回测」中复现方可采信。
  * 用指数点位而非 ETF 价格，不含跟踪误差与费率。

用法：python3 scripts/fundamental_timing.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_engine.fundamental_loader import LEGU_BROAD_INDEXES, fetch_lg_monthly

ROOT = Path("/home/hyz0906/workspace/alpha_quant")
FUND_DIR = ROOT / "data" / "fundamental"

FWD_MONTHS = 12          # 未来收益视界（估值是慢变量，用长视界）
PCT_WINDOW = 60          # 估值分位滚动窗口（5 年，月频）
PCT_MIN = 36             # 滚动分位最小样本（3 年）


def rolling_percentile(s: pd.Series, window: int = PCT_WINDOW,
                       min_periods: int = PCT_MIN) -> pd.Series:
    """滚动估值分位（0~1），t 时点只用 t 及之前的数据（无前视）。"""
    return s.rolling(window, min_periods=min_periods).apply(
        lambda x: float((x <= x.iloc[-1]).mean()), raw=False
    )


def quantile_bucket(pct: pd.Series, n_q: int = 5) -> pd.Series:
    """按分位值归入 n_q 档（0 基索引），Q0 最便宜 → Q4 最贵。NaN 保持 NaN。"""
    q = np.floor(pct.clip(0, 0.999999) * n_q)
    return q.where(pct.notna())


def future_ret(close: pd.Series, months: int = FWD_MONTHS) -> pd.Series:
    """t 时点买入持有 months 个月的收益 = close[t+months]/close[t] - 1。"""
    return close.shift(-months) / close - 1.0


def layered_future_ret(pct: pd.Series, fwd: pd.Series, n_q: int = 5) -> list[float]:
    """按估值分位分 n_q 档，返回各档「未来 FWD_MONTHS 月收益」均值。"""
    bucket = quantile_bucket(pct, n_q)
    out = []
    for q in range(n_q):
        v = fwd[(bucket == q) & fwd.notna()]
        out.append(float(v.mean()) if v.size else float("nan"))
    return out


def backtest_timing(close: pd.Series, pct: pd.Series) -> dict:
    """估值择时策略 vs 买入持有（月频，t 月末定仓位，t+1 月持有）。

    规则：估值分位 < 0.3 全仓；0.3~0.7 半仓；> 0.7 空仓。
    """
    pos = pct.map(lambda x: 1.0 if x < 0.3 else (0.5 if x <= 0.7 else 0.0))
    pos = pos.shift(1)                       # t 月末定仓，t+1 月生效（无前视）
    ret_m = close.pct_change()               # 月收益
    strat_ret = (ret_m * pos).dropna()
    bh_ret = ret_m.dropna()

    def stats(r: pd.Series) -> dict:
        n = r.size
        ann_ret = float((1 + r).prod() ** (12 / n) - 1) if n else float("nan")
        ann_vol = float(r.std() * np.sqrt(12)) if n else float("nan")
        sharpe = float(ann_ret / ann_vol) if ann_vol > 0 else float("nan")
        eq = (1 + r).cumprod()
        mdd = float((eq / eq.cummax() - 1).min())
        return {"ann_ret": round(ann_ret, 4), "ann_vol": round(ann_vol, 4),
                "sharpe": round(sharpe, 3), "mdd": round(mdd, 4), "n": int(n)}
    return {"timing": stats(strat_ret), "buy_hold": stats(bh_ret)}


def main():
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    print("=" * 96)
    print("基本面因子时序诊断：估值分位（value）择时有效性（乐咕月频，2005 至今）")
    print("=" * 96)

    for symbol, etf in LEGU_BROAD_INDEXES.items():
        df = fetch_lg_monthly(symbol)
        # 缓存
        df.to_csv(FUND_DIR / f"legu_monthly_{etf}.csv", encoding="utf-8")
        close = df["close"]
        pe = df["pe_ttm"]
        pct = rolling_percentile(pe)
        fwd = future_ret(close, FWD_MONTHS)

        # 有效样本（估值分位 + 未来收益都非 NaN）
        mask = pct.notna() & fwd.notna()
        n_eff = int(mask.sum())

        layered = layered_future_ret(pct, fwd)
        bt = backtest_timing(close, pct)

        # 分段稳健性：5 年一段，看 Q1-Q5 多空是否稳定为正（排除「仅早期极端行情驱动」）
        seg_ls = {}
        for year in range(2008, 2028, 5):
            lo = pd.Timestamp(f"{year}-01-01")
            hi = pd.Timestamp(f"{year + 5}-01-01")
            seg_pct = pct[(pct.index >= lo) & (pct.index < hi)]
            seg_fwd = fwd[(fwd.index >= lo) & (fwd.index < hi)]
            if int(seg_pct.notna().sum()) < 20:   # 每段至少 20 个有效月
                continue
            lay = layered_future_ret(seg_pct, seg_fwd)
            ls_val = lay[0] - lay[-1]
            if not np.isnan(ls_val):   # 段内 Q1/Q5 档为空时跳过（如中证500 分位长期不极端）
                seg_ls[f"{year}-{year + 4}"] = round(ls_val, 4)

        results[symbol] = {
            "etf": etf, "n_months": int(len(df)), "n_eff": n_eff,
            "pe_min": round(float(pe.min()), 2), "pe_max": round(float(pe.max()), 2),
            "pe_now": round(float(pe.iloc[-1]), 2), "pct_now": round(float(pct.iloc[-1]), 4),
            "layered_fwd12": [round(x, 4) for x in layered],
            "long_short": round(layered[0] - layered[-1], 4),   # Q1-Q5，正值=便宜跑赢贵
            "seg_long_short": seg_ls,
            "backtest": bt,
        }

        # value 有效 = 越贵收益越低 = 严格递减（Q1 ≥ Q2 ≥ ... ≥ Q5）
        mono = all(layered[i] >= layered[i + 1] for i in range(len(layered) - 1))
        qs = "  ".join(f"Q{i+1}:{layered[i]*100:+.1f}%" for i in range(5))
        print(f"\n{symbol}({etf})  月频 {results[symbol]['n_months']} 个月  有效 {n_eff}")
        print(f"  PE(TTM) {results[symbol]['pe_min']:.1f}~{results[symbol]['pe_max']:.1f}，"
              f"当前 {results[symbol]['pe_now']:.1f}（分位 {results[symbol]['pct_now']:.0%}）")
        print(f"  未来{FWD_MONTHS}月收益分层: {qs}  多空 {results[symbol]['long_short']*100:+.1f}%  单调递减 {'是' if mono else '否'}")
        b = bt
        print(f"  择时 年化 {b['timing']['ann_ret']*100:+.2f}% 波动 {b['timing']['ann_vol']*100:.1f}% "
              f"夏普 {b['timing']['sharpe']} 回撤 {b['timing']['mdd']*100:.1f}%")
        print(f"  持有 年化 {b['buy_hold']['ann_ret']*100:+.2f}% 波动 {b['buy_hold']['ann_vol']*100:.1f}% "
              f"夏普 {b['buy_hold']['sharpe']} 回撤 {b['buy_hold']['mdd']*100:.1f}%")
        seg_str = "  ".join(f"{k}:{v*100:+.1f}%" for k, v in seg_ls.items())
        print(f"  分段多空(5y): {seg_str}")

    # ---- 落盘 JSON + Markdown ----
    out_json = {
        "method": {
            "fwd_months": FWD_MONTHS, "pct_window": PCT_WINDOW,
            "pct_min_periods": PCT_MIN, "timing_rule": "pct<0.3全仓/0.3-0.7半仓/>0.7空仓",
            "source": "乐咕乐股月频估值（指数点位，2005 至今，免 token）",
        },
        "results": results,
    }
    (ROOT / "runs" / "fundamental_timing.json").write_text(
        json.dumps(out_json, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    L = []
    L.append("# 基本面因子时序诊断：估值分位（value）择时有效性\n")
    L.append("> 数据源：乐咕乐股月频估值（指数点位，2005 至今约 21 年，免 token）。")
    L.append("> 方法：滚动 5 年 PE(TTM) 分位（无前视）→ 未来 12 月收益分层 + 估值择时回测。\n")
    L.append("> **诚实边界**：仅 3 个宽基，无法做多标的截面 IC；月频未来收益重叠、独立样本有限；用指数点位不含费率。\n")

    L.append("## 1. 估值分位 → 未来 12 月收益（5 档）\n")
    L.append("| 宽基 | ETF | Q1(便宜) | Q2 | Q3 | Q4 | Q5(贵) | 多空Q1-Q5 | 单调递减 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for symbol, r in results.items():
        ls = r["layered_fwd12"]
        mono = all(ls[i] >= ls[i + 1] for i in range(len(ls) - 1))
        L.append(f"| {symbol} | {r['etf']} | " + " | ".join(f"{x*100:+.1f}%" for x in ls)
                 + f" | {r['long_short']*100:+.1f}% | {'是' if mono else '否'} |")

    L.append("\n## 2. 估值择时回测 vs 买入持有（月度再平衡）\n")
    L.append("| 宽基 | 策略 | 年化收益 | 年化波动 | 夏普 | 最大回撤 |")
    L.append("|---|---|---|---|---|---|")
    for symbol, r in results.items():
        t, b = r["backtest"]["timing"], r["backtest"]["buy_hold"]
        L.append(f"| {symbol} | 估值择时 | {t['ann_ret']*100:+.2f}% | {t['ann_vol']*100:.1f}% "
                 f"| {t['sharpe']} | {t['mdd']*100:.1f}% |")
        L.append(f"| {symbol} | 买入持有 | {b['ann_ret']*100:+.2f}% | {b['ann_vol']*100:.1f}% "
                 f"| {b['sharpe']} | {b['mdd']*100:.1f}% |")

    L.append("\n## 3. 分段稳健性（5 年一段的 Q1-Q5 多空差）\n")
    L.append("> 检验「多空差」是否只是早期极端行情（2005-2008 大牛熊）驱动，还是跨时段稳定。\n")
    seg_cols = sorted({k for r in results.values() for k in r["seg_long_short"]})
    L.append("| 宽基 | " + " | ".join(seg_cols) + " |")
    L.append("|---|" + "|".join(["---"] * len(seg_cols)) + "|")
    for symbol, r in results.items():
        cells = " | ".join(f"{r['seg_long_short'].get(k, float('nan'))*100:+.1f}%"
                           if k in r["seg_long_short"] else "—" for k in seg_cols)
        L.append(f"| {symbol} | {cells} |")

    L.append("\n## 4. 当前估值快照\n")
    L.append("| 宽基 | PE(TTM) | 历史区间 | 当前分位(5y) |")
    L.append("|---|---|---|---|")
    for symbol, r in results.items():
        L.append(f"| {symbol} | {r['pe_now']:.1f} | {r['pe_min']:.1f}~{r['pe_max']:.1f} "
                 f"| {r['pct_now']:.0%} |")

    L.append("\n## 5. 结论与注意事项\n")
    L.append("- **解读**：多空 Q1-Q5 为正值且各档单调递减，说明「便宜买入跑赢贵时买入」，value 因子在时序维度有效；若多空接近 0 或反向，则估值择时在该宽基无效。")
    L.append("- **择时 vs 持有**：看夏普是否抬升 + 回撤是否收敛。估值择时本质是「降波动/控回撤」，收益端常跑输牛市满仓（2019-2021、2024-2025），但熊市保护显著。")
    L.append("- **样本重叠**：未来 12 月收益在相邻月重叠 11 个月，点估计可信、显著性需打折（有效独立样本 ≈ 月数/12）。")
    L.append("- **下一优先**：Tushare `index_dailybasic`（需 2000 积分）补齐多年×多标的截面 IC，才是 carry/value 因子的标准检验；本报告是数据受限下的时序替代。")
    (ROOT / "runs" / "fundamental_timing.md").write_text("\n".join(L), encoding="utf-8")

    print(f"\nMarkdown 已写入: runs/fundamental_timing.md")
    print(f"JSON 已写入:     runs/fundamental_timing.json")


if __name__ == "__main__":
    main()
