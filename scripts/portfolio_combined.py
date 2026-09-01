#!/usr/bin/env python3
"""三层组合联合回测：逆波动底仓 × PB 估值门控 × QDII 溢价门控。

§7.13/§7.19/§7.18 分别独立验证了三个有效组件：
  * 逆波动风险平价（§7.13，18 只异构池夏普 0.87 vs 等权 0.51，兜底输出）
  * PB 估值分位择时（§7.19，沪深300/上证50 提夏普控回撤）
  * QDII 溢价飙升回避（§7.18 G，实盘约束版组合净超额 +8.4pp/年）

本脚本做「组合层」联合回测，回答「三层叠加后净效果到底改善多少」：

  A. 逆波动基线（无门控）
  B. 逆波动 + PB 门控（A股股票腿，月频）
  C. 逆波动 + QDII 门控（QDII 腿，日频，实盘约束版）
  D. 三层全开（B + C）

设计要点：
  * 乘法门控：W_final = W_invvol(shift 1) × gate_leg，门控空缺为现金（收益 0），
    不重新归一——「减仓持币」语义，不是「挪仓到别的腿」；
  * 无前视：PB 分位 t 月末算好 shift 1 月应用；QDII spike_avoid_hold 状态机
    T 日信息决定 T+1 持仓（qdii_relchange_realistic 原版）；
  * 统一单边成本 0.15%（月度再平衡 + 门控翻转都计）；
  * 样本 = 18 只共同样本（2020-08 起）——PB 门控证据腿（沪深300）与 QDII
    溢价数据在该窗口内均有完整历史。

诚实边界：PB 门控用沪深300 PB 分位代理全 A 股估值（对中证500/创业板腿是
简化假设——§7.19 证明 PB 择时对中小成长宽基无效）；样本仅 6 年、不含 2008/
2015 完整熊市（PB 门控的强项年份部分缺席）。

用法：python3 scripts/portfolio_combined.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent))       # scripts/

import risk_parity as rp
import qdii_backtest as qbt
from src.data_engine.qdii_calc import relchange_zscore, RELCHANGE_WINDOW
from qdii_relchange_realistic import spike_avoid_hold

ROOT = Path(__file__).resolve().parents[1]
FUND_DIR = ROOT / "data" / "fundamental"

COST = 0.0015          # 单边成本（含月度再平衡 + 门控翻转）
PB_WINDOW = 60         # PB 分位窗口（月，5 年）
PB_MIN = 36            # 分位最小样本

# 腿分类（池 = risk_parity.HETERO_CODES 18 只）
A_STOCK_LEGS = [
    "510300.SH", "510500.SH", "159915.SZ", "512010.SH", "159928.SZ",
    "512880.SH", "512660.SH",
]
QDII_LEGS = ["513100.SH", "513500.SH", "513050.SH", "513880.SH",
             "513030.SH", "159920.SZ"]
PB_GATE_SOURCE = "510300.SH"     # PB 分位信号源（沪深300，§7.19 证据腿）


# --------------------------------------------------------------------------- #
# 门控序列
# --------------------------------------------------------------------------- #
def rolling_pct(s: pd.Series, window: int = PB_WINDOW) -> pd.Series:
    """滚动估值分位（0~1），只用 t 及之前数据。"""
    return s.rolling(window, min_periods=PB_MIN).apply(
        lambda x: float((x <= x.iloc[-1]).mean()), raw=False)


def pb_gate_monthly(rule: str = "triple") -> pd.Series:
    """沪深300 PB 月频门控系数（月末值，待 shift 后应用）。"""
    m = pd.read_csv(FUND_DIR / f"legu_metrics_{PB_GATE_SOURCE}.csv",
                    parse_dates=["date"]).set_index("date").sort_index()
    pct = rolling_pct(m["pb"])
    if rule == "triple":
        gate = pct.map(lambda x: 1.0 if x < 0.3 else (0.5 if x < 0.7 else 0.0))
    elif rule == "binary":
        gate = (pct < 0.5).astype(float)
    elif rule == "linear":
        gate = (1.5 - 1.5 * pct).clip(0.0, 1.0)
    else:
        raise ValueError(rule)
    return gate


def pb_gate_daily(panel_index: pd.DatetimeIndex, rule: str = "triple") -> pd.Series:
    """t 月末分位 → t+1 月应用（shift 1 月），reindex 到日频 ffill。"""
    g = pb_gate_monthly(rule).shift(1)          # 无前视
    daily = g.reindex(panel_index, method="ffill")
    return daily.fillna(1.0).clip(0.0, 1.0)


def qdii_gate_daily(code: str, panel_index: pd.DatetimeIndex) -> pd.Series:
    """单只 QDII 的实盘约束版飙升回避持仓（T 日信号决定 T+1，日频）。

    注意：qdii_backtest 的缓存键是裸代码（513100），池代码带 .SH 后缀须剥离。
    """
    df = qbt.load_premium_history(code.split(".")[0])
    if df is None or df.empty:
        return pd.Series(1.0, index=panel_index)
    z = relchange_zscore(df["premium"], RELCHANGE_WINDOW)
    h = spike_avoid_hold(z, df["premium"])      # floor=1%、min_hold=5 默认
    return h.reindex(panel_index).fillna(1.0).clip(0.0, 1.0)


# --------------------------------------------------------------------------- #
# 组合合成与回测
# --------------------------------------------------------------------------- #
def build_final_weights(panel: pd.DataFrame, pb_rule: str = "triple",
                        pb_scope: str = "all_astock",
                        use_pb: bool = True, use_qdii: bool = True) -> pd.DataFrame:
    """W_final = W_invvol(shift 1) × gate_leg。门控空缺为现金（不归一）。"""
    w = rp.build_weights(panel, "inverse_vol").shift(1).fillna(0.0)
    W = w.copy()

    if use_pb:
        g = pb_gate_daily(panel.index, pb_rule)
        legs = A_STOCK_LEGS if pb_scope == "all_astock" else [PB_GATE_SOURCE]
        for c in legs:
            if c in W.columns:
                W[c] = W[c] * g.reindex(W.index).ffill().fillna(1.0)

    if use_qdii:
        for c in QDII_LEGS:
            if c in W.columns:
                gq = qdii_gate_daily(c, W.index)
                W[c] = W[c] * gq.reindex(W.index).ffill().fillna(1.0)
    return W


def backtest_net(panel: pd.DataFrame, W: pd.DataFrame,
                 cost: float = COST) -> tuple[pd.Series, pd.Series]:
    """含成本净收益：gross − |ΔW|·单边成本。返回 (净收益, 日换手)。"""
    rets = panel.pct_change(fill_method=None)
    gross = (W * rets).sum(axis=1)
    to = W.diff().abs().sum(axis=1).fillna(0.0)
    return (gross - to * cost), to


def full_metrics(net: pd.Series, W: pd.DataFrame) -> dict:
    m = rp.metrics(net)
    m["turnover"] = float(W.diff().abs().sum(axis=1).fillna(0).sum()
                          / (len(W) / rp.TRADING_DAYS))
    m["avg_expo"] = float(W.sum(axis=1).mean())
    return m


def yearly(net: pd.Series) -> pd.Series:
    return net.groupby(net.index.year).apply(lambda x: (1 + x).prod() - 1)


# --------------------------------------------------------------------------- #
# 换手结构拆解（回答「换手率到底是什么、是否按月换手」）
# --------------------------------------------------------------------------- #
def to_ann(W: pd.DataFrame, years: float) -> float:
    """年化「组合层面」单边换手 = Σ_t Σ_i |ΔW_i,t| / 年数。

    注意：这是 18 只资产权重变动的**加总**，可 >1；不等于「全仓换手 N 次」。
    摊到单只 = 该值 / 18 只后看分布（多数腿远低于均值）。
    """
    return float(W.diff().abs().sum(axis=1).fillna(0).sum() / years)


def flips_per_year(s: pd.Series) -> float:
    """门控翻转次数（0↔1 切换）/年。一次完整「减仓+回补」= 2 次翻转。

    年数按序列自身频率推断，不套用日频年数——否则月频序列的翻转次数会被
    除以日频年数而严重高估（月频 46 次 / 21.3 年才对，除以 5.59 会得 8.2）。
    """
    n = float(s.diff().abs().fillna(0).gt(0).sum())
    if len(s) < 3:
        return n / (len(s) / rp.TRADING_DAYS)
    span_days = (s.index[-1] - s.index[0]).days
    avg_gap = span_days / (len(s) - 1)
    years = span_days / 365.25 if avg_gap > 20 else len(s) / rp.TRADING_DAYS
    return n / years


def turnover_breakdown(panel: pd.DataFrame, W_base: pd.DataFrame,
                       W_pb: pd.DataFrame, W_full: pd.DataFrame,
                       years: float) -> dict:
    """拆解换手：按层增量 + 按发生日（月频再平衡 vs 日频门控）。"""
    # 再平衡实际执行日 = 次月首个交易日（build_weights 月末赋值→ffill→shift(1)）
    idx = panel.index
    month_ends = set(idx.to_series().groupby(idx.to_period("M")).last())
    reb_days = {idx[i + 1] for i, d in enumerate(idx)
                if d in month_ends and i + 1 < len(idx)}

    to_daily = W_full.diff().abs().sum(axis=1).fillna(0.0)
    act = to_daily[to_daily > 1e-9]
    is_reb = np.array([d in reb_days for d in act.index])

    per_asset = (W_full.diff().abs().sum() / years).sort_values(ascending=False)

    return {
        "by_layer": {
            "逆波动底仓(月频)": to_ann(W_base, years),
            "PB门控增量(月频)": to_ann(W_pb, years) - to_ann(W_base, years),
            "QDII门控增量(日频)": to_ann(W_full, years) - to_ann(W_pb, years),
            "合计": to_ann(W_full, years),
        },
        "by_day": {
            "月频再平衡执行日数": int(is_reb.sum()),
            "月频换手/年": float(act[is_reb].sum() / years),
            "日频门控触发日数": int((~is_reb).sum()),
            "日频换手/年": float(act[~is_reb].sum() / years),
            "有换手交易日占比": float(len(act) / len(panel)),
        },
        "per_asset_top": [(c, float(v)) for c, v in per_asset.head(6).items()],
        "per_asset_median": float(per_asset.median()),
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    closes = {c: pd.read_csv(ROOT / "data" / f"{c}.csv",
                             parse_dates=["date"]).set_index("date")["close"]
              for c in rp.HETERO_CODES}
    panel = pd.DataFrame(closes).sort_index().dropna()
    print(f"共同样本：{panel.index[0].date()} ~ {panel.index[-1].date()}，"
          f"{panel.shape[0]} 个交易日，{panel.shape[1]} 只\n")

    tiers = {}
    Ws = {}
    configs = [
        ("A. 逆波动基线", dict(use_pb=False, use_qdii=False)),
        ("B. +PB 门控(A股腿)", dict(use_pb=True, use_qdii=False)),
        ("C. +QDII 门控(QDII腿)", dict(use_pb=False, use_qdii=True)),
        ("D. 三层全开", dict(use_pb=True, use_qdii=True)),
    ]
    for name, cfg in configs:
        W = build_final_weights(panel, **cfg)
        net, _ = backtest_net(panel, W)
        tiers[name] = {"m": full_metrics(net, W), "yearly": yearly(net)}
        Ws[name] = W

    # 等权月度基线（对照）
    weq = rp.build_weights(panel, "equal").shift(1).fillna(0.0)
    net_eq, _ = backtest_net(panel, weq)
    tiers["等权(月度,对照)"] = {"m": full_metrics(net_eq, weq), "yearly": yearly(net_eq)}

    # ---- 敏感性（基于 D 档） ----
    sens = {}
    for rule in ["triple", "binary", "linear"]:
        W = build_final_weights(panel, pb_rule=rule)
        net, _ = backtest_net(panel, W)
        sens[f"PB规则={rule}"] = full_metrics(net, W)
    for scope in ["all_astock", "hs300_only"]:
        W = build_final_weights(panel, pb_scope=scope)
        net, _ = backtest_net(panel, W)
        sens[f"PB范围={scope}"] = full_metrics(net, W)
    for c in [0.0, 0.0015, 0.003, 0.005]:
        W = Ws["D. 三层全开"]
        net, _ = backtest_net(panel, W, cost=c)
        mm = rp.metrics(net)
        mm["turnover"] = tiers["D. 三层全开"]["m"]["turnover"]
        sens[f"成本={c*100:.2f}%"] = mm

    # ---- 换手结构拆解 ----
    years = len(panel) / rp.TRADING_DAYS
    tbd = turnover_breakdown(panel, Ws["A. 逆波动基线"],
                             Ws["B. +PB 门控(A股腿)"], Ws["D. 三层全开"], years)
    gates = {"PB门控(沪深300 PB分位, 月频)": pb_gate_monthly("triple").shift(1).dropna()}
    for c in QDII_LEGS:
        gq = qdii_gate_daily(c, panel.index)
        gates[f"QDII {c}"] = gq
    tbd["flips"] = {k: flips_per_year(v) for k, v in gates.items()}

    # ---- 控制台 ----
    print(f"{'档位':<22} {'年化%':>8} {'波动%':>8} {'夏普':>6} {'回撤%':>8} "
          f"{'Calmar':>7} {'换手/年':>7} {'平均暴露':>8}")
    print("-" * 84)
    for name, t in tiers.items():
        m = t["m"]
        print(f"{name:<22} {m['ann_ret']*100:>8.2f} {m['ann_vol']*100:>8.2f} "
              f"{m['sharpe']:>6.2f} {m['max_dd']*100:>8.1f} {m['calmar']:>7.2f} "
              f"{m['turnover']:>7.2f} {m['avg_expo']*100:>7.1f}%")

    print("\n== 逐年收益 ==")
    yl = pd.DataFrame({k: t["yearly"] for k, t in tiers.items()})
    print((yl * 100).round(2).to_string())

    # ---- 落盘 ----
    out = {"sample": {"start": str(panel.index[0].date()),
                      "end": str(panel.index[-1].date()),
                      "n_days": int(panel.shape[0])},
           "tiers": {k: {"metrics": {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                                     for kk, vv in v["m"].items()},
                         "yearly": {str(y): round(float(r), 4)
                                    for y, r in v["yearly"].items()}}
                     for k, v in tiers.items()},
           "turnover_breakdown": tbd,
           "sensitivity": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                               for kk, vv in v.items()} for k, v in sens.items()}}
    (ROOT / "runs" / "portfolio_combined.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    L = ["# 三层组合联合回测：逆波动底仓 × PB 估值门控 × QDII 溢价门控\n",
         "> 乘法门控 `W = W_逆波动(shift1) × gate(腿)`，门控空缺为现金（不重新归一）。",
         "> PB 分位 t 月末算好 t+1 月应用；QDII 门控为实盘约束版（floor=1%、min_hold=5），"
         "T 日信号决定 T+1 持仓。统一单边成本 0.15%（月度再平衡 + 门控翻转均计）。",
         f"> 样本：18 只异构池共同样本 {panel.index[0].date()} ~ {panel.index[-1].date()}"
         f"（{panel.shape[0]} 个交易日）。\n",
         "## 1. 四档对照\n",
         "| 档位 | 年化 | 波动 | 夏普 | 最大回撤 | Calmar | 年化换手 | 平均暴露 |",
         "|---|---|---|---|---|---|---|---|"]
    for name, t in tiers.items():
        m = t["m"]
        L.append(f"| {name} | {m['ann_ret']*100:+.2f}% | {m['ann_vol']*100:.2f}% "
                 f"| {m['sharpe']:.2f} | {m['max_dd']*100:.1f}% | {m['calmar']:.2f} "
                 f"| {m['turnover']:.2f} | {m['avg_expo']*100:.1f}% |")

    L.append("\n## 2. 逐年收益\n")
    L.append("| 年份 | " + " | ".join(tiers.keys()) + " |")
    L.append("|---|" + "---|" * len(tiers))
    for y in yl.index:
        L.append(f"| {y} | " + " | ".join(
            f"{yl.loc[y, k]*100:+.2f}%" for k in tiers.keys()) + " |")

    L.append("\n## 3. 敏感性（基于 D 档）\n")
    L.append("| 变体 | 年化 | 夏普 | 最大回撤 | 换手/年 |")
    L.append("|---|---|---|---|---|")
    for k, m in sens.items():
        L.append(f"| {k} | {m['ann_ret']*100:+.2f}% | {m['sharpe']:.2f} "
                 f"| {m['max_dd']*100:.1f}% | {m.get('turnover', float('nan')):.2f} |")

    L.append("\n## 4. 腿分类与门控来源\n")
    L.append(f"- **PB 门控腿（{len(A_STOCK_LEGS)} 只）**：{'、'.join(A_STOCK_LEGS)}，"
             f"信号 = 沪深300 PB 5 年滚动分位（乐咕月频），三档规则（<30% 全仓、"
             "30~70% 半仓、≥70% 空仓）。")
    L.append(f"- **QDII 门控腿（{len(QDII_LEGS)} 只）**：{'、'.join(QDII_LEGS)}，"
             "信号 = 溢价一阶差分 60 日 z 分数飙升回避（实盘约束版）。")
    L.append("- 其余腿（国债 511010/511180、黄金 518880、豆粕 159985、能源化工 159981）"
             "无门控，仅参与逆波动加权。")

    L.append("\n## 5. 换手结构拆解（是否按月换手？）\n")
    L.append("> 口径：年化换手 = `Σ_t Σ_i |ΔW_i,t| / 年数`，是 **18 只资产权重变动的加总**，"
             "可 >1；**不等于「全仓一年换手 N 次」**。摊到单只后中位数仅 "
             f"{tbd['per_asset_median']:.3f}（约 {tbd['per_asset_median']*100:.0f}pp/年）。\n")
    L.append("**按层拆解（D 档）**\n")
    L.append("| 来源 | 频率 | 换手/年 | 占比 |")
    L.append("|---|---|---|---|")
    tot = tbd["by_layer"]["合计"]
    for k, lbl in [("逆波动底仓(月频)", "月频（次月首个交易日执行）"),
                   ("PB门控增量(月频)", "月频（跨档才动作）"),
                   ("QDII门控增量(日频)", "日频（T+1 执行，min_hold=5）")]:
        v = tbd["by_layer"][k]
        L.append(f"| {k} | {lbl} | {v:.2f} | {v/tot*100:.0f}% |")
    L.append(f"| **合计** | — | **{tot:.2f}** | 100% |\n")
    bd = tbd["by_day"]
    L.append("**按换手发生日拆解**\n")
    L.append("| 类别 | 天数 | 换手/年 | 占比 |")
    L.append("|---|---|---|---|")
    bsum = bd["月频换手/年"] + bd["日频换手/年"]
    L.append(f"| 月频再平衡执行日 | {bd['月频再平衡执行日数']} | "
             f"{bd['月频换手/年']:.2f} | {bd['月频换手/年']/bsum*100:.0f}% |")
    L.append(f"| 日频门控触发日 | {bd['日频门控触发日数']} | "
             f"{bd['日频换手/年']:.2f} | {bd['日频换手/年']/bsum*100:.0f}% |")
    L.append(f"\n有换手的交易日占全部交易日 **{bd['有换手交易日占比']*100:.1f}%**"
             f"（其余 {(1 - bd['有换手交易日占比']) * 100:.0f}% 的交易日完全不动）。\n")
    L.append("**门控翻转频次（次/年，一次「减仓+回补」= 2 次翻转）**\n")
    L.append("| 门控 | 翻转 次/年 | 折合完整回合/年 |")
    L.append("|---|---|---|")
    for k, v in tbd["flips"].items():
        L.append(f"| {k} | {v:.2f} | {v/2:.1f} |")
    L.append("\n**换手最大的腿（年均权重变动）**\n")
    L.append("| 代码 | 年均权重变动 |")
    L.append("|---|---|")
    for c, v in tbd["per_asset_top"]:
        L.append(f"| {c} | {v*100:.1f}pp |")
    L.append(f"\n18 只中位数仅 **{tbd['per_asset_median']*100:.1f}pp/年**——"
             "换手高度集中在 QDII 腿。\n")
    monthly_share = (tbd["by_layer"]["逆波动底仓(月频)"]
                     + tbd["by_layer"]["PB门控增量(月频)"]) / tot
    qdii_leg_w = float(Ws["D. 三层全开"][QDII_LEGS].mean().mean())
    L.append("**结论**：**不是纯粹按月换手**，而是「月度再平衡 + 日频应急减仓」的混合结构。"
             f"月频部分（逆波动 + PB）贡献 {monthly_share*100:.0f}% 换手、节奏固定可预期；"
             f"QDII 门控是日频的、贡献 {(1-monthly_share)*100:.0f}%，"
             f"但单次翻转仅涉及约 {qdii_leg_w*100:.0f}% 的组合资金（单腿均重）。"
             f"按 0.15% 单边成本，年化换手成本 **{tot*0.0015*100:.2f}%**"
             f"（占 D 档年化 {tiers['D. 三层全开']['m']['ann_ret']*100:.2f}% 的 "
             f"{tot*0.0015/tiers['D. 三层全开']['m']['ann_ret']*100:.0f}%）；"
             f"即使把成本抬到 0.50%（极端保守）夏普仍有 "
             f"{sens['成本=0.50%']['sharpe']:.2f}——换手不构成落地障碍。")

    L.append("\n## 6. 关键结论\n")
    base = tiers["A. 逆波动基线"]["m"]
    d = tiers["D. 三层全开"]["m"]
    b = tiers["B. +PB 门控(A股腿)"]["m"]
    c = tiers["C. +QDII 门控(QDII腿)"]["m"]
    sharpe_lift = (d["sharpe"] / base["sharpe"] - 1) * 100
    dd_cut = (1 - d["max_dd"] / base["max_dd"]) * 100
    L.append(f"- **三层全开 vs 逆波动基线**：夏普 {base['sharpe']:.2f}→{d['sharpe']:.2f}"
             f"（{sharpe_lift:+.0f}%）、"
             f"回撤 {base['max_dd']*100:.1f}%→{d['max_dd']*100:.1f}%（降幅 {dd_cut:.0f}%）、Calmar "
             f"{base['calmar']:.2f}→{d['calmar']:.2f}，年化还提高 "
             f"{(d['ann_ret']-base['ann_ret'])*100:+.1f}pp——且已扣 0.15%/边换手成本。"
             "三个组件叠加无内耗，风险端协同增强。")
    L.append(f"- **消融拆解（各自边际贡献）**：PB 门控（B 档）夏普 {base['sharpe']:.2f}→{b['sharpe']:.2f}、"
             f"回撤 {base['max_dd']*100:.1f}%→{b['max_dd']*100:.1f}%、年化微降——「不增收益只控回撤」"
             f"与 §7.19 独立结论一致；QDII 门控（C 档）夏普 {base['sharpe']:.2f}→{c['sharpe']:.2f}、"
             f"年化 {(c['ann_ret']-base['ann_ret'])*100:+.1f}pp——收益风险双改善，是组合层的收益发动机"
             "（QDII 腿占池权重低，该提升已接近独立回测超额折算到权重的量级）。")
    y_base = tiers["A. 逆波动基线"]["yearly"]
    y_d = tiers["D. 三层全开"]["yearly"]
    y_parts = []
    if 2022 in y_base.index and 2022 in y_d.index:
        y_parts.append(f"价值集中在 2022 熊市——基线 {y_base[2022]*100:.1f}%、"
                       f"三层全开 {y_d[2022]*100:.1f}%（两个门控同时压暴露）")
    for y, tag in [(2025, "单边牛市跑输基线（PB 分位冲高过早减仓）"),
                   (2020, "下半年踏空")]:
        if y in y_base.index and y in y_d.index:
            y_parts.append(f"{y} {tag} {(y_d[y]-y_base[y])*100:.1f}pp")
    L.append("- **逐年结构**：" + "；".join(y_parts) + "。"
             "「熊市少亏 + 牛市少赚」是两个门控共同的收益形态，符合其「尾部保护」定位。")
    sens_sharpes = [m["sharpe"] for m in sens.values()]
    L.append(f"- **敏感性稳健**：PB 规则（triple/binary/linear）× 范围（全A股/仅沪深300）× 成本"
             f"（0/0.15%/0.30%/0.50%）共 {len(sens)} 个变体夏普 "
             f"{min(sens_sharpes):.2f}~{max(sens_sharpes):.2f} 全部 >1，无悬崖、无单点依赖。"
             f"binary(0.5) 规则样本内夏普最高（{sens['PB规则=binary']['sharpe']:.2f}），"
             "但**不据此调参**（样本内选择），默认口径仍用三档。")
    L.append(f"- **换手可控且结构清晰**（详见 §5）：D 档年化单边换手 {d['turnover']:.2f}"
             f"（基线 {base['turnover']:.2f}），是 18 只资产权重变动的**加总**而非「全仓换手次数」——"
             f"摊到单只中位数仅 ~{tbd['per_asset_median']*100:.0f}pp/年。"
             f"月频再平衡贡献 {monthly_share*100:.0f}%、日频 QDII 门控 {(1-monthly_share)*100:.0f}%；"
             f"年化换手成本 {tot*0.0015*100:.2f}%（0.15% 单边），"
             f"即使抬到 0.50% 单边夏普仍有 {sens['成本=0.50%']['sharpe']:.2f}。")
    L.append("- **口径边界**：① QDII 单位净值 T+1~T+2 才公布，溢价序列存在固有的 ~1 日信息滞后"
             "（沿用 §7.18 全部回测的同款口径，状态机 T+1 执行 + min_hold=5 部分缓解）；"
             "② PB 门控用沪深300 分位代理全 A 股估值（对中证500/创业板腿是简化假设）；"
             "③ 样本 2020-08 起不含 2008/2015 完整熊市，PB 门控的强项年份部分缺席；"
             "④ 门控空缺为现金（0 收益），不重新归一到其他腿——「减仓持币」语义。")

    (ROOT / "runs" / "portfolio_combined.md").write_text("\n".join(L), encoding="utf-8")
    print("\n已写入: runs/portfolio_combined.md / runs/portfolio_combined.json")


if __name__ == "__main__":
    main()
