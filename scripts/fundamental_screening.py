#!/usr/bin/env python3
"""基本面/估值因子诊断（carry + value 快照）。

§7.12 已证伪价量维度全部三条主动 alpha 路线，§9 收口把「基本面/另类因子」列为
最高优先。本脚本落地第一项：用 akshare 免费数据抓取估值快照，做截面
carry/value 诊断。

诚实边界（重要）：
  * 免费源可得的估值数据分两类：
      - 中证指数官方估值快照（最新约 20 日 PE + 股息率）—— 单截面，可覆盖 6 只行业/宽基 ETF
      - 乐咕乐股月频完整历史（2005 至今）—— 但仅覆盖 3 个宽基（上证50/沪深300/中证500）
    因此本脚本做「截面估值/股息率排名 + 股债性价比」，全历史分位仅对沪深300/中证500 可得。
    标准截面 IC 时间序列检验需「多年 × 多标的」估值面板（Tushare index_dailybasic，需 2000 积分）。
  * 债券/商品/跨境 ETF 无 PE 概念，仅标注替代 carry 口径，不参与权益估值排名。

用法：python3 scripts/fundamental_screening.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_engine.fundamental_loader import (
    ETF_INDEX_MAP, NON_EQUITY_NOTE, fetch_bond_yield_10y, fetch_valuation_snapshot,
)
from src.strategies.factors.fundamental_factors import (
    earnings_yield, equity_risk_premium,
)

ROOT = Path("/home/hyz0906/workspace/alpha_quant")


def main():
    snap = fetch_valuation_snapshot()
    # 盈利收益率（%）
    snap["earnings_yield"] = snap["pe"].map(lambda p: round(100.0 / p, 2) if pd.notna(p) else None)
    # 1/PE 排序（贵→便宜）
    snap["pe_rank"] = snap["pe"].rank(ascending=True)
    snap["dy_rank"] = snap["dividend_yield"].rank(ascending=False)

    try:
        r10y = fetch_bond_yield_10y()
    except Exception as e:
        r10y = None
        print(f"[warn] 10Y 国债收益率抓取失败: {str(e)[:60]}")

    snap["erp"] = snap["earnings_yield"].map(
        lambda ey: round(equity_risk_premium(ey, r10y), 2) if (pd.notna(ey) and r10y) else None)

    # 落地 JSON
    out_json = {
        "snap_date": snap["snap_date"].dropna().iloc[0] if snap["snap_date"].notna().any() else None,
        "risk_free_10y": r10y,
        "equity": snap.to_dict(orient="records"),
        "non_equity_note": NON_EQUITY_NOTE,
    }
    (ROOT / "runs" / "fundamental_valuation.json").write_text(
        json.dumps(out_json, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # 生成 Markdown
    L = []
    L.append("# 基本面/估值因子诊断（carry + value 快照）\n")
    L.append("> 数据源：中证指数官方估值快照 + 乐咕乐股 1 年 PE/PB 分位（akshare，免 token）。")
    L.append("> **局限**：免费源无多年点-in-time 估值历史，本报告为单截面快照，非 IC 时间序列。\n")

    L.append("## 1. A 股权益 ETF 估值与 carry 快照\n")
    L.append("| ETF | 指数 | PE(TTM) | 静态PE | 股息率% | 盈利收益率% | PE分位(全史) | PB分位(全史) | 10Y国债 | ERP% |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for _, r in snap.iterrows():
        def fmt(x, n=2):
            return f"{x:.{n}f}" if pd.notna(x) else "—"
        L.append(
            f"| {r['etf']} | {r['index']} | {fmt(r['pe'])} | {fmt(r['pe_static'])} "
            f"| {fmt(r['dividend_yield'])} | {fmt(r['earnings_yield'])} "
            f"| {fmt(r['pe_pct_hist']*100 if pd.notna(r['pe_pct_hist']) else None, 0)}% "
            f"| {fmt(r['pb_pct_hist']*100 if pd.notna(r['pb_pct_hist']) else None, 0)}% "
            f"| {fmt(r10y) if r10y else '—'} | {fmt(r['erp'])} |"
        )

    L.append("\n## 2. 估值排名（价值因子：越便宜越靠前）\n")
    eq = snap.dropna(subset=["pe"]).sort_values("pe")
    L.append("**按 PE 从低到高（便宜 → 贵）**：")
    L.append(" | ".join(f"{r['etf']}({r['pe']:.1f}x)" for _, r in eq.iterrows()))
    dy = snap.dropna(subset=["dividend_yield"]).sort_values("dividend_yield", ascending=False)
    L.append("**按股息率从高到低（carry 排序）**：")
    L.append(" | ".join(f"{r['etf']}({r['dividend_yield']:.2f}%)" for _, r in dy.iterrows()))

    L.append("\n## 3. 股债性价比（FED 模型简版）\n")
    if r10y:
        L.append(f"- 10 年期国债收益率 = **{r10y:.2f}%**。")
        for _, r in snap.dropna(subset=["erp"]).iterrows():
            flag = "权益占优" if r["erp"] > 0 else "债券占优"
            L.append(f"- {r['etf']}（{r['index']}）：盈利收益率 {r['earnings_yield']:.2f}% − 国债 {r10y:.2f}% = **ERP {r['erp']:.2f}%** → {flag}")
    else:
        L.append("- 10 年期国债收益率未能获取，跳过 ERP 计算。")

    L.append("\n## 4. 非权益资产 carry 口径（不参与 PE 排名）\n")
    L.append("| ETF | carry/估值口径 |")
    L.append("|---|---|")
    for etf, note in NON_EQUITY_NOTE.items():
        L.append(f"| {etf} | {note} |")

    L.append("\n## 5. 数据覆盖与局限\n")
    L.append("- **已覆盖**：7 只 A 股权益 ETF 中 6 只有中证指数官方 PE + 股息率（创业板 399006 因中证官网 404 缺失，待走深证/国证官网或乐咕）。")
    L.append("- **全历史分位**：仅沪深300/中证500 拿到乐咕月频完整历史（2005 至今）PE/PB 分位，其余宽基以下指数乐咕不覆盖。")
    L.append("- **无法做多标的截面 IC 检验**：截面 Spearman IC 需要「多年 × 多标的 点-in-time 因子 × 未来收益」面板，免费源只有「20 日快照 × 6 行业」+「月频多年 × 3 宽基」，无法支撑标准截面 IC。")
    L.append("- **补全路径**：Tushare Pro `index_dailybasic`（指数估值日频历史，需 2000 积分；已实测当前 token 无权限，错误码 40203）。这是剩余的数据接入任务。")

    (ROOT / "runs" / "fundamental_valuation.md").write_text("\n".join(L), encoding="utf-8")

    # 控制台打印摘要
    print("=" * 90)
    print("基本面/估值因子诊断（快照）")
    print("=" * 90)
    print(snap[["etf", "index", "pe", "dividend_yield", "earnings_yield",
                "pe_pct_hist", "pb_pct_hist", "erp"]].to_string(index=False))
    if r10y:
        print(f"\n10Y 国债收益率 = {r10y:.2f}%")
    print("\nMarkdown 已写入: runs/fundamental_valuation.md")
    print("JSON 已写入:     runs/fundamental_valuation.json")


if __name__ == "__main__":
    main()
