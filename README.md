# AlphaQuant

个人量化信号生成系统：**QDII 溢价套利 + 三层组合**（逆波动底仓 × PB 估值门控 × QDII 溢价门控）。
免费数据源（东财/新浪/乐咕/腾讯），每日定时产出「明日目标持仓 + 动作清单」，人工执行。

**核心结论**：三层组合样本内 **夏普 1.63 / 年化 +8.20% / 回撤 −5.2%**
（2020-08~2026-08，已扣 0.15% 单边成本，T→T+1 无前视）。

> ⚠️ 本项目是**信号系统，不是自动交易系统**：不接券商账户，所有下单由人执行。
> 回测为样本内验证，QDII 存在限购/净值滞后/T+2 等实盘风险，自负盈亏。

## 快速开始

```bash
# 环境：WSL Ubuntu 26.04，Python 3.14（/usr/bin/python3）
cd ~/workspace/alpha_quant
pip install -r requirements.txt        # 或 pip install -e .

# 手动跑一次完整链路（等价于定时任务）
python3 scripts/qdii_daily.py

# 只看组合信号（跳过刷新，用本地缓存）
python3 scripts/portfolio_live.py --no-refresh
```

产物在 `runs/`：
- `runs/portfolio_live.md` —— **每日必读**：明日目标持仓 + 动作清单（见 `TRADING_GUIDE.md`）
- `runs/qdii_premium.md` / `runs/qdii_backtest.md` —— 监控快照与回测刷新

## 定时任务（crontab）

```cron
30 21 * * 1-5 cd /home/hyz0906/workspace/alpha_quant && /usr/bin/python3 scripts/qdii_daily.py >/dev/null 2>>logs/qdii_daily.err.log
```

工作日 **21:30** 自动跑三步（监控 → 净值刷新 → 组合信号）。
选 21:30 而非 15:30：东财晚间才更新 QDII 当日净值，21:30 跑可把门控信息
滞后从 2 天降到 0~1 天（详见 `ARCHITECTURE.md` §2）。

## 文档索引

| 文档 | 内容 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统架构：数据流、模块清单、定时任务编排 |
| [Design.md](Design.md) | 策略设计：三层组合原理、回测口径、已证伪方向 |
| [USAGE.md](USAGE.md) | 使用说明：安装、配置、各脚本用法、产物解读 |
| [UNIVERSE.md](UNIVERSE.md) | 标的清单：18 只池、6 只 QDII 底层映射、数据文件 |
| [TRADING_GUIDE.md](TRADING_GUIDE.md) | 人工交易指导：信号解读、动作执行、实盘注意事项 |
| [WORKFLOW.md](WORKFLOW.md) | 完整开发/验证日志（§7.x 全部实验 + §9 收口结论） |
| [AGENTS.md](AGENTS.md) | 面向 AI 编码代理的项目上下文（生产链分层 / 编码约定 / 不得修正的口径） |
| [STRATEGY_PLAN.md](STRATEGY_PLAN.md) | 策略体系全景：10 策略矩阵、已证伪方向、剩余增强 |
| [Background.md](Background.md) | 项目背景与演进史（蓝图 → 证伪 → 三层组合） |

## 目录结构

```text
alpha_quant/
├── scripts/                 # 生产链 12 个脚本（编排/监控/回测/信号）+ archive/ 研究归档
├── src/
│   ├── data_engine/         # qdii_calc.py（影子 IOPV 溢价、relchange_zscore）——生产链在用
│   ├── backtest/ strategies/ execution/ database/ dashboard/ llm_agent/   # RSRS 时代遗产
├── config/                  # pydantic-settings 配置
├── data/                    # ETF 日线 + 基本面缓存（QDII 溢价/乐咕估值/蛋卷面板），.gitignore
├── runs/                    # 每日信号 + 研究报告（.md/.json）
├── logs/                    # qdii_daily.log 定时任务日志
└── tests/                   # pytest（10 个用例，全绿）
```

## 数据源

| 数据 | 通道 | 用途 |
|---|---|---|
| QDII 净值 / IOPV / 折价率 | 东方财富 akshare | 溢价序列、监控告警 |
| ETF 前复权收盘 | vibe broker（腾讯 qfq） | 组合面板 |
| 全球指数 / 汇率 | 新浪 / 中行 akshare | 影子 IOPV |
| 沪深300 PE/PB | 乐咕 | PB 门控 |
| 蛋卷估值面板 | 蛋卷 VIP（cookie 在 `data/.danjuan_cookie`） | 截面研究（已归档） |

## 测试

```bash
python3 -m pytest tests/ -q    # 10 passed（门控状态机、权重、LLM、DB）
```

## 免责声明

本项目仅用于个人研究与学习。所有信号仅供参考，不构成任何投资建议。
