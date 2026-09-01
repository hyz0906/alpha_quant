#!/usr/bin/env python3
"""扩展池(12只)显著性检验 + 成本归因 + 滑动窗口配对检验。

对已完成的 validate_pool12_equal / validate_pool12_weekly 两个全区间 run：
  1. 权益曲线切片到评估窗(eval_start=2020-01-02)，剔除 2015~2019 warmup 空仓平段
  2. vibe validation 三件套：monte_carlo / bootstrap_sharpe_ci / walk_forward
  3. 成本归因：汇总 fills.jsonl 手续费，剥离交易成本后的理论收益
  4. 滑动窗口配对检验(13窗超额，来自 sliding_pool12/summary.json)

输出：runs/pool12_validation_20260831.md
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import validation as V

ROOT = Path(__file__).resolve().parents[2]  # archive 下移一层
RUNS = {
    "equal_weight": ROOT / "runs/validate_pool12_equal",
    "rsrs_rotation_weekly": ROOT / "runs/validate_pool12_weekly",
}
EVAL_START = "2020-01-02"
N_SIM = 2000
N_BOOT = 2000
N_WF = 13


def load_equity(run_dir: Path) -> pd.Series:
    df = pd.read_csv(run_dir / "artifacts" / "equity.csv", index_col=0, parse_dates=True)
    eq = df["equity"]
    return eq[eq.index >= pd.Timestamp(EVAL_START)]


def load_trades(run_dir: Path):
    return V._load_trades(run_dir)


def total_fees(run_dir: Path) -> float:
    fees = 0.0
    with open(run_dir / "artifacts" / "fills.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            fees += float(json.loads(line)["fee"])
    return fees


def sign_test_p(k: int, n: int) -> float:
    """符号检验单尾 p = P(Binomial(n,0.5) >= k)，math.comb 手算。"""
    total = 0.0
    for i in range(k, n + 1):
        total += math.comb(n, i) * (0.5 ** n)
    return total


def one_sample_t(vals):
    vals = np.asarray(vals, dtype=float)
    n = len(vals)
    mean = vals.mean()
    std = vals.std(ddof=1)
    if std == 0:
        return (0.0, n - 1, 1.0)
    t = mean / (std / math.sqrt(n))
    return (t, n - 1, mean, std)


def main():
    out = {}
    for name, run_dir in RUNS.items():
        eq = load_equity(run_dir)
        trades = load_trades(run_dir)
        fees = total_fees(run_dir)

        mc = V.monte_carlo_test(trades, 100000.0, n_simulations=N_SIM, seed=42)
        bs = V.bootstrap_sharpe_ci(eq, n_bootstrap=N_BOOT, confidence=0.95, seed=42)
        wf = V.walk_forward_analysis(eq, trades, n_windows=N_WF)

        final_value = float(eq.iloc[-1])
        gross_final = final_value + fees  # 线性近似：把手续费加回
        net_ret = final_value / 100000.0 - 1
        gross_ret = gross_final / 100000.0 - 1
        cost_drag = gross_ret - net_ret  # 成本侵蚀(绝对收益)

        out[name] = {
            "eval_start": EVAL_START,
            "eval_end": str(eq.index[-1].date()),
            "bars": int(len(eq)),
            "final_value": round(final_value, 2),
            "net_return": round(net_ret, 6),
            "gross_return": round(gross_ret, 6),
            "total_fees": round(fees, 2),
            "cost_drag_abs": round(cost_drag, 6),
            "monte_carlo": mc,
            "bootstrap": bs,
            "walk_forward": wf,
        }

    # ---- 滑动窗口配对检验(13窗超额) ----
    sw = json.loads((ROOT / "runs/sliding_pool12/summary.json").read_text(encoding="utf-8"))
    windows = sw["windows"]
    n_win = len(windows)
    for strat in ("equal_weight", "rsrs_rotation_weekly", "rsrs_rotation"):
        excess = [w["strategies"][strat]["excess_return"] for w in windows]
        pos = sum(1 for e in excess if e > 0)
        t, df, mean, std = one_sample_t(excess)
        out[f"paired_{strat}"] = {
            "n": n_win,
            "excess_list": [round(e, 6) for e in excess],
            "mean_excess": round(mean, 6),
            "std_excess": round(std, 6),
            "positive_windows": pos,
            "sign_test_p": round(sign_test_p(pos, n_win), 6),
            "t_stat": round(t, 6),
            "df": df,
        }

    # ---- 写 Markdown 报告 ----
    lines = []
    lines.append("# 扩展池(12只)显著性检验 + 成本归因（2020-01-02 ~ 2026-06-30）\n")
    lines.append("标的：510300/510500/159915/512010/159928/512880/512660/512400/513100/513500/513050/518880\n")
    lines.append("检验工具：vibe `backtest.validation`（monte_carlo / bootstrap / walk_forward）；权益曲线已切片到评估窗剔除 warmup。\n")

    lines.append("## 1. 全区间三件套 + 成本归因\n")
    lines.append("| 策略 | 净收益 | 毛收益 | 总手续费 | 成本侵蚀 | 观察夏普 | Bootstrap 95% CI | MC p(夏普) | WF 正收益窗 |\n")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for name in ("equal_weight", "rsrs_rotation_weekly"):
        o = out[name]
        bs = o["bootstrap"]
        mc = o["monte_carlo"]
        wf = o["walk_forward"]
        lines.append(
            f"| {name} | {o['net_return']*100:.2f}% | {o['gross_return']*100:.2f}% | "
            f"¥{o['total_fees']:.0f} | {o['cost_drag_abs']*100:.2f}% | "
            f"{bs['observed_sharpe']} | [{bs['ci_lower']}, {bs['ci_upper']}] | "
            f"{mc['p_value_sharpe']} | {wf['profitable_windows']}/{wf['n_windows']} |"
        )
    lines.append("")

    lines.append("## 2. 滑动窗口超额配对检验（n=13，基准=窗口内等权买入持有）\n")
    lines.append("| 策略 | 平均超额 | 超额标准差 | 正超额窗 | 符号检验 p | t 统计量 |\n")
    lines.append("|---|---|---|---|---|---|")
    for strat in ("equal_weight", "rsrs_rotation_weekly", "rsrs_rotation"):
        o = out[f"paired_{strat}"]
        lines.append(
            f"| {strat} | {o['mean_excess']*100:.2f}% | {o['std_excess']*100:.2f}% | "
            f"{o['positive_windows']}/{o['n']} | {o['sign_test_p']} | {o['t_stat']} |"
        )
    lines.append("")
    lines.append("> df=12 单尾临界值：1.356(p=0.10) / 1.782(p=0.05) / 2.179(p=0.025)；")
    lines.append("> 符号检验 p = P(二项分布 ≥ 观察正窗数)，越小表示「正超额窗偏多」越不可能是偶然。")
    lines.append("> MC p 值越小越好(<0.05 表示显著优于随机排序)；Bootstrap CI 不含 0 表示夏普显著为正。\n")

    lines.append("## 3. 结论\n")
    lines.append("见正文分析；关键数字：扩展池 12 只、13 窗含 2018/2022 两轮下跌后，")
    lines.append("所有 RSRS 主动策略的窗口超额均为负，等权被动组合是唯一正收益项。")

    report = ROOT / "runs/pool12_validation_20260831.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 同时写 JSON 备查
    json_out = ROOT / "runs/pool12_validation_20260831.json"
    json_out.write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )

    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n报告已写入: {report}")


if __name__ == "__main__":
    main()
