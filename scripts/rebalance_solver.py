"""整数手调仓求解器（reconcile / daily_advice 共用的唯一事实源）。

为什么单独抽模块
----------------
2026-09-01 发现 `paper_trading.py reconcile` 的建议是**次优的**：它对卖出腿与
买入腿**分别向下取整到整手**，两边同时 floor 导致「卖出回款 > 买入支出」，
多余现金溢出。同日实测（已用 10x12 全网格暴力枚举交叉验证）：

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
实测 2026-09-02 / 09-03 连续两日：511010 +6.8pp、8 条买腿全在 -0.8~-1.6pp、
现金 +2.5pp → 21.8pp 偏离锁死；而阈值放宽到 1pp 可降到约 5.8pp。

**v2（2026-09-03）**：把「阈值」从**搜索空间前置过滤**改为**净改善后置判据**：

1. 候选腿不再被 `min_dev` 砍掉，全部进 MILP（scipy/HiGHS 精确解；18 腿约
   6n+1 个变量，毫秒级），不再受 v1 的 3e8 组合爆炸限制。
2. 目标函数是份额向量上的**凸分段线性（L1）**函数，MILP 给出**全局最优**，
   不是局部搜索近似。
3. 防佣金磨损改由**净改善门槛** `MIN_GAIN_PP` 把关：调仓带来的总偏离改善不足
   0.05pp 就整体不调，而不是逐腿卡门槛。
4. 档位阶梯：min_dev 只作为**候选腿激活门槛**的起点，依次按 /2 放宽到 0，
   全部跑完后再选最终解。
   注意不是「先达标就停」——2026-09-03 实测那样会让优化器把多余现金硬塞进
   少数几条腿，513100 被买到超配 0.85pp，总偏离 5.89pp；而全局最优是 3.38pp
   且各腿均无显著超配。

**v2.2（2026-09-24）**：修掉档位回选的两个缺陷，并新增最小交易额约束。

* 缺陷 A —— **「更严档 = 更少下单」是无根据的假设**。更严档的可行域是
  `lvl=0` 可行域的**子集**（frozen 腿被钉死 `b=x=0, k=k0`），所以 `lvl=0`
  的 MILP 最优偏离**必然 <= 任何更严档**。于是「回选不劣于最优 +LEVEL_TOL_PP
  的最严档」= 主动接受更差的偏离去换"假设中更少的笔数"，而 MILP 在每个档位
  内部**只最小化偏离、从不最小化笔数** —— 腿少 != 笔数少。2026-09-21 实测
  反例：0.00125 档给出 4 笔 / 换手 1,427 元 / 2.7834pp，而 `lvl=0` 是
  3 笔 / 换手 779 元 / 2.7825pp —— **偏离、笔数、换手三项全面更优**，
  旧实现却选了前者。
* 缺陷 B —— **容差 `LEVEL_TOL_PP = 0.3pp` 过大**。20 万组合下 0.3pp 约
  600 元跟踪误差，换来的只是不到 1 元的佣金节省。

改法（见 `solve_lots` 内「档位选择」一节）：
1. 容差改名 `DEV_TIE_PP = 0.02pp`，且**必须 < `MIN_GAIN_PP`**，否则会用容差
   绕过净改善门槛。
2. 在「偏离 <= ceil_dev」的等价解池内，**显式比较（笔数, 换手金额）**，
   不再按档位顺序盲选。
3. 新增**两阶段 MILP**：第二阶段固定 `SUM(d) + dc <= ceil_dev`，把目标改成
   「最小化交易笔数」重解，得到**严格意义上**并列解里笔数最少的那一个
   （第一阶段只保证偏离最优，不保证同偏离下笔数最少）。
4. 新增**单腿最小交易额** `MIN_TRADE_CNY`（半连续约束，**默认 0 = 关闭**）：
   要么不交易，要么单腿成交额 >= 阈值。目标为 0 的腿（门控清仓）豁免，
   保证清仓一定执行得完。

   ⚠️ 默认关闭的理由（2026-09-24 实测）：场内佣金是**比例费**（实盘约
   0.077%、回测口径 0.15%），233 元的单佣金只有 0.35 元、**占比与大单完全
   相同** —— "小额单佣金占比过高"这个动机不成立。60 组随机输入的 A/B 对照
   显示：启用 300 元门槛后 new_worse 由 0 升到 10 例（全部因合规解空间被
   压缩），而 new_better 28 例完全来自缺陷 A 的修复、与门槛无关。
   2026-09-23 真实输入上更是把「2 笔 / 换手 506 / 2.5849pp」换成了
   「3 笔 / 换手 1,472 / 2.5856pp」—— 偏离持平、笔数与成本双升，**净负收益**。

   仅当券商收取**最低佣金** C（如 5 元）时该约束才有意义，此时应设为
   `C / 实际费率`（例：5 / 0.00077 约 6,500 元）。注意求解器的实际下界是
   `ceil(MIN_TRADE_CNY / 每手市值)` **手**，会向上取整到整手，比阈值本身更严。

佣金通过 `cash_after` 进入目标函数：买入按 (1+FEE) 扣款、卖出按 (1-FEE) 回款，
因此优化器天然把 0.15% 成本计入权衡，无需额外的手续费惩罚项。
"""

from __future__ import annotations

from itertools import product

import numpy as np

LOT = 100          # 场内 ETF 1 手 = 100 份
FEE = 0.0015       # 单边佣金 0.15%（与回测口径一致，ETF 免印花税）

MIN_GAIN_PP = 0.05     # 净改善门槛（pp）：低于此值认为不值得付佣金折腾
DEV_TIE_PP = 0.02      # 偏离等价容差（pp）：必须 < MIN_GAIN_PP，否则绕过净改善门槛
MIN_TRADE_CNY = 0.0    # 单腿最小成交额（元）。0 = 关闭；
                      # 启用条件见模块 docstring「v2.2」第 4 条
MAX_RELAX = 8          # 阈值放宽次数上限，防病态输入死循环

try:  # scipy 缺失时降级到暴力枚举（可用，但会死锁/组合爆炸）
    from scipy.optimize import Bounds, LinearConstraint, milp
    _HAS_SCIPY = True
except Exception:  # noqa: BLE001
    _HAS_SCIPY = False


def _solve_milp(codes, k0, resid, prices, targets, total, cash, target_cash,
                frozen, dev_ceil=None, objective="dev"):
    """精确整数规划：最小化 SUM|占比-目标| + |现金占比-目标现金|。

    变量（以「手」为单位；不足 1 手的碎股 resid 固定不动，只作为常数偏移）：
        k_j   最终手数（整数 >=0）
        b_j   买入手数（整数 >=0）
        x_j   卖出手数（整数 >=0，<= k0_j）
        zb_j  **是否买入**（二元）  <- v2.2 起替代 y_j，同时承载最小交易额
        zx_j  **是否卖出**（二元）
        d_j   |w_j - t_j|      （连续 >=0）
        dc    |cash_after/total - target_cash| （连续 >=0）

    约束：
        k_j - b_j + x_j = k0_j
        minlots_j*zb_j <= b_j <= Bmax_j*zb_j      <- 半连续，杜绝小额单
        minlots_j*zx_j <= x_j <= Xmax_j*zx_j
        zb_j + zx_j <= 1                          <- 方向互斥
        d_j +- w_j*k_j >= +-(t_j - resid_j*p_j/total)
        dc +- SUM[-C^buy*b_j + C^sell*x_j] >= +-(cash/total - target_cash)
        cash_after >= 0 ；frozen 腿的 k_j = k0_j 且 b=x=zb=zx = 0
        （可选，第二阶段）SUM(d_j) + dc <= dev_ceil

    注意：v2.2 起 dev_ceil / objective 参数服务于两阶段求解。

    ⚠️ 为什么必须有方向互斥（2026-09-03 踩坑，v2.2 由 y_j 换成 zb/zx）
    ------------------------------------------------------------------
    目标函数把「现金偏离」也计为成本，而实盘常出现**现金超配**。此时「减少
    现金」能直接改善目标值，优化器于是找到了两种凭空烧钱的解法：

      a) 同一腿同时 b_j>0 且 x_j>0 —— 净头寸不变，但白付两遍佣金；
      b) 冻结腿（k_j 被钉死）上的 b_j 不受约束 —— 幽灵买入，钱花了、份额没增加。

    不修的话 MILP 会报出比真实最优还「优」的假解（实测：同一组 k 下 MILP 报
    0.1603pp，全网格真值 0.2065pp；18 腿实盘报 12.90pp 含 3 万份幽灵买单）。
    v2 用二元指示 y_j 强制 min(b_j, x_j) = 0；v2.2 改用 zb/zx 并加 `zb+zx <= 1`，
    语义更直接，且能顺带表达「要么不交易、要么至少 minlots 手」。

    返回 (lots_after, trade, cash_after)，trade 为 {code: (买手, 卖手)}；无解返回 None。
    """
    n = len(codes)
    if n == 0 or total <= 0:
        return None

    lot_p = np.array([LOT * prices[c] for c in codes], dtype=float)   # 每手市值
    k0v = np.array([float(k0[c]) for c in codes])
    resid_val = np.array([resid[c] * prices[c] for c in codes], dtype=float)

    w_coef = lot_p / total                                    # k_j -> 占比 的系数
    t_eff = np.array([targets.get(c, 0.0) for c in codes]) - resid_val / total

    kmax = np.maximum(np.ceil(total * 1.2 / np.maximum(lot_p, 1e-9)) + 10,
                      k0v + 10)

    # v2.2：最小交易手数（半连续下界）。目标为 0 的腿 = 门控清仓腿，豁免，
    # 否则会出现「只剩 1 手、金额 < 阈值 -> 卖不掉 -> 门控清仓执行不完」。
    minlots = np.ones(n, dtype=float)
    for j, c in enumerate(codes):
        if targets.get(c, 0.0) > 0.0:
            minlots[j] = max(1.0, float(np.ceil(MIN_TRADE_CNY
                                                / max(lot_p[j], 1e-9))))

    # 变量顺序: k(n) | b(n) | x(n) | zb(n) | zx(n) | d(n) | dc(1)
    nv = 6 * n + 1
    iB, iX, iZB, iZX = n, 2 * n, 3 * n, 4 * n
    iD, iDC = 5 * n, 6 * n

    lb = np.zeros(nv)
    ub = np.empty(nv)
    ub[0:n] = kmax
    ub[iB:iB + n] = np.maximum(kmax - k0v, 0.0)     # 买入上限
    ub[iX:iX + n] = k0v                             # 卖出上限（不卖超持仓）
    ub[iZB:iZB + n] = 1.0                           # 是否买入（二元）
    ub[iZX:iZX + n] = 1.0                           # 是否卖出（二元）
    ub[iD:iD + n] = np.inf
    ub[iDC] = np.inf

    # 冻结腿：份额钉死 + 禁止任何买卖（否则会出现幽灵买单凭空烧现金）
    for j, c in enumerate(codes):
        if c in frozen:
            ub[iB + j] = 0.0
            ub[iX + j] = 0.0
            ub[iZB + j] = 0.0
            ub[iZX + j] = 0.0

    integrality = np.zeros(nv)
    integrality[0:iD] = 1                    # k / b / x / zb / zx 整数，d / dc 连续

    rows, rlb, rub = [], [], []

    # (1) 份额守恒 k_j - b_j + x_j = k0_j；frozen 腿直接钉死 k_j = k0_j
    for j, c in enumerate(codes):
        r = np.zeros(nv)
        r[j] = 1.0
        if c not in frozen:
            r[iB + j] = -1.0
            r[iX + j] = 1.0
        rows.append(r); rlb.append(k0v[j]); rub.append(k0v[j])

    # (2)(3) 半连续买入：minlots*zb <= b <= Bmax*zb
    for j in range(n):
        r = np.zeros(nv); r[iB + j] = 1.0; r[iZB + j] = -ub[iB + j]
        rows.append(r); rlb.append(-np.inf); rub.append(0.0)
        r = np.zeros(nv); r[iB + j] = 1.0; r[iZB + j] = -minlots[j]
        rows.append(r); rlb.append(0.0); rub.append(np.inf)

    # (4)(5) 半连续卖出：minlots*zx <= x <= Xmax*zx
    for j in range(n):
        r = np.zeros(nv); r[iX + j] = 1.0; r[iZX + j] = -ub[iX + j]
        rows.append(r); rlb.append(-np.inf); rub.append(0.0)
        r = np.zeros(nv); r[iX + j] = 1.0; r[iZX + j] = -minlots[j]
        rows.append(r); rlb.append(0.0); rub.append(np.inf)

    # (6) 方向互斥 zb + zx <= 1 —— 强制 min(b_j, x_j) = 0，
    #     杜绝「同腿双向虚付佣金」骗取现金偏离改善
    for j in range(n):
        r = np.zeros(nv); r[iZB + j] = 1.0; r[iZX + j] = 1.0
        rows.append(r); rlb.append(-np.inf); rub.append(1.0)

    # (7)(8) d_j +- w_coef*k_j >= +-t_eff_j
    for j in range(n):
        r = np.zeros(nv); r[iD + j] = 1.0; r[j] = w_coef[j]
        rows.append(r); rlb.append(t_eff[j]); rub.append(np.inf)
        r = np.zeros(nv); r[iD + j] = 1.0; r[j] = -w_coef[j]
        rows.append(r); rlb.append(-t_eff[j]); rub.append(np.inf)

    # (9)(10) dc +- SUM[-C^buy*b_j + C^sell*x_j] >= +-(cash/total - target_cash)
    c_buy = lot_p * (1 + FEE) / total
    c_sell = lot_p * (1 - FEE) / total
    base = cash / total - target_cash

    r = np.zeros(nv); r[iDC] = 1.0
    r[iB:iB + n] = -c_buy; r[iX:iX + n] = c_sell
    rows.append(r); rlb.append(-base); rub.append(np.inf)

    r = np.zeros(nv); r[iDC] = 1.0
    r[iB:iB + n] = c_buy; r[iX:iX + n] = -c_sell
    rows.append(r); rlb.append(base); rub.append(np.inf)

    # (11) cash_after >= 0
    r = np.zeros(nv)
    r[iB:iB + n] = -lot_p * (1 + FEE)
    r[iX:iX + n] = lot_p * (1 - FEE)
    rows.append(r); rlb.append(-cash); rub.append(np.inf)

    # (12) 第二阶段：锁定偏离上界，转而最小化笔数
    if dev_ceil is not None:
        r = np.zeros(nv)
        r[iD:iD + n] = 1.0
        r[iDC] = 1.0
        rows.append(r); rlb.append(-np.inf); rub.append(float(dev_ceil))

    c_obj = np.zeros(nv)
    if objective == "trades":
        c_obj[iZB:iZB + n] = 1.0
        c_obj[iZX:iZX + n] = 1.0
    else:                                   # "dev"
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
            # v2.2：清仓腿（目标 0）豁免最小交易额，其余腿过滤掉小额单
            if targets.get(c, 0.0) > 0.0 and 0 < sh * prices[c] < MIN_TRADE_CNY:
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
    """整数手调仓求解 —— **MILP 精确最小化总绝对偏离**（v2.2）。

    目标函数 = SUM|执行后占比 - 目标占比| + |执行后现金占比 - 目标现金占比|

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
        target_cash: 目标现金占比（通常 = 1 - SUM 目标权重）
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

    def n_trades(trade: dict) -> int:
        return sum(1 for c in codes if trade[c][0] > 0 or trade[c][1] > 0)

    def turnover(trade: dict) -> float:
        return sum((trade[c][0] + trade[c][1]) * LOT * prices[c] for c in codes)

    before_w = {c: cur_val[c] / total for c in codes}
    before_dev = (sum(abs(before_w[c] - targets.get(c, 0.0)) for c in codes)
                  + abs(cash / total - target_cash)) * 100

    # 整手数 k0 与不足 1 手的碎股 resid（碎股固定不动）
    k0, resid = {}, {}
    for c in codes:
        sh = int(positions.get(c, 0))
        k0[c] = sh // LOT
        resid[c] = sh - k0[c] * LOT

    # ---- 阈值阶梯：min_dev -> /2 -> /4 -> … -> 0
    levels, m, guard = [], float(min_dev), 0
    while m > 1e-9 and guard < MAX_RELAX:
        levels.append(m)
        m /= 2.0
        guard += 1
    levels.append(0.0)

    results = []        # [(lvl, dev_pp, lots_after, trade, cash_after, frozen)]
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
                        lots_after, trade, cash_after, frozen))

    method = "milp" if _HAS_SCIPY else "brute"

    # ---- 档位选择（v2.2，2026-09-24 重写）
    # 为什么不「先达标就停」：2026-09-03 实测，0.01 档只解锁 7 条腿，优化器把
    # 多余现金硬塞进少数几条，513100 被买到 3.85%（目标 3.00%，超配 0.85pp），
    # 而 513050/513880/159981 因未达阈值被冻结在 -0.85~-0.95pp。总偏离 5.89pp。
    # 放宽到 0.005 档后 10 条腿全部参与，偏离降到 3.38pp（= 全腿全局最优），
    # 且**没有一条腿超配超过 0.1pp**。多 3 笔单换 2.5pp 跟踪误差，明显划算。
    #
    # 为什么也不「回选最严档」（v2.1 的做法，已废弃）：
    #   更严档的可行域 包含于 lvl=0 的可行域 => lvl=0 的 MILP 最优偏离**必然最小**。
    #   「取第一个 dev <= 最优+LEVEL_TOL_PP 的档位」= 主动用更差偏离换"假设中
    #   更少的笔数"，但 MILP 在档位内部只最小化偏离、从不最小化笔数，腿少 !=
    #   笔数少。2026-09-21 实测反例：0.00125 档 4 笔 / 换手 1,427 / 2.7834pp，
    #   lvl=0 是 3 笔 / 换手 779 / 2.7825pp —— 三项全面更优却被舍弃。
    #   另外 LEVEL_TOL_PP=0.3pp 在 20 万组合下约 600 元跟踪误差，只为省不到
    #   1 元佣金，明显过大。
    # 现做法：
    #   1) 先求全局最优 best_dev，用 MIN_GAIN_PP 判「值不值得调」；
    #   2) ceil_dev = min(best_dev + DEV_TIE_PP, before_dev - MIN_GAIN_PP)，
    #      DEV_TIE_PP < MIN_GAIN_PP 保证被选中的解仍满足净改善门槛；
    #   3) 在「偏离 <= ceil_dev」的等价池里，对每档跑一次**两阶段 MILP**
    #      （固定偏离上界，改最小化笔数），得到严格意义上并列解里笔数最少的；
    #   4) 最终按 (笔数, 偏离, 换手金额) 升序选。
    #      **笔数优先于偏离**：容差 0.02pp 在 20 万组合下仅约 40 元配置偏差，
    #      而少一笔单省的是实打实的操作负担（用户对多笔清单执行率一贯很低）。
    #      **偏离优先于换手**：换手 4,000 元只省约 6 元佣金，却可能换来
    #      0.015pp 约 30 元的配置偏差 —— 2026-09-24 性质测试实测，把换手排在
    #      偏离前面会让 60 组里 9 组的偏离无谓变差，故调回。
    if not results:
        return _no_trade(positions, raw_codes, cash, before_w, before_dev,
                         span, method, min_dev)

    best_dev, best_lvl = min((r[1], r[0]) for r in results)
    if before_dev - best_dev < MIN_GAIN_PP:
        # 全局最优都不够本 -> 不折腾；min_dev_used 报实际达到最优的档位
        return _no_trade(positions, raw_codes, cash, before_w, before_dev,
                         span, method, best_lvl)

    # 档位上界同时受「偏离容差」与「净改善下限」约束，取严者
    ceil_dev = min(best_dev + DEV_TIE_PP, before_dev - MIN_GAIN_PP)
    tie_pool = [r for r in results if r[1] <= ceil_dev + 1e-9]
    if not tie_pool:                       # 兜底：容差过窄时退回全局最优
        tie_pool = [min(results, key=lambda r: r[1])]

    # 第二阶段：等价池内每档重解「笔数最少」；失败则沿用该档第一阶段的解
    refined = []
    for lvl, dev_pp, lots_after, trade, cash_after, frozen in tie_pool:
        cand = (lvl, dev_pp, lots_after, trade, cash_after)
        if _HAS_SCIPY:
            try:
                sol2 = _solve_milp(codes, k0, resid, prices, targets, total,
                                   cash, target_cash, frozen,
                                   dev_ceil=ceil_dev / 100.0, objective="trades")
            except Exception:  # noqa: BLE001
                sol2 = None
            if sol2 is not None:
                lots2, trade2, _ = sol2
                if not any(trade2[c][0] > 0 and trade2[c][1] > 0 for c in codes):
                    cash2 = (cash
                             - sum(trade2[c][0] * LOT * prices[c] * (1 + FEE)
                                   for c in codes)
                             + sum(trade2[c][1] * LOT * prices[c] * (1 - FEE)
                                   for c in codes))
                    if cash2 >= -1e-6:
                        pos2 = {c: lots2[c] * LOT + resid[c] for c in codes}
                        dev2 = deviation(pos2, cash2) * 100
                        # 仅在笔数**严格更少**时才替换：第二阶段只保证
                        # dev <= ceil_dev，不保证偏离仍是该档最优；无条件替换
                        # 会丢掉第一阶段的全局最优（2026-09-24 实测退化 3/60）。
                        if (dev2 <= ceil_dev + 1e-9
                                and n_trades(trade2) < n_trades(trade)):
                            cand = (lvl, dev2, lots2, trade2, cash2)
        refined.append(cand)

    lvl, dev_pp, lots_after, trade, cash_after = min(
        refined, key=lambda r: (n_trades(r[3]), r[1], turnover(r[3])))

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
