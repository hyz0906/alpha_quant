#!/usr/bin/env python3
"""20 万模拟盘账本 + 每日对账（与 portfolio_live 目标权重对齐）。

模拟盘语义：
  * 账本 data/paper_ledger.json：初始资金、现金、持仓（份额）、交易流水。
    建仓/调仓都按 100 股整数手（ETF 场内交易规则）。
  * 估值价 = data/{code}.csv 最新收盘价（与信号同源，T 日收盘后即当日价）。
  * 佣金 0.15% 单边（与 §7 回测口径一致，ETF 免印花税），成交时从现金扣。
  * 净值历史 data/paper_nav.csv：每日 append（date, total, cash, market,
    day_ret, cum_ret），同日重复运行只保留最后一行。
  * 对账 runs/paper_trading.md：模拟盘实际权重 vs runs/portfolio_live.json
    目标权重（三层组合信号，qdii_daily 第 3 步产物），输出偏差与调仓建议。
    对账阈值：权重差 > 2pp 且金额差 > max(200 元, 总资产×0.5%)。

命令（均在项目根执行，WSL python3）：
  init --capital 200000 [--name 模拟盘]
      初始化账本；已有账本时拒绝（需 --force 重置，会清空历史）。
  set-holdings --file broker_holdings.csv [--date YYYY-MM-DD] [--note ...]
      用券商实际持仓一次性同步账本（替代 init+11×buy）：
      CSV 列 code,shares,cost（券商实际成本价），适用于建仓首日已有完整
      实际成交清单、跳过中间过程的场景。
  buy  --code 511010.SH --shares 1300 [--price P] [--date YYYY-MM-DD]
      按最新收盘价买入（价格/日期可用参数覆盖）；佣金 = 成交额×0.15%。
  sell --code 511010.SH --shares 300 [--price P] [--date YYYY-MM-DD]
      卖出（对称，允许部分卖出）。
  entry-guide --capital 200000 [--out runs/paper_entry_guide.md]
      建仓指导：读 portfolio_live 目标权重 × 资金 → 按最新收盘价折整数手，
      输出每只的份数/金额/占比与现金余量（不写账本）。
  reconcile
      估值 + 对账（读 portfolio_live.json，缺失则只出账本视图），
      记录当日净值并写 runs/paper_trading.md。接入 qdii_daily 第 4 步。
  status
      账本视图（不写报告文件）。

诚实边界：
  * 成交价用「最新可得收盘价」= 信号数据同源；若最新价日期早于执行日，
    报告会标注（QDII 净值 T+1~T+2 公布，正常滞后 1~2 日）。
  * 目标权重是「信号级」，模拟盘按信号执行，不代表券商真实可成交价。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent))       # scripts/

import qdii_backtest as qbt

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RUNS = ROOT / "runs"
LEDGER = DATA_DIR / "paper_ledger.json"
NAV_CSV = DATA_DIR / "paper_nav.csv"
REPORT_MD = RUNS / "paper_trading.md"
GUIDE_MD = RUNS / "paper_entry_guide.md"
LIVE_JSON = RUNS / "portfolio_live.json"

COST = 0.0015           # 单边佣金（与回测口径一致）
LOT = 100               # ETF 最小交易单位（份）
W_DIFF_TH = 0.02        # 对账权重差阈值（2pp）
MIN_AMT_TH = 200.0      # 对账最小金额差阈值（元）

NAMES = {
    "510300.SH": "沪深300", "510500.SH": "中证500", "159915.SZ": "创业板指",
    "512010.SH": "医药", "159928.SZ": "消费", "512880.SH": "证券",
    "512660.SH": "军工", "511010.SH": "国债ETF", "511880.SH": "银华日利",
    "518880.SH": "黄金ETF", "159985.SZ": "豆粕ETF", "159981.SZ": "能源化工",
    "513100.SH": "纳指100", "513500.SH": "标普500", "513050.SH": "中概互联",
    "513880.SH": "日经225", "513030.SH": "德国30", "159920.SZ": "恒生ETF",
}

try:  # 腿分类复用 portfolio_combined（与 portfolio_live 同一事实源）
    from portfolio_combined import A_STOCK_LEGS, QDII_LEGS
except Exception:  # noqa: BLE001  降级：不区分腿（仅影响报告里腿标注）
    A_STOCK_LEGS, QDII_LEGS = [], []

# 整数手调仓求解器：与 daily_advice.py 共用同一实现。
# 2026-09-02 起 reconcile 的建议列改走这里，替代原先「买卖腿分别向下取整」
# 的次优逻辑（实测偏离 28.33pp vs 全局最优 21.34pp，且会让现金非预期溢出）。
try:
    from rebalance_solver import LOT as _SOLVER_LOT, solve_lots as _solve_lots
    SOLVER_OK = True
    assert _SOLVER_LOT == LOT, "rebalance_solver 与本地 LOT 不一致"
except Exception:  # noqa: BLE001  降级：退回逐腿 floor（不应发生，仅兜底）
    _solve_lots = None
    SOLVER_OK = False


# --------------------------------------------------------------------------- #
# 账本读写（原子写，防中断损坏）
# --------------------------------------------------------------------------- #
def load_ledger() -> dict | None:
    if not LEDGER.exists():
        return None
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_ledger(led: dict) -> None:
    tmp = LEDGER.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(led, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(LEDGER)


def load_nav() -> pd.DataFrame:
    if not NAV_CSV.exists():
        return pd.DataFrame(columns=["date", "total", "cash", "market",
                                     "day_ret", "cum_ret"])
    return pd.read_csv(NAV_CSV, parse_dates=["date"]).sort_values("date")


def save_nav(nav: pd.DataFrame) -> None:
    nav = nav.drop_duplicates(subset="date", keep="last").sort_values("date")
    qbt.atomic_to_csv(nav, NAV_CSV, index=False)


def get_last_close(code: str) -> tuple[float, str] | None:
    """最新收盘价与日期；文件缺失返回 None。

    返回市价（元/份）。注：数据源 vibe tencent 前复权链对货币 ETF
    （511880.SH 银华日利）返回真实 100 元档价格，无需缩放。
    """
    p = DATA_DIR / f"{code}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, usecols=["date", "close"], parse_dates=["date"])
    row = df.dropna(subset=["close"]).iloc[-1]
    return float(row["close"]), str(row["date"].date())


def valuation(positions: dict[str, int]) -> tuple[dict[str, float], dict[str, str]]:
    """按最新收盘价估值：{code: 市值} + {code: 价日期}。"""
    market, last_dates = {}, {}
    for c, sh in positions.items():
        v = get_last_close(c)
        if v is None:
            market[c], last_dates[c] = 0.0, "—（缺数据）"
            continue
        px, d = v
        market[c], last_dates[c] = px * sh, d
    return market, last_dates


# --------------------------------------------------------------------------- #
# 命令：init / buy / sell / status
# --------------------------------------------------------------------------- #
def cmd_init(capital: float, name: str, force: bool) -> int:
    if load_ledger() is not None and not force:
        print(f"⚠️ 账本已存在（{LEDGER}）。需要重建请加 --force（会清空历史与净值）。")
        return 1
    led = {
        "name": name,
        "capital": round(capital, 2),
        "currency": "CNY",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cash": round(capital, 2),
        "positions": {},       # {code: 份额}
        "trades": [],          # [{date, code, side, shares, price, amount, fee, note}]
        "note": "模拟盘，按 portfolio_live 三层组合信号执行；成交价=最新收盘价",
    }
    save_ledger(led)
    print(f"✅ 模拟盘「{name}」初始化完成：初始资金 {capital:,.0f} 元。")
    return 0


def _fee(amount: float) -> float:
    return round(amount * COST, 2)


def cmd_trade(side: str, code: str, shares: int, price: float | None,
              date: str | None) -> int:
    led = load_ledger()
    if led is None:
        print("⚠️ 账本不存在，请先 init。")
        return 1
    if code not in NAMES:
        print(f"⚠️ 未知代码 {code}（不在 18 只池）。")
        return 1
    if shares <= 0 or shares % LOT != 0:
        print(f"⚠️ 份额必须为正且为 {LOT} 的整数倍。")
        return 1
    px, last_d = (price, date) if price else (None, None)
    if px is None:
        v = get_last_close(code)
        if v is None:
            print(f"⚠️ {code} 无行情数据，无法定价（可 --price 指定）。")
            return 1
        px, last_d = v
    px = float(px)
    trade_date = date or datetime.now().strftime("%Y-%m-%d")
    amount = px * shares
    fee = _fee(amount)

    pos = dict(led.get("positions", {}))
    cash = float(led["cash"])
    if side == "buy":
        if cash < amount + fee:
            print(f"⚠️ 现金不足：需 {amount+fee:,.2f}，账本现金 {cash:,.2f}。")
            return 1
        cash -= amount + fee
        pos[code] = pos.get(code, 0) + shares
    else:  # sell
        if pos.get(code, 0) < shares:
            print(f"⚠️ 持仓不足：持有 {pos.get(code, 0)}，想卖 {shares}。")
            return 1
        cash += amount - fee
        pos[code] -= shares
        if pos[code] == 0:
            del pos[code]

    led["cash"] = round(cash, 2)
    led["positions"] = pos
    led.setdefault("trades", []).append({
        "date": trade_date, "code": code, "side": side, "shares": shares,
        "price": px, "amount": round(amount, 2), "fee": fee,
        "note": f"成交价={last_d}收盘价",
    })
    save_ledger(led)
    print(f"✅ {side.upper()} {NAMES[code]} {shares} 份 @ {px:.3f} = {amount:,.2f}"
          f"（佣金 {fee:.2f}）→ 现金 {cash:,.2f}")
    return 0


def cmd_status() -> int:
    led = load_ledger()
    if led is None:
        print("⚠️ 账本不存在，请先 init。")
        return 1
    market, last_dates = valuation(led.get("positions", {}))
    mv = sum(market.values())
    total = mv + float(led["cash"])
    print(f"模拟盘「{led['name']}」 初始 {led['capital']:,.0f} 元")
    print(f"  现金 {led['cash']:,.2f} · 持仓市值 {mv:,.2f} · 总资产 {total:,.2f}"
          f"（{(total/led['capital']-1)*100:+.2f}%）")
    for c, sh in sorted(led.get("positions", {}).items(), key=lambda x: -market[x[0]]):
        print(f"  {c} {NAMES.get(c, c):<8} {sh:>7} 份 × {last_dates[c]} 收盘"
              f" = {market[c]:,.2f}（{market[c]/total*100:.1f}%）")
    return 0


def cmd_set_holdings(csv_path: Path, trade_date: str, note: str,
                    charge_fee: bool = False) -> int:
    """从券商实际持仓 CSV 一次性初始化账本（替代 init+11×buy）。

    文件格式：header=code,shares,cost（其余列忽略）；可含 comments 行（# 开头）。
    默认 cost = 券商展示的"成本价"，**已含佣金**，sync 只累加 cost×shares，
    不再二次扣 0.15%；如传入的是净价则加 --charge-fee 重算佣金。
    自动校核：合计 cash = capital - Σ amount，不得为负。
    """
    led = load_ledger()
    if led is None:
        print("⚠️ 账本不存在，请先 init（一次性记录初始资金）。")
        return 1

    import csv as csvmod
    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        for row in csvmod.DictReader(f):
            if not row.get("code") or row["code"].startswith("#"):
                continue
            rows.append(row)
    if not rows:
        print(f"⚠️ {csv_path} 无有效行（需 code,shares,cost 三列）。")
        return 1

    positions: dict[str, int] = {}
    trades: list[dict] = []
    total_cost: float = 0.0
    total_fee: float = 0.0
    for row in rows:
        c, sh = row["code"].strip(), int(row["shares"])
        cost = float(row["cost"])
        if sh <= 0 or sh % LOT != 0:
            print(f"⚠️ {c} 份额 {sh} 不是 {LOT} 的整数倍，跳过。")
            return 1
        if c not in NAMES:
            print(f"⚠️ {c} 不在 18 只池，跳过。")
            return 1
        amount = round(cost * sh, 2)
        fee = _fee(amount) if charge_fee else 0.0
        positions[c] = positions.get(c, 0) + sh
        total_cost += amount
        total_fee += fee
        trades.append({
            "date": trade_date, "code": c, "side": "buy",
            "shares": sh, "price": cost, "amount": amount, "fee": fee,
            "note": f"{note}（券商实际成交价"
                    + ("，二次计费" if charge_fee else "，含佣金") + "）",
        })

    cash_left = round(float(led["capital"]) - total_cost - total_fee, 2)
    if cash_left < 0:
        print(f"⚠️ 持仓占用 {total_cost+total_fee:,.2f} 元超过初始资金 "
              f"{led['capital']:,.0f}，请先 init --capital 调高或调整 CSV。")
        return 1

    led["cash"] = cash_left
    led["positions"] = positions
    led["trades"] = trades
    led["note"] = note
    save_ledger(led)

    fee_str = f" + 佣金 {total_fee:,.2f}" if charge_fee else ""
    print(f"✅ 从 {csv_path.name} 同步 {len(positions)} 只持仓（{len(trades)} 笔），"
          f"占用 {total_cost:,.2f}{fee_str} = 合计 {total_cost+total_fee:,.2f}，"
          f"剩余现金 {cash_left:,.2f}")
    print(f"   成交日期：{trade_date}")
    return 0


# --------------------------------------------------------------------------- #
# 建仓指导
# --------------------------------------------------------------------------- #
def load_target() -> dict | None:
    if not LIVE_JSON.exists():
        return None
    try:
        return json.loads(LIVE_JSON.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def cmd_entry_guide(capital: float, out: Path) -> int:
    snap = load_target()
    if snap is None:
        print("⚠️ 缺 runs/portfolio_live.json——先跑 portfolio_live.py 生成目标权重。")
        return 1
    tw = snap.get("target_weights", {})
    cash_target = snap.get("cash", 0.0)
    as_of = snap.get("as_of", "?")

    rows = []
    used = 0.0
    for c, w in sorted(tw.items(), key=lambda x: -x[1]):
        amt = capital * w
        v = get_last_close(c)
        px, last_d = (v[0], v[1]) if v else (None, None)
        if px is None or amt <= 0:
            rows.append({"code": c, "name": NAMES.get(c, c), "w": w,
                         "target_amt": amt, "shares": 0, "px": px,
                         "date": last_d, "amt": 0.0})
            continue
        shares = int(amt / px / LOT) * LOT
        act = shares * px
        used += act
        rows.append({"code": c, "name": NAMES.get(c, c), "w": w,
                     "target_amt": amt, "shares": shares, "px": px,
                     "date": last_d, "amt": act})
    fees = sum(_fee(r["amt"]) for r in rows if r["amt"] > 0)
    cash_left = capital - used - fees

    L = [f"# 模拟盘建仓指导（信号 {as_of} → 次日执行）\n",
         f"> 资金 {capital:,.0f} 元 · 目标 = portfolio_live 三层组合信号 "
         f"（逆波动 × PB 门控 × QDII 门控）\n",
         f"> 成交价 = 最新可得收盘价（{max((r['date'] for r in rows if r['date']), default='?')}）"
         f"，按 {LOT} 份整数手向下取整；预估佣金合计 {fees:,.2f} 元。\n",
         f"> **A 股腿门控：PB 分位 {snap.get('pb', {}).get('percentile', '?')} → 档位"
         f" {snap.get('pb', {}).get('level', '?')}**"
         f"（0=空仓/0.5=半仓/1=全仓）；QDII 六腿门控见下表。\n"]
    L.append("\n| 代码 | 名称 | 层 | 目标权重 | 目标金额 | 份数 | 最新价 | 预计金额 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r["shares"] == 0 and r["w"] < 0.0005:
            continue
        layer = ("A股" if r["code"] in A_STOCK_LEGS
                 else "QDII" if r["code"] in QDII_LEGS else "其他")
        px = f"{r['px']:.3f}" if r["px"] else "—"
        amt = f"{r['amt']:,.0f}" if r["amt"] > 0 else "—"
        L.append(f"| {r['code']} | {r['name']} | {layer} | {r['w']*100:.1f}% "
                 f"| {r['target_amt']:,.0f} | {r['shares']} | {px} | {amt} |")
    L.append(f"\n- 已配置仓位合计：{used:,.0f} 元（{used/capital*100:.1f}%）")
    L.append(f"- 预估建仓佣金：{fees:,.2f} 元")
    L.append(f"- **剩余现金（含佣金缓冲）：{cash_left:,.2f} 元（{cash_left/capital*100:.1f}%）**")
    L.append(f"- 目标现金（信号口径）：{cash_target*100:.1f}% = {capital*cash_target:,.0f} 元"
             f"——门控减出的仓位本就应留在现金，与「整数手取整余量」共同构成现金头寸。\n")
    L.append("\n## 建仓执行\n")
    L.append("```bash")
    L.append(f"python3 scripts/paper_trading.py init --capital {capital:,.0f}".replace(",", ""))
    for r in sorted(rows, key=lambda x: -x["amt"]):
        if r["shares"] > 0:
            L.append(f"python3 scripts/paper_trading.py buy --code {r['code']} "
                     f"--shares {r['shares']}")
    L.append("python3 scripts/paper_trading.py reconcile")
    L.append("```")
    L.append("\n> 一次性生成，仅供参考；实际下单请对照账户实时行情。"
             "今晚 21:30 定时任务将自动对账。")
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"建仓指导已写入 {out}")
    print(f"共 {sum(1 for r in rows if r['shares'] > 0)} 只需买入，"
          f"预计占用 {used:,.0f} 元，现金余量 {cash_left:,.0f} 元")
    return 0


# --------------------------------------------------------------------------- #
# 每日对账
# --------------------------------------------------------------------------- #
def cmd_reconcile() -> int:
    led = load_ledger()
    if led is None:
        print("⚠️ 账本不存在，请先 init（或先跑 entry-guide 生成建仓指令）。")
        return 1
    snap = load_target()
    sol = None          # 整数手求解结果（snap 为 None 时保持 None）

    positions = led.get("positions", {})
    market, last_dates = valuation(positions)
    cash = float(led["cash"])
    mv = sum(market.values())
    total = mv + cash
    day = datetime.now().strftime("%Y-%m-%d")

    # 净值历史（同日去重）
    # 注意：必须真的去重——同一天多次跑 reconcile（调仓后重跑、手动补跑）
    # 若直接追加会累积重复行，prev_total 取到同日旧值导致 day_ret 失真。
    nav = load_nav()
    if not nav.empty:
        nav = nav.copy()
        nav["date"] = pd.to_datetime(nav["date"])
        same_day = nav["date"] == pd.Timestamp(day)
        if same_day.any():
            nav = nav[~same_day]              # 剔除当日旧行，稍后以新值覆盖
        prev_total = None if nav.empty else float(nav.iloc[-1]["total"])
    else:
        prev_total = None
    day_ret = (total / prev_total - 1) if prev_total else None
    cum_ret = total / float(led["capital"]) - 1
    row = pd.DataFrame([{
        "date": pd.Timestamp(day), "total": round(total, 2),
        "cash": round(cash, 2), "market": round(mv, 2),
        "day_ret": round(day_ret, 6) if day_ret is not None else None,
        "cum_ret": round(cum_ret, 6),
    }])
    nav = row if nav.empty else pd.concat([nav, row], ignore_index=True)
    save_nav(nav)

    L = [f"# 模拟盘对账（{day}）\n"]
    L.append(f"> 账本「{led['name']}」初始 {led['capital']:,.0f} 元 · "
             f"总资产 **{total:,.2f} 元**（累计 {cum_ret*100:+.2f}%"
             + (f"，当日 {day_ret*100:+.2f}%）" if day_ret is not None else "）") + "\n")
    if snap is None:
        L.append("> ⚠️ 缺 runs/portfolio_live.json，本次仅账本视图，未对账信号。\n")
    else:
        L.append(f"> 对账基准：portfolio_live 信号（{snap.get('as_of')} 收盘后）目标权重；"
                 "偏差 > 2pp 且金额差 > 阈值才提示调仓。\n")
    if any(d == "—（缺数据）" for d in last_dates.values()):
        L.append("> ⚠️ 部分标的缺行情数据，市值为 0，请检查 data/*.csv。\n")

    # ---- 持仓明细 ----
    L.append("\n## 1. 持仓明细（按最新收盘价估值）\n")
    L.append("| 代码 | 名称 | 层 | 份额 | 最新价(日期) | 市值 | 实际权重 |")
    L.append("|---|---|---|---|---|---|---|")
    for c, sh in sorted(positions.items(), key=lambda x: -market[x[0]]):
        layer = ("A股" if c in A_STOCK_LEGS
                 else "QDII" if c in QDII_LEGS else "其他")
        px_d = last_dates.get(c, "?")
        px = f"—（{px_d}）" if market[c] == 0 else ""
        L.append(f"| {c} | {NAMES.get(c, c)} | {layer} | {sh} | {px_d} | "
                 f"{market[c]:,.2f} | {market[c]/total*100:.1f}% |")
    L.append(f"| — | **现金** | — | — | — | {cash:,.2f} | {cash/total*100:.1f}% |")

    # ---- 对账（实际 vs 目标）----
    L.append("\n## 2. 对账（实际权重 vs 信号目标）\n")
    if snap is None:
        L.append("（无信号基准，跳过）")
    else:
        tw = snap.get("target_weights", {})
        cash_target = snap.get("cash", 0.0)
        codes = sorted(set(list(tw.keys()) + list(positions.keys())),
                       key=lambda x: -float(tw.get(x, 0.0)))

        # ---- 整数手最优求解（与 daily_advice.py 共用 rebalance_solver）----
        # 替代原先「各腿独立 floor 取整」：那会让卖出回款 > 买入支出、现金溢出，
        # 实测偏离 28.33pp vs 全局最优 21.34pp。
        px_all: dict[str, float] = {}
        for c in codes:
            v = get_last_close(c)
            if v and v[0] > 0:
                px_all[c] = float(v[0])
        sol = None
        if SOLVER_OK and px_all:
            try:
                sol = _solve_lots(
                    positions={c: int(positions.get(c, 0)) for c in px_all},
                    prices=px_all,
                    targets={c: float(tw.get(c, 0.0)) for c in px_all},
                    total=total, cash=cash,
                    target_cash=float(cash_target or 0.0),
                    min_dev=W_DIFF_TH,
                )
            except Exception:  # noqa: BLE001  求解失败不应阻断对账
                sol = None
        plan: dict[str, tuple[str, int]] = {}     # code -> (方向, 份数)
        if sol:
            for c, sh in sol["sells"].items():
                plan[c] = ("卖出", int(sh))
            for c, sh in sol["buys"].items():
                plan[c] = ("买入", int(sh))

        L.append("| 代码 | 名称 | 目标权重 | 目标金额 | 实际金额 | 偏差金额 | 偏差pp | 建议 |")
        L.append("|---|---|---|---|---|---|---|---|")
        suggestions, sub_lot = [], []
        th = max(MIN_AMT_TH, total * W_DIFF_TH)
        for c in codes:
            w_t = float(tw.get(c, 0.0))
            amt_t = total * w_t
            amt_a = float(market.get(c, 0.0))
            diff = amt_a - amt_t
            diff_pp = diff / total * 100 if total else 0.0
            act = ""
            if c in plan:
                side, n_sh = plan[c]
                act = f"**{side} {n_sh} 份**"
                suggestions.append((c, NAMES.get(c, c), diff, n_sh, side))
            else:
                over = abs(diff) > th and abs(diff_pp) > W_DIFF_TH * 100
                if over:
                    v = get_last_close(c)
                    px = v[0] if v else None
                    if px and abs(diff) / px < LOT:
                        # 偏差金额超阈值但不足一手：凑一手会反向超配，求解器自然排除
                        act = "不动（不足一手）"
                        sub_lot.append((c, NAMES.get(c, c), diff))
                    elif sol is not None:
                        act = "不动（调后总偏离变大）"
            if w_t < 0.0005 and diff_pp == 0 and act == "":
                continue
            L.append(f"| {c} | {NAMES.get(c, c)} | {w_t*100:.1f}% | {amt_t:,.0f} "
                     f"| {amt_a:,.0f} | {diff:+,.0f} | {diff_pp:+.1f}pp | {act} |")
        cash_a, cash_t = cash, total * cash_target
        cdiff = cash_a - cash_t
        L.append(f"| — | **现金** | {cash_target*100:.1f}% | {cash_t:,.0f} "
                 f"| {cash_a:,.0f} | {cdiff:+,.0f} | {cdiff/total*100:+.1f}pp | — |")
        L.append("\n### 调仓建议（整数手全局最优）\n")
        if sol is not None:
            L.append(f"> 求解器枚举搜索：总绝对偏离 {sol['before_abs_dev']:.1f}pp → "
                     f"**{sol['total_abs_dev']:.1f}pp**"
                     + (f"（改善 {sol['before_abs_dev']-sol['total_abs_dev']:.1f}pp）"
                        if sol["before_abs_dev"] - sol["total_abs_dev"] > 0.05
                        else "（当前持仓已是最优，不调仓）") + "\n")
        if suggestions:
            for c, name, diff, n_sh, side in suggestions:
                px = px_all.get(c, 0.0)
                L.append(f"- **{side} {name}（{c}）{n_sh} 份** ≈ {n_sh*px:,.0f} 元"
                         f"（现价 {px:.3f}；当前{'超配' if diff > 0 else '低配'} "
                         f"{abs(diff):,.0f} 元）")
            if sol:
                L.append(f"\n> 卖出回款（扣佣金）≈ {sol['proceeds']:,.0f} 元 ｜ "
                         f"买入支出（含佣金）≈ {sol['spend']:,.0f} 元 ｜ "
                         f"现金 {cash:,.0f} → {sol['cash_after']:,.0f} 元"
                         f"（{cash/total*100:.1f}% → {sol['cash_after']/total*100:.1f}%）")
            L.append("\n> 与 `scripts/daily_advice.py` 共用 `rebalance_solver.py`，"
                     "两入口结果一致；执行后重跑本脚本刷新对账。")
        else:
            why = []
            if sub_lot:
                why.append("超阈值腿均不足一手（凑一手会反向超配）")
            has_blocked = any("调后总偏离变大" in x for x in L)
            if has_blocked:
                why.append("其余超阈值腿调整后总偏离反而变大")
            L.append("求解器判定**无需调仓**"
                     + ("：" + "；".join(why) + "，" if why else "（无超阈值偏差），")
                     + "当前持仓已是整数手约束下的最优解。")
        if sub_lot:
            L.append("\n> ⚠️ 以下腿偏差超阈值但**不足一手**（硬凑一手会反向超配）："
                     + "、".join(f"{name}（{c}，{'超配' if d > 0 else '低配'} "
                                 f"{abs(d):,.0f} 元）" for c, name, d in sub_lot)
                     + "。建议保持现金头寸，等偏差累积到一手以上再调。")
        if abs(cdiff) > th and abs(cdiff) > total * 0.02:
            L.append("\n> 现金较目标超配——主要来自建仓整数手取整余量，"
                     "属正常；如需贴紧信号可补买高权重腿（见上表）。")
        L.append(f"\n> 门控参考：PB 分位 {snap.get('pb', {}).get('percentile')} → "
                 f"A 股档位 {snap.get('pb', {}).get('level')}；QDII 明日门控："
                 + "、".join(f"{NAMES.get(c, c)} {g['tomorrow']}"
                             for c, g in snap.get("qdii_gates", {}).items()))

    # ---- 交易流水 ----
    trades = led.get("trades", [])
    L.append(f"\n## 3. 交易流水（{len(trades)} 笔）\n")
    if trades:
        L.append("| 日期 | 代码 | 名称 | 方向 | 份额 | 价格 | 金额 | 佣金 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for t in trades[-20:]:
            L.append(f"| {t['date']} | {t['code']} | {NAMES.get(t['code'], t['code'])} "
                     f"| {t['side'].upper()} | {t['shares']} | {t['price']:.3f} "
                     f"| {t['amount']:,.0f} | {t['fee']:.2f} |")
    else:
        L.append("（无交易）")

    L.append("\n## 4. 口径备忘\n")
    L.append("- 估值价 = data/*.csv 最新收盘价；QDII 净值 T+1~T+2 公布，"
             "估值含 1~2 日滞后（与信号同源，可接受）。")
    L.append("- 佣金 0.15% 单边（ETF 免印花税），成交即扣；"
             "「总资产」已含建仓成本拖累。")
    L.append("- 目标权重来自 portfolio_live（三层组合：逆波动 × PB 门控 × QDII 门控），"
             "A 股腿按 PB 分位三档、QDII 腿按溢价 z 状态机。")
    REPORT_MD.write_text("\n".join(L), encoding="utf-8")

    print("=" * 64)
    print(f"模拟盘对账（{day}）：总资产 {total:,.2f} 元"
          f"（累计 {cum_ret*100:+.2f}%"
          + (f"，当日 {day_ret*100:+.2f}%）" if day_ret is not None else "）"))
    if snap is not None:
        # 注：原实现用 `'suggestions' in dir()` 判断，但模块级 dir() 恒为 False，
        # 一直打印「—」。改为直接取局部变量（snap 非 None 分支必然已定义）。
        n_act = len(suggestions)
        dev_txt = ""
        if sol is not None:
            dev_txt = (f"；总绝对偏离 {sol['before_abs_dev']:.1f}pp → "
                       f"{sol['total_abs_dev']:.1f}pp")
        print(f"  对账基准 {snap.get('as_of')}：调仓建议 {n_act} 项{dev_txt}")
    print("  详见 runs/paper_trading.md")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟盘账本 + 对账（20 万示例资金）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="初始化账本")
    p_init.add_argument("--capital", type=float, default=200000.0)
    p_init.add_argument("--name", default="模拟盘")
    p_init.add_argument("--force", action="store_true", help="重建（清空历史）")

    p_buy = sub.add_parser("buy", help="买入")
    p_buy.add_argument("--code", required=True)
    p_buy.add_argument("--shares", type=int, required=True)
    p_buy.add_argument("--price", type=float)
    p_buy.add_argument("--date")

    p_sell = sub.add_parser("sell", help="卖出")
    p_sell.add_argument("--code", required=True)
    p_sell.add_argument("--shares", type=int, required=True)
    p_sell.add_argument("--price", type=float)
    p_sell.add_argument("--date")

    p_eg = sub.add_parser("entry-guide", help="建仓指导（不写账本）")
    p_eg.add_argument("--capital", type=float, default=200000.0)
    p_eg.add_argument("--out", type=Path, default=GUIDE_MD)

    p_set = sub.add_parser("set-holdings",
                           help="从券商实际持仓一次性初始化（替代 init+11×buy）")
    p_set.add_argument("--file", required=True,
                       help="CSV 列：code,shares,cost（券商实际成本价，已含佣金）")
    p_set.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                       help="成交日期（默认今日）")
    p_set.add_argument("--note", default="从券商持仓同步",
                       help="备注，写入交易流水")
    p_set.add_argument("--charge-fee", action="store_true",
                       help="cost 为不含佣金的成交价——额外扣 0.15%%；"
                            "默认 cost 已含佣金，sync 只扣 cost×shares")

    sub.add_parser("reconcile", help="估值 + 对账（qdii_daily 第 4 步）")
    sub.add_parser("status", help="账本视图")

    args = ap.parse_args()
    if args.cmd == "init":
        return cmd_init(args.capital, args.name, args.force)
    if args.cmd == "buy":
        return cmd_trade("buy", args.code, args.shares, args.price, args.date)
    if args.cmd == "sell":
        return cmd_trade("sell", args.code, args.shares, args.price, args.date)
    if args.cmd == "entry-guide":
        return cmd_entry_guide(args.capital, args.out)
    if args.cmd == "set-holdings":
        return cmd_set_holdings(Path(args.file), args.date, args.note,
                                args.charge_fee)
    if args.cmd == "reconcile":
        return cmd_reconcile()
    if args.cmd == "status":
        return cmd_status()
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
