"""整数手调仓求解器（reconcile / daily_advice 共用的唯一事实源）。

为什么单独抽模块
----------------
2026-09-01 发现 `paper_trading.py reconcile` 的建议是**次优的**：它对卖出腿与
买入腿**分别向下取整到整手**，两边同时 floor 导致「卖出回款 > 买入支出」，
多余现金溢出。同日实测（已用 10×12 全网格暴力枚举交叉验证）：

| 方案                              | 总绝对偏离 | 现金占比 |
|-----------------------------------|-----------|---------|
| 不调仓                             | 69.75pp   | 16.1%   |
| reconcile 双 floor（卖400/买400）   | 28.33pp   | 24.1%   |
| **本模块枚举最优（卖400/买500）**   | **21.34pp** | 19.1% |

本模块被 `daily_advice.py` 与 `paper_trading.py cmd_reconcile` 同时 import，
保证两个入口给出**完全一致**的调仓建议。改算法只改这里。
"""

from __future__ import annotations

from itertools import product

LOT = 100          # 场内 ETF 1 手 = 100 份
FEE = 0.0015       # 单边佣金 0.15%（与回测口径一致，ETF 免印花税）


def solve_lots(positions: dict[str, int], prices: dict[str, float],
               targets: dict[str, float], total: float, cash: float,
               target_cash: float, min_dev: float = 0.02,
               span: int = 4) -> dict:
    """整数手调仓求解 —— **枚举搜索最小化总绝对偏离**。

    为什么不用贪心：见模块 docstring 的实测对比。而「按缺口比例分配回款」的
    贪心版本在只有单个买入腿时会吃掉全部回款造成超买（缺口 4.7 万却买 5.6 万）。

    枚举搜索直接对目标函数求最优，能自然权衡三种用法：
        多余回款 -> 多买该腿 / 留作现金 / 少卖一点

    目标函数 = Σ|执行后占比 − 目标占比| + |执行后现金占比 − 目标现金占比|

    搜索空间控制：每条腿只取「理想变动手数 ±span 手」的候选（默认 9+1 个），
    总组合数超 30 万时自动收紧 span。约束：现金不可为负。

    Args:
        positions: 当前持仓 {code: 份额}
        prices:    最新价 {code: 价格}
        targets:   目标权重 {code: 权重}，键需覆盖 prices
        total:     总资产（现金 + 持仓市值）
        cash:      当前现金
        target_cash: 目标现金占比（通常 = 1 − Σ目标权重）
        min_dev:   调仓触发阈值（占比偏差），低于此值的腿不动
        span:      每腿搜索半径（手）

    Returns:
        dict: sells / buys / proceeds / spend / pos_after / cash_after /
              after_w / before_w / total_abs_dev / before_abs_dev / span
              偏离字段单位为 **pp**（百分点）。
    """
    cur_val = {c: positions.get(c, 0) * prices[c] for c in prices}
    tgt_val = {c: total * targets.get(c, 0.0) for c in prices}

    # 只处理 |偏差| > min_dev 的腿（避免小额噪音调仓）
    sell_codes, buy_codes = [], []
    for c in prices:
        dev = (cur_val[c] - tgt_val[c]) / total
        if abs(dev) < min_dev:
            continue
        (sell_codes if dev > 0 else buy_codes).append(c)

    def cands(c: str, is_sell: bool) -> list[int]:
        cur = positions.get(c, 0)
        ideal = abs(tgt_val[c] - cur_val[c]) / prices[c]
        center = int(round(ideal / LOT))
        out = {0}
        for k in range(max(0, center - span), center + span + 1):
            sh = k * LOT
            if is_sell and sh > cur:
                continue
            out.add(sh)
        return sorted(out)

    sell_cand = [cands(c, True) for c in sell_codes]
    buy_cand = [cands(c, False) for c in buy_codes]

    def ncomb(cl):
        n = 1
        for x in cl:
            n *= len(x)
        return n

    while ncomb(sell_cand) * ncomb(buy_cand) > 300_000 and span > 1:
        span -= 1
        sell_cand = [cands(c, True) for c in sell_codes]
        buy_cand = [cands(c, False) for c in buy_codes]

    best = None
    for sc in product(*sell_cand):
        proceeds = sum(sh * prices[c] * (1 - FEE) for c, sh in zip(sell_codes, sc))
        avail = cash + proceeds
        for bc in product(*buy_cand):
            spend = sum(sh * prices[c] * (1 + FEE) for c, sh in zip(buy_codes, bc))
            cash_after = avail - spend
            if cash_after < 0:
                continue
            pos_after = dict(positions)
            for c, sh in zip(sell_codes, sc):
                pos_after[c] = pos_after.get(c, 0) - sh
            for c, sh in zip(buy_codes, bc):
                pos_after[c] = pos_after.get(c, 0) + sh
            after_w = {c: pos_after.get(c, 0) * prices[c] / total for c in prices}
            dev = sum(abs(after_w[c] - targets.get(c, 0.0)) for c in prices)
            dev += abs(cash_after / total - target_cash)
            if best is None or dev < best[0]:
                best = (dev, dict(zip(sell_codes, sc)), dict(zip(buy_codes, bc)),
                        cash_after, pos_after, after_w, proceeds, spend)

    if best is None:                       # 无可行解（理论上不会）：不调仓
        best = (0.0, {}, {}, cash, dict(positions),
                {c: cur_val[c] / total for c in prices}, 0.0, 0.0)

    dev_pp, sells_raw, buys_raw, cash_after, pos_after, after_w, proceeds, spend = best
    sells = {c: sh for c, sh in sells_raw.items() if sh > 0}
    buys = {c: sh for c, sh in buys_raw.items() if sh > 0}

    before_w = {c: cur_val[c] / total for c in prices}
    before_dev = (sum(abs(before_w[c] - targets.get(c, 0.0)) for c in prices)
                  + abs(cash / total - target_cash)) * 100

    return {
        "sells": sells, "buys": buys, "proceeds": proceeds, "spend": spend,
        "pos_after": pos_after, "cash_after": cash_after,
        "after_w": after_w, "before_w": before_w,
        "total_abs_dev": dev_pp * 100, "before_abs_dev": before_dev,
        "span": span,
    }
