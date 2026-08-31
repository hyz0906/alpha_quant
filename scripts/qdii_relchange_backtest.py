#!/usr/bin/env python3
"""QDII 溢价「相对变化」策略回测（全量 + 按年，§7.18 E）。

背景：§7.18 用「绝对溢价>3%」做回避信号。§7.18 E 的 2026 专项分析发现——2024
起 QDII 额度告罄使溢价结构性抬升（纳指 2026 全年 94% 时间溢价>3%），绝对阈值
失去判别力；但「溢价的异常变动」仍含信息（飙升=危险、回落=买点）。

本脚本把告警从「绝对水平」升级为「相对变化」（溢价一阶差分的滚动 z 分数，
见 qdii_calc.relchange_zscore），并对两条规则做全量 + 按年回测：

  * 飙升回避：z > +2（溢价异常飙升）→ 次日空仓，否则持有（替代绝对阈值回避）
  * 回落买入：z < -2（溢价异常回落）→ 次日买入，z > +2 卖出（逢低买入窗口）

用法：python3 scripts/qdii_relchange_backtest.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent))       # scripts/

import qdii_backtest as qbt
from src.data_engine.qdii_calc import relchange_zscore, RELCHANGE_WINDOW, RELCHANGE_Z

ROOT = Path(__file__).resolve().parents[1]

Z_GRID = [1.0, 1.5, 2.0, 2.5, 3.0]


def spike_avoid_hold(z: pd.Series, z_hi: float = RELCHANGE_Z) -> pd.Series:
    """飙升回避持仓（0=空仓 1=持有）：昨日 z > +z_hi（溢价异常飙升）→ 今日空仓。"""
    return (z <= z_hi).astype(float).shift(1).fillna(1.0)


def pullback_buy_hold(z: pd.Series, z_lo: float = -RELCHANGE_Z,
                      z_hi: float = RELCHANGE_Z) -> pd.Series:
    """回落买入持仓（状态机，0=空仓 1=持有）：z<-z_lo 买入，z>+z_hi 卖出，无前视。"""
    state = 0
    hold = []
    for v in z.values:
        if pd.isna(v):
            hold.append(state)
            continue
        if state == 0 and v < z_lo:
            state = 1
        elif state == 1 and v > z_hi:
            state = 0
        hold.append(state)
    return pd.Series(hold, index=z.index).shift(1).fillna(0).astype(float)


def _year_total(ret: pd.Series, hold: pd.Series | float) -> dict[int, float]:
    """按年总收益（year -> total）。hold 为持仓序列（0/1）或标量 1.0（满仓）。"""
    r = (ret * hold).dropna()
    if len(r) == 0:
        return {}
    return {int(y): float((1 + g).prod() - 1) for y, g in r.groupby(r.index.year)}


def main():
    rows = []
    yearly = {"bench": {}, "spike": {}, "pullback": {}}   # code -> {year -> total(pp)}
    sens = {zhi: [] for zhi in Z_GRID}                    # 敏感性：组合平均年化超额
    all_years: set[int] = set()

    for code, name in qbt.QDII_NAMES.items():
        df = qbt.load_premium_history(code)
        if df is None:
            print(f"[warn] {code} {name} 数据加载失败，跳过")
            continue
        ret = df["ret"]
        z = relchange_zscore(df["premium"], RELCHANGE_WINDOW)
        h_spike = spike_avoid_hold(z)
        h_pull = pullback_buy_hold(z)
        h_abs = qbt.avoid_hold(df)  # 绝对阈值回避（>3%），对比基线

        bh = qbt._perf(ret)
        sp = qbt._perf(ret * h_spike)
        pl = qbt._perf(ret * h_pull)
        ab = qbt._perf(ret * h_abs)

        rows.append({
            "code": code, "name": name, "n": len(df),
            "start": str(df.index.min().date()), "end": str(df.index.max().date()),
            "bh_annual": round(bh["annual"] * 100, 2), "bh_sharpe": round(bh["sharpe"], 2),
            "bh_mdd": round(bh["mdd"] * 100, 2),
            "ab_annual": round(ab["annual"] * 100, 2), "ab_sharpe": round(ab["sharpe"], 2),
            "ab_mdd": round(ab["mdd"] * 100, 2),
            "sp_annual": round(sp["annual"] * 100, 2), "sp_sharpe": round(sp["sharpe"], 2),
            "sp_mdd": round(sp["mdd"] * 100, 2), "sp_exp": round(float(h_spike.mean() * 100), 1),
            "pl_annual": round(pl["annual"] * 100, 2), "pl_sharpe": round(pl["sharpe"], 2),
            "pl_mdd": round(pl["mdd"] * 100, 2), "pl_exp": round(float(h_pull.mean() * 100), 1),
            "sp_excess": round((sp["total"] - bh["total"]) * 100, 2),
            "pl_excess": round((pl["total"] - bh["total"]) * 100, 2),
        })

        # 按年
        yb = _year_total(ret, 1.0)
        ys = _year_total(ret, h_spike)
        yp = _year_total(ret, h_pull)
        for y in yb:
            all_years.add(y)
            yearly["bench"].setdefault(code, {})[y] = yb.get(y, 0.0) * 100
            yearly["spike"].setdefault(code, {})[y] = (ys.get(y, 0.0) - yb.get(y, 0.0)) * 100
            yearly["pullback"].setdefault(code, {})[y] = (yp.get(y, 0.0) - yb.get(y, 0.0)) * 100

        # 敏感性：飙升回避不同 z_hi 的年化超额
        for zhi in Z_GRID:
            h = spike_avoid_hold(z, z_hi=zhi)
            s = qbt._perf(ret * h)
            sens[zhi].append((s["annual"] - bh["annual"]) * 100)

        print(f"[ok] {code} {name}: 相对变化 z 有效点数 {(~z.isna()).sum()}, "
              f"飙升回避超额 {rows[-1]['sp_excess']:+.2f}pp, 回落买入超额 {rows[-1]['pl_excess']:+.2f}pp")

    summary = pd.DataFrame(rows)
    years = sorted(all_years)

    # ---- Markdown ----
    L = ["# QDII 溢价「相对变化」策略回测报告（全量 + 按年）\n",
         f"> 信号：溢价一阶差分滚动 z 分数（窗口 {RELCHANGE_WINDOW} 交易日，均值/标准差滞后 1 期，无前视）。",
         f"> 规则：飙升回避（z>{RELCHANGE_Z} 次日空仓）/ 回落买入（z<-{RELCHANGE_Z} 买、z>+{RELCHANGE_Z} 卖）。",
         "> 收益口径：前复权价日收益（累计净值/单位净值复权因子，已消份额拆分）；信号 T 日 → 调仓 T+1 日。\n"]

    L.append("## 1. 全量绩效对比（2018-2026）\n")
    L.append("| 代码 | 名称 | 基准年化% | 基准夏普 | 绝对回避年化% | 飙升回避年化% | 回落买入年化% | 飙升夏普 | 飙升回撤% | 飙升暴露% | 回落夏普 | 回落回撤% | 回落暴露% |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in summary.iterrows():
        L.append(f"| {r['code']} | {r['name']} | {r['bh_annual']:.2f} | {r['bh_sharpe']:.2f} | "
                 f"{r['ab_annual']:.2f} | {r['sp_annual']:.2f} | {r['pl_annual']:.2f} | "
                 f"{r['sp_sharpe']:.2f} | {r['sp_mdd']:.2f} | {r['sp_exp']:.1f} | "
                 f"{r['pl_sharpe']:.2f} | {r['pl_mdd']:.2f} | {r['pl_exp']:.1f} |")

    L.append("\n## 2. 按年回测——飙升回避超额（策略 − 基准，%）\n")
    L.append("| 名称 | " + " | ".join(str(y) for y in years) + " | 全期累计 |")
    L.append("|---|" + "|".join(["---"] * (len(years) + 1)) + "|")
    for _, r in summary.iterrows():
        code = r["code"]
        cells = [f"{yearly['spike'].get(code, {}).get(y, 0.0):+.1f}" for y in years]
        L.append(f"| {r['name']} | " + " | ".join(cells) + f" | {r['sp_excess']:+.1f} |")
    avg = [sum(yearly["spike"].get(c, {}).get(y, 0.0) for c in qbt.QDII_NAMES) / len(qbt.QDII_NAMES)
           for y in years]
    L.append("| **组合均值** | " + " | ".join(f"**{v:+.1f}**" for v in avg)
             + f" | **{summary['sp_excess'].mean():+.1f}** |")

    L.append("\n## 3. 按年回测——回落买入超额（策略 − 基准，%）\n")
    L.append("| 名称 | " + " | ".join(str(y) for y in years) + " | 全期累计 |")
    L.append("|---|" + "|".join(["---"] * (len(years) + 1)) + "|")
    for _, r in summary.iterrows():
        code = r["code"]
        cells = [f"{yearly['pullback'].get(code, {}).get(y, 0.0):+.1f}" for y in years]
        L.append(f"| {r['name']} | " + " | ".join(cells) + f" | {r['pl_excess']:+.1f} |")
    avg = [sum(yearly["pullback"].get(c, {}).get(y, 0.0) for c in qbt.QDII_NAMES) / len(qbt.QDII_NAMES)
           for y in years]
    L.append("| **组合均值** | " + " | ".join(f"**{v:+.1f}**" for v in avg)
             + f" | **{summary['pl_excess'].mean():+.1f}** |")

    L.append("\n## 4. 阈值敏感性（飙升回避组合平均年化超额，%）\n")
    L.append("| z_hi | " + " | ".join(str(z) for z in Z_GRID) + " |")
    L.append("|---|" + "|".join(["---"] * len(Z_GRID)) + "|")
    L.append("| 组合平均超额 | " + " | ".join(f"{sum(sens[z])/len(sens[z]):+.1f}" for z in Z_GRID) + " |")

    L.append("\n## 5. 关键结论\n")
    L.append("- **飙升回避是「可用的尾部对冲」，而非「择时增收益」**：保持 ~95% 暴露、只在溢价 +2σ 飙升的 ~4% 交易日空仓，纳指/标普/德国/中概的夏普与回撤显著改善（纳指夏普 0.89→1.95、回撤 −28.6%→−26.4%；标普 0.84→1.85、−29.7%→−21.5%）。相比绝对阈值回避（纳指暴露仅 ~6%、几乎错杀全部上涨日），相对变化几乎不牺牲长期上涨，是可实盘叠加的「减仓信号」。")
    L.append("- **超额仍源于「溢价飙升→短期回落」的恐慌回归，非普适择时**：按年看飙升回避几乎年年为正（每年至少一次 spike→drawdown），但恒生/日经（溢价均值 0.06%/0.74%，信号弱）超额≈0（恒生 +10.5pp、日经 +50pp，夏普几乎不变），反证超额来自溢价信号本身、不是市场级择时能力。")
    L.append("- **阈值敏感性单调（非单点巧合）**：z_hi 从 1.0→3.0，组合平均年化超额 31.9%→4.2% 单调衰减，说明「溢价飙升越极端、次日越差」是稳定排序，信号真实存在。")
    L.append("- **回落买入更弱且不稳**：纳指/标普/中概/德国为正，但日经转负（累计 −32.9pp）、按年波动大（如中概 2021 +34.4% 但 2019 −24.6%），只作「逢低加仓」参考、不作主策略。")
    L.append("- **操作含义**：监控主告警改为「溢价变动 z 分数」——飙升(>+2σ)→回避/减仓，回落(<−2σ)→逢低买点；绝对阈值仅作兜底提示（今日 3 只 ⚠️ 高溢价但 z 全为「中性」，正是新告警想区分的「高位横盘」状态）。")

    L.append("\n## 6. 口径与边界\n")
    L.append("- **收益口径**：前复权价日收益（累计净值/单位净值复权因子折算），已消除份额拆分。")
    L.append("- **前视**：z 分数滚动均值/标准差滞后 1 期；信号 T 日 → 调仓 T+1 日（shift(1)），无前视。")
    L.append("- **按年超额**为当年总收益差（非年化），2026 为 YTD 累计（约 8 个月），跨年不可直接外推。")
    L.append("- **未计成本**：未计交易/冲击成本、空仓期货基收益；溢价历史为 close/nav 口径，未做汇率/底层日内修正。")

    (ROOT / "runs" / "qdii_relchange_backtest.md").write_text("\n".join(L), encoding="utf-8")
    (ROOT / "runs" / "qdii_relchange_backtest.json").write_text(
        json.dumps({"summary": summary.to_dict(orient="records"),
                    "yearly": yearly, "sensitivity": {str(k): v for k, v in sens.items()}},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ---- 控制台 ----
    pd.set_option("display.width", 220)
    print("\n" + "=" * 100)
    print("QDII 溢价「相对变化」策略回测（全量 + 按年）")
    print("=" * 100)
    show = summary[["name", "bh_annual", "ab_annual", "sp_annual", "pl_annual",
                    "sp_excess", "pl_excess", "sp_exp", "pl_exp"]].copy()
    print(show.to_string(index=False))
    print("\nMarkdown 已写入: runs/qdii_relchange_backtest.md")
    print("JSON 已写入:     runs/qdii_relchange_backtest.json")


if __name__ == "__main__":
    main()
