#!/usr/bin/env python3
"""QDII 溢价「飙升回避」实盘约束版回测（§7.18 E 收口）。

背景：§7.18 F 的 `qdii_relchange_backtest.py` 证明「溢价一阶差分 z 分数 >+2σ 次日空仓」
能把纳指夏普从 0.89 拉到 1.95、标普 0.84→1.85，但那是「理想化」口径——零成本、可任意
空仓/买回、忽略 QDII 申赎摩擦。本脚本给同一信号加三大约束，量化「真实净超额还剩多少」：

  1. 触发前提 floor（%）：premium > floor 时才允许 z 飙升触发空仓，过滤低溢价噪声区的
     误触发（恒生/日经溢价均值仅 0.06%/0.74%，z 触发多为噪声）；
  2. 最小持有期 min_hold（交易日）：空仓后至少 N 日才允许买回，贴合 QDII 申赎 T+2 到账
     与决策缓冲，同时压降换手；
  3. 单边交易成本 cost（小数）：每次仓位翻转扣 cost（含佣金 + 冲击 + 滑点 + 限购摩擦）。

用法：python3 scripts/qdii_relchange_realistic.py
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

# 实盘约束默认参数
FLOOR = 1.0        # 触发前提：溢价 > 1% 才看 z（%）
MIN_HOLD = 5       # 最小持有期：空仓至少 5 交易日才买回
COST = 0.0015      # 单边交易成本 0.15%（佣金 + 冲击 + 滑点 + 限购摩擦）

FLOOR_GRID = [0.0, 0.5, 1.0, 2.0, 3.0]
MINHOLD_GRID = [1, 3, 5, 10, 20]
COST_GRID = [0.0, 0.0005, 0.0015, 0.003]


def spike_avoid_hold(z: pd.Series, premium: pd.Series, z_hi: float = RELCHANGE_Z,
                     floor: float = FLOOR, min_hold: int = MIN_HOLD) -> pd.Series:
    """飙升回避持仓（实盘约束版，状态机，无前视）。

    - 持有中：premium > floor% 且 z > +z_hi → 次日空仓（减仓）
    - 空仓中：至少空仓 min_hold 日、且 z 回落到 z_hi 以下 → 次日买回

    用截至 T 日信息决定 T+1 日持仓（hold[i] 由 i-1 轮更新的 state 决定）。
    """
    n = len(z)
    hold = pd.Series(1.0, index=z.index, dtype=float)
    state = 1        # 1=持有, 0=空仓（代表 T+1 日的持仓决策）
    short_days = 0   # 已空仓的交易日数

    for i in range(n):
        hold.iloc[i] = state            # T 日持仓
        v = z.iloc[i]
        p = premium.iloc[i]
        if state == 1:
            if pd.notna(v) and pd.notna(p) and p > floor / 100.0 and v > z_hi:
                state = 0
                short_days = 0
        else:
            short_days += 1
            if short_days >= min_hold and pd.notna(v) and v <= z_hi:
                state = 1
    return hold


def net_ret(ret: pd.Series, hold: pd.Series, cost: float = COST) -> pd.Series:
    """含单边交易成本的净收益序列：净收益 = 持仓收益 − 换手×单边成本。"""
    return ret * hold - hold.diff().abs() * cost


def main():
    rows = []
    sens_floor = {f: [] for f in FLOOR_GRID}
    sens_hold = {m: [] for m in MINHOLD_GRID}
    sens_cost = {c: [] for c in COST_GRID}

    for code, name in qbt.QDII_NAMES.items():
        df = qbt.load_premium_history(code)
        if df is None:
            print(f"[warn] {code} {name} 数据加载失败，跳过")
            continue
        ret = df["ret"]
        z = relchange_zscore(df["premium"], RELCHANGE_WINDOW)

        # 原版（理想，无成本无约束）——对比基线
        h_ideal = (z <= RELCHANGE_Z).astype(float).shift(1).fillna(1.0)
        # 实盘约束版
        h_real = spike_avoid_hold(z, df["premium"])

        bh = qbt._perf(ret)
        ideal = qbt._perf(ret * h_ideal)
        real = qbt._perf(net_ret(ret, h_real, COST))

        rows.append({
            "code": code, "name": name,
            "bh_annual": round(bh["annual"] * 100, 2),
            "bh_sharpe": round(bh["sharpe"], 2),
            "ideal_annual": round(ideal["annual"] * 100, 2),
            "ideal_sharpe": round(ideal["sharpe"], 2),
            "ideal_mdd": round(ideal["mdd"] * 100, 2),
            "ideal_exp": round(float(h_ideal.mean() * 100), 1),
            "ideal_turn": float(h_ideal.diff().abs().sum()),
            "real_annual": round(real["annual"] * 100, 2),
            "real_sharpe": round(real["sharpe"], 2),
            "real_mdd": round(real["mdd"] * 100, 2),
            "real_exp": round(float(h_real.mean() * 100), 1),
            "real_turn": float(h_real.diff().abs().sum()),
            "real_excess": round((real["annual"] - bh["annual"]) * 100, 2),
            "ideal_excess": round((ideal["annual"] - bh["annual"]) * 100, 2),
        })

        # 敏感性：组合平均「净年化超额」（策略年化 − 基准年化，已扣成本）
        for f in FLOOR_GRID:
            h = spike_avoid_hold(z, df["premium"], floor=f, min_hold=MIN_HOLD)
            s = qbt._perf(net_ret(ret, h, COST))
            sens_floor[f].append((s["annual"] - bh["annual"]) * 100)
        for m in MINHOLD_GRID:
            h = spike_avoid_hold(z, df["premium"], floor=FLOOR, min_hold=m)
            s = qbt._perf(net_ret(ret, h, COST))
            sens_hold[m].append((s["annual"] - bh["annual"]) * 100)
        for c in COST_GRID:
            h = spike_avoid_hold(z, df["premium"], floor=FLOOR, min_hold=MIN_HOLD)
            s = qbt._perf(net_ret(ret, h, c))
            sens_cost[c].append((s["annual"] - bh["annual"]) * 100)

        print(f"[ok] {code} {name}: 理想超额 {rows[-1]['ideal_excess']:+.2f}pp "
              f"→ 实盘净超额 {rows[-1]['real_excess']:+.2f}pp, "
              f"换手 {rows[-1]['ideal_turn']:.0f}→{rows[-1]['real_turn']:.0f} 次")

    summary = pd.DataFrame(rows)

    # ---- Markdown ----
    L = ["# QDII 溢价「飙升回避」实盘约束版回测报告\n",
         f"> 信号：溢价一阶差分滚动 z 分数（窗口 {RELCHANGE_WINDOW} 交易日，滞后 1 期，无前视）。",
         f"> 实盘约束：触发前提 floor={FLOOR}%（premium>floor 才看 z）、最小持有期 {MIN_HOLD} 日、单边成本 {COST*100:.2f}%。",
         "> 收益口径：前复权价日收益（已消份额拆分）；信号 T 日 → 调仓 T+1 日。\n"]

    L.append("## 1. 理想版 vs 实盘约束版（全量 2018-2026）\n")
    L.append("| 代码 | 名称 | 基准年化% | 基准夏普 | 理想年化% | 理想夏普 | 理想换手 | 实盘年化% | 实盘夏普 | 实盘回撤% | 实盘暴露% | 实盘换手 | 净超额pp |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in summary.iterrows():
        L.append(f"| {r['code']} | {r['name']} | {r['bh_annual']:.2f} | {r['bh_sharpe']:.2f} | "
                 f"{r['ideal_annual']:.2f} | {r['ideal_sharpe']:.2f} | {r['ideal_turn']:.0f} | "
                 f"{r['real_annual']:.2f} | {r['real_sharpe']:.2f} | {r['real_mdd']:.2f} | "
                 f"{r['real_exp']:.1f} | {r['real_turn']:.0f} | {r['real_excess']:+.2f} |")

    L.append("\n## 2. 触发前提敏感性（floor，%，min_hold=5、cost=0.15%）\n")
    L.append("| floor | " + " | ".join(f"{f:g}" for f in FLOOR_GRID) + " |")
    L.append("|---|" + "|".join(["---"] * len(FLOOR_GRID)) + "|")
    L.append("| 组合平均净年化超额 | " + " | ".join(
        f"{sum(sens_floor[f])/len(sens_floor[f]):+.1f}" for f in FLOOR_GRID) + " |")

    L.append("\n## 3. 最小持有期敏感性（min_hold 交易日，floor=1%、cost=0.15%）\n")
    L.append("| min_hold | " + " | ".join(str(m) for m in MINHOLD_GRID) + " |")
    L.append("|---|" + "|".join(["---"] * len(MINHOLD_GRID)) + "|")
    L.append("| 组合平均净年化超额 | " + " | ".join(
        f"{sum(sens_hold[m])/len(sens_hold[m]):+.1f}" for m in MINHOLD_GRID) + " |")

    L.append("\n## 4. 单边成本敏感性（cost，floor=1%、min_hold=5）\n")
    L.append("| cost | " + " | ".join(f"{c*100:.2f}%" for c in COST_GRID) + " |")
    L.append("|---|" + "|".join(["---"] * len(COST_GRID)) + "|")
    L.append("| 组合平均净年化超额 | " + " | ".join(
        f"{sum(sens_cost[c])/len(sens_cost[c]):+.1f}" for c in COST_GRID) + " |")

    L.append("\n## 5. 关键结论\n")
    S = summary.set_index("code")

    def _v(code: str, col: str) -> float:
        return float(S.loc[code, col]) if code in S.index else float("nan")

    def _avg(xs: list) -> float:
        return sum(xs) / len(xs) if xs else float("nan")

    nas_shrink = (1 - _v("513100", "real_excess") / _v("513100", "ideal_excess")) * 100
    L.append(f"- **实盘约束不杀信号，只杀「高换手高溢价」的纳指**：标普/德国/中概的净超额"
             f"几乎完整保留甚至微升（标普 {_v('513500', 'ideal_excess'):+.2f}→{_v('513500', 'real_excess'):+.2f}、"
             f"德国 {_v('513030', 'ideal_excess'):+.2f}→{_v('513030', 'real_excess'):+.2f}、"
             f"中概 {_v('513050', 'ideal_excess'):+.2f}→{_v('513050', 'real_excess'):+.2f}），"
             f"纳指缩水 {nas_shrink:.0f}%（{_v('513100', 'ideal_excess'):+.2f}→{_v('513100', 'real_excess'):+.2f}），"
             f"日经 {_v('513880', 'ideal_excess'):+.2f}→{_v('513880', 'real_excess'):+.2f}。"
             f"原因：纳指溢价波动最大、换手最高（理想 {_v('513100', 'ideal_turn'):.0f} 次），"
             "成本 + min_hold 约束的杀伤集中落在它身上。")
    L.append(f"- **触发前提 floor 净效果为正**：floor=0→3% 组合净超额 "
             f"{_avg(sens_floor[0.0]):+.1f}→{_avg(sens_floor[3.0]):+.1f} 平缓单调、无悬崖；"
             "floor 抬到 1% 过滤低溢价噪声触发，标普/德国因此净超额不降反微升。")
    L.append(f"- **最小持有期有效压降换手**：min_hold={MIN_HOLD} 把纳指换手 "
             f"{_v('513100', 'ideal_turn'):.0f}→{_v('513100', 'real_turn'):.0f}、"
             f"恒生 {_v('159920', 'ideal_turn'):.0f}→{_v('159920', 'real_turn'):.0f}，"
             "贴合 QDII T+2 到账约束；min_hold 1→20 组合净超额 "
             f"{_avg(sens_hold[1]):+.1f}→{_avg(sens_hold[20]):+.1f} 平缓衰减。")
    L.append(f"- **成本敏感性温和，信号并非「薄利到碰不得」**：cost 0→0.30% 组合净超额仅 "
             f"{_avg(sens_cost[0.0]):+.1f}→{_avg(sens_cost[0.003]):+.1f}"
             f"（降 {_avg(sens_cost[0.0]) - _avg(sens_cost[0.003]):.1f}pp），"
             f"0.15% 合理成本下组合净超额 {_avg(sens_cost[0.0015]):+.1f}pp，仍显著为正。")
    L.append(f"- **操作含义（修正上一轮判断）**：上一轮「收益被高估 2-3 倍」的说法偏悲观——"
             "实盘约束下真实净超额是「纳指被高估、其余基本实」。"
             f"组合平均净年化超额 {_avg(list(S['real_excess'])):+.1f}pp，"
             f"纳指 {_v('513100', 'real_excess'):+.2f}、标普 {_v('513500', 'real_excess'):+.2f}、"
             f"德国 {_v('513030', 'real_excess'):+.2f}，仍可作为「减仓/对冲」信号叠加；"
             "但必须容忍 QDII 限购导致「想买回时买不回」的执行风险，且 2026 年信号衰减趋势未变。")

    L.append("\n## 6. 口径与边界\n")
    L.append("- **收益口径**：前复权价日收益（累计净值/单位净值复权因子折算），已消除份额拆分。")
    L.append("- **前视**：z 分数滚动均值/标准差滞后 1 期；状态机用截至 T 日信息决定 T+1 持仓，无前视。")
    L.append("- **成本**：单边 cost=0.15% 为佣金 + 冲击 + 滑点 + 限购摩擦的保守合计；QDII 场内卖出仅佣金（免印花税），但申赎 T+2、限购暂停申购是额外隐性成本。")
    L.append("- **未计**：空仓期资金再投资收益、溢价历史未做汇率/底层日内修正。")

    (ROOT / "runs" / "qdii_relchange_realistic.md").write_text("\n".join(L), encoding="utf-8")
    (ROOT / "runs" / "qdii_relchange_realistic.json").write_text(
        json.dumps({"summary": summary.to_dict(orient="records"),
                    "sens_floor": {str(k): v for k, v in sens_floor.items()},
                    "sens_hold": {str(k): v for k, v in sens_hold.items()},
                    "sens_cost": {str(k): v for k, v in sens_cost.items()}},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    pd.set_option("display.width", 240)
    print("\n" + "=" * 110)
    print("QDII 溢价「飙升回避」实盘约束版回测")
    print("=" * 110)
    show = summary[["name", "bh_annual", "ideal_annual", "real_annual",
                    "ideal_excess", "real_excess", "ideal_turn", "real_turn",
                    "real_sharpe", "real_mdd"]].copy()
    print(show.to_string(index=False))
    print("\nMarkdown 已写入: runs/qdii_relchange_realistic.md")
    print("JSON 已写入:     runs/qdii_relchange_realistic.json")


if __name__ == "__main__":
    main()
