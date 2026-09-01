# AGENTS.md

面向 AI 编码代理的项目上下文。**请先通读本文件再动手改代码**，尤其注意
§8「不得修正的口径」——项目里有几处"看似 bug、实为刻意"的设计。

## 1. 项目定位

AlphaQuant：个人量化**信号生成系统**——免费数据源 → 三层组合信号 → 人工执行。

- **现行实盘口径 = 三层组合**：逆波动底仓（18 只异构 ETF）× PB 估值门控（沪深300，
  A 股 7 腿） × QDII 溢价门控（6 只 QDII 腿）。样本内夏普 1.31 / 回撤 −5.4%
  （2020-08~2026-08，已扣 0.15% 单边成本，T→T+1 无前视）。
- **不是自动交易系统**：不接券商账户，所有下单由人执行（`TRADING_GUIDE.md`）。
- 每日 21:30 定时任务产出 `runs/portfolio_live.md`（明日目标持仓 + 动作清单）。

策略原理见 `Design.md`；系统形态见 `ARCHITECTURE.md`；使用见 `USAGE.md`；
标的池见 `UNIVERSE.md`；人工交易见 `TRADING_GUIDE.md`；完整验证历史见
`WORKFLOW.md` §7.x。`Background.md` 是项目演进背景，`STRATEGY_PLAN.md` 是
策略体系全景与已证伪方向。

**代码库分层**（重要，改代码前先分清）：
- **生产链**（每日在跑，改动需谨慎）：`scripts/qdii_daily.py` 编排 +
  `qdii_monitor.py` / `qdii_backtest.py` / `portfolio_live.py`，共享库
  `risk_parity.py` / `qdii_relchange_realistic.py` / `qdii_relchange_backtest.py`，
  数据桥 `vibe_fetch_broker.py`，核心计算 `src/data_engine/qdii_calc.py`。
- **研究脚本**（可复跑，非每日）：`strategy_matrix.py`（10 策略横评，新策略准入）、
  `portfolio_combined.py`（三层消融）、`value_timing_backtest.py`（PB 独立回测）。
- **历史遗产**（RSRS 路线，已证伪，不在生产链）：`src/backtest/`、
  `src/strategies/`、`src/execution/`、`src/database/`、`src/dashboard/`、
  `src/analysis/`、`src/llm_agent/`、`main.py`、`scripts/init_db.py`、`scripts/archive/`（18 个一次性研究脚本）。

## 2. 运行环境

- 代码运行于 **WSL Ubuntu 26.04**，解释器 **Python 3.14**（`/usr/bin/python3`）。
  所有命令在 WSL shell 中执行：`cd ~/workspace/alpha_quant && python3 ...`
- **禁止**用 Windows 侧 Python 直接运行本项目（路径解析会出错）。从 Windows
  访问文件走 `\\wsl.localhost\Ubuntu-26.04\home\hyz0906\workspace\alpha_quant`。
- 从 Windows 侧驱动 WSL（Git Bash）：
  `export MSYS_NO_PATHCONV=1 && wsl.exe -- bash -c 'cd ~/workspace/alpha_quant && python3 ...'`。
  复杂脚本先写成文件再执行（`\\wsl.localhost\...\home\hyz0906\xxx.py` →
  `python3 ~/xxx.py`），heredoc 经 wsl.exe 传递会破坏转义；PowerShell 5.1
  会吃引号，不要用。详见 `WORKFLOW.md` §3.1。
- 依赖：`pip install -r requirements.txt`（已钉死生产实测版本）或
  `pip install -e .`。生产链**不需要任何密钥**；`.env` 里的
  `DEEPSEEK_API_KEY`（LLM 研究用，非生产链）、`TUSHARE_TOKEN`（已弃用）均可空。
- 配置经 `config/settings.py`（pydantic-settings）读取 `.env`。
- Docker compose（PG/Redis）存在但默认未启用；`alphaquant.db`（SQLite）已基本弃用。

## 3. 常用命令

```bash
cd ~/workspace/alpha_quant

# —— 生产链 ——
python3 scripts/qdii_daily.py                 # 每日编排：①监控→②净值刷新+回测→③组合信号
python3 scripts/qdii_daily.py --skip-backtest --skip-portfolio   # 只跑监控
python3 scripts/portfolio_live.py             # 组合信号（明日目标持仓+动作清单）→ runs/portfolio_live.md
python3 scripts/portfolio_live.py --no-refresh# 调试：仅用本地缓存
python3 scripts/qdii_monitor.py               # QDII 溢价快照 → runs/qdii_premium.md/.json
python3 scripts/qdii_backtest.py --refresh    # 净值增量刷新(末日期-14天窗口)+回测 → runs/qdii_*.md

# —— 研究（非每日）——
python3 scripts/strategy_matrix.py            # 10 策略统一横评（新策略准入基准，纯本地可复跑）
python3 scripts/portfolio_combined.py         # 三层组合消融回测 A/B/C/D
python3 scripts/value_timing_backtest.py      # PB 门控独立回测

# —— 历史入口（RSRS 时代，已证伪，不建议使用）——
python3 main.py run_all --codes ...           # RSRS 链路：fetch→factors→signals→backtest

# —— 测试 ——
python3 -m pytest tests/ -q                   # 10 passed（门控状态机/权重/LLM/DB/RSRS）
```

## 4. 目录结构

```text
scripts/            12 个脚本（生产链 11 + init_db 遗产工具，分层见 §1）+ archive/ 18 个研究归档
src/
├── data_engine/    qdii_calc.py —— 生产链核心计算（影子 IOPV 溢价、relchange_zscore、池映射）
├── backtest/ strategies/ execution/ database/ dashboard/ analysis/ llm_agent/   # RSRS 遗产
config/             settings.py（pydantic-settings）、logging_config.py
data/               缓存（ETF 日线 / qdii_premium_* / legu_metrics_* / 蛋卷面板），.gitignore
runs/               每日信号 + 研究报告（portfolio_live.md 每日必读）
logs/               qdii_daily.log（定时任务日志）+ qdii_daily.err.log
tests/              4 个测试文件（test_gates/test_rsrs/test_llm/test_db_connect）
```

## 5. 编码约定（生产链硬性要求）

1. **无前视是全系统底线**：任何信号 T 日计算、T+1 应用（`shift(1)` 语义）。
   回测与实盘口径必须逐字一致。
2. **门控空缺 = 现金，不重新归一**：腿被门控减出 = 持币（收益 0），权重不摊给其他腿。
3. **降级容忍优于硬失败**：生产脚本对数据缺失/网络错误全部 try/except + 沿用旧缓存
   + 报告标注；缓存写入**原子写**（临时文件 + `os.replace`）+ 覆盖前 sanity check
   （行数不减少、末日期不倒退）。
4. **数据滞后显式告警**：ETF 收盘 >3 天（`STALE_DAYS`）、QDII 溢价 >5 天
   （`PREM_STALE_DAYS`）→ 快照 JSON + 报告 + 控制台三处提示。
5. **研究脚本改生产脚本前先过 `strategy_matrix.py` 准入**：新策略先统一口径横评
   （18 池公共样本、单边 0.15% 成本、T→T+1），再谈上线。
6. **报告数字一律 f-string 数据驱动**：研究报告里禁止硬编码叙事性数字
   （绩效/回撤/换手必须来自回测结果变量）。
7. 日志统一 loguru（`config.logging_config.setup_logging()`），不要裸 print。
8. 新增依赖同步更新 `requirements.txt` 与 `pyproject.toml`。
9. **生产脚本 ROOT 用 `__file__` 推导**（`parents[2]`），禁硬编码绝对路径。

## 6. 核心算法备忘（生产链）

- **逆波动底仓**（`risk_parity.build_weights`）：月末前 60 交易日波动率倒数归一化，
  次月持有不变（ffill）。池 = `HETERO_CODES`（18 只，见 UNIVERSE.md）。
- **PB 门控**（`portfolio_live.py`）：沪深300 PB（乐咕月频）5 年滚动分位（窗口 60 月、
  最小 36），三档：<30% 全仓 / 30~70% 半仓 / ≥70% 空仓。作用于 7 只 A 股腿。
- **QDII 门控**（`qdii_relchange_realistic.py` 的 `spike_avoid_hold`）：
  `relchange_zscore(premium)`（溢价一阶差分 60 日滚动 z，滚动均值/标准差**滞后 1 期**）；
  参数 floor=1%（z 高但溢价低不触发）、min_hold=5（空仓最短 5 日，贴合 QDII T+2 到账）、
  z=+2。z>+2 且溢价>1% → 空仓；z≤+2 且空仓≥5 日 → 回补。
- **溢价序列**（`qdii_backtest.py` 刷新）：净值 × 价格 inner join，
  `close_qfq = close × (累计净值/单位净值) / 末因子`。增量刷新只拉「缓存末日期 − 14 天」窗口。
- **影子 IOPV**（`qdii_calc.py`）：`IOPV_now = NAV_last × (1 + Future%) × Rate_now / Rate_base`，
  底层指数映射见 `QDII_UNDERLYING`（纳指→.IXIC、标普→.INX、中概→.IXIC、德国→DAX、
  恒生→HSI、日经→N225），汇率为中行日频牌价。

## 7. Vibe-Trading 集成要点（仅 `vibe_fetch_broker.py` 在用）

- 取数封装为 broker 子进程（**cwd=$HOME**，规避顶层包 `src`/`backtest` 同名冲突）：
  `fetch_market_data(codes, start, end, source="auto", max_rows=0)`，回退链
  tencent→mootdx→eastmoney→baostock→akshare→tushare，前复权（qfq）。
- **tencent loader 有 500 根静默截断**（vibe bug）：broker 按 `--slice-days 365`
  分段拉取绕开（2021-2026 可拿 1327 根）。**务必传 `max_rows=0`**（默认 250 会等距降采样）。
- `fetch_market_data` 返回 **`list[dict]`**（非 DataFrame），日期字段名 `trade_date`。
- 数据正确性核验：拉完后对照 `data/<code>.csv` 与新浪/东财收盘价，确认没退化成不复权。
- 完整坑列表见 `WORKFLOW.md` §6（10 条实测 bug）。

## 8. 不得修正的口径（看似 bug，实为刻意设计）

1. **PB 门控 shift(1)**：t 月实际应用的是 t−1 月末分位（比直觉多滞后一个月）。
   乐咕数据月初才更新上月末点，实盘天然如此；回测 1.31 夏普就是这个口径跑出来的。
   **不要"修正"成当月分位**。
2. **QDII 门控实盘技巧**：`portfolio_live.py` 给溢价序列**末尾追加一行 NaN** 再跑
   状态机取末值——这是把「状态机处理完真实数据后的 state」当作 T+1 持仓的惯用法，
   与回测 T→T+1 语义一致。不是 bug。
3. **QDII 相对变化信号**（`relchange_zscore`）：2026 起纳指绝对溢价结构性 >3%
   （额度告罄），绝对阈值失效，改用一阶差分 z——`qdii_monitor.py` 的 z 口径必须与
   回测一致（滞后 1 期），不要改回绝对溢价告警。
4. **门控空缺不归一**：门控减出的资金 = 现金，**不要**重新归一给其他腿（见 §5.2）。
5. **21:30 定时**：东财晚间才更新 QDII 当日净值，21:30 跑可把门控滞后从 2 天降到
   0~1 天。**不要改回 15:30**（除非重新量化滞后代价，见 `scripts/archive/gate_lag_test.py`）。

## 9. 已知未完成项（不要重复造轮子）

- MiniQMT 实盘执行（`src/execution/qmt_client.py` RealTrader）未接入，仅 Mock；
  系统定位是信号生成，接不接实盘由用户决定。
- Streamlit Dashboard（`src/dashboard/app.py`）未动，streamlit 包未装。
- LLM 研报情感链（`src/llm_agent/`）需 API key，非生产链；融合信号已证伪/搁置。
- 蛋卷估值面板（`data/fundamental/danjuan_valuation*.csv`）仅研究用（截面 IC 已证伪），
  生产链不读；cookie 在 `data/.danjuan_cookie`。
- 可选增强（WORKFLOW.md §9 剩余项）：QDII 盘中实时轮询、折价买入信号接入提醒、
  券商账户实际持仓对账。
