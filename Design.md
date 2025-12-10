# Project Blueprint: AlphaQuant (Agent-Optimized Edition)

> **Meta-Instructions for Coding Agent:**
>
> 1.  **Single Source of Truth**: This document is the absolute specification. Do not deviate from the directory structure or variable naming conventions defined here.
> 2.  **Test-Driven Development (TDD)**: For every module, generate the unit test file *before* or *simultaneously* with the implementation code.
> 3.  **Environment**: Assume Python 3.10+ in a Linux/WSL environment.
> 4.  **Incremental Execution**: Execute tasks in the order defined in Section 4. Do not jump ahead.

-----

## 1\. Project Manifest & Directory Structure

**Strictly** adhere to this file tree. Create empty `__init__.py` files where necessary.

```text
AlphaQuant/
├── .env.example                # Template for API keys (DeepSeek, Tushare) and DB URLs
├── pyproject.toml              # Dependency management (Poetry/Setuptools)
├── docker-compose.yml          # Postgres & Redis services
├── config/
│   ├── __init__.py
│   └── settings.py             # Pydantic based settings loading
├── src/
│   ├── __init__.py
│   ├── database/
│   │   ├── __init__.py
│   │   ├── connection.py       # SQLAlchemy Sync/Async Engine
│   │   └── models.py           # SQLModels/SQLAlchemy ORM classes
│   ├── data_engine/
│   │   ├── __init__.py
│   │   ├── tushare_loader.py   # Historical data ETL
│   │   └── realtime_feed.py    # AkShare & IOPV calculation
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── base.py             # Abstract Strategy Class
│   │   └── rsrs_momentum.py    # RSRS + Dual Momentum implementation
│   ├── analysis/
│   │   ├── __init__.py
│   │   └── llm_agent.py        # DeepSeek API integration
│   ├── backtest/
│   │   ├── __init__.py
│   │   └── engine.py           # Backtrader wrapper
│   └── execution/
│   │   ├── __init__.py
│   │   └── qmt_gateway.py      # MiniQMT Bridge
└── tests/                      # Mirror the src structure
    ├── test_data_engine.py
    ├── test_strategies.py
    └── ...
```

-----

## 2\. Environment Specifications

### 2.1 Core Dependencies (`pyproject.toml`)

Agent, ensure these libraries are installed:

  * `pandas >= 2.0.0`
  * `numpy >= 1.24.0`
  * `sqlalchemy >= 2.0.0`
  * `psycopg2-binary` (Postgres driver)
  * `redis`
  * `pydantic-settings`
  * `tushare`, `akshare`
  * `backtrader` (Note: Fix matplotlib compatibility if error occurs)
  * `openai` (Standard SDK compatible with DeepSeek)
  * `loguru` (For structured logging)

### 2.2 Infrastructure (`docker-compose.yml`)

Generate a docker-compose file with:

  * **Service: db**: Image `postgres:15`, Ports `5432:5432`.
  * **Service: cache**: Image `redis:7`, Ports `6379:6379`.

-----

## 3\. Module Specifications & Interface Definitions

### 3.1 Data Layer (`src/database/models.py`)

**Instruction**: Use `SQLAlchemy` Declarative Base.

  * **Table 1: `MarketData`**

      * `ts_code` (String, PK)
      * `trade_date` (Date, PK)
      * `open`, `high`, `low`, `close`, `vol` (Float)
      * `rsrs_beta` (Float, Nullable, Index=True)

  * **Table 2: `ReportSentiment`**

      * `id` (Integer, AutoInc)
      * `ts_code` (String)
      * `publish_date` (Date)
      * `title` (String)
      * `sentiment_score` (Float) - Range [-1.0, 1.0]
      * `summary` (Text)

### 3.2 Strategy Core (`src/strategies`)

**File: `src/strategies/rsrs_momentum.py`**
Implement a class `RSRSCalculator` with vectorized numpy operations.

```python
# Interface Definition
class RSRSCalculator:
    def __init__(self, n: int = 18, m: int = 600):
        pass

    def process(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Input: DataFrame with ['high', 'low']
        Output: DataFrame with added columns ['beta', 'r2', 'rsrs_zscore']
        Logic: 
          1. Rolling OLS regression of High ~ Low over window N.
          2. Calculate Z-Score of Beta over window M.
          3. Apply R2 correction.
        """
        pass
```

### 3.3 LLM Agent (`src/analysis/llm_agent.py`)

**Instruction**: Create a class `ResearchAgent` wrapping the DeepSeek API.

  * **Method**: `analyze_text(text: str) -> dict`
  * **Prompt Template**:
    ```text
    Role: Financial Analyst. 
    Task: Analyze the provided report text.
    Output JSON keys: 
    - sentiment_score (float, -1.0 to 1.0)
    - key_risks (list of strings)
    - growth_logic (list of strings)
    ```

-----

## 4\. Execution Sequence (Task Queue for Agent)

**Agent, please execute the following tasks sequentially. Do not proceed to the next task until the verification step passes.**

### Task 01: Scaffold & Infrastructure

  * **Action**: Create the directory structure, `pyproject.toml`, and `docker-compose.yml`. Start the Docker services.
  * **Verification**: Run `docker compose ps` and ensure DB/Redis are healthy.

### Task 02: Database Layer

  * **Action**: Implement `src/database/connection.py` and `models.py`. Create a script `scripts/init_db.py` to create tables.
  * **Verification**: Run `pytest tests/test_db_connect.py` (You must create this test to insert and query a dummy record).

### Task 03: RSRS Logic Implementation

  * **Action**: Implement `src/strategies/rsrs_momentum.py`. Use `numpy` for vectorized regression (do not use loops).
  * **Verification**: Create `tests/test_rsrs.py`. Input a synthetic DataFrame where `High = 2 * Low` (perfect correlation). Assert that `r2` is close to 1.0 and `beta` is close to 2.0.

### Task 04: Data Ingestion (Tushare)

  * **Action**: Implement `src/data_engine/tushare_loader.py`. It should fetch daily bars and upsert them into Postgres. Handle duplicates using `ON CONFLICT DO UPDATE`.
  * **Verification**: Fetch data for `000001.SZ` for the last 5 days. Check DB count.

### Task 05: LLM Connector

  * **Action**: Implement `src/analysis/llm_agent.py`. Use `pydantic-settings` to load `DEEPSEEK_API_KEY`. Mock the API call in tests.
  * **Verification**: Run `pytest tests/test_llm.py` with a mocked response to ensure JSON parsing works.

### Task 06: Backtrader Integration

  * **Action**: Implement `src/backtest/engine.py`.
      * Create a custom `PandasData` feed that maps the DB columns (especially `rsrs_beta`) to Backtrader lines.
      * Implement the Strategy class that buys if `rsrs_zscore > 0.7`.
  * **Verification**: Run a backtest on the data fetched in Task 04. Output the final portfolio value.

### Task 07: Execution Gateway (Stub)

  * **Action**: Implement `src/execution/qmt_gateway.py`.
      * Since MiniQMT requires Windows, create a **Mock Class** `MockTrader` that logs orders to a file (`logs/orders.log`) and Redis channel (`trade_signal`).
  * **Verification**: Call `MockTrader.buy("000001.SZ", 100)`. Check if the log file exists and contains the order.

-----

## 5\. Coding Standards (Style Guide)

  * **Type Hinting**: All function signatures must have Python 3.10+ type hints.
  * **Docstrings**: Use Google-style docstrings.
  * **Error Handling**: Never swallow exceptions silently. Use `loguru` to log errors with stack traces.
  * **Config**: No hardcoded credentials. All secrets must come from `os.getenv`.