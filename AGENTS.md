# AGENTS.md

面向 AI 编码代理的项目上下文。请先通读本文件再动手改代码。

## 项目定位

AlphaQuant：个人独立开发者的 Quantamental 量化投研系统。三大核心能力：

1. **RSRS 趋势择时**（`src/strategies/factors/rsrs.py`）——向量化实现，用于 A 股行业 ETF 轮动
2. **QDII ETF 溢价监控**（`src/data_engine/qdii_calc.py`）——影子 IOPV 计算，溢价超阈值报警
3. **LLM 基本面分析**（`src/llm_agent/`、`src/analysis/`）——DeepSeek API 解析研报，输出结构化情感分数，与 RSRS 信号融合

策略原理见 `Background.md`；任务拆解与实现规范见 `Design.md`（任务 ID：P1-xx / P2-xx / P3-xx）。**新功能开发优先对齐 Design.md 中的任务编号与 Spec**，不得随意偏离既定目录结构。

## 运行环境（重要）

- 代码运行于 **WSL Ubuntu 26.04** 内，解释器只有 **Python 3.14**（`/usr/bin/python3`）。所有命令在 WSL shell 中执行：`cd ~/workspace/alpha_quant && python3 ...`
- 仓库里的 `cpython-312` 字节码缓存是旧环境遗留，已无对应解释器，可忽略。
- **依赖现状（user site-packages）**：已装 pandas 2.3.3、numpy 2.4.6、scipy、sqlalchemy 2.0.51、tushare、akshare、loguru、matplotlib、duckdb、pyarrow、pydantic-settings、vibe-trading-ai 0.1.14；**未装** backtrader、redis、streamlit、pymupdf/pymupdf4llm。
  主链路（fetch_data/calc_factors/gen_signals/backtest）**不再依赖** backtrader/redis/tushare-token：回测走 vibe ChinaAEngine，redis 已改惰性导入（仅 monitor 告警推送用到，缺失时降级为日志）。
- **禁止**用 Windows 侧 Python 直接运行本项目（路径解析会出错）。从 Windows 访问文件走 `\\wsl.localhost\Ubuntu-26.04\home\hyz0906\workspace\alpha_quant`。
- **从 Windows 侧驱动 WSL（免切终端，已验证可用）**：在 Git Bash 里
  `export MSYS_NO_PATHCONV=1 && wsl.exe -- bash -c 'cd ~/workspace/alpha_quant && python3 main.py ...'`。
  复杂脚本**先写成文件再执行**（写到 `\\wsl.localhost\...\home\hyz0906\xxx.py`
  后 `python3 ~/xxx.py`），heredoc 经 wsl.exe 传递会破坏转义。
  PowerShell 5.1 会吃掉引号和反斜杠，**不要用**它调 wsl。详见 `WORKFLOW.md` §3.1。
- 配置经 `config/settings.py`（pydantic-settings）读取 `.env`。所需键：`DEEPSEEK_API_KEY`、`TUSHARE_TOKEN`、`DATABASE_URL`（默认 `sqlite:///./alphaquant.db`）、`REDIS_URL`。`.env` 不入库。
- Docker（PG/Redis）compose 文件存在但**默认未启用**，当前以 SQLite 为数据底座；Redis 不可用时监控循环降级为仅日志报警。

## 常用命令

```bash
# 安装依赖（在 WSL 内）
pip install -e .          # 或 pip install -r requirements.txt

# 初始化数据库
python3 scripts/init_db.py

# CLI 入口（vibe-trading 集成版，详见 WORKFLOW.md）
python3 main.py run_all       # 一条龙：拉数据→算因子→出信号→跑回测
python3 main.py fetch_data    # vibe 免token链(tencent,前复权)拉日线 -> csv+DB
python3 main.py calc_factors  # RSRS 因子计算（已实装，回写 market_data+factor_data）
python3 main.py gen_signals   # 截面轮动信号 top_k 等权 -> output/signals_*.json
python3 main.py backtest      # vibe ChinaAEngine 回测(ETF 免印花税) -> runs/
python3 main.py monitor       # QDII 溢价监控死循环（纳指 ETF 513100，3s 轮询）

# 独立模块
PYTHONPATH=. python3 src/execution/qmt_gateway.py  # Mock 执行网关

# 测试
python3 -m pytest tests/test_rsrs.py tests/test_db_connect.py
```

测试注意：`test_rsrs.py`、`test_db_connect.py` 可离线跑；`test_llm.py` 需 DeepSeek Key，`test_data_engine.py` 需 Tushare Token。

## 目录结构

```text
config/            # settings.py（env 读取）、logging_config.py（loguru）
src/
├── analysis/      # llm_agent.py：ResearchAgent
├── backtest/      # vibe_exporter.py（vibe 回测导出器）、engine.py（Backtrader 旧路，未装依赖）
├── data_engine/   # vibe_market_loader.py（主数据源）、tushare_loader.py（备用）、qdii_calc.py
├── database/      # models.py（MarketData、FactorData）、connection.py
├── execution/     # qmt_client.py（Mock/Real Trader）、qmt_gateway.py
├── llm_agent/     # crawler.py、converter.py（PDF→Markdown）、analyzer.py
├── strategies/    # factors/rsrs.py（canonical）、signal_generator.py（轮动+融合）
└── dashboard/     # app.py（Streamlit，Phase 3）
scripts/           # init_db.py、verify_flow.py、vibe_fetch_broker.py（vibe 数据桥接子进程）
data/              # vibe 拉取的日线 csv + vibe_cache/（loader cache 注入）
runs/              # vibe 回测 run_dir（config.json + code/signal_engine.py + 结果）
output/            # 信号 JSON
tests/             # pytest 测试
WORKFLOW.md        # 集成工作流权威文档（调用顺序/IO依赖/替代方案）
```

## 编码约定

- **因子计算必须向量化**：用 `numpy.lib.stride_tricks.sliding_window_view` / pandas rolling，禁止逐行 Python 循环（参见 `rsrs.py` 的实现范式）。
- **DB 写入必须处理主键冲突**：`MarketData`/`FactorData` 主键为 `(ts_code, trade_date)`，写入用 upsert 逻辑。
- **LLM 调用必须用 JSON Mode**（`response_format={"type": "json_object"}`），保证输出可程序化解析；Client 走 `openai` SDK 兼容层指向 `https://api.deepseek.com`。
- **交易指令先入 Redis 队列**（`trade_instruction_queue`）再由执行端消费，策略层与执行层解耦。
- 新增依赖同步更新 `requirements.txt` 与 `pyproject.toml`。
- 日志统一用 loguru（`config.logging_config.setup_logging()`），不要裸 print。

## 核心算法备忘

- **RSRS**（Design.md [P1-03]）：N=18 窗口 OLS 回归 `High = β×Low + α`，β 取斜率；M=600 日 Z-Score 标准化；**修正分 `rsrs_zscore = z × R²`（RSRS-Right 标准定义，全项目统一语义）**。signal_generator 的 ±0.7 阈值针对修正分设计。
- **IOPV**（[P2-01]）：`IOPV_now = NAV_last × (1 + Future%) × Rate_now / Rate_base`，`Premium = Price / IOPV - 1`。溢价阈值：>3% 报警。
- **信号融合**（[P3-03]）：`rsrs > 0.7 且 sentiment > 0.2 → STRONG_BUY`；技术面好但 sentiment < -0.2 → `DIVERGENCE_WATCH`。

## 外部工具：Vibe-Trading（vibe-trading-ai 0.1.14）

WSL 内已安装（Python 3.14 user site-packages，源码 `~/source/Vibe-Trading`），命令 `vibe-trading` / `vibe-trading-mcp`。可**脱离 LLM** 当作「数据源 + 回测引擎」使用。**本项目已深度集成**（数据桥接 + loader cache 注入 + ChinaAEngine 回测，见 `src/backtest/vibe_exporter.py` 与 `WORKFLOW.md`），不要重复造轮子。

- **取数**（已封装为 `main.py fetch_data`）：`fetch_market_data(codes, start_date, end_date, source="auto", max_rows=0)`。A 股回退链 `tencent → mootdx → eastmoney → baostock → akshare → tushare`。**必须在 cwd=$HOME 的子进程里调**（`scripts/vibe_fetch_broker.py`），否则 `import src` 解析到本项目 src 包。
- **回测**（已封装为 `main.py backtest`）：`python -m backtest.runner <run_dir>`。**引擎路由按 source 字段**：只有 `source ∈ {tushare, akshare}` 路由 ChinaAEngine；`local`/`tencent` 会错误落到 CryptoEngine！本项目解法：config 声明 `source="akshare"` + 把数据注入 loader cache（`VIBE_TRADING_DATA_CACHE=true`），缓存命中则不调 API 不需要 token。
- **坑（均为实测踩过，详见 `WORKFLOW.md` §6）**：
  1. **缓存通道必须是 akshare，不能是 tushare** —— tushare 的 `is_available()`
     要求 TUSHARE_TOKEN，无 token 时被 registry 跳过，注入的缓存永远读不到，
     引擎会真去调 akshare 拿**不复权**数据（512480 同日收盘 0.352 vs 0.703）。
     且这种情况**不报错，只是结果全错**。
  2. **tencent loader 有 500 根静默截断**（vibe bug）：腾讯接口窗口内 K 线
     >500 时返回"end_date 往前 500 根"，vibe 的分页因此立即 break。broker 已
     按 `--slice-days 365` 分段拉取绕开（2021-2026 可拿 1327 根）。
  3. **`evaluation_start_date` 在 0.1.14 不存在**（那是 `~/source/Vibe-Trading`
     新版源码的字段），因 `extra="allow"` 被静默忽略。评估窗口靠
     signal_engine 的 `EVAL_START` 门槛实现；评估窗口指标看
     `runs/<name>/evaluation_metrics.json`（vibe 自家指标是全区间口径）。
  4. `position_adjustment` 必须 `"hold"`（`rebalance` 遇 T+1 必然抛
     `insufficient capital for position rebalance`）。
  5. `fetch_market_data` 返回 **`list[dict]`**（非 DataFrame），日期字段名为
     `trade_date`；`max_rows` 默认 250 会触发**等距降采样**，务必传 0。
  6. ETF 免印花税，config 须显式 `stamp_tax=0`（exporter 已内置）。
  7. `signal_engine.py` 受 AST 沙箱约束（禁顶层可执行语句、禁写文件、禁网络）。
  8. 顶层包 `src`/`backtest` 与本项目同名，vibe 子进程一律 cwd=$HOME。
  9. run_dir 有白名单，须设 `VIBE_TRADING_ALLOWED_RUN_ROOTS`。
  10. `~/source/Vibe-Trading` 源码**新于**已安装的 0.1.14，查问题时以
      site-packages 里的实际文件为准（行号对不上）。
- **验证要点**：每次首跑后核对 `runs/<name>/artifacts/ohlcv_*.csv` 是否与
  `data/*.csv` 一致，确认引擎没偷偷改用不复权数据。

## 已知未完成项

- ~~`main.py` 的 `cmd_calc_factors` 为 stub~~（2026-08-30 已实装）
- MiniQMT 实盘执行（`RealTrader`）未接入，当前仅 Mock
- Streamlit Dashboard（Phase 3 [P3-04]）待完善（streamlit 未安装）
- `qdii_calc.py` 影子 IOPV 的期货涨幅/汇率仍为 stub
- ~~vibe 集成链路待 WSL 内首跑验证~~（2026-08-31 已通过，见 `WORKFLOW.md` §7.2）
- **策略有效性未证**：当前参数下评估窗口超额仅 +6.20%（基准 +176.59%），
  需更长窗口 + 显著性检验，见 `WORKFLOW.md` §9
