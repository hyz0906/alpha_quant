#!/usr/bin/env python3
"""QDII 溢价监控：真实 IOPV + 影子 IOPV 溢价告警。

§7.12 收口后，QDII 溢价套利是唯一有「结构套利」性质的独立 alpha。本脚本把
qdii_calc.py（已实装真实 IOPV/折价率 + 影子调整）落成可复用监控：
  * 官方溢价 = -东财基金折价率（正值=溢价）
  * 影子溢价 = 价格 / (官方IOPV × (1+底层市场最新涨跌幅)) - 1
  * >3% 溢价告警 / <−3% 折价提示

用法：python3 scripts/qdii_monitor.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_engine.qdii_calc import QDIICalculator, ALERT_THRESHOLD, QDII_UNDERLYING

ROOT = Path("/home/hyz0906/workspace/alpha_quant")


def fmt(x, n=3, suffix=""):
    return f"{x:.{n}f}{suffix}" if pd.notna(x) else "—"


def main():
    calc = QDIICalculator()
    df = calc.get_premiums()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # JSON
    (ROOT / "runs" / "qdii_premium.json").write_text(
        json.dumps({"timestamp": ts, "rows": df.to_dict(orient="records")},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # Markdown
    L = ["# QDII 溢价监控报告\n",
         f"> 时点：{ts}　数据源：东财实时行情（真实 IOPV/折价率）+ 美股/港股指数日线（影子调整）。",
         f"> 告警阈值：|溢价| > {ALERT_THRESHOLD}%。\n"]
    L.append("| 代码 | 名称 | 市场 | 最新价 | 官方IOPV | 官方溢价% | 底层涨跌% | 汇率变动% | 影子IOPV | 影子溢价% | 信号 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in df.iterrows():
        L.append(
            f"| {r['code']} | {r['name']} | {r['market']} | {fmt(r['price'])} "
            f"| {fmt(r['iopv_official'], 4)} | {fmt(r['official_premium_pct'])} "
            f"| {fmt(r['underlying_chg_pct'])} | {fmt(r['fx_chg_pct'])} | {fmt(r['shadow_iopv'], 4)} "
            f"| {fmt(r['shadow_premium_pct'])} | {r['alert']} |"
        )

    L.append("\n## 说明\n")
    L.append("- **官方溢价** = −东财「基金折价率」（正值=溢价、负值=折价），是交易所盘中 IOPV 口径。")
    L.append("- **影子溢价** 把底层市场（美股/港股/德国DAX/日经225）最新一跳 + 汇率最新一跳折进 IOPV，更接近真实净值；美股 QDII 在 A 股盘中官方 IOPV 滞后，影子口径更准。")
    L.append("- **汇率变动** = 中行每日牌价（央行中间价）今日/昨日 − 1，正值=外币升值（人民币计价净值上升）。")
    L.append("- 溢价套利逻辑：影子溢价 >3% 时二级价格显著高于真实净值，回避买入/持有者注意回落风险；<−3% 折价是潜在套利买入窗口。")

    (ROOT / "runs" / "qdii_premium.md").write_text("\n".join(L), encoding="utf-8")

    # 控制台摘要
    print("=" * 100)
    print(f"QDII 溢价监控（{ts}）")
    print("=" * 100)
    pd.set_option("display.width", 200)
    print(df[["code", "name", "price", "iopv_official", "official_premium_pct",
              "underlying_chg_pct", "fx_chg_pct", "shadow_premium_pct", "alert"]].to_string(index=False))
    alerts = df[df["alert"].str.contains("⚠️", na=False)]
    if not alerts.empty:
        print("\n⚠️ 溢价偏高标的：", ", ".join(alerts["code"]))
    print("\nMarkdown 已写入: runs/qdii_premium.md")
    print("JSON 已写入:     runs/qdii_premium.json")


if __name__ == "__main__":
    main()
