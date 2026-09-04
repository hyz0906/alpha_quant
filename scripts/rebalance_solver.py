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

两代算法
--------
**v1（2026-09-01，已废弃）**：`itertools.product` 暴力枚举 + `min_dev` 前置过滤
候选腿。存在**阈值死锁**：当「某腿超配 > min_dev，但所有对手腿偏离都 < min_dev」
时，卖出的钱只能进现金；若现金同样超配，则「减 A 之偏差 = 增现金之偏差」，
目标函数退化为常数 → 最优解取到 0 交易，巨大偏离原地锁死。
实测 2026-09-02 / 09-03 连续两日：511010 +6.8pp、8 条买腿全在 −0.8~−1.6pp、
现金 +2.5pp → 21.8pp 偏离锁死；而阈值放宽到 1pp 可降到约 5.8pp。

**v2（2026-09-03，现行）**：把「阈值」从**搜索空间前置过滤**改为**净改善后置
判据**：

1. 候选腿不再被 `min_dev` 砍掉，全部进 MILP（scipy/HiGHS 精确解；18 腿约 73
   变量 / 57 约束，毫秒级），不再受 v1 的 3e8 组合爆炸限制。
2. 目标函数是份额向量上的**凸分段线性（L1）**函数，MILP 给出**全局最优**，
   不是局部搜索近似。
3. 防佣金磨损改由**净改善门槛** `MIN_GAIN_PP` 把关：调仓带来的总偏离改善不足
   0.05pp 就整体不调，而不是逐腿卡门槛。
4. 档位阶梯：min_dev 只作为**候选腿激活门槛**的起点，依次按 /2 放宽到 0，
   全部跑完后**先取全局最优偏离，再回选「不劣于最优 +LEVEL_TOL_PP 的最严
   档位」**（= 同样跟踪质量下最少下单笔数）。
   注意不是「先达标就停」——2026-09-03 实测那样会让优化器把多余现金硬塞进
   少数几条腿，513100 被买到超配 0.85pp，总偏离 5.89pp；而全局最优是 3.38pp
   且各腿均无显著超配。

佣金通过 `cash_after` 进入目标函数：买入按 (1+FEE) 扣款、卖出按 (1−FEE) 回款，
因此优化器天然把 0.15% 成本计入权衡，无需额外的手续费惩罚项。
"""

from __future__ import annotations

from itertools import product

import numpy as np

LOT = 100          # 场内 ETF 1 手 = 100 份
FEE = 0.0015       # 单边佣金 0.15%（与回测口径一致，ETF 免印花税）

MIN_GAIN_PP = 0.05    # 净改善门槛（pp）：低于此值认为不值得付佣金折腾
LEVEL_TOL_PP = 0.3    # 档位容差（pp）：偏离不劣于最优超过此值，优先用更严档位
MAX_RELAX = 8         # 阈值放宽次数上限，防病态输入死循环

try:  # scipy 缺失时降级到 v1 暴力枚举（可用，但会死锁/组合爆炸）
    from scipy.optimize import Bounds, LinearConstraint, milp
    _HAS_SCIPY = True
except Exception:  # noqa: BLE001
    _HAS_SCIPY = False


def _solve_milp(codes, k0, resid, prices, targets, total, cash, target_cash,
                frozen):
    """精确整数规划：最小化 Σ|占比−目标| + |现金占比−目标现金|。

    变量（以「手」为单位；不足 1 手的碎股 resid 固定不动，只作为常数偏移）：
        k_j  最终手数（整数 ≥0）
        b_j  买入手数（整数 ≥0）
        x_j  卖出手数（整数 ≥0，≤ k0_j）
        y_j  **方向指示**（二元）：y=1 只能买、y=0 只能卖
        d_j  |w_j − t_j|      （连续 ≥0）
        dc   |cash_after/total − target_cash| （连续 ≥0）

    约束：
        k_j − b_j + x_j = k0_j
        b_j ≤ Bmax_j·y_j ； x_j ≤ Xmax_j·(1 − y_j)      ← 关键，见下
        d_j ± w_j·k_j ≥ ±(t_j − resid_j·p_j/total)
        dc ± Σ[−C^buy·b_j + C^sell·x_j] ≥ ±(cash/total − target_cash)
        cash_after ≥ 0 ；frozen 腿的 k_j = k0_j 且 b_j = x_j = 0

    ⚠️ 为什么必须有方向指示 y_j（2026-09-03 踩坑）
    ----------------------------------------------
    目标函数把「现金偏离」也计为成本，而实盘常出现**现金超配**。此时「减少
    现金」能直接改善目标值，优化器于是找到了两种凭空烧钱的解法：

      a) 同一腿同时 b_j>0 且 x_j>0 —— 净头寸不变，但白付两遍佣金；
      b) 冻结腿（k_j 被钉死）上的 b_j 不受约束 —— 幽灵买入，钱花了、份额没增加。

    不修的话 MILP 会报出比真实最优还「优」的假解（实测：同一组 k 下 MILP 报
    0.1603pp，全网格真值 0.2065pp；18 腿实盘报 12.90pp 含 3 万份幽灵买单）。
    加二元指示 y_j 强制 min(b_j, x_j) = 0，并把冻结腿的 b/x 上界压到 0 即可。

    返回 (lots_after, trade, cash_after)，trade 为 {code: (买手, 卖手)}；无解返回 None。
    """
    n = len(codes)
    if n == 0 or total <= 0:
        return None

    lot_p = np.array([LOT * prices[c] for c in codes], dtype=float)   # 每手市值
    k0v = np.array([float(k0[c]) for c in codes])
    resid_val = np.array([resid[c] * prices[c] for c in codes], dtype=float)

    w_coef = lot_p / total                                    # k_j → 占比 的系数
    t_eff = np.array([targets.get(c, 0.0) for c in codes]) - resid_val / total

    kmax = np.maximum(np.ceil(total * 1.2 / np.maximum(lot_p, 1e-9)) + 10,
                      k0v + 10)

    # 变量顺序: k(n) | b(n) | x(n) | y(n) | d(n) | dc(1)
    nv = 5 * n + 1
    iB, iX, iY = n, 2 * n, 3 * n
    iD, iDC = 4 * n, 5 * n

    lb = np.zeros(nv)
    ub = np.empty(nv)
    ub[0:n] = kmax
    ub[iB:iB + n] = np.maximum(kmax - k0v, 0.0)     # 买入上限
    ub[iX:iX + n] = k0v                             # 卖出上限（不卖超持仓）
    ub[iY:iY + n] = 1.0                             # 方向指示（二元）
    ub[iD:iD + n] = np.inf
    ub[iDC] = np.inf

    # 冻结腿：份额钉死 + 禁止任何买卖（否则会出现幽灵买单凭空烧现金）
    for j, c in enumerate(codes):
        if c in frozen:
            ub[iB + j] = 0.0
            ub[iX + j] = 0.0

    integrality = np.zeros(nv)
    integrality[0:iD] = 1                    # k / b / x / y 整数，d / dc 连续

    rows, rlb, rub = [], [], []

    # (1) 份额守恒 k_j − b_j + x_j = k0_j；frozen 腿直接钉死 k_j = k0_j
    for j, c in enumerate(codes):
        r = np.zeros(nv)
        r[j] = 1.0
        if c not in frozen:
            r[iB + j] = -1.0
            r[iX + j] = 1.0
        rows.append(r); rlb.append(k0v[j]); rub.append(k0v[j])

    # (2)(3) 方向互斥：b_j ≤ Bmax·y_j ，x_j ≤ Xmax·(1 − y_j)
    #        强制 min(b_j, x_j) = 0，杜绝「同腿双向虚付佣金」骗取现金偏离改善
    for j in range(n):
        r = np.zeros(nv); r[iB + j] = 1.0; r[iY + j] = -ub[iB + j]
        rows.append(r); rlb.append(-np.inf); rub.append(0.0)
        r = np.zeros(nv); r[iX + j] = 1.0; r[iY + j] = ub[iX + j]
        rows.append(r); rlb.append(-np.inf); rub.append(ub[iX + j])

    # (4)(5) d_j ± w_coef·k_j ≥ ±t_eff_j
    for j in range(n):
        r = np.zeros(nv); r[iD + j] = 1.0; r[j] = w_coef[j]
        rows.append(r); rlb.append(t_eff[j]); rub.append(np.inf)
        r = np.zeros(nv); r[iD + j] = 1.0; r[j] = -w_coef[j]
        rows.append(r); rlb.append(-t_eff[j]); rub.append(np.inf)

    # (6)(7) dc ± Σ[−C^buy·b_j + C^sell·x_j] ≥ ±(cash/total − target_cash)
    c_buy = lot_p * (1 + FEE) / total
    c_sell = lot_p * (1 - FEE) / total
    base = cash / total - target_cash

    r = np.zeros(nv); r[iDC] = 1.0
    r[iB:iB + n] = -c_buy; r[iX:iX + n] = c_sell
    rows.append(r); rlb.append(-base); rub.append(np.inf)

    r = np.zeros(nv); r[iDC] = 1.0
    r[iB:iB + n] = c_buy; r[iX:iX + n] = -c_sell
    rows.append(r); rlb.append(base); rub.append(np.inf)

    # (8) cash_after ≥ 0
    r = np.zeros(nv)
    r[iB:iB + n] = -lot_p * (1 + FEE)
    r[iX:iX + n] = lot_p * (1 - FEE)
    rows.append(r); rlb.append(-cash); rub.append(np.inf)

    c_obj = np.zeros(nv)
    c_obj[iD:iD + n] = 1.0
    c_obj[iDC] = 1.0

    res = milp(c=c_obj,
               constraints=LinearConstraint(np.array(rows, dtype=float), rlb, rub),
               integrality=integrality,
               bounds=Bounds(lb, ub))
    if res is None or not res.success or res.x is None:
        return None

    xv = res.x
    k_after = np.rint(xv[0:n]).astype(int)
    b_after = np.rint(xv[iB:iB + n]).astype(int)
    x_after = np.rint(xv[iX:iX + n]).astype(int)

    cash_after = (cash
                  - float(np.sum(b_after * lot_p * (1 + FEE)))
                  + float(np.sum(x_after * lot_p * (1 - FEE))))
    if cash_after < -1e-6:
        return None

    lots_after = {c: int(k_after[j]) for j, c in enumerate(codes)}
    trade = {c: (int(b_after[j]), int(x_after[j])) for j, c in enumerate(codes)}
    return lots_after, trade, float(cash_after)


def _solve_brute(positions, prices, targets, total, cash, target_cash,
                 cur_val, tgt_val, tradable, span):
    """scipy 不可用时的暴力枚举回退（v1 算法，腿多时会截断/退化）。"""
    sell_codes = [c for c in tradable if (cur_val[c] - tgt_val[c]) / total > 0]
    buy_codes = [c for c in tradable if (cur_val[c] - tgt_val[c]) / total < 0]

    # ⚠️ span 必须作为形参传入：早期版本让 cands 闭包捕获外层 span，
    # 导致下面收紧 span 的循环重算出的候选集**完全相同**，ncomb 永不下降，
    # 循环只能靠 sp>1 退出，最后仍把一个 30 万+ 的组合空间丢给 product()，
    # 回退路径会直接卡死（2026-09-05 修）。
    def cands(c: str, is_sell: bool, sp: int) -> list[int]:
        cur = int(positions.get(c, 0))
        ideal = abs(tgt_val[c] - cur_val[c]) / prices[c]
        center = int(round(ideal / LOT))
        out = {0}
        for k in range(max(0, center - sp), center + sp + 1):
            sh = k * LOT
            if is_sell and sh > cur:
                continue
            out.add(sh)
        return sorted(out)

    sell_cand = [cands(c, True, span) for c in sell_codes]
    buy_cand = [cands(c, False, span) for c in buy_codes]

    def ncomb(cl):
        n = 1
        for x in cl:
            n *= len(x)
        return n

    sp = span
    while ncomb(sell_cand) * ncomb(buy_cand) > 300_000 and sp > 1:
        sp -= 1
        sell_cand = [cands(c, True, sp) for c in sell_codes]
        buy_cand = [cands(c, False, sp) for c in buy_codes]

    best = None
    for sc in product(*sell_cand):
        proceeds = sum(sh * prices[c] * (1 - FEE) for c, sh in zip(sell_codes, sc))
        avail = cash + proceeds
        for bc in product(*buy_cand):
            spend = sum(sh * prices[c] * (1 + FEE) for c, sh in zip(buy_codes, bc))
            cash_after = avail - spend
            if cash_after < 0:
                continue
            pos_after = {c: int(positions.get(c, 0)) for c in prices}
            for c, sh in zip(sell_codes, sc):
                pos_after[c] -= sh
            for c, sh in zip(buy_codes, bc):
                pos_after[c] += sh
            dev = sum(abs(pos_after[c] * prices[c] / total - targets.get(c, 0.0))
                      for c in prices)
            dev += abs(cash_after / total - target_cash)
            if best is None or dev < best[0]:
                best = (dev, dict(zip(sell_codes, sc)), dict(zip(buy_codes, bc)),
                        cash_after, pos_after)
    if best is None:
        return None

    _, sells_raw, buys_raw, cash_after, pos_after = best
    lots_after = {c: pos_after[c] // LOT for c in prices}
    trade = {c: (buys_raw.get(c, 0) // LOT, sells_raw.get(c, 0) // LOT)
             for c in prices}
    return lots_after, trade, cash_after


def _no_trade(positions, raw_codes, cash, before_w, before_dev,
              span, method, min_dev_used) -> dict:
    """构造「不调仓」结果对象（净改善不足 / 无候选档位时的统一出口）。"""
    return {
        "sells": {}, "buys": {}, "proceeds": 0.0, "spend": 0.0,
        "pos_after": {c: int(positions.get(c, 0)) for c in raw_codes},
        "cash_after": cash,
        "after_w": before_w, "before_w": before_w,
        "total_abs_dev": before_dev, "before_abs_dev": before_dev,
        "span": span, "method": method, "min_dev_used": min_dev_used,
    }


def solve_lots(positions: dict[str, int], prices: dict[str, float],
               targets: dict[str, float], total: float, cash: float,
               target_cash: float, min_dev: float = 0.02,
               span: int = 4) -> dict:
    """整数手调仓求解 —— **MILP 精确最小化总绝对偏离**（v2）。

    目标函数 = Σ|执行后占比 − 目标占比| + |执行后现金占比 − 目标现金占比|

    为什么不用贪心：见模块 docstring 的实测对比。「按缺口比例分配回款」的贪心
    版本在只有单个买入腿时会吃掉全部回款造成超买（缺口 4.7 万却买 5.6 万）。

    为什么阈值不再前置过滤候选腿：见模块 docstring「两代算法」。前置过滤会在
    「单一超配腿 + 全部对手腿小幅低配」时把目标函数压成常数，导致 0 交易死锁。

    Args:
        positions: 当前持仓 {code: 份额}
        prices:    最新价 {code: 价格}
        targets:   目标权重 {code: 权重}，键需覆盖 prices
        total:     总资产（现金 + 持仓市值）
        cash:      当前现金
        target_cash: 目标现金占比（通常 = 1 − Σ目标权重）
        min_dev:   调仓触发阈值（占比偏差）。**不再用于砍候选腿**，而是阶梯的
                   起始档位；若该档无有效改善则自动减半重解，直至 0。
        span:      （兼容保留）v2 的 MILP 路径不使用；仅暴力回退路径的搜索半径。

    Returns:
        dict: sells / buys / proceeds / spend / pos_after / cash_after /
              after_w / before_w / total_abs_dev / before_abs_dev / span /
              method / min_dev_used
              偏离字段单位 **pp**；sells / buys 单位 **份**。
    """
    raw_codes = list(prices)
    prices = {c: float(p) for c, p in prices.items() if p and float(p) > 0}
    codes = list(prices)
    cur_val = {c: int(positions.get(c, 0)) * prices[c] for c in codes}
    tgt_val = {c: total * targets.get(c, 0.0) for c in codes}

    def deviation(pos_shares: dict[str, int], cash_v: float) -> float:
        w = {c: pos_shares.get(c, 0) * prices[c] / total for c in codes}
        return (sum(abs(w[c] - targets.get(c, 0.0)) for c in codes)
                + abs(cash_v / total - target_cash))

    before_w = {c: cur_val[c] / total for c in codes}
    before_dev = (sum(abs(before_w[c] - targets.get(c, 0.0)) for c in codes)
                  + abs(cash / total - target_cash)) * 100

    # 整手数 k0 与不足 1 手的碎股 resid（碎股固定不动）
    k0, resid = {}, {}
    for c in codes:
        sh = int(positions.get(c, 0))
        k0[c] = sh // LOT
        resid[c] = sh - k0[c] * LOT

    # ---- 阈值阶梯：min_dev → /2 → /4 → … → 0
    levels, m, guard = [], float(min_dev), 0
    while m > 1e-9 and guard < MAX_RELAX:
        levels.append(m)
        m /= 2.0
        guard += 1
    levels.append(0.0)

    results = []        # [(lvl, dev_pp, lots_after, trade, cash_after)] 由严到宽
    for lvl in levels:
        if lvl > 0:
            tradable = [c for c in codes
                        if abs((cur_val[c] - tgt_val[c]) / total) >= lvl]
            if not tradable:
                continue
            frozen = set(codes) - set(tradable)
        else:
            tradable, frozen = list(codes), set()

        sol = None
        if _HAS_SCIPY:
            try:
                sol = _solve_milp(codes, k0, resid, prices, targets, total,
                                  cash, target_cash, frozen)
            except Exception:  # noqa: BLE001  MILP 失败不阻断，走暴力回退
                sol = None
        if sol is None:
            try:
                sol = _solve_brute(positions, prices, targets, total, cash,
                                   target_cash, cur_val, tgt_val, tradable, span)
            except Exception:  # noqa: BLE001
                sol = None
        if sol is None:
            continue

        lots_after, trade, cash_after = sol

        # 自校验：任何腿出现「同买同卖」说明模型出了幽灵单，整档弃用
        if any(trade[c][0] > 0 and trade[c][1] > 0 for c in codes):
            continue
        # 现金一律按实际成交重算，不信任求解器内部值
        cash_after = (cash
                      - sum(trade[c][0] * LOT * prices[c] * (1 + FEE) for c in codes)
                      + sum(trade[c][1] * LOT * prices[c] * (1 - FEE) for c in codes))
        if cash_after < -1e-6:
            continue

        pos_after = {c: lots_after[c] * LOT + resid[c] for c in codes}
        results.append((lvl, deviation(pos_after, cash_after) * 100,
                        lots_after, trade, cash_after))

    method = "milp" if _HAS_SCIPY else "brute"

    # ---- 档位选择：先求全局最优，再取「偏离不显著劣于最优的最严档位」
    # 为什么不「先达标就停」：2026-09-03 实测，0.01 档只解锁 7 条腿，优化器把
    # 多余现金硬塞进少数几条，513100 被买到 3.85%（目标 3.00%，超配 0.85pp），
    # 而 513050/513880/159981 因未达阈值被冻结在 −0.85~−0.95pp。总偏离 5.89pp。
    # 放宽到 0.005 档后 10 条腿全部参与，偏离降到 3.38pp（= 全腿全局最优），
    # 且**没有一条腿超配超过 0.1pp**。多 3 笔单换 2.5pp 跟踪误差，明显划算。
    # 2026-09-04 修正：档位选择与净改善门槛的判定顺序曾互相打架——
    # 「不劣于最优 +0.3pp 的最严档位」会选中一个**改善为 0** 的档位（如整手粒度
    # 过粗导致无解），随后门槛又拿这个档位去比对，于是全局最优那 0.1pp 的真实
    # 改善被白白放弃。现改为：**门槛一律以全局最优 best_dev 判定**，再把
    # 「至少改善 MIN_GAIN_PP」作为档位选择的上界，保证两者不会互相否定。
    if not results:
        return _no_trade(positions, raw_codes, cash, before_w, before_dev,
                         span, method, min_dev)

    best_dev, best_lvl = min((r[1], r[0]) for r in results)
    if before_dev - best_dev < MIN_GAIN_PP:
        # 全局最优都不够本 → 不折腾；min_dev_used 报实际达到最优的档位
        return _no_trade(positions, raw_codes, cash, before_w, before_dev,
                         span, method, best_lvl)

    # 档位上界同时受「档位容差」与「净改善下限」约束，取严者
    ceil_dev = min(best_dev + LEVEL_TOL_PP, before_dev - MIN_GAIN_PP)
    best = next((r for r in results if r[1] <= ceil_dev + 1e-9), None)
    if best is None:
        best = min(results, key=lambda r: r[1])

    lvl, dev_pp, lots_after, trade, cash_after = best
    pos_after = {c: lots_after[c] * LOT + resid[c] for c in codes}
    after_w = {c: pos_after[c] * prices[c] / total for c in codes}

    sells = {c: trade[c][1] * LOT for c in codes if trade[c][1] > 0}
    buys = {c: trade[c][0] * LOT for c in codes if trade[c][0] > 0}
    proceeds = sum(sells[c] * prices[c] * (1 - FEE) for c in sells)
    spend = sum(buys[c] * prices[c] * (1 + FEE) for c in buys)

    return {
        "sells": sells, "buys": buys, "proceeds": proceeds, "spend": spend,
        "pos_after": pos_after, "cash_after": cash_after,
        "after_w": after_w, "before_w": before_w,
        "total_abs_dev": dev_pp, "before_abs_dev": before_dev,
        "span": span, "method": method, "min_dev_used": lvl,
    }
