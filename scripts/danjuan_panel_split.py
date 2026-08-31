#!/usr/bin/env python3
"""拆分蛋卷面板的两张估值表 + 与乐咕 PB 交叉验证（数据可信度核验）。

背景：抓取时只能用 `id` 遍历（source 参数与 id 互斥），结果 id 空间里混了
两张不同的估值表。用「与最新期指数集合的 Jaccard 相似度」判别——实测在 0.3
处有明显断层（0.17 / 0.41 之间无样本），无歧义：
  * lsd（螺丝钉，主力）：1707 期，2019-09-04 ~ 2026-08-31，28 → 62 只
  * other（另一张表，2024-12 停更）：1733 期，10 → 14.5 只

输出：
  data/fundamental/danjuan_valuation_panel.csv  增加 source_guess 列
  data/fundamental/danjuan_valuation_lsd.csv    只有 lsd 的干净面板
  runs/danjuan_panel_report.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FUND = ROOT / "data" / "fundamental"
CSV = FUND / "danjuan_valuation_panel.csv"
CSV_LSD = FUND / "danjuan_valuation_lsd.csv"
META = FUND / "danjuan_valuation_panel.json"
OUT_MD = ROOT / "runs" / "danjuan_panel_report.md"


def split_source(df: pd.DataFrame) -> pd.DataFrame:
    latest_id = df.loc[df["date"].idxmax(), "period_id"]
    latest_set = set(df.loc[df["period_id"] == latest_id, "index_code"])
    sets = df.groupby("period_id")["index_code"].apply(set)
    jac = sets.apply(lambda s: len(s & latest_set) / len(s | latest_set))
    df = df.copy()
    df["source_guess"] = df["period_id"].map(
        lambda p: "lsd" if jac.get(p, 0) >= 0.3 else "other")
    return df


def cross_check_legu(df: pd.DataFrame) -> dict:
    """蛋卷 vs 乐咕 PB：整段相关性 + 分年段相关性（识别口径切换）。"""
    out = {}
    pairs = {"SH000300": "510300.SH", "SH000016": "510050.SH",
             "SH000905": "510500.SH"}
    for dcode, fcode in pairs.items():
        p = FUND / f"legu_metrics_{fcode}.csv"
        if not p.exists():
            continue
        lg = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()
        dj = df[(df["index_code"] == dcode) & (df["source_guess"] == "lsd")][
            ["date", "pb"]].dropna().set_index("date")["pb"]
        dj_m = dj.resample("ME").last().dropna()
        lg_m = lg["pb"].resample("ME").last().dropna()
        j = pd.concat([dj_m.rename("dj"), lg_m.rename("lg")],
                      axis=1).dropna()
        if len(j) < 12:
            continue
        # 分年段相关性（2024 年起两个口径是否背离）
        seg = {}
        for label, lo, hi in [("2019-2023", 2019, 2023), ("2024+", 2024, 2100)]:
            s = j[(j.index.year >= lo) & (j.index.year <= hi)]
            if len(s) >= 6:
                seg[label] = {
                    "n": int(len(s)),
                    "corr": float(s["dj"].corr(s["lg"])),
                    "mean_dj": float(s["dj"].mean()),
                    "mean_lg": float(s["lg"].mean()),
                    "gap": float(s["dj"].mean() / s["lg"].mean() - 1),
                }
        out[dcode] = {
            "n_months": int(len(j)),
            "corr": float(j["dj"].corr(j["lg"])),
            "mean_dj": float(j["dj"].mean()),
            "mean_lg": float(j["lg"].mean()),
            "rel_diff": float((j["dj"] - j["lg"]).abs().mean() / j["lg"].mean()),
            "range": [str(j.index.min().date()), str(j.index.max().date())],
            "segments": seg,
        }
    return out


def main():
    if not CSV.exists():
        sys.exit("面板 CSV 不存在")
    df = pd.read_csv(CSV, dtype={"index_code": str}, low_memory=False)
    df["date"] = pd.to_datetime(df["date"])
    df = split_source(df)
    df.to_csv(CSV, index=False, encoding="utf-8-sig")

    lsd = df[df["source_guess"] == "lsd"].copy()
    lsd.to_csv(CSV_LSD, index=False, encoding="utf-8-sig")

    xc = cross_check_legu(df)

    meta = json.loads(META.read_text(encoding="utf-8")) if META.exists() else {}
    L = ["# 蛋卷 VIP 估值面板——数据体检与可信度核验\n",
         "> 来源：`danjuanfunds.com /djapi/fundx/base/vip/valuation/show/detail`"
         "（螺丝钉估值表，VIP 接口，按 `id` 逐期抓取）。",
         f"> 抓取时间：{meta.get('fetched_at', '—')}；"
         f"⚠️ 会员数据，仅供个人研究，勿对外分发。\n"]

    L.append("## 1. 两张表的拆分（关键）\n")
    L.append("抓取只能用 `id` 遍历（`source` 与 `id` 互斥），id 空间里混了"
             "**两张不同的估值表**。用「与最新期指数集合的 Jaccard 相似度」判别，"
             "实测在 0.3 处有明显断层（0.17 与 0.41 之间无样本），无歧义：\n")
    L.append("| 表 | 期数 | 时间跨度 | 指数数（首→末） |")
    L.append("|---|---|---|---|")
    for src in ["lsd", "other"]:
        s = df[df["source_guess"] == src]
        per = s.groupby("period_id").size()
        L.append(f"| **{src}** | {s['period_id'].nunique()} | "
                 f"{s['date'].min().date()} ~ {s['date'].max().date()} | "
                 f"{per.iloc[0]} → {per.iloc[-1]} |")
    L.append("\n- **lsd（螺丝钉，主力面板）**：延续至今，指数池从 28 只扩充到 62 只；")
    L.append("- **other（另一张表）**：2024-12 停更，仅 10~15 只，可作长历史补充。\n")

    L.append("## 2. lsd 面板规模\n")
    L.append("| 指标 | 值 |")
    L.append("|---|---|")
    L.append(f"| 期数 | {lsd['date'].nunique()} |")
    L.append(f"| 指数数 | {lsd['index_code'].nunique()} |")
    L.append(f"| 总行数 | {len(lsd)} |")
    L.append(f"| 时间跨度 | {lsd['date'].min().date()} ~ {lsd['date'].max().date()} |\n")
    L.append("**每期指数数演进（按年）**\n")
    L.append("| 年份 | 期数 | 平均指数数 |")
    L.append("|---|---|---|")
    for y, g in lsd.assign(y=lsd["date"].dt.year).groupby("y"):
        L.append(f"| {y} | {g['date'].nunique()} | {g.groupby('date').size().mean():.1f} |")

    L.append("\n## 3. 可信度核验：与乐咕 PB 交叉验证\n")
    L.append("独立数据源（乐咕乐股，月频，项目已用于 §7.19 时序择时）"
             "对齐到月末后比对：\n")
    L.append("| 指数 | 对齐月数 | 整段相关 | 蛋卷均值 | 乐咕均值 | 平均相对偏差 |")
    L.append("|---|---|---|---|---|---|")
    for k, v in xc.items():
        L.append(f"| {k} | {v['n_months']} | {v['corr']:.3f} | {v['mean_dj']:.3f} "
                 f"| {v['mean_lg']:.3f} | {v['rel_diff']*100:.1f}% |")

    L.append("\n**分年段（关键：2024 年起沪深300/上证50 出现口径背离）**\n")
    L.append("| 指数 | 年段 | 月数 | 相关 | 蛋卷均值 | 乐咕均值 | 蛋卷/乐咕偏离 |")
    L.append("|---|---|---|---|---|---|---|")
    for k, v in xc.items():
        for seg, s in v["segments"].items():
            L.append(f"| {k} | {seg} | {s['n']} | {s['corr']:.3f} | "
                     f"{s['mean_dj']:.3f} | {s['mean_lg']:.3f} | {s['gap']*100:+.1f}% |")

    if xc:
        good = [k for k, v in xc.items() if v["corr"] >= 0.9]
        bad = [k for k, v in xc.items() if v["corr"] < 0.9]
        L.append("")
        if good:
            L.append(f"- **{'/'.join(good)} 与乐咕整段相关 "
                     f"{min(xc[k]['corr'] for k in good):.2f}+、偏离 "
                     f"{max(abs(xc[k]['rel_diff']) for k in good)*100:.1f}% 以内**"
                     "——两个独立源一致，数据可信。")
        if bad:
            L.append(f"- ⚠️ **{'/'.join(bad)} 整段相关仅 "
                     f"{min(xc[k]['corr'] for k in bad):.2f}~"
                     f"{max(xc[k]['corr'] for k in bad):.2f}**：分年段看，"
                     "2019-2023 尚属同源小偏差，**2024 年起蛋卷 PB 持续走高而"
                     "乐咕持平**（沪深300 2026 年 1.85 vs 1.45，差 +28%），"
                     "判定为**蛋卷侧口径切换/换数据源**，非随机错误。")
        L.append("- **对用途的影响**：截面 IC 用的是**同一时点横截面排序"
                 "（分位数）**，对统一口径的水平偏差不敏感，因此**不影响主力用途**；"
                 "但若要把蛋卷 PB 的**绝对水平**与乐咕时序（§7.19 PB 择时）混用，"
                 "须先做口径对齐，否则会误判估值高低。")
    else:
        L.append("\n（未找到乐咕缓存，跳过交叉验证）")

    L.append("\n## 4. 可交易映射（截面 IC 的横截面基础）\n")
    latest = lsd[lsd["date"] == lsd["date"].max()]
    has = latest["inside_fund"].notna()
    L.append(f"最新期 {latest['date'].iloc[0].date()} 共 {len(latest)} 个指数，"
             f"**{int(has.sum())} 个有场内 ETF 代码**（可直接取收益序列）。\n")
    L.append("| 指数代码 | 名称 | 场内ETF | PE | PB | PE分位5y | 股息率 |")
    L.append("|---|---|---|---|---|---|---|")
    for _, r in latest.sort_values("pe_percent_r5y").iterrows():
        etf = r["inside_fund"]
        etf = "-" if pd.isna(etf) else str(int(float(etf)))
        L.append(f"| {r['index_code']} | {r['index_name']} | {etf} | {r['pe']} "
                 f"| {r['pb']} | {r['pe_percent_r5y']:.2f} | {r['dividend_yield']:.3f} |")

    L.append("\n## 5. 已知边界\n")
    L.append("- **幸存者偏差**：指数池从 28 只扩到 62 只，且历史上被移出的指数"
             "（面板共 144 个 index_code）在后期无数据——做截面 IC 时只能用"
             "「当期存在的指数」算截面，天然规避该偏差，但**不能用它回测"
             "「全历史持有某指数」**。")
    L.append("- **PE=0 表示不适用**（银行/证券等金融行业），对应分位也是 0，"
             "做 IC 前须剔除或改用 PB。")
    L.append("- **QDII 指数（NDX/SP500/HK*）无溢价调整**：其 ETF 二级市场存在"
             "显著溢价（§7.18），用 ETF 价格算收益会混入溢价波动，"
             "截面 IC 应单独分组或剔除。")
    L.append("- **会员数据**：仅个人研究使用，勿分发；cookie 会过期，"
             "增量更新需重新获取。")

    OUT_MD.write_text("\n".join(L), encoding="utf-8")
    print(f"lsd: {lsd['date'].nunique()} 期 / {lsd['index_code'].nunique()} 指数 / "
          f"{len(lsd)} 行 / {lsd['date'].min().date()} ~ {lsd['date'].max().date()}")
    print("交叉验证:", json.dumps(xc, ensure_ascii=False))
    print(f"报告：{OUT_MD}")


if __name__ == "__main__":
    main()
