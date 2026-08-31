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

from src.data_engine.qdii_calc import (QDIICalculator, ALERT_THRESHOLD, QDII_UNDERLYING,
                                        RELCHANGE_WINDOW, RELCHANGE_Z)

ROOT = Path("/home/hyz0906/workspace/alpha_quant")


def fmt(x, n=3, suffix=""):
    return f"{x:.{n}f}{suffix}" if pd.notna(x) else "—"


def cache_premium(code6: str) -> pd.Series | None:
    """读历史溢价序列（close/nav − 1，缓存自 qdii_backtest 每日刷新）。"""
    f = ROOT / "data" / "fundamental" / f"qdii_premium_{code6}.csv"
    if not f.exists():
        return None
    try:
        h = pd.read_csv(f, parse_dates=["date"]).set_index("date")["premium"]
        return h if len(h) >= 20 else None
    except Exception:
        return None


def relchange_alert(code6: str, today_prem: float | None):
    """相对变化告警：今日溢价变动（相对昨日）在近 N 日变动分布中的 z 分数。

    返回 (变动bp, z分数, 告警标签)。today_prem 为溢价分数（影子优先，官方兜底）。
    """
    h = cache_premium(code6)
    if h is None or today_prem is None or pd.isna(today_prem):
        return None, None, "—"
    d_hist = h.diff().dropna()
    if len(d_hist) < 20:
        return None, None, "数据不足"
    mu = float(d_hist.tail(RELCHANGE_WINDOW).mean())
    sd = float(d_hist.tail(RELCHANGE_WINDOW).std())
    d_today = float(today_prem) - float(h.iloc[-1])
    bp = d_today * 1e4  # 万分之一 = 1bp
    z = (d_today - mu) / sd if sd and sd > 0 else None
    if z is None:
        return round(bp, 1), None, "—"
    if z >= RELCHANGE_Z:
        return round(bp, 1), round(z, 2), "🔺 溢价飙升"
    if z <= -RELCHANGE_Z:
        return round(bp, 1), round(z, 2), "🔻 溢价回落"
    return round(bp, 1), round(z, 2), "中性"


def main():
    calc = QDIICalculator()
    df = calc.get_premiums()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 相对变化告警：今日溢价变动 vs 近 N 日变动分布的 z 分数
    rel = []
    for _, r in df.iterrows():
        code6 = str(r["code"]).split(".")[0]
        today = r["shadow_premium_pct"]
        if pd.isna(today):
            today = r["official_premium_pct"]
        today_frac = today / 100.0 if pd.notna(today) else None
        rel.append(relchange_alert(code6, today_frac))
    df["rel_change_bp"] = [x[0] for x in rel]
    df["rel_zscore"] = [x[1] for x in rel]
    df["rel_alert"] = [x[2] for x in rel]

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

    L.append("\n## 相对变化告警（溢价一阶差分 z 分数）\n")
    L.append(f"> 用近 {RELCHANGE_WINDOW} 个交易日的溢价日变动分布衡量今日变动的「异常度」；"
             f"z > +{RELCHANGE_Z} = 溢价飙升（额度骤紧/抢购，回避买入）；"
             f"z < -{RELCHANGE_Z} = 溢价回落（底层大涨、场内价滞后，潜在买点）。")
    L.append("| 代码 | 名称 | 溢价变动(bp) | 变动z | 相对变化告警 |")
    L.append("|---|---|---|---|---|")
    for _, r in df.iterrows():
        L.append(f"| {r['code']} | {r['name']} | {fmt(r['rel_change_bp'], 1)} "
                 f"| {fmt(r['rel_zscore'], 2)} | {r['rel_alert']} |")

    L.append("\n## 说明\n")
    L.append("- **官方溢价** = −东财「基金折价率」（正值=溢价、负值=折价），是交易所盘中 IOPV 口径。")
    L.append("- **影子溢价** 把底层市场（美股/港股/德国DAX/日经225）最新一跳 + 汇率最新一跳折进 IOPV，更接近真实净值；美股 QDII 在 A 股盘中官方 IOPV 滞后，影子口径更准。")
    L.append("- **汇率变动** = 中行每日牌价（央行中间价）今日/昨日 − 1，正值=外币升值（人民币计价净值上升）。")
    L.append("- **相对变化告警（主）**：2024 起额度告罄使溢价结构性抬升，绝对阈值几乎全天触发、失去判别力，故改为看「溢价的异常变动」——飙升回避买入、回落是逢低买点。")
    L.append("- **绝对阈值（兜底）**：|溢价| >3% 仍作为兜底提示（影子溢价 >3% 注意回落风险、<−3% 是折价套利买入窗口）。")

    (ROOT / "runs" / "qdii_premium.md").write_text("\n".join(L), encoding="utf-8")

    # 控制台摘要
    print("=" * 100)
    print(f"QDII 溢价监控（{ts}）")
    print("=" * 100)
    pd.set_option("display.width", 200)
    print(df[["code", "name", "price", "iopv_official", "official_premium_pct",
              "underlying_chg_pct", "fx_chg_pct", "shadow_premium_pct", "alert",
              "rel_change_bp", "rel_zscore", "rel_alert"]].to_string(index=False))
    alerts = df[df["alert"].str.contains("⚠️", na=False)]
    if not alerts.empty:
        print("\n⚠️ 溢价偏高标的：", ", ".join(alerts["code"]))
    rel_alerts = df[df["rel_alert"].str.contains("飙升|回落", na=False)]
    if not rel_alerts.empty:
        print("\n🔺 相对变化告警：", ", ".join(
            f"{r['code']}({r['rel_alert']})" for _, r in rel_alerts.iterrows()))
    print("\nMarkdown 已写入: runs/qdii_premium.md")
    print("JSON 已写入:     runs/qdii_premium.json")


if __name__ == "__main__":
    main()
