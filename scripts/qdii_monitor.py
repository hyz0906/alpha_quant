#!/usr/bin/env python3
"""QDII 溢价监控：真实 IOPV + 影子 IOPV 溢价告警。

§7.12 收口后，QDII 溢价套利是唯一有「结构套利」性质的独立 alpha。本脚本把
qdii_calc.py（已实装真实 IOPV/折价率 + 影子调整）落成可复用监控：
  * 官方溢价 = -东财基金折价率（正值=溢价）
  * 影子溢价 = 价格 / (官方IOPV × (1+底层市场最新涨跌幅)) - 1
  * >3% 溢价告警 / <−3% 折价提示

## 两条路径（2026-09-23 起）

**主路径（live）**：东财 ETF 实时行情（`ak.fund_etf_spot_em()`）→ 官方 IOPV +
折价率 + 影子 IOPV。需要 push2delay.eastmoney.com 可达。

**降级路径（degraded）**：东财行情推送集群（push2/push2delay）会**按 IP 限流
拒连**（`RemoteDisconnected`），实测该限流与本地网络无关、也不是代码 bug。
一旦主路径取数失败，本脚本自动退回**官方溢价缓存序列**
（`data/fundamental/qdii_premium_<code>.csv`，由 `qdii_backtest.py --refresh`
每日产出，走 fund.eastmoney.com，不受限流影响）重建同一张表：
  * price ← 缓存末行 `close`；官方溢价 ← 缓存末行 `premium`
  * z / 相对变化告警 ← `relchange_zscore(缓存序列)` 末值（**与门控同一口径**）
  * 影子 IOPV / 底层涨跌 / 汇率变动 ← 显式置空（缺东财 IOPV，无法计算）
降级不改变任何门控输入（门控走官方溢价序列，与本脚本无关），只影响 §3 展示；
好处是顺带消掉了「§3 影子 z ≠ 门控官方 z」的双口径歧义。

降级是**显式**的：JSON 带 `mode/degraded/degrade_reason/premium_as_of` 字段，
Markdown、控制台、以及 `daily_advice.py` §2 三处都会告警；主路径恢复后自动转回。

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
                                        RELCHANGE_WINDOW, RELCHANGE_Z, relchange_zscore)

ROOT = Path(__file__).resolve().parents[1]


def fmt(x, n=3, suffix=""):
    return f"{x:.{n}f}{suffix}" if pd.notna(x) else "—"


def cache_frame(code6: str) -> pd.DataFrame | None:
    """读历史溢价序列表（close/nav/premium，缓存自 qdii_backtest 每日刷新）。"""
    f = ROOT / "data" / "fundamental" / f"qdii_premium_{code6}.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f, parse_dates=["date"]).set_index("date")
        return df if len(df) >= 20 else None
    except Exception:
        return None


def cache_premium(code6: str) -> pd.Series | None:
    """读历史溢价序列（close/nav − 1，缓存自 qdii_backtest 每日刷新）。"""
    df = cache_frame(code6)
    return None if df is None else df["premium"]


def relchange_alert(code6: str, today_prem: float | None):
    """相对变化告警：今日溢价变动（相对昨日）在近 N 日变动分布中的 z 分数。

    复用 src.data_engine.qdii_calc.relchange_zscore（滚动均值/标准差滞后 1 期），
    与回测/门控同一口径——把今日盘中溢价追加到缓存序列末尾（同日则覆盖），
    取序列最后一个 z 值。返回 (变动bp, z分数, 告警标签)。
    today_prem 为溢价分数（影子优先，官方兜底）。
    """
    h = cache_premium(code6)
    if h is None or today_prem is None or pd.isna(today_prem):
        return None, None, "—"
    if len(h) < 21:
        return None, None, "数据不足"
    today_ts = pd.Timestamp(datetime.now().date())
    if today_ts > h.index[-1]:
        h2 = pd.concat([h, pd.Series([float(today_prem)], index=[today_ts])])
    else:  # 缓存已含今日点（如盘后重跑）：覆盖为盘中最新值
        h2 = h.copy()
        h2.iloc[-1] = float(today_prem)
    z_ser = relchange_zscore(h2, RELCHANGE_WINDOW)
    z = z_ser.iloc[-1]
    d_today = float(h2.iloc[-1] - h2.iloc[-2])
    bp = d_today * 1e4  # 万分之一 = 1bp
    if pd.isna(z):
        return round(bp, 1), None, "—"
    z = float(z)
    if z >= RELCHANGE_Z:
        return round(bp, 1), round(z, 2), "🔺 溢价飙升"
    if z <= -RELCHANGE_Z:
        return round(bp, 1), round(z, 2), "🔻 溢价回落"
    return round(bp, 1), round(z, 2), "中性"


def prev_names() -> dict[str, str]:
    """复用上一次快照里的中文名（降级路径下东财 spot 不可用时的兜底）。"""
    f = ROOT / "runs" / "qdii_premium.json"
    if not f.exists():
        return {}
    try:
        rows = json.loads(f.read_text(encoding="utf-8")).get("rows", [])
        return {r["code"]: r.get("name", "") for r in rows if r.get("code")}
    except Exception:
        return {}


def degraded_rows() -> tuple[pd.DataFrame, str]:
    """降级路径：东财 spot 不可用时，用官方溢价缓存序列重建监控表。

    产出与主路径**同构**（影子相关字段置 None），保证 runs/qdii_premium.json 的
    schema 对下游（`daily_advice.py` §3）不变。返回 (df, premium_as_of)，
    premium_as_of 取 6 腿缓存末日期的**最小值**（最保守口径，任一腿落后即体现）。
    """
    names = prev_names()
    rows, dates = [], []
    for code, meta in QDII_UNDERLYING.items():
        code6 = code.split(".")[0]
        f = cache_frame(code6)
        if f is None or f.empty or "premium" not in f.columns:
            continue
        h = f["premium"].dropna()
        if len(h) < 21:
            continue
        last, prev = float(h.iloc[-1]), float(h.iloc[-2])
        bp = (last - prev) * 1e4
        z_raw = relchange_zscore(h, RELCHANGE_WINDOW).iloc[-1]
        if pd.isna(z_raw):
            z, alert = None, "—"
        else:
            z = round(float(z_raw), 2)
            if z >= RELCHANGE_Z:
                alert = "🔺 溢价飙升"
            elif z <= -RELCHANGE_Z:
                alert = "🔻 溢价回落"
            else:
                alert = "中性"
        close = pd.to_numeric(f["close"].iloc[-1], errors="coerce") if "close" in f.columns else None
        dates.append(h.index[-1].strftime("%Y-%m-%d"))
        rows.append({
            "code": code,
            "name": names.get(code) or meta.get("label", ""),
            "market": meta.get("market", ""),
            "underlying": meta.get("label", ""),
            "price": round(float(close), 4) if pd.notna(close) else None,
            "iopv_official": None,
            "official_premium_pct": round(last * 100, 3),
            "underlying_chg_pct": None,
            "fx_chg_pct": None,
            "shadow_iopv": None,
            "shadow_premium_pct": None,
            "rel_change_bp": round(bp, 1),
            "rel_zscore": z,
            "rel_alert": alert,
            "premium_date": dates[-1],
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["alert"] = [QDIICalculator._alert(r) for _, r in df.iterrows()]
    return df, (min(dates) if dates else "")


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    calc = QDIICalculator()
    degraded, reason, as_of = False, "", ""

    try:
        df = calc.get_premiums()
    except Exception as e:  # 东财 spot 不可用 → 降级到官方溢价缓存序列
        reason = f"{type(e).__name__}: {e}"[:300]
        df, as_of = degraded_rows()
        if df.empty:
            print("❌ 东财 spot 取数失败，且官方溢价缓存序列不可用，无法降级",
                  file=sys.stderr)
            print(reason, file=sys.stderr)
            raise
        degraded = True

    if not degraded:
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

    # JSON（schema 对下游保持不变，仅新增 mode/degraded/degrade_reason/premium_as_of）
    (ROOT / "runs" / "qdii_premium.json").write_text(
        json.dumps({"timestamp": ts,
                    "mode": "degraded" if degraded else "live",
                    "degraded": degraded,
                    "degrade_reason": reason,
                    "premium_as_of": as_of,
                    "rows": df.to_dict(orient="records")},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # Markdown
    if degraded:
        L = ["# QDII 溢价监控报告（降级模式）\n",
             f"> 时点：{ts}",
             "> ⚠️ **降级模式**：东财 ETF 实时行情（push2delay）取数失败，"
             "已退回**官方溢价缓存序列**（`qdii_backtest --refresh` 产出）。",
             "> 本表**官方溢价/z 与门控同源同口径**，可用；"
             "**影子 IOPV、底层涨跌、汇率变动不可用**（需东财 IOPV 实时估值）。",
             f"> 溢价数据日期：{as_of}　失败原因：`{reason}`",
             f"> 告警阈值：|溢价| > {ALERT_THRESHOLD}%。\n"]
        L.append("| 代码 | 名称 | 市场 | 数据日期 | 收盘价 | 官方溢价% | 信号 |")
        L.append("|---|---|---|---|---|---|---|")
        for _, r in df.iterrows():
            L.append(f"| {r['code']} | {r['name']} | {r['market']} | {r['premium_date']} "
                     f"| {fmt(r['price'])} | {fmt(r['official_premium_pct'])} | {r['alert']} |")
    else:
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
    if degraded:
        L.append(f"> 降级模式下取缓存序列（至 {as_of}）的**最后一个 z 值**，"
                 "与门控/回测同一口径、无盘中追加点。")
    L.append("| 代码 | 名称 | 溢价变动(bp) | 变动z | 相对变化告警 |")
    L.append("|---|---|---|---|---|")
    for _, r in df.iterrows():
        L.append(f"| {r['code']} | {r['name']} | {fmt(r['rel_change_bp'], 1)} "
                 f"| {fmt(r['rel_zscore'], 2)} | {r['rel_alert']} |")

    L.append("\n## 说明\n")
    if degraded:
        L.append("- ⚠️ **本报告为降级模式产物**：官方溢价与 z 取自 `data/fundamental/"
                 "qdii_premium_<code>.csv`（净值×价格，由 `qdii_backtest.py --refresh` 刷新），"
                 "与 QDII 溢价门控**同一数据源**，可直接用于判断门控状态。")
        L.append("- 影子 IOPV 需要东财「IOPV实时估值」，降级期无法计算，故该列缺失；"
                 "东财行情推送集群（push2/push2delay）恢复后本脚本自动转回主路径。")
        L.append("- 判「门控是否翻转」仍以 `runs/portfolio_live.json → qdii_gates` 为准，"
                 "本表 z 与其同口径、可互相印证。")
    else:
        L.append("- **官方溢价** = −东财「基金折价率」（正值=溢价、负值=折价），是交易所盘中 IOPV 口径。")
        L.append("- **影子溢价** 把底层市场（美股/港股/德国DAX/日经225）最新一跳 + 汇率最新一跳折进 IOPV，更接近真实净值；美股 QDII 在 A 股盘中官方 IOPV 滞后，影子口径更准。")
        L.append("- **汇率变动** = 中行每日牌价（央行中间价）今日/昨日 − 1，正值=外币升值（人民币计价净值上升）。")
    L.append("- **相对变化告警（主）**：2024 起额度告罄使溢价结构性抬升，绝对阈值几乎全天触发、失去判别力，故改为看「溢价的异常变动」——飙升回避买入、回落是逢低买点。")
    L.append("- **绝对阈值（兜底）**：|溢价| >3% 仍作为兜底提示（影子溢价 >3% 注意回落风险、<−3% 是折价买入窗口）。")

    (ROOT / "runs" / "qdii_premium.md").write_text("\n".join(L), encoding="utf-8")

    # 控制台摘要
    print("=" * 100)
    if degraded:
        print("⚠️ QDII 溢价监控：降级模式（东财 spot 取数失败，改用官方溢价缓存序列）")
        print(f"   原因：{reason}")
        print(f"   溢价数据日期：{as_of}（最落后腿）")
    print(f"QDII 溢价监控（{ts}）")
    print("=" * 100)
    pd.set_option("display.width", 200)
    cols = (["code", "name", "premium_date", "price", "official_premium_pct",
             "alert", "rel_change_bp", "rel_zscore", "rel_alert"] if degraded else
            ["code", "name", "price", "iopv_official", "official_premium_pct",
             "underlying_chg_pct", "fx_chg_pct", "shadow_premium_pct", "alert",
             "rel_change_bp", "rel_zscore", "rel_alert"])
    print(df[cols].to_string(index=False))
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
