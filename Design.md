1.  **原子化任务 (Atomic Tasks)**：将大目标拆解为 AI 可独立执行的最小单元。
2.  **上下文锚定 (Context Anchoring)**：明确文件路径、依赖库版本和输入/输出接口。
3.  **伪代码即规范 (Pseudocode as Spec)**：对核心逻辑（如 IOPV 计算、RSRS 择时）提供数学到代码的直接映射，防止 AI 幻觉。

-----

# System Manifest: Quantamental-Pro (Full Stack Quant System)

**Target Executor**: Autonomous Coding Agent
**Project Root**: `/workspace/quant_system`
**Environment**: Python 3.10+, Docker (PostgreSQL/Redis)

## 0\. 全局配置与上下文 (Global Context)

### 0.1 技术栈锁定 (Tech Stack Lock)

  * **Data**: `tushare`, `akshare`, `sqlalchemy`, `psycopg2-binary`, `redis`
  * **Calc**: `pandas`, `numpy`, `scipy`
  * **Strategy**: `backtrader`
  * **AI**: `openai` (compatible SDK for DeepSeek), `pymupdf4llm`
  * **Web**: `streamlit` (Phase 3 Dashboard)
  * **Execution**: `xtquant` (Mocked for Linux dev env, Deployment on Windows)

### 0.2 目录结构规范 (Master Directory Tree)

Agent **必须** 严格遵守此文件结构，不得随意创建根目录文件。

```text
/workspace/quant_system
├── .env.example                # 环境变量模板
├── docker-compose.yml          # PG & Redis 基础设施
├── requirements.txt
├── config/
│   ├── settings.py             # 读取 env 并导出配置对象
│   └── logging_config.py       # 日志配置
├── src/
│   ├── database/
│   │   ├── models.py           # SQLAlchemy Schema
│   │   └── connection.py       # DB Session Manager
│   ├── data_engine/
│   │   ├── tushare_loader.py   # 历史数据同步
│   │   ├── realtime_feed.py    # AkShare 实时流
│   │   └── qdii_calc.py        # IOPV 计算器
│   ├── strategies/
│   │   ├── factors/
│   │   │   └── rsrs.py         # RSRS 因子库
│   │   └── signal_generator.py # 信号生成逻辑
│   ├── backtest/
│   │   ├── feeds.py            # 自定义 DataFeed
│   │   └── engine.py           # Backtrader 运行入口
│   ├── llm_agent/
│   │   ├── crawler.py          # 研报爬虫
│   │   └── analyzer.py         # DeepSeek 接口
│   └── execution/
│       ├── qmt_client.py       # MiniQMT 封装
│       └── risk_manager.py     # 风控模块
└── main.py                     # CLI 入口
```

-----

## Phase 1: Infrastructure & Data Logic (Task ID: P1-xx)

### [P1-01] 基础设施搭建

  * **Action**: 创建 `docker-compose.yml`。
  * **Spec**:
      * Service 1: `postgres:15` (Port 5432, User/Pass from env).
      * Service 2: `redis:7` (Port 6379).
  * **Action**: 创建 `src/database/models.py`。
  * **Schema Spec**:
      * `MarketData`: `ts_code(PK), trade_date(PK), open, high, low, close, vol`
      * `FactorData`: `ts_code(PK), trade_date(PK), rsrs_beta, rsrs_zscore, rsrs_r2`

### [P1-02] Tushare 数据管道

  * **File**: `src/data_engine/tushare_loader.py`
  * **Function**: `sync_daily_data(ts_code, start_date, end_date)`
  * **Logic**:
    1.  初始化 Tushare Pro API。
    2.  调用 `daily` 接口获取行情。
    3.  调用 `adj_factor` 获取复权因子。
    4.  计算**前复权 (Forward Adjusted)** 价格。
    5.  使用 `pandas.to_sql` (method='multi') 批量写入 `MarketData` 表。
    6.  **Constraint**: 必须处理重复主键冲突 (Upsert logic)。

### [P1-03] RSRS 因子核心算法 (向量化)

  * **File**: `src/strategies/factors/rsrs.py`
  * **Function**: `calculate_rsrs_vectorized(df: pd.DataFrame, N=18, M=600) -> pd.DataFrame`
  * **Logic (Strict)**:
    1.  **Input**: DataFrame 必须包含 `high`, `low`。
    2.  **Rolling Regression**:
          * 使用 `numpy.lib.stride_tricks.sliding_window_view` 构建滚动窗口，避免循环。
          * 对每个窗口执行 OLS 回归: $High = \beta \times Low + \alpha$。
          * 提取 $\beta$ (斜率) 和 $R^2$。
    3.  **Z-Score**: `z_score = (beta - rolling_mean(beta, M)) / rolling_std(beta, M)`
    4.  **Correction**: `rsrs_score = z_score * r2 * sign(z_score)`
    5.  **Output**: 返回添加了 `rsrs_score` 列的 DataFrame。

### [P1-04] 混合周期回测框架

  * **File**: `src/backtest/feeds.py`
  * **Class**: `HybridPandasData(bt.feeds.PandasData)`
  * **Spec**:
      * 增加 lines: `('rsrs_score', 'market_type')`
      * `market_type`: 0=A股(T+1), 1=QDII(T+0)。
  * **File**: `src/backtest/engine.py`
  * **Class**: `HybridStrategy(bt.Strategy)`
  * **Logic**:
      * 在 `next()` 中，若 `d.market_type == 0` (A股)，使用默认下单。
      * 若 `d.market_type == 1` (QDII)，开启 `broker.set_coo(True)` (Cheat-On-Open) 模拟 T+0 或在回测引擎层手动处理日内平仓逻辑。

-----

## Phase 2: Live Execution & Arbitrage (Task ID: P2-xx)

### [P2-01] QDII 影子净值计算器

  * **File**: `src/data_engine/qdii_calc.py`
  * **Function**: `get_realtime_premium(etf_code, benchmark_future_symbol)`
  * **Logic**:
    1.  Fetch `Last_NAV` (T-1 净值) from DB.
    2.  Fetch `Realtime_Future_Pct` (美股期货涨跌) via AkShare (`ak.futures_foreign_commodity_realtime`).
    3.  Fetch `USD_CNY` rate via AkShare.
    4.  **Formula**:
        $$IOPV_{now} = NAV_{last} \times (1 + Future\%) \times \frac{Rate_{now}}{Rate_{base}}$$
        $$Premium = \frac{Price_{market}}{IOPV_{now}} - 1$$
    5.  **Output**: Float (溢价率，如 0.02 代表 2%)。

### [P2-02] MiniQMT 交互层 (Mockable)

  * **File**: `src/execution/qmt_client.py`
  * **Design**:
      * 由于开发环境可能无 MiniQMT，需创建一个 `BaseTrader` 接口。
      * 实现 `RealTrader` (调用 `xtquant`) 和 `MockTrader` (仅打印日志)。
      * **Method**: `place_order(code, amount, action, strategy_id)`
      * **Method**: `get_positions()`
  * **Constraint**: 所有交易指令必须先推送到 Redis 队列 `trade_instruction_queue`，由该脚本作为 Consumer 消费执行。

### [P2-03] 自动化调度器

  * **File**: `main.py` (Command Pattern)
  * **Commands**:
      * `python main.py update_data`: 运行 Tushare Loader。
      * `python main.py calc_factors`: 运行 RSRS 计算并更新 DB。
      * `python main.py monitor`: 启动死循环，每 3 秒计算一次 QDII 溢价，若 `abs(premium) > 0.03`，触发 Redis 报警信号。

-----

## Phase 3: AI Agent & Quantamental (Task ID: P3-xx)

### [P3-01] 研报处理 Pipeline

  * **File**: `src/llm_agent/crawler.py`
  * **Action**: 使用 `requests` 获取东方财富研报 PDF URL（模拟 Headers 必不可少）。
  * **File**: `src/llm_agent/converter.py`
  * **Action**: 使用 `pymupdf4llm.to_markdown(pdf_path)` 将 PDF 转为 Markdown。

### [P3-02] DeepSeek 分析器

  * **File**: `src/llm_agent/analyzer.py`
  * **Function**: `analyze_report(markdown_text)`
  * **Spec**:
      * Client: `openai.OpenAI(base_url="https://api.deepseek.com", api_key=...)`
      * **System Prompt**:
        ```text
        你是一个严谨的量化基本面分析师。请分析研报并输出 JSON:
        {
          "sentiment": float (-1.0 to 1.0),
          "confidence": float (0.0 to 1.0),
          "key_drivers": ["string"],
          "risks": ["string"]
        }
        ```
      * **Constraint**: 必须使用 JSON Mode (`response_format={"type": "json_object"}`) 确保程序可解析。

### [P3-03] 信号融合 (Signal Fusion)

  * **File**: `src/strategies/signal_generator.py`
  * **Logic**:
      * 读取 `FactorData` 中的 RSRS Score。
      * 读取 `ReportSentiment` 中的 Sentiment Score。
      * **Fusion Logic**:
        ```python
        if rsrs > 0.7 and sentiment > 0.2:
            signal = "STRONG_BUY"
        elif rsrs > 0.7 and sentiment < -0.2:
            signal = "DIVERGENCE_WATCH" # 技术面好但基本面差，减仓观察
        else:
            signal = "HOLD/SELL"
        ```

### [P3-04] Streamlit Dashboard

  * **File**: `src/dashboard/app.py`
  * **Action**:
      * Page 1: **Market Monitor**. 显示 QDII 实时溢价表 (Auto-refresh every 10s)。
      * Page 2: **Backtest Viewer**. 上传回测结果 Pickle，绘制 pnl 曲线。
      * Page 3: **AI Reports**. 展示最近分析的研报及其 Sentiment 打分。

-----

## Execution Guide for Coding Agent

**Step 1: Initialization**
Run `mkdir -p` commands to create the directory tree. create `requirements.txt` with specified libraries.

**Step 2: Database Layer**
Implement `models.py` and bring up Docker containers. Verify connection string.

**Step 3: Data Ingestion**
Implement `tushare_loader.py`. *Verification*: Run script to fetch '000001.SZ' for last 30 days and query DB to confirm data.

**Step 4: Strategy Logic**
Implement `rsrs.py`. *Verification*: Create a dummy CSV with clear uptrend, check if `rsrs_score` \> 0.8.

**Step 5: AI Integration**
Implement `analyzer.py`. *Verification*: Pass a dummy text "Company profit doubled" to DeepSeek API and verify JSON output contains positive sentiment.

**Step 6: System Integration**
Tie everything together in `main.py` with CLI arguments (`argparse`).

**Step 7: Real Trading Simulation**
Implement `qmt_client.py` with Mock mode enabled. Simulate a "Buy" signal flow from Strategy -\> Redis -\> Execution -\> Log.