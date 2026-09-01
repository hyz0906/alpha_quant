# AlphaQuant 使用说明

> 覆盖：安装、配置、定时任务、各脚本用法、产物解读。标的池见 `UNIVERSE.md`，
> 交易执行见 `TRADING_GUIDE.md`。

## 1. 环境要求

- **WSL Ubuntu 26.04**（代码在 WSL 内运行；Windows 侧仅作文件访问/编辑）
- Python 3.14（`/usr/bin/python3`）；**不要**用 Windows 侧 Python 直接跑本项目
- 依赖安装（推荐 `requirements.txt`，已钉死生产实测版本）：

```bash
cd ~/workspace/alpha_quant
pip install -r requirements.txt
# 或可编辑安装：pip install -e .  （pyproject 为下界镜像 + legacy 可选组）
```

## 2. 配置

| 配置项 | 位置 | 说明 |
|---|---|---|
| 环境变量 | `.env`（不入库） | `DEEPSEEK_API_KEY`（LLM 研究用，非生产链）、`TUSHARE_TOKEN`（已弃用） |
| 蛋卷 cookie | `data/.danjuan_cookie` | 仅截面研究脚本用（`scripts/archive/danjuan_*`），生产链不需要 |

生产链（监控/刷新/信号）**不需要任何密钥**，全部走免费公开数据源。

## 3. 定时任务

crontab（`crontab -e` 查看/编辑）：

```cron
30 21 * * 1-5 cd /home/hyz0906/workspace/alpha_quant && /usr/bin/python3 scripts/qdii_daily.py >/dev/null 2>>logs/qdii_daily.err.log
```

- 工作日 21:30 触发，节假日空跑无害（数据无变化）
- 日志：`logs/qdii_daily.log`（脚本内写，时间戳 + 每步成败 + 摘要）；
  未捕获异常落 `logs/qdii_daily.err.log`
- 手动触发：`python3 scripts/qdii_daily.py [--skip-backtest] [--skip-portfolio] [--skip-paper]`
- 迁移机器后：crontab 中的绝对路径 `/home/hyz0906/workspace/alpha_quant` 需按实际改

## 4. 脚本用法

### 4.1 每日必跑：`scripts/qdii_daily.py`

四步编排入口（21:30 自动跑，也可手动）：

| 步骤 | 脚本 | 产出 |
|---|---|---|
| ① 监控快照 | `qdii_monitor.py` | `runs/qdii_premium.md/.json` |
| ② 回测刷新 | `qdii_backtest.py --refresh` | `runs/qdii_backtest.md/.json` + 溢价缓存 |
| ③ 组合信号 | `portfolio_live.py` | `runs/portfolio_live.md/.json` |
| ④ 模拟盘对账 | `paper_trading.py reconcile` | `runs/paper_trading.md` + `data/paper_nav.csv` |

### 4.1b 模拟盘：`paper_trading.py`

20 万模拟盘账本（账本 `data/paper_ledger.json`，估值价 = `data/*.csv` 最新
收盘价，佣金 0.15% 单边，与回测口径一致）：

```bash
python3 scripts/paper_trading.py entry-guide --capital 200000   # 建仓指导 → runs/paper_entry_guide.md
python3 scripts/paper_trading.py init --capital 200000          # 初始化账本（已有需 --force 重置）
python3 scripts/paper_trading.py buy --code 511010.SH --shares 900   # 买入（100 份整数手）
python3 scripts/paper_trading.py sell --code 511010.SH --shares 300  # 卖出
python3 scripts/paper_trading.py reconcile                       # 估值 + 对账（自动比对目标权重）
python3 scripts/paper_trading.py status                          # 账本视图
```

对账报告 `runs/paper_trading.md`：实际权重 vs `portfolio_live.json` 目标权重，
偏差 > 2pp 且金额超阈值才提示调仓（不足一手的偏差会标注「保持现金」）；
净值历史 `data/paper_nav.csv` 每日追加。模拟盘建仓建议先跑 `entry-guide`
拿到按整数手折算的份额表，再逐笔 `buy` 录入。

### 4.2 组合信号：`portfolio_live.py`

```bash
python3 scripts/portfolio_live.py            # 正常：先刷新数据再算信号
python3 scripts/portfolio_live.py --no-refresh  # 调试：仅用本地缓存
```

输出 `runs/portfolio_live.md`，含三节：
1. **三层门控状态**：PB 分位/档位 + 6 只 QDII 的溢价/z/今日明日持仓
2. **明日目标持仓**：18 只池目标权重表（含现金）
3. **动作清单**：较上一快照 ≥0.5pp 的变动（归因：QDII 门控 > PB 调档 > 月度再平衡 > 漂移）

### 4.3 监控快照：`qdii_monitor.py`

```bash
python3 scripts/qdii_monitor.py
```

- 官方溢价（东财 IOPV 折价率取负）+ 影子溢价（折入底层指数 + 汇率最新一跳）
- 相对变化告警：溢价一阶差分 60 日 z（滞后 1 期口径，与回测一致）
- z≥+2 → 🔺 溢价飙升；z≤−2 → 🔻 溢价回落
- 输出 `runs/qdii_premium.md/.json`

### 4.4 回测刷新：`qdii_backtest.py`

```bash
python3 scripts/qdii_backtest.py --refresh   # 增量刷新净值（末日期−14 天窗口）+ 重算回测
python3 scripts/qdii_backtest.py             # 仅用缓存重算
```

- 增量刷新：6 只 QDII 净值（东财）+ 收盘价（新浪），inner join 保证溢价口径纯净
- 刷新失败/异常 → 沿用旧缓存（降级容忍），不中断第 ③ 步

### 4.5 研究脚本（非每日）

| 脚本 | 用途 | 产出 |
|---|---|---|
| `strategy_matrix.py` | 10 策略统一横评（新策略准入基准） | `runs/strategy_matrix.md/.json` |
| `portfolio_combined.py` | 三层组合消融回测（A/B/C/D） | `runs/portfolio_combined.md` |
| `value_timing_backtest.py` | PB 门控独立回测 | `runs/value_timing_*.md` |
| `qdii_relchange_*.py` | 相对变化门控回测 | `runs/qdii_relchange*.md` |
| `scripts/archive/*.py` | 一次性研究（截面 IC/动量/周频/面板） | 已归档，按需运行 |

## 5. 产物与缓存

### 5.1 `runs/`（每日更新）

| 文件 | 内容 |
|---|---|
| `portfolio_live.md/.json` | **明日目标持仓 + 动作清单**（交易依据） |
| `qdii_premium.md/.json` | QDII 溢价监控快照 + 告警 |
| `qdii_backtest.md/.json` | 溢价回避/折价买入策略绩效 |
| `strategy_matrix.md` | 全策略横评（重跑时更新） |

### 5.2 `data/`（缓存，勿手改，脚本自动维护）

```
data/<code>.csv                              18 只池前复权日线
data/fundamental/qdii_premium_<code>.csv     QDII 溢价序列
data/fundamental/legu_metrics_<code>.csv     乐咕 PE/PB 月频
data/fundamental/danjuan_valuation*.csv      蛋卷估值面板（研究）
```

### 5.3 数据新鲜度自检

- ETF 收盘缓存末尾距今 >3 天 → 报告标 ⚠️ 数据滞后
- QDII 溢价缓存滞后 >5 天 → 快照 JSON/报告/控制台三处告警（正常滞后 ≤3 天：
  净值 T+1~T+2 公布）
- 定时任务日志尾部有每步耗时与成败；`logs/qdii_daily.err.log` 有未捕获异常

## 6. 常见问题

**Q：为什么 21:30 才跑？**
东财晚间才更新 QDII 当日净值。15:30 跑时欧美腿净值只到 T-2（实盘夏普
1.63→~1.2），21:30 跑到 T-1~T。代价是监控告警从盘中变盘后。

**Q：QDII 溢价缓存为什么停在好几天前？**
净值 T+1~T+2 晚公布是基金公司披露节奏，属设计内滞后；>5 天才算异常告警。

**Q：`--refresh` 每次拉全量吗？**
不。增量刷新只拉「缓存末日期 − 14 天」窗口，避免全量 8 年请求与限流。

**Q：为什么不能用 Windows 侧 Python 跑？**
路径解析（`\\wsl.localhost\...` vs `C:\...`）与 akshare 依赖均在 WSL 侧验证，
跨边界跑会因路径/编码报错。请始终在 WSL shell 内执行。
