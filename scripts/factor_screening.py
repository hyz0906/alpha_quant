#!/usr/bin/env python3
"""多因子批量筛选诊断：截面 IC + ICIR + 分层收益 + 多空收益 + 因子相关性。

在扩展池（12 只 ETF，2017-09 起共同样本）上，对 factor_library 注册的
16 个候选因子逐一跑 §7.9 的因子有效性三件套，输出统一排名与 pass/fail 判定。

判定门槛（避免重蹈 RSRS「跑通链路→调参→样本外全灭」弯路，卡得偏严）：
  * 强通过  STRONG ：max|ICIR| ≥ 0.5 且正 IC 占比落在 [45%,55%] 之外
  * 弱通过  WEAK   ：max|ICIR| ≥ 0.3 且正 IC 占比落在 [45%,55%] 之外
  * 淘汰    FAIL   ：其余
  * 另附单调性（Q1→Q5 分层收益是否严格递增），作定性参考。

实现要点：截面 Spearman IC 用「逐行 rank → 中心化 → Pearson 秩相关」向量化，
避免逐交易日 Python 循环（12 只 × 2700 交易日 × 16 因子 × 120 相关对）。
用法：python3 scripts/factor_screening.py [--horizons 5,10,20]
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategies.factors.factor_library import (
    build_panels, compute_all_factors,
)

ROOT = Path("/home/hyz0906/workspace/alpha_quant")
DATA_DIR = ROOT / "data"

CODES = [
    "510300.SH", "510500.SH", "159915.SZ", "512010.SH", "159928.SZ",
    "512880.SH", "512660.SH", "512400.SH", "513100.SH", "513500.SH",
    "513050.SH", "518880.SH",
]
N_QUANTILES = 5
STRONG_ICIR = 0.5
WEAK_ICIR = 0.3
POS_RATE_BAND = (0.45, 0.55)


def _rank_corr_series(a: pd.DataFrame, b: pd.DataFrame) -> np.ndarray:
    """逐行（截面）Spearman 秩相关，向量化。返回长度 = 行数的 float 数组。"""
    ra = a.rank(axis=1)
    rb = b.rank(axis=1)
    rac = ra.sub(ra.mean(axis=1), axis=0)
    rbc = rb.sub(rb.mean(axis=1), axis=0)
    num = (rac * rbc).sum(axis=1)
    den = np.sqrt((rac ** 2).sum(axis=1) * (rbc ** 2).sum(axis=1))
    return np.divide(num.values, den.values, out=np.full(num.shape, np.nan),
                     where=den.values != 0)


def cross_sectional_ic(factor: pd.DataFrame, fwd: pd.DataFrame) -> np.ndarray:
    """逐交易日截面 IC 序列（已剔除 NaN 行）。"""
    ic = _rank_corr_series(factor, fwd)
    return ic[~np.isnan(ic)]


def layered_returns(factor: pd.DataFrame, fwd: pd.DataFrame,
                    n_q: int = N_QUANTILES) -> list[float]:
    """每交易日按因子分 n_q 组，返回各组未来收益的时间均值（Q1 低 → Qn 高）。

    T 循环用 numpy 行内操作（N=12 很小），无 pandas qcut 开销。
    """
    rx = factor.rank(axis=1, method="first").values
    ry = fwd.values
    qsum = np.zeros(n_q)
    qcnt = np.zeros(n_q)
    for t in range(rx.shape[0]):
        row_rx, row_ry = rx[t], ry[t]
        v = ~np.isnan(row_rx) & ~np.isnan(row_ry)
        n = int(v.sum())
        if n < n_q:
            continue
        order = np.argsort(row_rx[v])
        rets = row_ry[v][order]
        for i in range(n):
            b = min(int(i * n_q / n), n_q - 1)
            qsum[b] += rets[i]
            qcnt[b] += 1
    return [float(qsum[q] / qcnt[q]) if qcnt[q] > 0 else float("nan")
            for q in range(n_q)]


def factor_correlation(factors: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """因子间平均截面秩相关（向量化），衡量因子冗余。"""
    names = list(factors)
    n = len(names)
    corr = pd.DataFrame(np.eye(n), index=names, columns=names)
    ranks = {name: f.rank(axis=1) for name, f in factors.items()}
    for i in range(n):
        for j in range(i + 1, n):
            ic = _rank_corr_series(ranks[names[i]], ranks[names[j]])
            c = float(np.nanmean(ic))
            corr.iloc[i, j] = corr.iloc[j, i] = c
    return corr


def verdict(icir: float, pos_rate: float) -> str:
    inside = POS_RATE_BAND[0] <= pos_rate <= POS_RATE_BAND[1]
    if inside:
        return "FAIL"
    if abs(icir) >= STRONG_ICIR:
        return "STRONG"
    if abs(icir) >= WEAK_ICIR:
        return "WEAK"
    return "FAIL"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="5,10,20")
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",")]

    # ---- 1. 读数据 → 面板 ----
    closes, highs, lows, volumes = {}, {}, {}, {}
    for code in CODES:
        df = pd.read_csv(DATA_DIR / f"{code}.csv", parse_dates=["date"]).set_index("date")
        df = df.sort_index()
        closes[code] = df["close"]
        highs[code] = df["high"]
        lows[code] = df["low"]
        volumes[code] = df["volume"]

    close = pd.DataFrame(closes).sort_index()
    high = pd.DataFrame(highs).sort_index()
    low = pd.DataFrame(lows).sort_index()
    volume = pd.DataFrame(volumes).sort_index()

    panels = build_panels(close, high, low, volume)
    factors = compute_all_factors(panels)
    fwd = {k: close.shift(-k) / close - 1.0 for k in horizons}

    # ---- 2. 截面 IC / ICIR / 分层 ----
    results = {}
    for name, f in factors.items():
        row = {"ic": {}, "layered": {}, "best_icir": 0.0, "best_horizon": None}
        for k in horizons:
            ics = cross_sectional_ic(f, fwd[k])
            if ics.size == 0:
                row["ic"][k] = {"mean_ic": 0.0, "icir": 0.0, "ic_std": 0.0,
                                "positive_ic_rate": 0.0, "n_obs": 0}
                row["layered"][k] = [float("nan")] * N_QUANTILES
                continue
            mean_ic = float(ics.mean())
            ic_std = float(ics.std())
            icir = float(mean_ic / ic_std) if ic_std > 0 else 0.0
            pos_rate = float((ics > 0).mean())
            row["ic"][k] = {
                "mean_ic": round(mean_ic, 4),
                "icir": round(icir, 4),
                "ic_std": round(ic_std, 4),
                "positive_ic_rate": round(pos_rate, 4),
                "n_obs": int(ics.size),
            }
            row["layered"][k] = [round(x, 6) for x in layered_returns(f, fwd[k])]
            if abs(icir) > abs(row["best_icir"]):
                row["best_icir"] = icir
                row["best_horizon"] = k
        bh = row["best_horizon"]
        pos_rate = row["ic"][bh]["positive_ic_rate"]
        row["verdict"] = verdict(row["best_icir"], pos_rate)
        results[name] = row

    # ---- 3. 因子相关性 ----
    corr = factor_correlation(factors)

    # ---- 4. 汇总排序（按 best |ICIR| 降序） ----
    order = sorted(results, key=lambda n: -abs(results[n]["best_icir"]))

    print("=" * 100)
    print("多因子筛选诊断：截面 IC / ICIR / 分层收益（扩展池 12 只，2017-09 起）")
    print("=" * 100)
    header = (f"{'因子':<16} {'判定':<7} {'bestH':>5} {'bestICIR':>9} "
              + " ".join([f"{'IC'+str(k):>9}" for k in horizons])
              + " ".join([f"{'IR'+str(k):>9}" for k in horizons])
              + f" {'正IC%':>7} {'多空20d':>9}")
    print(header)
    print("-" * 100)
    for n in order:
        r = results[n]
        ic_cols = " ".join([f"{r['ic'][k]['mean_ic']:+9.3f}" for k in horizons])
        ir_cols = " ".join([f"{r['ic'][k]['icir']:+9.3f}" for k in horizons])
        pos = r["ic"][r["best_horizon"]]["positive_ic_rate"]
        ls20 = r["layered"][20][-1] - r["layered"][20][0]
        print(f"{n:<16} {r['verdict']:<7} {r['best_horizon']:>5} "
              f"{r['best_icir']:+9.3f} {ic_cols}{ir_cols} "
              f"{pos:>7.1%} {ls20*100:+9.2f}%")

    # ---- 5. 分层收益明细（monotonicity） ----
    print("\n" + "=" * 100)
    print("分层收益（20 日）：Q1(低) → Q5(高)，及单调性")
    print("=" * 100)
    for n in order:
        ls = results[n]["layered"][20]
        mono = all(ls[i] <= ls[i + 1] for i in range(len(ls) - 1))
        ls_str = "  ".join(f"Q{i+1}:{ls[i]*100:+.2f}%" for i in range(N_QUANTILES))
        print(f"  {n:<16} {ls_str}  | 多空 {((ls[-1]-ls[0])*100):+.2f}% | 单调 {'是' if mono else '否'}")

    # ---- 6. 因子相关性热力摘要 ----
    print("\n" + "=" * 100)
    print("因子间高相关对（|平均截面秩相关| > 0.5，提示冗余）")
    print("=" * 100)
    names = list(factors)
    found = False
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            c = corr.iloc[i, j]
            if abs(c) > 0.5:
                print(f"  {names[i]:<16} <-> {names[j]:<16}  corr={c:+.3f}")
                found = True
    if not found:
        print("  无 |corr|>0.5 的高相关因子对")

    # ---- 7. 落盘 JSON + Markdown ----
    out = {
        "codes": CODES,
        "horizons": horizons,
        "thresholds": {"strong_icir": STRONG_ICIR, "weak_icir": WEAK_ICIR,
                       "pos_rate_band": list(POS_RATE_BAND)},
        "results": results,
        "factor_correlation": corr.round(3).to_dict(),
    }
    (ROOT / "runs/factor_screening.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md_lines = [
        "# 多因子筛选诊断报告（扩展池 12 只，2017-09 起）\n",
        f"> 判定门槛：强通过 |ICIR|≥{STRONG_ICIR}、弱通过 |ICIR|≥{WEAK_ICIR}，"
        f"且正 IC 占比须落在 {POS_RATE_BAND[0]:.0%}~{POS_RATE_BAND[1]:.0%} 之外。\n",
        "## 1. 因子排名（按 best |ICIR| 降序）\n",
        "| 因子 | 判定 | bestH | bestICIR | " + " | ".join(f"IC{k}" for k in horizons)
        + " | " + " | ".join(f"IR{k}" for k in horizons) + " | 正IC% | 多空20d |",
        "|---|---|---|---|" + "|".join(["---"] * (len(horizons) * 2 + 2)),
    ]
    for n in order:
        r = results[n]
        ic_cols = " | ".join(f"{r['ic'][k]['mean_ic']:+.3f}" for k in horizons)
        ir_cols = " | ".join(f"{r['ic'][k]['icir']:+.3f}" for k in horizons)
        pos = r["ic"][r["best_horizon"]]["positive_ic_rate"]
        ls20 = (r["layered"][20][-1] - r["layered"][20][0]) * 100
        md_lines.append(
            f"| {n} | {r['verdict']} | {r['best_horizon']} | {r['best_icir']:+.3f} "
            f"| {ic_cols} | {ir_cols} | {pos:.1%} | {ls20:+.2f}% |"
        )

    md_lines += ["\n## 2. 分层收益（20 日）\n",
                 "| 因子 | Q1 | Q2 | Q3 | Q4 | Q5 | 多空 | 单调 |",
                 "|---|---|---|---|---|---|---|---|"]
    for n in order:
        ls = results[n]["layered"][20]
        mono = all(ls[i] <= ls[i + 1] for i in range(len(ls) - 1))
        md_lines.append(
            f"| {n} | " + " | ".join(f"{x*100:+.2f}%" for x in ls)
            + f" | {((ls[-1]-ls[0])*100):+.2f}% | {'是' if mono else '否'} |"
        )

    md_lines += ["\n## 3. 高相关因子对（|corr|>0.5）\n"]
    high_pairs = [(names[i], names[j], corr.iloc[i, j])
                  for i in range(len(names)) for j in range(i + 1, len(names))
                  if abs(corr.iloc[i, j]) > 0.5]
    if high_pairs:
        for a, b, c in high_pairs:
            md_lines.append(f"- {a} ↔ {b}：{c:+.3f}")
    else:
        md_lines.append("- 无")

    md_lines += ["\n## 4. 结论与注意事项\n",
                 "- 截面仅 12 只标的，单日 IC 噪声大；ICIR 用「日频 IC 均值/标准差」"
                 "近似，相邻日 IC 因重叠前视收益而高度自相关，正式显著性需 Newey-West。",
                 "- 16 因子 × 3 视界 ≈ 48 次检验，5% 显著性下约 2.4 个假阳性，"
                 "凡通过的因子须在下一轮轮动回测中复现，不可直接采信。",
                 "- RSRS（rsrs_zscore）此前诊断 ICIR≈-0.03，为对照下限。\n"]
    (ROOT / "runs/factor_screening_20260831.md").write_text(
        "\n".join(md_lines), encoding="utf-8"
    )
    print(f"\nJSON 已写入: runs/factor_screening.json")
    print(f"Markdown 已写入: runs/factor_screening_20260831.md")


if __name__ == "__main__":
    main()
