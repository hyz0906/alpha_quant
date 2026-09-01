#!/usr/bin/env python3
"""蛋卷基金 63 指数截面估值排名报告（§7.17）。

背景：§7.13/§7.16 确认免费 akshare 无「多年×多标的」估值面板，Tushare
`index_dailybasic` 需 2000 积分（当前 token 无权限）。§7.16 收尾调研时实测发现
蛋卷基金公开接口（danjuanapp.com，雪球旗下）免登录直接返回 63 个指数的当前估值，
自带 pe/pb 历史分位（0~1，越低越便宜），且覆盖跨境指数（纳指100/标普500/恒生/
德国DAX 等）。本脚本落地「63 指数截面估值排名」，替代中证官网 20 日 xls 快照。

诚实边界（重要）：
  * 单截面快照：判断「当前谁便宜谁贵」，非 IC 时间序列（无法回溯历史）。
  * pe=0 表示该指数无 PE 指标（亏损行业），其 pe_percentile=0 不可信，须以 pb 分位为准。
  * 蛋卷历史序列接口 /history 需登录，故仅单截面可用（历史面板仍待 Tushare 或登录 cookie）。

用法：python3 scripts/fundamental_snapshot_report.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 项目根（archive 下移一层）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/（跨脚本 import）

from src.data_engine.fundamental_loader import (
    ETF_INDEX_MAP, fetch_danjuan_index_valuation,
)

ROOT = Path(__file__).resolve().parents[2]  # archive 下移一层


def _fmt(x, n=2):
    return f"{x:.{n}f}" if pd.notna(x) else "—"


def _pct(x, n=1):
    """0~1 分位 → 百分比字符串；x=0 且对应指标缺失时标「—」。"""
    if pd.isna(x):
        return "—"
    return f"{x*100:.{n}f}%"


def main():
    dj = fetch_danjuan_index_valuation()
    print(f"蛋卷指数估值：共 {len(dj)} 个指数")

    # 去掉 SH/SZ 前缀，得到纯数字代码，便于与 ETF_INDEX_MAP 的 cs_code 匹配
    dj["pure_code"] = dj["index_code"].str[2:]

    # ETF 池映射：cs_code -> (etf, 中文名)
    etf_map = {cs: (etf, name) for etf, (cs, name, _) in ETF_INDEX_MAP.items()}
    dj["etf"] = dj["pure_code"].map(lambda c: etf_map[c][0] if c in etf_map else None)
    dj["in_pool"] = dj["etf"].notna()

    # pe=0 表示无 PE 指标（亏损行业），其 pe_percentile 无意义 → 置 NaN
    dj["pe_valid"] = dj["pe"].where(dj["pe"] > 0)
    dj["pe_pct_valid"] = dj["pe_percentile"].where(dj["pe"] > 0)

    # 落地 JSON + CSV
    (ROOT / "runs" / "fundamental_snapshot.json").write_text(
        json.dumps({"count": len(dj), "items": dj.drop(columns=["pure_code", "in_pool"])
                    .to_dict(orient="records")},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    csv_path = ROOT / "data" / "fundamental" / "danjuan_valuation.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    dj.drop(columns=["pure_code"]).to_csv(csv_path, index=False, encoding="utf-8")

    # ------------------------------------------------------------------ #
    # 生成 Markdown
    # ------------------------------------------------------------------ #
    L = []
    snap_date = dj["date"].dropna().iloc[0] if dj["date"].notna().any() else "?"
    L.append("# 蛋卷基金 63 指数截面估值排名报告\n")
    L.append(f"> 数据源：蛋卷基金公开接口（danjuanapp.com，免登录，单截面快照，日期 {snap_date}）。")
    L.append("> **性质**：当前截面「谁便宜谁贵」，非 IC 时间序列。pe=0 表示无 PE（亏损行业），其 PE 分位无意义，以 PB 分位为准。\n")

    # 1. ETF 池对应指数（重点）
    pool = dj[dj["in_pool"]].sort_values("pb_percentile")
    L.append("## 1. 自选 ETF 池对应指数估值（按 PB 分位从便宜到贵）\n")
    L.append("| ETF | 指数 | PE(TTM) | PB | PE分位 | PB分位 | 股息率% | ROE% | 蛋卷评级 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for _, r in pool.iterrows():
        eva = {"low": "低估", "mid": "中性", "high": "高估"}.get(r["eva_type"], r["eva_type"])
        L.append(
            f"| {r['etf']} | {r['name']} | {_fmt(r['pe_valid'])} | {_fmt(r['pb'])} "
            f"| {_pct(r['pe_pct_valid'])} | {_pct(r['pb_percentile'])} "
            f"| {_fmt(r['dividend_yield']*100)} | {_fmt(r['roe']*100)} | {eva} |"
        )
    missing = [etf for etf in ETF_INDEX_MAP if etf not in set(pool["etf"])]
    if missing:
        L.append(f"\n> ⚠️ 蛋卷未覆盖的 ETF 池指数：{', '.join(missing)}")

    # 2. 全市场最便宜 / 最贵（按 PB 分位）
    L.append("\n## 2. 全 63 指数 PB 分位排名（便宜 → 贵）\n")
    L.append("| 排名 | 指数 | 代码 | PE(TTM) | PB | PE分位 | PB分位 | 股息率% | 蛋卷评级 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    ranked = dj.sort_values("pb_percentile")
    for i, (_, r) in enumerate(ranked.iterrows(), 1):
        eva = {"low": "低估", "mid": "中性", "high": "高估"}.get(r["eva_type"], r["eva_type"])
        L.append(
            f"| {i} | {r['name']} | {r['index_code']} | {_fmt(r['pe_valid'])} | {_fmt(r['pb'])} "
            f"| {_pct(r['pe_pct_valid'])} | {_pct(r['pb_percentile'])} "
            f"| {_fmt(r['dividend_yield']*100)} | {eva} |"
        )

    # 3. 蛋卷评级分组统计
    L.append("\n## 3. 蛋卷估值评级分组\n")
    for eva, label in [("low", "低估"), ("mid", "中性"), ("high", "高估"), (None, "其他")]:
        if eva is None:
            sub = dj[~dj["eva_type"].isin(["low", "mid", "high"])]
        else:
            sub = dj[dj["eva_type"] == eva]
        if sub.empty:
            continue
        names = "、".join(sub["name"].tolist())
        L.append(f"- **{label}（{len(sub)} 个）**：{names}")

    # 4. 跨境指数（PE 无国内口径可比，单列）
    cross_kw = ["纳指", "标普", "恒生", "德国", "日经", "中概", "美国", "港股"]
    cross = dj[dj["name"].apply(lambda n: any(k in n for k in cross_kw))]
    if not cross.empty:
        L.append("\n## 4. 跨境/境外指数（单独列示）\n")
        L.append("| 指数 | PE(TTM) | PB | PE分位 | PB分位 | 股息率% |")
        L.append("|---|---|---|---|---|---|")
        for _, r in cross.sort_values("pb_percentile").iterrows():
            L.append(
                f"| {r['name']} | {_fmt(r['pe_valid'])} | {_fmt(r['pb'])} "
                f"| {_pct(r['pe_pct_valid'])} | {_pct(r['pb_percentile'])} "
                f"| {_fmt(r['dividend_yield']*100)} |"
            )

    # 5. 局限
    L.append("\n## 5. 数据覆盖与局限\n")
    L.append("- **覆盖**：63 指数单截面估值（含跨境），自带 pe/pb 历史分位，比中证官网 20 日 xls（6 A 股）广得多。")
    L.append("- **局限 1（单截面）**：无法回溯历史，不能跑截面 IC 时间序列。历史面板仍待 Tushare 2000 积分或蛋卷登录 cookie 解锁 /history。")
    L.append("- **局限 2（pe=0）**：亏损/无 PE 行业（地产等）pe=0，其 PE 分位无意义，估值以 PB 分位为准。")
    L.append("- **局限 3（分位口径）**：蛋卷分位基于其自身历史窗口（begin_at 起），不同指数窗口长短不一，跨指数比较分位时需留意。")

    md_path = ROOT / "runs" / "fundamental_snapshot.md"
    md_path.write_text("\n".join(L), encoding="utf-8")

    # ------------------------------------------------------------------ #
    # 控制台摘要
    # ------------------------------------------------------------------ #
    print("=" * 92)
    print("蛋卷基金 63 指数截面估值排名")
    print("=" * 92)
    print("\n[自选 ETF 池对应指数（按 PB 分位从便宜到贵）]")
    cols = ["etf", "name", "pe_valid", "pb", "pe_pct_valid", "pb_percentile",
            "dividend_yield", "eva_type"]
    show = pool[cols].copy()
    show.columns = ["ETF", "指数", "PE", "PB", "PE分位", "PB分位", "股息率", "评级"]
    print(show.to_string(index=False))
    print("\n[全市场 PB 分位最低 5 个（最便宜）]")
    print(ranked[["name", "index_code", "pb", "pb_percentile"]].head(5).to_string(index=False))
    print("\n[全市场 PB 分位最高 5 个（最贵）]")
    print(ranked[["name", "index_code", "pb", "pb_percentile"]].tail(5).to_string(index=False))
    print(f"\nMarkdown 已写入: {md_path}")
    print(f"JSON 已写入:     {ROOT / 'runs' / 'fundamental_snapshot.json'}")
    print(f"CSV 已缓存:      {csv_path}")


if __name__ == "__main__":
    main()
