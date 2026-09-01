#!/usr/bin/env python3
"""三层组合实盘信号：qdii_daily.py 第 3 步（§9 待办第 3 项「组合实盘化」）。

§7.20 联合回测证明三层组合（逆波动底仓 × PB 估值门控 × QDII 溢价门控）
夏普 1.28、回撤 −5.4%。本脚本把回测口径逐日复现为「明日目标持仓」，由
每日定时任务收盘后调用，输出三层门控状态 + 明日目标权重 + 动作清单
（与上一快照的权重差，含归因）-> runs/portfolio_live.md / .json

口径（与 §7.20 回测逐字一致，不得偏离）：
  * 逆波动底仓：月末用此前 60 日波动率定权、次月持有（rp.build_weights）。
    「明日权重」= build_weights(...).iloc[-1]——今日恰为本月最后交易日则为
    新算权重（明日执行再平衡），否则为上月末权重的延续，与回测
    月末赋值→ffill→shift(1) 语义等价。
  * PB 门控：沪深300 PB 5 年滚动分位三档（<30% 全仓/30~70% 半仓/≥70% 空仓），
    月末算好 shift(1) 应用——回测原样口径（t+1 月使用 t-1 月末分位，比直觉
    多滞后一个月，但回测成绩 1.28 夏普就是这个口径跑出来的，实盘不擅自"修正"）。
  * QDII 门控：溢价一阶差分 60 日 z 状态机（floor=1%、min_hold=5、z=+2）。
    「明日持仓」= 溢价序列末尾追加一行 NaN 再跑状态机取末值——状态机处理完
    最后一行真实数据后的 state 即 T+1 持仓，与回测 T→T+1 语义一致。

数据刷新（免费通道，失败降级用旧数据并在报告标注）：
  * 18 只 ETF 收盘价：落后于今日时经 vibe broker（tencent 前复权链）增量
    拉取（start=最后日期−20 天，重叠区新数据优先——qfq 以最新价为基准，
    新拉的重叠段包含最新分红复权，更准）。
  * 乐咕 PB：缓存未覆盖本月时重拉（月频、轻量、schema 同
    value_timing_backtest.load_metrics，缓存互通）。
  * QDII 溢价：复用 qdii_daily 第 2 步（qdii_backtest --refresh）刚刷新的
    缓存，故本脚本必须排在第 2 步之后。净值 T+1~T+2 公布，溢价最新日期
    可能滞后 1~2 个交易日——与回测同款信息滞后。

诚实边界：输出是「信号级」目标权重，未接券商账户；动作清单按目标权重差
计算（与回测换手口径一致），实际执行须自行对照账户真实持仓与现金。

用法：python3 scripts/portfolio_live.py [--no-refresh]
  --no-refresh  跳过数据拉取，仅用本地缓存算信号（调试用）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent))       # scripts/

import risk_parity as rp
import portfolio_combined as pc
import qdii_backtest as qbt
from src.data_engine.qdii_calc import relchange_zscore, RELCHANGE_WINDOW
from qdii_relchange_realistic import spike_avoid_hold

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
FUND_DIR = DATA_DIR / "fundamental"
RUNS = ROOT / "runs"
LIVE_JSON = RUNS / "portfolio_live.json"
LIVE_MD = RUNS / "portfolio_live.md"

NAMES = {
    "510300.SH": "沪深300", "510500.SH": "中证500", "159915.SZ": "创业板指",
    "512010.SH": "医药", "159928.SZ": "消费", "512880.SH": "证券",
    "512660.SH": "军工", "511010.SH": "国债ETF", "511880.SH": "银华日利",
    "518880.SH": "黄金ETF", "159985.SZ": "豆粕ETF", "159981.SZ": "能源化工",
    "513100.SH": "纳指100", "513500.SH": "标普500", "513050.SH": "中概互联",
    "513880.SH": "日经225", "513030.SH": "德国30", "159920.SZ": "恒生ETF",
}

MIN_ACTION = 0.005     # 权重变动 ≥0.5pp 才进动作清单
STALE_DAYS = 3         # panel 末尾距今天超过 3 个自然日 → 数据滞后告警
PREM_STALE_DAYS = 5    # 溢价缓存末日期滞后 panel 超过 5 个自然日 → 门控数据滞后告警
                       # （净值 T+1~T+2 公布，正常滞后 ≤3 天，超过即刷新链路异常）


# --------------------------------------------------------------------------- #
# 数据刷新（全部降级容忍）
# --------------------------------------------------------------------------- #
def refresh_etf_closes(codes: list[str]) -> dict[str, str]:
    """增量刷新 ETF 收盘价（vibe broker，tencent 前复权链，cwd=$HOME 子进程）。

    只拉「最后日期 < 今日」的代码；窗口 = min(最后日期)−20 天 ~ 今日，
    与旧缓存重叠区以新数据为准。任一步失败只告警，保留旧数据。
    返回 {code: 最新日期}。
    """
    today = pd.Timestamp(datetime.now().date())
    lasts: dict[str, str] = {}
    for c in codes:
        p = DATA_DIR / f"{c}.csv"
        if not p.exists():
            print(f"[live] ⚠️ 缺 {c}.csv")
            continue
        df = pd.read_csv(p, usecols=["date"], parse_dates=["date"])
        lasts[c] = str(df["date"].max().date())

    stale = [c for c in codes if c in lasts and pd.Timestamp(lasts[c]) < today]
    if not stale:
        return lasts

    start = (min(pd.Timestamp(lasts[c]) for c in stale)
             - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    tmp = DATA_DIR / "_live_tmp"
    print(f"[live] 刷新 {len(stale)} 只 ETF 收盘价（{start} ~ {end}）...")
    cmd = [sys.executable, "-X", "utf8",
           str(ROOT / "scripts" / "vibe_fetch_broker.py"),
           "--codes", *stale, "--start", start, "--end", end,
           "--out-dir", str(tmp)]
    try:
        cp = subprocess.run(cmd, cwd=str(Path.home()), capture_output=True,
                            text=True, timeout=600)
        if cp.returncode != 0:
            print(f"[live] ⚠️ broker 退出码 {cp.returncode}（部分失败可容忍）")
        for c in stale:
            new_p, old_p = tmp / f"{c}.csv", DATA_DIR / f"{c}.csv"
            if not new_p.exists():
                print(f"[live] ⚠️ {c} 增量拉取失败，用旧数据（截至 {lasts[c]}）")
                continue
            new = pd.read_csv(new_p, parse_dates=["date"])
            old = pd.read_csv(old_p, parse_dates=["date"])
            merged = (pd.concat([new, old])
                      .drop_duplicates(subset="date", keep="first")
                      .sort_values("date"))
            # sanity check：行数/末日期不倒退才允许覆盖，且原子写防中断损坏
            if len(merged) >= len(old) and merged["date"].max() >= old["date"].max():
                qbt.atomic_to_csv(merged, old_p, index=False)
                lasts[c] = str(merged["date"].max().date())
            else:
                print(f"[live] ⚠️ {c} 合并结果异常（行数/日期倒退），保留旧数据")
    except Exception as e:  # noqa: BLE001
        print(f"[live] ⚠️ 收盘价刷新异常 {type(e).__name__}: {e}，用旧数据继续")
    return lasts


def refresh_legu() -> str:
    """乐咕沪深300 月频估值缓存：未覆盖本月时重拉（原子覆盖写，schema 与
    value_timing_backtest.load_metrics 一致）。失败降级用缓存。返回最新日期。

    缓存缺失/损坏（FileNotFoundError、空文件、解析失败）不崩溃，按全量重拉处理；
    重拉也失败且无缓存可用时返回 "—"，由调用方决定 PB 门控降级。
    """
    cache = FUND_DIR / f"legu_metrics_{pc.PB_GATE_SOURCE}.csv"
    m = pd.DataFrame()
    try:
        m = pd.read_csv(cache, parse_dates=["date"]).set_index("date").sort_index()
    except Exception as e:  # noqa: BLE001
        print(f"[live] ⚠️ 乐咕缓存读取失败 {type(e).__name__}: {e}，按全量重拉处理")
    today = pd.Timestamp(datetime.now().date())
    if not m.empty and m.index.max() >= pd.Timestamp(today.year, today.month, 1):
        return str(m.index.max().date())
    last_cached = str(m.index.max().date()) if not m.empty else "无缓存"
    print(f"[live] 乐咕 PB 缓存截至 {last_cached}，重拉...")
    try:
        import akshare as ak
        pe = ak.stock_index_pe_lg(symbol="沪深300").rename(columns={
            "日期": "date", "指数": "close",
            "滚动市盈率": "pe_ttm", "静态市盈率": "pe_lyr",
            "滚动市盈率中位数": "pe_med"})
        pb = ak.stock_index_pb_lg(symbol="沪深300").rename(columns={
            "日期": "date", "市净率": "pb", "市净率中位数": "pb_med"})
        pe = pe[[c for c in ["date", "close", "pe_ttm", "pe_lyr", "pe_med"]
                 if c in pe.columns]]
        pb = pb[[c for c in ["date", "pb", "pb_med"] if c in pb.columns]]
        df = pe.merge(pb, on="date", how="outer")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
        for c in ["close", "pe_ttm", "pe_lyr", "pe_med", "pb", "pb_med"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["close"]).sort_index()
        if not df.empty and len(df) > len(m):
            qbt.atomic_to_csv(df, cache)
            print(f"[live] 乐咕缓存更新至 {df.index.max().date()}")
            return str(df.index.max().date())
        print("[live] 乐咕暂无新数据，沿用缓存")
    except Exception as e:  # noqa: BLE001
        print(f"[live] ⚠️ 乐咕重拉失败 {type(e).__name__}: {e}，用缓存")
    return last_cached


# --------------------------------------------------------------------------- #
# 三层信号
# --------------------------------------------------------------------------- #
def qdii_gate_next(code: str) -> dict:
    """明日 QDII 门控 + 诊断信息。

    末尾追加一行 NaN 再跑 spike_avoid_hold：状态机处理完最后一行真实数据后
    的 state 即下一交易日持仓（T 日信息决定 T+1，与回测一致）。
    """
    df = qbt.load_premium_history(code.split(".")[0])
    if df is None or df.empty:
        return {"today": 1.0, "tomorrow": 1.0, "premium_pct": None, "z": None,
                "days_in_state": None, "prem_last": None}
    z = relchange_zscore(df["premium"], RELCHANGE_WINDOW)
    nxt = df.index[-1] + pd.Timedelta(days=1)
    z2 = pd.concat([z, pd.Series([float("nan")], index=[nxt])])
    p2 = pd.concat([df["premium"], pd.Series([float("nan")], index=[nxt])])
    h = spike_avoid_hold(z2, p2)
    today_g, tomorrow_g = float(h.iloc[-2]), float(h.iloc[-1])

    # 当前状态已持续天数（仅按真实数据行计）
    days = 0
    for v in h.iloc[:-1].values[::-1]:
        if v == today_g:
            days += 1
        else:
            break
    z_last = z.iloc[-1]
    return {
        "today": today_g,
        "tomorrow": tomorrow_g,
        "premium_pct": round(float(df["premium"].iloc[-1]) * 100, 2),
        "z": round(float(z_last), 2) if pd.notna(z_last) else None,
        "days_in_state": int(days),
        "prem_last": str(df.index[-1].date()),
    }


def _fmt_gate(v: float | None) -> str:
    if v is None:
        return "—"
    return {1.0: "持有", 0.5: "半仓", 0.0: "空仓"}.get(round(float(v), 2), f"{v:.2f}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(refresh: bool = True) -> int:
    codes = rp.HETERO_CODES

    # ---- 0. 数据刷新 ----
    if refresh:
        etf_last = refresh_etf_closes(codes)
        legu_last = refresh_legu()
    else:
        etf_last = {}
        legu_last = "（未刷新）"

    closes = {}
    for c in codes:
        p = DATA_DIR / f"{c}.csv"
        if p.exists():
            closes[c] = pd.read_csv(p, parse_dates=["date"]).set_index("date")["close"]
        else:
            print(f"[live] ⚠️ 缺 {c}.csv，该腿权重记 0")
            closes[c] = pd.Series(dtype=float)
    panel = pd.DataFrame(closes).sort_index().dropna()
    if panel.shape[0] < 120 or panel.shape[1] < 10:
        print(f"[live] ✅ 共同样本不足（{panel.shape}），退出")
        return 1
    as_of = panel.index[-1]
    today = pd.Timestamp(datetime.now().date())
    data_stale = (today - as_of).days > STALE_DAYS
    etf_max = max(etf_last.values()) if etf_last else str(as_of.date())

    # ---- 1. 逆波动底仓（明日权重 = 末行；月末则为新算权重）----
    w_iv = rp.build_weights(panel, "inverse_vol").iloc[-1]

    # ---- 2. PB 门控（shift(1) 语义 = 回测原样口径）----
    # 乐咕缓存不可用时降级：档位取全仓并在报告显著标注（宁可告警，不可崩溃）
    pb_ok = True
    try:
        g = pc.pb_gate_monthly("triple")
        pb_next = float(g.shift(1).dropna().iloc[-1])
        m = pd.read_csv(FUND_DIR / f"legu_metrics_{pc.PB_GATE_SOURCE}.csv",
                        parse_dates=["date"]).set_index("date").sort_index()
        pct = pc.rolling_pct(m["pb"])
        pct_now = float(pct.dropna().iloc[-1])
        pct_month = str(m.index.max().date())
    except Exception as e:  # noqa: BLE001
        print(f"[live] ⚠️ PB 门控数据不可用 {type(e).__name__}: {e}，档位降级为全仓")
        pb_ok = False
        pb_next, pct_now, pct_month = 1.0, None, "—"
    legu_last = pct_month if (refresh or legu_last == "（未刷新）") and pb_ok else legu_last

    # ---- 月末判定（今日为本月最后交易日 → 明日执行月度再平衡）----
    month_ends = panel.index.to_series().groupby(panel.index.to_period("M")).last()
    is_month_end = bool(month_ends.iloc[-1] == as_of) and not data_stale

    # ---- 3. QDII 门控（明日）----
    qdii = {c: qdii_gate_next(c) for c in pc.QDII_LEGS}
    prem_max = max((v["prem_last"] or "" for v in qdii.values()), default="")
    # 溢价缓存滞后检查：净值 T+1~T+2 公布属正常（≤3 天），超过阈值说明
    # 第 2 步刷新链路异常（如数据源断更/接口变更），门控信号可信度下降
    prem_stale = {
        c: v["prem_last"] for c, v in qdii.items()
        if v["prem_last"] and (as_of - pd.Timestamp(v["prem_last"])).days > PREM_STALE_DAYS
    }

    # ---- 明日目标权重 ----
    target: dict[str, float] = {}
    for c in panel.columns:
        w = float(w_iv.get(c, 0.0))
        if c in pc.A_STOCK_LEGS:
            w *= pb_next
        if c in pc.QDII_LEGS:
            w *= qdii[c]["tomorrow"]
        target[c] = w
    cash = max(0.0, 1.0 - sum(target.values()))

    # ---- 动作清单（vs 上一快照的目标权重）----
    prev = None
    if LIVE_JSON.exists():
        try:
            prev = json.loads(LIVE_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prev = None

    actions = []
    if prev and "target_weights" in prev:
        prev_pb = prev.get("pb", {}).get("level")
        for c in panel.columns:
            w_old = float(prev["target_weights"].get(c, 0.0))
            delta = target[c] - w_old
            if abs(delta) < MIN_ACTION:
                continue
            if c in pc.QDII_LEGS and qdii[c]["today"] != qdii[c]["tomorrow"]:
                why = "QDII 门控减仓" if delta < 0 else "QDII 门控回补"
            elif c in pc.A_STOCK_LEGS and prev_pb is not None and prev_pb != pb_next:
                why = f"PB 门控调档 {_fmt_gate(prev_pb)}→{_fmt_gate(pb_next)}"
            elif is_month_end:
                why = "月度再平衡"
            else:
                why = "波动率漂移修正"
            actions.append({"code": c, "name": NAMES.get(c, c),
                            "from": round(w_old, 4), "to": round(target[c], 4),
                            "delta": round(delta, 4), "reason": why})
    actions.sort(key=lambda a: abs(a["delta"]), reverse=True)

    # ---- 快照 JSON ----
    snap = {
        "as_of": str(as_of.date()),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_freshness": {
            "etf_close_last": etf_max,
            "legu_pb_last": legu_last,
            "qdii_premium_last": {c: v["prem_last"] for c, v in qdii.items()},
            "panel_stale": bool(data_stale),
            "qdii_premium_stale": prem_stale,
        },
        "is_month_end": is_month_end,
        "pb": {"percentile": round(pct_now, 4) if pct_now is not None else None,
               "level": pb_next, "pct_month": pct_month, "degraded": not pb_ok},
        "invvol_weights": {c: round(float(w_iv.get(c, 0.0)), 4) for c in panel.columns},
        "qdii_gates": qdii,
        "target_weights": {c: round(w, 4) for c, w in target.items()},
        "cash": round(cash, 4),
        "actions": actions,
        "prev_as_of": (prev or {}).get("as_of"),
        "params": {"pb_rule": "triple", "pb_window_months": pc.PB_WINDOW,
                   "relchange_window": RELCHANGE_WINDOW, "floor_pct": 1.0,
                   "min_hold_days": 5, "z_hi": 2.0, "vol_lookback": rp.VOL_LOOKBACK},
    }
    RUNS.mkdir(exist_ok=True)
    # 原子写：先写临时文件再替换，避免中断留下半个 JSON 污染次日对比基准
    tmp_json = LIVE_JSON.with_suffix(".json.tmp")
    tmp_json.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    tmp_json.replace(LIVE_JSON)

    # ---- Markdown 报告 ----
    L = [f"# 三层组合实盘信号（{as_of.date()} 收盘后）\n",
         f"> 数据截至：ETF 收盘 {etf_max} · 沪深300 PB 月频 {legu_last} · "
         f"QDII 溢价 {prem_max}（净值 T+1~T+2 公布滞后，同回测口径）。",
         f"> {'**明日为月度再平衡执行日**（今日为 ' + str(as_of.strftime('%m')) + ' 月最后交易日）'
             if is_month_end else '非再平衡日，逆波动底仓沿用上月末权重'}；"
         f"PB 门控作用于 {len(pc.A_STOCK_LEGS)} 只 A 股腿、QDII 门控作用于 "
         f"{len(pc.QDII_LEGS)} 只 QDII 腿（乘法门控，减出部分为现金）。",
         "> 目标权重是「信号级」目标，实际执行请对照账户真实持仓；"
         f"动作阈值 ±{MIN_ACTION*100:.1f}pp。\n"]

    if data_stale:
        L.append(f"> ⚠️ **ETF 数据滞后**：panel 末尾为 {as_of.date()}（距今 "
                 f"{(today - as_of).days} 天），月末判定与再平衡宣告可能失真。\n")
    if prem_stale:
        legs = "、".join(f"{NAMES.get(c, c)}(截至 {d})" for c, d in prem_stale.items())
        L.append(f"> ⚠️ **QDII 溢价缓存滞后超 {PREM_STALE_DAYS} 天**：{legs}——"
                 "第 2 步刷新链路可能异常，相关门控信号可信度下降，请检查日志。\n")
    if not pb_ok:
        L.append("> ⚠️ **PB 门控数据不可用**：乐咕缓存读取失败，本次 A 股腿档位"
                 "降级为全仓（1.0），请尽快修复数据源。\n")

    L.append("## 1. 三层门控状态\n")
    pct_str = f"{pct_now:.0%}" if pct_now is not None else "—（数据不可用）"
    L.append(f"- **PB 门控**（沪深300 PB 5 年滚动分位，{pct_month}）：当前分位 "
             f"**{pct_str}** → 明日档位 **{_fmt_gate(pb_next)}**"
             f"（<30% 全仓 / 30~70% 半仓 / ≥70% 空仓）")
    qd_flips = [c for c in pc.QDII_LEGS
                if qdii[c]["today"] != qdii[c]["tomorrow"]]
    L.append(f"- **QDII 门控**（溢价一阶差分 60 日 z 飙升回避，floor=1%、"
             f"min_hold=5）：明日{'**有翻转：' + '、'.join(qd_flips) + '**' if qd_flips else '无翻转'}\n")
    L.append("| 代码 | 名称 | 溢价% | z | 今日 | 明日 | 状态持续(日) | 数据截至 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for c in pc.QDII_LEGS:
        v = qdii[c]
        L.append(f"| {c} | {NAMES.get(c, c)} | {v['premium_pct'] if v['premium_pct'] is not None else '—'} "
                 f"| {v['z'] if v['z'] is not None else '—'} "
                 f"| {_fmt_gate(v['today'])} | {_fmt_gate(v['tomorrow'])} "
                 f"| {v['days_in_state']} | {v['prem_last'] or '—'} |")

    L.append("\n## 2. 明日目标持仓\n")
    L.append("| 代码 | 名称 | 层 | 逆波动 | PB | QDII | 目标权重 | 较上快照 |")
    L.append("|---|---|---|---|---|---|---|---|")
    prev_w = (prev or {}).get("target_weights", {})
    for c in sorted(panel.columns, key=lambda x: -target[x]):
        layer = ("A股" if c in pc.A_STOCK_LEGS
                 else "QDII" if c in pc.QDII_LEGS else "其他")
        pb_g = _fmt_gate(pb_next) if c in pc.A_STOCK_LEGS else "—"
        qd_g = _fmt_gate(qdii[c]["tomorrow"]) if c in pc.QDII_LEGS else "—"
        d = target[c] - float(prev_w.get(c, 0.0)) if prev_w else None
        dcol = f"{d*100:+.1f}pp" if (d is not None and abs(d) >= MIN_ACTION) else "—"
        if target[c] < 0.0005 and (d is None or abs(d) < 0.0005):
            continue
        L.append(f"| {c} | {NAMES.get(c, c)} | {layer} "
                 f"| {float(w_iv.get(c, 0.0))*100:.1f}% | {pb_g} | {qd_g} "
                 f"| **{target[c]*100:.1f}%** | {dcol} |")
    L.append(f"| — | **现金（门控减出）** | — | — | — | — | **{cash*100:.1f}%** | — |")

    L.append("\n## 3. 动作清单（较上一快照"
             + (f" {snap['prev_as_of']}" if snap["prev_as_of"] else "")
             + "）\n")
    if prev is None:
        L.append("首次运行，无对比基准——本表目标权重即建仓基准。")
    elif not actions:
        L.append(f"无 ≥{MIN_ACTION*100:.1f}pp 的权重变动，**明日无需交易**。")
    else:
        L.append("| 代码 | 名称 | 从 | 到 | 变动 | 归因 |")
        L.append("|---|---|---|---|---|---|")
        for a in actions:
            L.append(f"| {a['code']} | {a['name']} | {a['from']*100:.1f}% "
                     f"| {a['to']*100:.1f}% | {a['delta']*100:+.1f}pp | {a['reason']} |")

    L.append("\n## 4. 口径备忘\n")
    L.append("- 逆波动：月末前 60 日波动率倒数定权（`risk_parity.build_weights`），"
             "明日权重 = 面板末行。")
    L.append("- PB 门控：月末分位三档 shift(1) 应用（回测原样口径，t+1 月使用 "
             "t−1 月末分位）；数据源乐咕月频，月初更新上月末点。")
    L.append("- QDII 门控：z>+2 且溢价>1% 次日空仓，空仓 ≥5 日且 z≤+2 回补；"
             "溢价按最新可得净值计算，存在 1~2 日信息滞后。")
    L.append("- 动作归因优先级：QDII 门控 > PB 调档 > 月度再平衡 > 漂移修正。")
    LIVE_MD.write_text("\n".join(L), encoding="utf-8")

    # ---- 控制台摘要（qdii_daily 日志只回显尾部 12 行，摘要放最后）----
    print("=" * 64)
    print(f"三层组合实盘信号（ETF 截至 {etf_max}，PB {legu_last}，溢价 {prem_max}）")
    print(f"  再平衡：{'明日执行月度再平衡（今日为月末）' if is_month_end else '非再平衡日'}"
          + ("（⚠️ 数据滞后，判定可能失真）" if data_stale else ""))
    print(f"  PB 门控：沪深300 PB 分位 {pct_str} → A 股腿档位 {_fmt_gate(pb_next)}"
          + ("（⚠️ 降级全仓）" if not pb_ok else ""))
    if prem_stale:
        print(f"  ⚠️ QDII 溢价缓存滞后超 {PREM_STALE_DAYS} 天："
              + "、".join(prem_stale.keys()))
    if qd_flips:
        for c in qd_flips:
            v = qdii[c]
            print(f"  QDII 门控翻转：{NAMES.get(c, c)} {_fmt_gate(v['today'])}"
                  f"→{_fmt_gate(v['tomorrow'])}（溢价 {v['premium_pct']}%、z {v['z']}）")
    else:
        print("  QDII 门控：明日无翻转")
    print(f"  明日目标：现金 {cash:.1%}，动作 {len(actions)} 项"
          + (f"（{actions[0]['reason']} 等）" if actions else ""))
    print("  详见 runs/portfolio_live.md / .json")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="三层组合实盘信号（qdii_daily 第 3 步）")
    ap.add_argument("--no-refresh", action="store_true",
                    help="跳过数据拉取，仅用本地缓存算信号（调试用）")
    args = ap.parse_args()
    raise SystemExit(main(refresh=not args.no_refresh))
