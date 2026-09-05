#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三层组合参数扫描与稳健性检验（实验分支 exp/new-strategy 专用）。

三层组合 = 逆波动底仓 × PB 估值门控 × QDII 溢价门控。本脚本把三层各自的
旋钮全部参数化，做单因子网格扫描，并额外检验两项结构性改动：

  1. 门控空缺资金从「0 收益现金」改投货币腿 511880（cash_mmf）
  2. 目标波动率缩放（vol_target）：按已实现波动动态调暴露

稳健性口径：全样本 = 2020-01 ~ 2026-09；IS（样本内）= ~2023-06；
OOS（样本外）= 2023-07 ~。所有网格结果同时给出 IS / OOS 夏普，
避免只报样本内最优（那本质是过拟合）。

用法：python3 scripts/tune_three_layer.py
输出：runs/tune_three_layer.md / .json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import risk_parity as rp                       # noqa: E402
import portfolio_combined as pc                # noqa: E402
import qdii_backtest as qbt                    # noqa: E402
from src.data_engine.qdii_calc import relchange_zscore   # noqa: E402
from qdii_relchange_realistic import spike_avoid_hold    # noqa: E402

COST = 0.0015
FUND_DIR = ROOT / "data" / "fundamental"
SPLIT = "2023-07-01"          # IS / OOS 切分点
TD = rp.TRADING_DAYS

A_LEGS = list(pc.A_STOCK_LEGS)
Q_LEGS = list(pc.QDII_LEGS)
MMF = "511880.SH"

# 现役基线参数（对照行）
BASE = dict(lb=60, vf=0.025, method="inverse_vol",
            pb_on=True, pb_rule="triple", pb_win=60, pb_lo=0.3, pb_hi=0.7,
            pb_src="510300.SH", pb_legs=tuple(A_LEGS),
            qdii_on=True, z_win=60, z_hi=2.0, q_floor=1.0, min_hold=5,
            cash_mmf=False, vol_target=None)


# --------------------------------------------------------------------------- #
# 数据 / 组件
# --------------------------------------------------------------------------- #
def load_panel() -> pd.DataFrame:
    closes = {c: pd.read_csv(ROOT / "data" / f"{c}.csv",
                             parse_dates=["date"]).set_index("date")["close"]
              for c in rp.HETERO_CODES}
    return pd.DataFrame(closes).sort_index().dropna()


def inv_base(panel: pd.DataFrame, lookback: int, floor_ann: float,
             method: str = "inverse_vol") -> pd.DataFrame:
    """逆波动底仓（临时改写模块常量，用完还原）。

    注意：BASE 行复现的是 L6 落地前的旧口径（p=1 / 月频 / 地板 2.5%），
    因此 VOL_P / REBAL_FREQ 一并钉回旧值——risk_parity 模块默认值已是 L6
    （p=1.2 / 季频 / 地板 6%），不钉住会静默漂移。
    """
    old = (rp.VOL_LOOKBACK, rp.VOL_FLOOR_ANN, rp.VOL_P, rp.REBAL_FREQ)
    rp.VOL_LOOKBACK, rp.VOL_FLOOR_ANN = lookback, floor_ann
    rp.VOL_P, rp.REBAL_FREQ = 1.0, "M"          # 钉住旧口径（L6 前）
    try:
        w = rp.build_weights(panel, method)
    finally:
        rp.VOL_LOOKBACK, rp.VOL_FLOOR_ANN, rp.VOL_P, rp.REBAL_FREQ = old
    return w.shift(1).fillna(0.0)


def pb_gate_daily(index: pd.DatetimeIndex, window: int = 60, rule: str = "triple",
                  lo: float = 0.3, hi: float = 0.7,
                  source: str = "510300.SH") -> pd.Series:
    """PB 滚动分位门控（月末算 → shift 1 月 → 日频 ffill）。"""
    m = pd.read_csv(FUND_DIR / f"legu_metrics_{source}.csv",
                    parse_dates=["date"]).set_index("date").sort_index()
    minp = min(window, 36)
    pct = m["pb"].rolling(window, min_periods=minp).apply(
        lambda x: float((x <= x.iloc[-1]).mean()), raw=False)
    if rule == "triple":
        gate = pd.Series(np.where(pct < lo, 1.0, np.where(pct < hi, 0.5, 0.0)),
                         index=pct.index)
    elif rule == "binary":
        gate = (pct < 0.5).astype(float)
    elif rule == "linear":
        gate = (1.5 - 1.5 * pct).clip(0.0, 1.0)
    elif rule == "smooth":
        gate = (1.0 - pct).clip(0.0, 1.0)
    else:
        raise ValueError(rule)
    return gate.shift(1).reindex(index, method="ffill").fillna(1.0).clip(0.0, 1.0)


_GATE_CACHE: dict = {}


def qdii_gate(code: str, index: pd.DatetimeIndex, window: int, z_hi: float,
              floor: float, min_hold: int) -> pd.Series:
    key = (code, window, z_hi, floor, min_hold)
    if key not in _GATE_CACHE:
        df = qbt.load_premium_history(code.split(".")[0])
        if df is None or df.empty:
            _GATE_CACHE[key] = pd.Series(1.0, index=index)
        else:
            z = relchange_zscore(df["premium"], window)
            h = spike_avoid_hold(z, df["premium"], z_hi=z_hi,
                                 floor=floor, min_hold=min_hold)
            _GATE_CACHE[key] = h.reindex(index).ffill().fillna(1.0).clip(0.0, 1.0)
    return _GATE_CACHE[key]


def build_W(panel: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    base = inv_base(panel, cfg.get("lb", 60), cfg.get("vf", 0.025),
                    cfg.get("method", "inverse_vol"))
    W = base.copy()
    if cfg.get("pb_on", True):
        g = pb_gate_daily(panel.index, window=cfg.get("pb_win", 60),
                          rule=cfg.get("pb_rule", "triple"),
                          lo=cfg.get("pb_lo", 0.3), hi=cfg.get("pb_hi", 0.7),
                          source=cfg.get("pb_src", "510300.SH"))
        for c in cfg.get("pb_legs", A_LEGS):
            if c in W.columns:
                W[c] = W[c] * g
    if cfg.get("qdii_on", True):
        half = cfg.get("qdii_half", False)
        for c in Q_LEGS:
            g = qdii_gate(c, panel.index, cfg.get("z_win", 60), cfg.get("z_hi", 2.0),
                          cfg.get("q_floor", 1.0), cfg.get("min_hold", 5))
            if half:                      # 减仓时留半仓，而非全空
                g = 0.5 + 0.5 * g
            W[c] = W[c] * g
    if cfg.get("cash_mmf", False):
        freed = (base.sum(axis=1) - W.sum(axis=1)).clip(lower=0.0)
        W[MMF] = W[MMF] + freed
    if cfg.get("vol_target"):
        rets = panel.pct_change(fill_method=None)
        realized = (W * rets).sum(axis=1)
        vol = realized.rolling(60, min_periods=30).std() * np.sqrt(TD)
        lev = (cfg["vol_target"] / vol).clip(upper=cfg.get("max_lev", 2.5))
        lev = lev.shift(1).fillna(1.0)
        W = W.mul(lev.replace([np.inf, -np.inf], 1.0), axis=0)
    return W


# --------------------------------------------------------------------------- #
# 评估
# --------------------------------------------------------------------------- #
def evaluate(panel: pd.DataFrame, W: pd.DataFrame, cost: float = COST) -> dict:
    net, _ = pc.backtest_net(panel, W, cost)
    years = len(net) / TD
    full = rp.metrics(net)
    ism = rp.metrics(net.loc[:SPLIT])
    oos = rp.metrics(net.loc[SPLIT:])
    to_ann = float(W.diff().abs().sum(axis=1).fillna(0).sum() / years)
    return {
        "ann_ret": full.get("ann_ret", float("nan")),
        "ann_vol": full.get("ann_vol", float("nan")),
        "sharpe": full.get("sharpe", float("nan")),
        "max_dd": full.get("max_dd", float("nan")),
        "calmar": full.get("calmar", float("nan")),
        "turnover": to_ann,
        "expo": float(W.sum(axis=1).mean()),
        "is_sharpe": ism.get("sharpe", float("nan")),
        "is_ret": ism.get("ann_ret", float("nan")),
        "oos_sharpe": oos.get("sharpe", float("nan")),
        "oos_ret": oos.get("ann_ret", float("nan")),
        "oos_dd": oos.get("max_dd", float("nan")),
        "net": net,
    }


def row(label: str, m: dict) -> dict:
    return {"label": label, **{k: v for k, v in m.items() if k != "net"}}


def fmt_table(rows: list[dict], keys: list[str]) -> list[str]:
    head = {"label": "变体", "ann_ret": "年化", "ann_vol": "波动", "sharpe": "夏普",
            "max_dd": "回撤", "calmar": "Calmar", "turnover": "换手/年",
            "expo": "暴露", "is_sharpe": "IS夏普", "oos_sharpe": "OOS夏普",
            "oos_ret": "OOS年化", "oos_dd": "OOS回撤"}
    L = ["| " + " | ".join(head[k] for k in keys) + " |",
         "|" + "---|" * len(keys)]
    for r in rows:
        cells = []
        for k in keys:
            v = r.get(k)
            if k == "label":
                cells.append(str(v))
            elif k in ("turnover",):
                cells.append(f"{v:.2f}")
            elif isinstance(v, float) and not np.isnan(v):
                cells.append(f"{v*100:+.2f}%" if k != "sharpe" and k != "is_sharpe"
                             and k != "oos_sharpe" and k != "calmar" else f"{v:.2f}")
            else:
                cells.append("—")
        L.append("| " + " | ".join(cells) + " |")
    return L


KEYS = ["label", "ann_ret", "sharpe", "max_dd", "calmar", "turnover",
        "is_sharpe", "oos_sharpe", "oos_ret"]


def main() -> int:
    panel = load_panel()
    years = len(panel) / TD
    print(f"[tune] 面板 {panel.shape[1]} 只, "
          f"{panel.index.min().date()} ~ {panel.index.max().date()} "
          f"({years:.2f} 年), IS/OOS 切分 {SPLIT}")

    sections: list[tuple[str, list[dict]]] = []

    # ---------- 0. 基线 ----------
    m_base = evaluate(panel, build_W(panel, BASE))
    print(f"[base] 夏普 {m_base['sharpe']:.2f} 年化 {m_base['ann_ret']*100:+.2f}% "
          f"回撤 {m_base['max_dd']*100:.1f}% 换手 {m_base['turnover']:.2f} "
          f"| IS {m_base['is_sharpe']:.2f} OOS {m_base['oos_sharpe']:.2f}")
    base_rows = [row("**BASE 现役口径**", m_base)]

    # ---------- 1. 底仓层：波动窗口 × 地板 × 加权法 ----------
    rows = list(base_rows)
    for lb in [20, 40, 60, 90, 120, 250]:
        cfg = dict(BASE, lb=lb)
        rows.append(row(f"VOL_LOOKBACK={lb}", evaluate(panel, build_W(panel, cfg))))
    for vf in [0.01, 0.02, 0.025, 0.04, 0.06, 0.08, 0.12]:
        cfg = dict(BASE, vf=vf)
        rows.append(row(f"VOL_FLOOR_ANN={vf*100:.1f}%",
                        evaluate(panel, build_W(panel, cfg))))
    for meth in ["inverse_vol", "inverse_var", "erc"]:
        cfg = dict(BASE, method=meth)
        rows.append(row(f"method={meth}", evaluate(panel, build_W(panel, cfg))))
    sections.append(("1. 底仓层（逆波动）", rows))
    print("[tune] 1/5 底仓层 done")

    # ---------- 2. PB 门控层 ----------
    rows = list(base_rows)
    for rule in ["triple", "binary", "linear", "smooth"]:
        cfg = dict(BASE, pb_rule=rule)
        rows.append(row(f"PB rule={rule}", evaluate(panel, build_W(panel, cfg))))
    for w in [36, 48, 60, 84, 120]:
        cfg = dict(BASE, pb_win=w)
        rows.append(row(f"PB window={w}M", evaluate(panel, build_W(panel, cfg))))
    for lo, hi in [(0.2, 0.5), (0.2, 0.6), (0.3, 0.6), (0.3, 0.7), (0.4, 0.8)]:
        cfg = dict(BASE, pb_lo=lo, pb_hi=hi)
        rows.append(row(f"PB 档位 <{lo:.0%}/{hi:.0%}",
                        evaluate(panel, build_W(panel, cfg))))
    scopes = {
        "全A股7腿(现役)": tuple(A_LEGS),
        "仅510300": ("510300.SH",),
        "510300+510500": ("510300.SH", "510500.SH"),
        "剔除创业板/军工": tuple(c for c in A_LEGS
                            if c not in ("159915.SZ", "512660.SH")),
        "PB门控全关": tuple(),
    }
    for name, legs in scopes.items():
        cfg = dict(BASE, pb_legs=legs, pb_on=bool(legs))
        rows.append(row(f"PB 范围={name}", evaluate(panel, build_W(panel, cfg))))
    sections.append(("2. PB 估值门控层", rows))
    print("[tune] 2/5 PB 层 done")

    # ---------- 3. QDII 门控层 ----------
    rows = list(base_rows)
    for z in [1.5, 2.0, 2.5, 3.0]:
        cfg = dict(BASE, z_hi=z)
        rows.append(row(f"z_hi={z}", evaluate(panel, build_W(panel, cfg))))
    for f in [0.0, 0.5, 1.0, 2.0, 3.0]:
        cfg = dict(BASE, q_floor=f)
        rows.append(row(f"floor={f}%", evaluate(panel, build_W(panel, cfg))))
    for mh in [1, 3, 5, 10, 20]:
        cfg = dict(BASE, min_hold=mh)
        rows.append(row(f"min_hold={mh}", evaluate(panel, build_W(panel, cfg))))
    for w in [40, 60, 90, 120]:
        cfg = dict(BASE, z_win=w)
        rows.append(row(f"z window={w}", evaluate(panel, build_W(panel, cfg))))
    cfg = dict(BASE, qdii_on=False)
    rows.append(row("QDII 门控全关", evaluate(panel, build_W(panel, cfg))))
    # 分级减仓：减仓时留半仓而非全空（0/1 → 0.5/1）
    cfg = dict(BASE, qdii_half=True)
    rows.append(row("QDII 减仓=半仓(0.5)", evaluate(panel, build_W(panel, cfg))))
    cfg = dict(BASE, qdii_half=True, cash_mmf=True)
    rows.append(row("半仓+现金→货币腿", evaluate(panel, build_W(panel, cfg))))
    sections.append(("3. QDII 溢价门控层", rows))
    print("[tune] 3/5 QDII 层 done")

    # ---------- 4. 结构性改动 ----------
    rows = list(base_rows)
    for flag in [False, True]:
        cfg = dict(BASE, cash_mmf=flag)
        rows.append(row(f"空仓资金→{'货币腿511880' if flag else '现金(现役)'}",
                        evaluate(panel, build_W(panel, cfg))))
    for vt in [0.04, 0.05, 0.06, 0.08, 0.10]:
        cfg = dict(BASE, vol_target=vt)
        rows.append(row(f"目标波动={vt*100:.0f}%",
                        evaluate(panel, build_W(panel, cfg))))
    sections.append(("4. 结构性改动", rows))
    print("[tune] 4/5 结构层 done")

    # ---------- 5. 组合候选（IS 选优 → OOS 验证） ----------
    cands = {
        "BASE 现役": dict(BASE),
        "A 现金→货币腿": dict(BASE, cash_mmf=True),
        "B 底仓 lb=40": dict(BASE, lb=40, cash_mmf=True),
        "C PB smooth": dict(BASE, pb_rule="smooth", cash_mmf=True),
        "D QDII z=1.5": dict(BASE, z_hi=1.5, cash_mmf=True),
        "E 目标波动6%": dict(BASE, vol_target=0.06, cash_mmf=True),
        "F 叠加(现金+lb40+smooth)": dict(BASE, lb=40, pb_rule="smooth", cash_mmf=True),
        "G 叠加F+目标波动6%": dict(BASE, lb=40, pb_rule="smooth",
                                vol_target=0.06, cash_mmf=True),
        "H 激进(目标波动8%)": dict(BASE, lb=40, pb_rule="smooth",
                               vol_target=0.08, cash_mmf=True),
    }
    rows = []
    nets = {}
    for name, cfg in cands.items():
        m = evaluate(panel, build_W(panel, cfg))
        nets[name] = m["net"]
        rows.append(row(name, m))
        print(f"[cand] {name:24s} 夏普 {m['sharpe']:.2f} "
              f"年化 {m['ann_ret']*100:+.2f}% 回撤 {m['max_dd']*100:.1f}% "
              f"| IS {m['is_sharpe']:.2f} OOS {m['oos_sharpe']:.2f}")
    sections.append(("5. 组合候选（IS 选优 → OOS 验证）", rows))
    print("[tune] 5/5 候选 done")

    # ---------- 报告 ----------
    L = ["# 三层组合参数扫描与稳健性检验（实验分支）", "",
         f"- 样本：{panel.index.min().date()} ~ {panel.index.max().date()}"
         f"（{len(panel)} 交易日 / {years:.2f} 年），18 只异构池，单边成本 0.15%",
         f"- 稳健性切分：**IS（样本内）= ~{SPLIT}**，**OOS（样本外）= {SPLIT} ~**。"
         "看参数不能只看全样本夏普，IS 好而 OOS 崩 = 过拟合。",
         "- 每组第一行为现役基线（BASE），其余为单因子变体（只改一个旋钮）。", ""]
    for title, rws in sections:
        L += [f"## {title}", ""] + fmt_table(rws, KEYS) + [""]

    # 分年收益（候选）
    L += ["## 6. 候选方案分年净收益", ""]
    names = list(nets.keys())
    ymat = pd.DataFrame({k: v.groupby(v.index.year).apply(lambda x: (1 + x).prod() - 1)
                         for k, v in nets.items()})
    L += ["| 年份 | " + " | ".join(names) + " |",
          "| --- | " + " | ".join(["---"] * len(names)) + " |"]
    for y in ymat.index:
        L.append(f"| {y} | " + " | ".join(
            (f"{ymat.loc[y, k]*100:+.1f}%" if pd.notna(ymat.loc[y, k]) else "—")
            for k in names) + " |")

    out = ROOT / "runs" / "tune_three_layer.md"
    out.write_text("\n".join(L), encoding="utf-8")
    (ROOT / "runs" / "tune_three_layer.json").write_text(
        json.dumps({t: rws for t, rws in sections}, ensure_ascii=False,
                   indent=2, default=str), encoding="utf-8")
    print(f"\n[tune] 报告已写出: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
