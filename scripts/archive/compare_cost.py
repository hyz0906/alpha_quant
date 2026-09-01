#!/usr/bin/env python3
"""对比周频 vs 月频 RSRS 轮动的换手成本与净收益（扩展池 12 只）。

从 sliding_pool12（周频，已跑）与 sliding_pool12_monthly（月频，本脚本运行时
应已跑完）的每个窗口 run 目录读 fills.jsonl，汇总：
  - 调仓次数（fills 行数 / 2，因 open+close 成对）
  - 总手续费（fee 字段求和）
  - 总换手（notional 绝对值和 / initial_cash）
并合并 evaluation_metrics.json 的净收益，输出成本归因对比表。

用法：python3 scripts/compare_cost.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # archive 下移一层
RUN_BASES = {
    "weekly": ROOT / "runs/sliding_pool12",
    "monthly": ROOT / "runs/sliding_pool12_monthly",
}
STRAT = {
    "weekly": "rsrs_rotation_weekly",
    "monthly": "rsrs_rotation_monthly",
}


def scan(run_base: Path, strat: str):
    rows = []
    for wdir in sorted(run_base.glob("w*")):
        if not wdir.is_dir():
            continue
        # 只取指定策略的窗口目录（命名 wNN_label_<strat>）
        if not wdir.name.endswith(strat):
            continue
        fills_path = wdir / "artifacts" / "fills.jsonl"
        metrics_path = wdir / "evaluation_metrics.json"
        if not fills_path.exists() or not metrics_path.exists():
            continue
        fees = 0.0
        notional = 0.0
        n_fills = 0
        with open(fills_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                fees += float(rec.get("fee", 0.0))
                notional += abs(float(rec.get("notional", 0.0)))
                n_fills += 1
        m = json.loads(metrics_path.read_text(encoding="utf-8"))
        rows.append({
            "window": wdir.name.split("_")[0],
            "net_return": m["total_return"],
            "fees": fees,
            "n_fills": n_fills,
            "turnover": notional / m.get("final_value", 100000.0) / 2.0,
            "final_value": m["final_value"],
        })
    return rows


def main():
    print("=" * 72)
    print("周频 vs 月频 RSRS 轮动：换手成本归因（扩展池 12 只）")
    print("=" * 72)
    for label, base in RUN_BASES.items():
        strat = STRAT[label]
        rows = scan(base, strat)
        if not rows:
            print(f"\n[{label}] 无数据（{base} 未跑或策略 {strat} 无产物）")
            continue
        n = len(rows)
        total_fees = sum(r["fees"] for r in rows)
        total_turnover = sum(r["turnover"] for r in rows)
        # 跨窗复利净收益
        compound = 1.0
        for r in rows:
            compound *= (1.0 + r["net_return"])
        print(f"\n[{label}] {strat}  ({n} 窗)")
        print(f"  跨窗复利净收益 : {(compound - 1.0) * 100:+.2f}%")
        print(f"  累计手续费     : ¥{total_fees:.0f}")
        print(f"  累计单边换手   : {total_turnover:.2f} 倍")
        print(f"  平均每窗调仓   : {sum(r['n_fills'] for r in rows) / n / 2:.1f} 次")
        print(f"  平均每窗手续费 : ¥{total_fees / n:.0f}")
        for r in rows:
            print(f"    {r['window']} 净收益 {r['net_return']*100:+6.2f}%  "
                  f"费 ¥{r['fees']:6.0f}  换手 {r['turnover']:.2f}x")


if __name__ == "__main__":
    main()
