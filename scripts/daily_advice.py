#!/usr/bin/env python3
"""每日收盘复盘 —— 汇总四类数据 + 输出**次日调仓建议**（§7.20 / §7.26 口径）。

由 WorkBuddy 每日 22:00 定时任务调用（21:30 的 crontab `qdii_daily.py` 已跑完
数据生成，本脚本只做读取、计算与报告，不联网不写账本）。

输入（全部为 21:30 任务的产出物）：
  1. runs/qdii_premium.json    —— 溢价快照（6 只 QDII）
  2. runs/qdii_backtest.json   —— 套利回测绩效
  3. runs/portfolio_live.json  —— 三层组合实盘信号（目标权重 / 门控 / PB）
  4. runs/paper_trading.md + data/paper_ledger.json + data/paper_nav.csv —— 模拟盘对账

输出：runs/daily_advice_<as_of>.md（as_of = 数据日期，非运行日期）

核心算法 —— **金额守恒配对的整数手求解**。
`paper_trading.py reconcile` 的原建议对卖出腿与买入腿分别 floor 到 100 份，
两边同时向下取整导致「卖出回款 > 买入支出」，多余现金溢出（实测现金从 16.1%
抬到 24.2%、总偏离 28.4pp；最优解仅 21.4pp）。本脚本改为：
    卖出腿份数 = floor(超配金额 / 卖出价 / LOT) * LOT
    买入腿预算 = 卖出总回款（净额，已扣佣金），按各买入腿缺口比例分配
    买入腿份数 = floor(预算_i / 买入价_i / LOT) * LOT
使「买入金额 ≈ 卖出回款」，现金占比基本不动、总偏离最小。

用法：python3 scripts/daily_advice.py [--as-of 2026-09-01] [--min-dev 0.02]
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
DATA = ROOT / "data"

# 整数手求解器抽到 rebalance_solver，与 paper_trading.py reconcile 共用同一实现，
# 保证两个入口给出完全一致的调仓建议。改算法只改那一个模块。
from rebalance_solver import FEE, LOT, solve_lots  # noqa: E402


# ---------------------------------------------------------------- 基础读取

def _json(p: Path):
    return json.load(open(p, encoding="utf-8"))


def last_close(code: str) -> tuple[str, float] | None:
    """取 CSV 最后一行 (date, close)。"""
    f = DATA / f"{code}.csv"
    if not f.exists():
        return None
    rows = list(csv.DictReader(open(f, encoding="utf-8")))
    if not rows:
        return None
    r = rows[-1]
    return r["date"], float(r["close"])


def load_names() -> dict:
    try:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        from portfolio_live import NAMES  # type: ignore
        return dict(NAMES)
    except Exception:
        return {}


def pct(x: float, d: int = 2) -> str:
    return f"{x*100:.{d}f}%"


# ---------------------------------------------------------------- 报告

def build_report(as_of: str, premium, backtest, live, ledger, nav_row,
                 names: dict, min_dev: float) -> tuple[str, dict]:
    tw = live["target_weights"]
    fresh = live.get("data_freshness", {})
    pb = live.get("pb", {})
    gates = live.get("qdii_gates", {})
    positions = {k: int(v) for k, v in ledger["positions"].items()}
    cash = float(ledger["cash"])

    codes = sorted(set(list(tw.keys()) + list(positions.keys())))
    px, px_date = {}, {}
    for c in codes:
        r = last_close(c)
        if r:
            px_date[c], px[c] = r
        else:
            px[c] = 0.0
            px_date[c] = "-"

    mv = sum(positions.get(c, 0) * px[c] for c in codes)
    total = cash + mv
    sol = solve_lots(positions, px, tw, total, cash,
                     live.get("cash", 0.0), min_dev)

    L: list[str] = []
    A = L.append

    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    A(f"# AlphaQuant 每日复盘与次日调仓建议（数据日期 {as_of}）\n")
    A(f"> 生成时间 {run_at} ｜ 口径：§7.20 三层组合 + §7.26 策略矩阵 "
      f"｜ 成本：单边 0.15% ｜ 整数手 100 份\n")

    # ---- 1. 一句话结论
    n_act = len(sol["sells"]) + len(sol["buys"])
    A("## 1. 一句话结论\n")
    if n_act == 0:
        A(f"**明日无需调仓** —— 当前持仓与目标权重偏离均在 {min_dev*100:.0f}pp "
          f"阈值内（总绝对偏离 {sol['before_abs_dev']:.1f}pp）。\n")
    else:
        parts = []
        for c, sh in sorted(sol["sells"].items()):
            parts.append(f"卖 {names.get(c, c)}({c}) {sh} 份")
        for c, sh in sorted(sol["buys"].items()):
            parts.append(f"买 {names.get(c, c)}({c}) {sh} 份")
        A("**明日需调仓：" + "、".join(parts) + "。**\n")
        A(f"执行后总绝对偏离 {sol['before_abs_dev']:.1f}pp → "
          f"**{sol['total_abs_dev']:.1f}pp**（改善 "
          f"{sol['before_abs_dev']-sol['total_abs_dev']:.1f}pp），"
          f"现金占比 {cash/total*100:.1f}% → {sol['cash_after']/total*100:.1f}%。\n")

    # ---- 2. 数据健康
    A("\n## 2. 数据健康\n")
    stale = fresh.get("panel_stale", False)
    prem_stale = fresh.get("qdii_premium_stale", {}) or {}
    err_log = ROOT / "logs" / "qdii_daily.err.log"
    err_size = err_log.stat().st_size if err_log.exists() else 0
    issues = []
    if stale:
        issues.append("⚠️ 行情面板陈旧（panel_stale=True）")
    if prem_stale:
        issues.append(f"⚠️ QDII 溢价数据滞后：{prem_stale}")
    if err_size > 0:
        issues.append(f"⚠️ qdii_daily.err.log 非空（{err_size} 字节）")
    if premium.get("timestamp", "")[:10] != as_of:
        issues.append(f"⚠️ 溢价快照时间戳 {premium.get('timestamp')} ≠ 数据日期 {as_of}")
    if issues:
        for i in issues:
            A(f"- {i}")
    else:
        A(f"- ✅ 全部新鲜：ETF 收盘至 {fresh.get('etf_close_last')}、"
          f"乐咕 PB 至 {fresh.get('legu_pb_last')}、"
          f"溢价快照 {premium.get('timestamp')}；错误日志 0 字节")
    A("")

    # ---- 3. 溢价快照
    A("\n## 3. QDII 溢价快照\n")
    A("| 代码 | 名称 | 市场 | 价格 | 官方溢价 | 影子溢价 | z 值 | 相对变化告警 | 门控(今→明) |")
    A("|---|---|---|---|---|---|---|---|---|")
    for r in premium.get("rows", []):
        c = r["code"]
        g = gates.get(c, {})
        gt = f"{g.get('today')}→{g.get('tomorrow')}" if g else "-"
        flag = " **翻转**" if g and g.get("today") != g.get("tomorrow") else ""
        A(f"| {c} | {r.get('name','')} | {r.get('market','')} | {r.get('price')} | "
          f"{r.get('official_premium_pct')}% | {r.get('shadow_premium_pct')}% | "
          f"{r.get('rel_zscore')} | {r.get('rel_alert','')} | {gt}{flag} |")
    flips = [c for c, g in gates.items() if g.get("today") != g.get("tomorrow")]
    A("")
    if flips:
        A(f"**门控翻转**：{', '.join(flips)} —— 次日需按新状态调整对应腿。")
    else:
        A("**门控无翻转**，6 只 QDII 腿次日维持当前持有/空仓状态。")
    A("")

    # ---- 4. 组合信号
    A("\n## 4. 三层组合信号\n")
    A(f"- **PB 估值门控**：沪深300 PB 分位 {pct(pb.get('percentile', 0), 1)} "
      f"（{pb.get('pct_month','')}）→ A 股腿目标仓位 **{pct(pb.get('level', 0), 0)}**"
      f"{'（已空仓）' if pb.get('level', 0) == 0 else ''}")
    if pb.get("degraded"):
        A("  - ⚠️ PB 数据降级，门控可能失真")
    A(f"- **QDII 溢价门控**：{len(gates)} 只，持有 "
      f"{sum(1 for g in gates.values() if g.get('tomorrow') == 1.0)} 只、"
      f"空仓 {sum(1 for g in gates.values() if g.get('tomorrow') == 0.0)} 只")
    A(f"- **目标现金比**：{pct(live.get('cash', 0))}")
    A(f"- **是否月末再平衡日**：{'是（月频再平衡触发）' if live.get('is_month_end') else '否'}")
    A(f"- **信号层动作清单**：{len(live.get('actions', []))} 项"
      f"（信号层只反映「目标权重较上一日的变化」，与下方对账层的偏离纠偏是两回事）")
    A("")

    # ---- 5. 套利回测
    A("\n## 5. 套利回测刷新\n")
    s = backtest.get("summary", []) if isinstance(backtest, dict) else []
    if isinstance(s, list) and s:
        ends = sorted({r.get("end", "") for r in s if r.get("end")})
        A(f"样本截至 {ends[-1] if ends else '-'}，共 {len(s)} 只。"
          "av=溢价回避（溢价高时空仓）、dc=折价买入、bh=买入持有。\n")
        A("| 代码 | 名称 | 样本 | 溢价均值 | P90 | bh年化/夏普 | av年化/夏普 | dc年化/夏普 |")
        A("|---|---|---|---|---|---|---|---|")
        for r in s:
            A(f"| {r.get('code','')} | {r.get('name','')} | {r.get('n','')} | "
              f"{r.get('prem_mean',0):.2f}% | {r.get('prem_p90',0):.2f}% | "
              f"{r.get('bh_annual','')}% / {r.get('bh_sharpe','')} | "
              f"{r.get('av_annual','')}% / {r.get('av_sharpe','')} | "
              f"{r.get('dc_annual','')}% / {r.get('dc_sharpe','')} |")
        if ends:
            A("")
            A(f"**数据新鲜度**：回测序列已刷新至 {ends[-1]}"
              f"{'（与数据日期一致）' if ends[-1] == as_of else '（⚠️ 落后于数据日期 %s）' % as_of}")
    else:
        A("（backtest summary 为空，详见 runs/qdii_backtest.md）")
    A("")

    # ---- 6. 模拟盘对账
    A("\n## 6. 模拟盘对账\n")
    if nav_row:
        A(f"- 总资产 **{nav_row['total']}** 元（现金 {nav_row['cash']} + "
          f"市值 {nav_row['market']}）")
        A(f"- 当日 {float(nav_row['day_ret'])*100:+.2f}% ｜ "
          f"累计 {float(nav_row['cum_ret'])*100:+.2f}%")
    A("")
    A("| 代码 | 名称 | 现价 | 持仓 | 实际占比 | 目标占比 | 偏差 | 拟动作 | 执行后占比 |")
    A("|---|---|---|---|---|---|---|---|---|")
    rows_sorted = sorted(codes, key=lambda c: -abs(sol["before_w"].get(c, 0) - tw.get(c, 0)))
    for c in rows_sorted:
        act = ""
        if c in sol["sells"]:
            act = f"**卖 {sol['sells'][c]}**"
        elif c in sol["buys"]:
            act = f"**买 {sol['buys'][c]}**"
        dev = (sol["before_w"].get(c, 0) - tw.get(c, 0)) * 100
        A(f"| {c} | {names.get(c, c)} | {px[c]} | {positions.get(c, 0)} | "
          f"{sol['before_w'].get(c,0)*100:.1f}% | {tw.get(c,0)*100:.1f}% | "
          f"{dev:+.1f}pp | {act} | {sol['after_w'].get(c,0)*100:.1f}% |")
    tgt_cash = live.get("cash", 0.0)
    A(f"| — | **现金** | — | — | {cash/total*100:.1f}% | {tgt_cash*100:.1f}% | "
      f"{(cash/total-tgt_cash)*100:+.1f}pp | — | {sol['cash_after']/total*100:.1f}% |")
    A("")
    A(f"- 调仓前总绝对偏离 **{sol['before_abs_dev']:.1f}pp** → 调仓后 "
      f"**{sol['total_abs_dev']:.1f}pp**（含现金项；目标函数 = Σ|占比−目标| "
      f"+ |现金占比−目标|，枚举搜索最优整数手）")
    A(f"- 卖出回款（扣佣金）≈ {sol['proceeds']:,.0f} 元 ｜ "
      f"买入支出（含佣金）≈ {sol['spend']:,.0f} 元 ｜ "
      f"现金 {cash:,.0f} → {sol['cash_after']:,.0f} 元")
    A(f"- 仅处理偏差 ≥ {min_dev*100:.0f}pp 的腿，其余为噪音不动")
    A("")

    # ---- 7. 明日指令
    A("\n## 7. 明日调仓指令（可直接下单）\n")
    if n_act == 0:
        A("无。保持当前持仓与现金。")
    else:
        A("| 方向 | 代码 | 名称 | 份数 | 参考价 | 金额(估) |")
        A("|---|---|---|---|---|---|")
        for c, sh in sorted(sol["sells"].items()):
            amt = sh * px[c]
            A(f"| 卖出 | {c} | {names.get(c,c)} | {sh} | {px[c]} | {amt:,.0f} |")
        for c, sh in sorted(sol["buys"].items()):
            amt = sh * px[c]
            A(f"| 买入 | {c} | {names.get(c,c)} | {sh} | {px[c]} | {amt:,.0f} |")
        A("")
        A(f"> 参考价为 {as_of} 收盘价，次日以实际成交价为准。"
          f"先卖后买（场内 T+0 可用，卖出资金当日即可买入）。")
    A("")

    # ---- 8. 风险提示
    A("\n## 8. 风险提示\n")
    risks = []
    hi = [r for r in premium.get("rows", [])
          if isinstance(r.get("official_premium_pct"), (int, float))
          and r["official_premium_pct"] > 5]
    if hi:
        risks.append("**溢价偏高**：" + "、".join(
            f"{r['code']} {r['official_premium_pct']}%" for r in hi)
            + " —— 若用 QDII_ABS 口径（溢价>3% 即空仓）会结构性踏空；"
              "现行 THREE 口径用 z 值变化信号，高溢价平稳期继续持有。")
    if live.get("is_month_end"):
        risks.append("月末再平衡日：目标权重按逆波动重算，换手可能高于平日。")
    if pb.get("percentile", 0) > 0.7:
        risks.append(f"PB 分位 {pct(pb['percentile'],1)} 处于高位，A 股腿已空仓，"
                     "若估值回落需等下月信号才回补。")
    risks.append("回测口径为 T→T+1 无前视、单边成本 0.15%；实盘成交价与收盘价"
                 "存在滑点，长期跟踪误差需持续观察。")
    for i, r in enumerate(risks, 1):
        A(f"{i}. {r}")
    A("")

    return "\n".join(L), sol


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="每日复盘与次日调仓建议")
    ap.add_argument("--as-of", default=None, help="数据日期 YYYY-MM-DD，默认取 portfolio_live.json 的 as_of")
    ap.add_argument("--min-dev", type=float, default=0.02,
                    help="调仓触发阈值（占比偏差），默认 0.02 = 2pp")
    args = ap.parse_args()

    live = _json(RUNS / "portfolio_live.json")
    as_of = args.as_of or live.get("as_of")
    premium = _json(RUNS / "qdii_premium.json")
    try:
        backtest = _json(RUNS / "qdii_backtest.json")
    except Exception:
        backtest = {}
    ledger = _json(DATA / "paper_ledger.json")

    nav_row = None
    nav_f = DATA / "paper_nav.csv"
    if nav_f.exists():
        rows = list(csv.DictReader(open(nav_f, encoding="utf-8")))
        for r in rows:
            if r["date"] == as_of:
                nav_row = r
        if nav_row is None and rows:
            nav_row = rows[-1]

    names = load_names()
    report, sol = build_report(as_of, premium, backtest, live, ledger,
                               nav_row, names, args.min_dev)

    out = RUNS / f"daily_advice_{as_of}.md"
    out.write_text(report, encoding="utf-8")

    print(f"[daily_advice] as_of={as_of}")
    print(f"[daily_advice] 卖出 {len(sol['sells'])} 腿 / 买入 {len(sol['buys'])} 腿")
    print(f"[daily_advice] 总绝对偏离 {sol['before_abs_dev']:.1f}pp -> {sol['total_abs_dev']:.1f}pp")
    print(f"[daily_advice] 报告 -> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
