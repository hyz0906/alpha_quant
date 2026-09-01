# AlphaQuant 架构文档

> 2026-09-01 更新。描述当前生产系统的真实形态（QDII 溢价套利 + 三层组合）。
> 本文档是「现状地图」；策略原理见 `Design.md`，运行方法见 `USAGE.md`，
> 标的池见 `UNIVERSE.md`，人工交易见 `TRADING_GUIDE.md`。

## 1. 系统总览

AlphaQuant 是一个个人量化的**信号生成系统**：免费数据源 → 三层组合信号 →
人工执行。核心资产是**三层组合**（逆波动底仓 × PB 估值门控 × QDII 溢价门控），
样本内夏普 1.31 / 回撤 −5.4%（2020-08~2026-08，已扣 0.15% 单边成本）。

系统边界（诚实声明）：

- **只产出「信号级」目标权重，未接券商账户**——交易由人执行（`TRADING_GUIDE.md`）。
- 回测为样本内验证；实盘存在净值滞后（QDII T+1~T+2 公布）、限购、冲击等执行风险。
- 代码库同时保留历史 RSRS 路线（`src/backtest/`、`src/strategies/rsrs*`、`main.py`）
  作为研究遗产，**不在生产链上**（见 §5 标注）。

```
┌────────────────────────────────────────────────────────────────────┐
│                        数据源（全部免费/公开）                        │
│  东财 akshare（净值/IOPV/spot） · 新浪（ETF收盘/全球指数/汇率/牌价）    │
│  乐咕乐咕（沪深300 PB 月频） · 腾讯 qfq（vibe broker 前复权收盘）      │
│  蛋卷 VIP 估值面板（研究用，cookie 在 data/.danjuan_cookie）           │
└───────────────┬────────────────────────────────────────────────────┘
                │ akshare / requests
                ▼
┌────────────────────────────────────────────────────────────────────┐
│                      数据缓存层（data/，全部 .gitignore）             │
│  data/<code>.csv                18 只池 ETF 前复权日线（vibe 增量）    │
│  data/fundamental/qdii_premium_*.csv  6 只 QDII 溢价序列（净值+价格） │
│  data/fundamental/legu_metrics_*.csv  乐咕 PE/PB 估值（月频）         │
│  data/fundamental/danjuan_valuation*.csv  蛋卷估值面板（研究）        │
└───────────────┬────────────────────────────────────────────────────┘
                ▼
┌────────────────────────────────────────────────────────────────────┐
│                    生产编排：scripts/qdii_daily.py                   │
│               crontab：工作日 21:30（30 21 * * 1-5）                 │
│   ① qdii_monitor.py     溢价监控快照 → runs/qdii_premium.md/.json   │
│   ② qdii_backtest.py --refresh  净值增量刷新+回测 → runs/qdii_*.md   │
│   ③ portfolio_live.py   三层组合明日信号 → runs/portfolio_live.md/.json│
│   ④ paper_trading.py    模拟盘对账 → runs/paper_trading.md + 净值曲线 │
└───────────────┬────────────────────────────────────────────────────┘
                ▼
┌────────────────────────────────────────────────────────────────────┐
│                         信号消费（人）                                │
│  阅读 runs/portfolio_live.md → 按动作清单人工下单（TRADING_GUIDE.md）；│
│  模拟盘 data/paper_ledger.json 每日自动对账（runs/paper_trading.md）  │
└────────────────────────────────────────────────────────────────────┘
```

## 2. 生产链路（每日定时任务）

`scripts/qdii_daily.py` 是唯一编排入口，工作日 **21:30** 由 crontab 触发，
四步用独立子进程跑、**任一步失败不影响其余步骤**（错误隔离），日志追加到
`logs/qdii_daily.log`：

| 步骤 | 脚本 | 产出 | 失败影响 |
|---|---|---|---|
| ① 监控快照 | `qdii_monitor.py` | `runs/qdii_premium.md/.json` | 仅告警缺失 |
| ② 回测刷新 | `qdii_backtest.py --refresh` | `runs/qdii_backtest.md/.json` + 溢价缓存 | ③ 用旧缓存（有滞后告警） |
| ③ 组合信号 | `portfolio_live.py` | `runs/portfolio_live.md/.json` | 当日无信号 |
| ④ 模拟盘对账 | `paper_trading.py reconcile` | `runs/paper_trading.md` + `data/paper_nav.csv` | 缺 ③ 时仅账本视图 |

**为什么 21:30**：东财晚间才更新 QDII 当日净值（15:30 时欧美腿净值只到 T-2、
亚洲腿 T-1，实测实盘夏普 1.31→~1.0）；21:30 时净值已补到 T-1~T（滞后降到
0~1 天，夏普 ~1.15~1.3）。代价：① 的监控从盘中变盘后。见 `scripts/archive/gate_lag_test.py`。

**失败模式与自愈**：
- 数据刷新全部「降级容忍」——拉取失败沿用旧缓存并在报告标注；
- 缓存写入全部**原子写**（临时文件 + `os.replace`）+ 覆盖前 sanity check；
- 数据滞后有显式告警：ETF 收盘 >3 天（`STALE_DAYS`）、QDII 溢价 >5 天
  （`PREM_STALE_DAYS`）→ 快照 JSON + 报告 + 控制台三处提示。

## 3. 数据源矩阵

| 数据 | 接口/通道 | 更新时点 | 用途 |
|---|---|---|---|
| QDII 单位/累计净值 | 东财 `fund_etf_fund_info_em` | T+1~T+2 晚（欧美 2 天、亚洲 1 天） | 溢价序列、门控 |
| ETF 盘中行情/IOPV/折价率 | 东财 `fund_etf_spot_em` | 盘中实时 | 监控快照、影子溢价 |
| 18 只池 ETF 前复权收盘 | vibe broker（腾讯 qfq，cwd=$HOME 子进程） | 15:00 后 | 组合面板、权重 |
| 美股/港股/DAX/日经指数 | 新浪/东财 akshare | 隔夜/当日 | 影子 IOPV 调整 |
| 汇率 | 中行牌价 `currency_boc_sina` | 日频 | 影子 IOPV 调整 |
| 沪深300 PE/PB | 乐咕 `stock_index_pe/pb_lg` | 月初更新上月末 | PB 门控 |
| 蛋卷估值面板 | 蛋卷 VIP 接口（需 cookie） | 周频 | 截面研究（已归档） |

## 4. 模块清单

### 4.1 生产脚本（`scripts/`）

| 脚本 | 角色 | 说明 |
|---|---|---|
| `qdii_daily.py` | 编排入口 | 四步串行、错误隔离、日志 |
| `qdii_monitor.py` | ① 监控 | 官方/影子 IOPV 溢价 + 相对变化 z 告警 |
| `qdii_backtest.py` | ② 刷新 | 净值增量刷新（末日期−14 天窗口）+ 溢价回避/折价买入回测 |
| `portfolio_live.py` | ③ 信号 | 三层门控状态 + 明日目标权重 + 动作清单 |
| `paper_trading.py` | ④ 对账 | 模拟盘账本（init/buy/sell/entry-guide/reconcile/status）|
| `portfolio_combined.py` | 研究 | 三层组合联合回测（A/B/C/D 消融） |
| `strategy_matrix.py` | 研究 | 10 策略统一横评（新策略准入基准） |
| `risk_parity.py` | 共享库 | 18 只池定义、逆波动/ERC 权重、指标 |
| `qdii_relchange_backtest.py` | 共享库 | 相对变化理想版状态机 |
| `qdii_relchange_realistic.py` | 共享库 | 实盘约束版 `spike_avoid_hold`（floor/min_hold） |
| `value_timing_backtest.py` | 研究 | PB 门控独立回测 |
| `vibe_fetch_broker.py` | 数据 | vibe broker 行情拉取封装 |
| `init_db.py` | 工具 | SQLite 初始化（RSRS 遗产用） |

### 4.2 src/ 核心（生产链实际 import）

- `src/data_engine/qdii_calc.py` —— QDII 影子 IOPV 溢价、`relchange_zscore`、池映射
- `config/settings.py` —— pydantic-settings 配置（`.env`）

### 4.3 src/ 其余 = 历史研究遗产（不在生产链，保留作存档）

| 模块 | 状态 |
|---|---|
| `src/backtest/`（engine/feeds/sliding_window/vibe_exporter） | RSRS 路线，已被 scripts/ 回测取代 |
| `src/strategies/`（rsrs_momentum、signal_generator、factors/） | RSRS 路线已证伪 |
| `src/execution/`（qmt_client/qmt_gateway） | MiniQMT mock，未接实盘 |
| `src/database/`、`src/data_engine/tushare_loader.py`、`src/dashboard/`、`src/analysis/` | 早期基建，`alphaquant.db` 已基本弃用 |
| `src/llm_agent/` | LLM 研报分析（需要 Key，非生产链） |
| `main.py` | RSRS CLI 入口（历史） |
| `scripts/archive/`（18 个） | 一次性研究脚本归档（截面 IC/动量/周频/面板抓取等） |

## 5. 缓存与产物（全部 .gitignore）

```
data/<code>.csv                    18 只池 ETF 前复权日线（键=裸代码如 510300.SH）
data/fundamental/qdii_premium_<code>.csv   QDII 溢价序列（nav/acc_nav/close/close_qfq/premium/ret）
data/fundamental/legu_metrics_<code>.csv   乐咕 PE/PB（date/close/pe_ttm/pb/pb_med…）
data/fundamental/danjuan_valuation*.csv    蛋卷估值面板（研究用）
data/.danjuan_cookie               蛋卷登录 cookie（不入库）
data/paper_ledger.json             模拟盘账本（现金/持仓/交易流水，原子写）
data/paper_nav.csv                 模拟盘净值历史（date/total/cash/market/day_ret/cum_ret）
runs/*.md /.json                   每日信号 + 研究报告
runs/paper_trading.md              模拟盘对账报告（vs portfolio_live 目标权重）
logs/qdii_daily.log                定时任务运行日志（+ .err.log 未捕获异常）
```

关键口径（写入文档前请先读代码，勿凭记忆修改）：
- 溢价序列 = 净值 × 价格 inner join，`close_qfq = close × (累计净值/单位净值) / 末因子`
- 缓存覆盖前校验行数不减少、末日期不倒退

## 6. 关键设计决策

1. **无前视是全系统底线**：所有信号 T 日计算、T+1 应用（`shift(1)` 语义）。
2. **门控空缺 = 现金**，不重新归一（减仓持币，不是挪仓）。
3. **降级容忍优于硬失败**：生产脚本对数据缺失/网络错误全部 try/except + 旧数据 + 告警。
4. **研究与生产分离**：研究脚本归档 `scripts/archive/`，生产链只保留 12 个脚本，
   交叉依赖受控（portfolio_live → portfolio_combined → risk_parity/qdii_backtest）。
5. **新策略准入**：任何新策略先过 `strategy_matrix.py` 统一口径横评，再谈上线。
