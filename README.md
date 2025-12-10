# AlphaQuant

**AlphaQuant** is an Agent-Optimized Quantitative Investment System designed for independent developers. It integrates **RSRS (Resistance Support Relative Strength)** trend tracking, **Cross-border QDII ETF arbitrage**, and **LLM-driven fundamental analysis** into a unified framework.

## Key Features

*   **RSRS Momentum Strategy**: Vectorized implementation of the RSRS indicator with Z-Score standardization and R2 correction for robust trend tracking.
*   **Quantamental Analysis**: Integration with **DeepSeek/OpenAI API** to analyze financial reports and generate sentiment scores to filter technical signals.
*   **Data Engine**: Automated data ingestion from **Tushare Pro** with efficient SQLite/PostgreSQL upsert logic.
*   **Backtesting**: Custom **Backtrader** integration to test RSRS strategies with proper data feeds.
*   **Execution Gateway**: A mock execution engine (extensible to MiniQMT) for logging orders and publishing signals via Redis.

## Directory Structure

```text
AlphaQuant/
├── config/             # Settings and configuration
├── src/
│   ├── analysis/       # LLM Agent and Research logic
│   ├── backtest/       # Backtrader engine and custom feeds
│   ├── data_engine/    # Tushare/AkShare data loaders
│   ├── database/       # SQLAlchemy models and connection
│   ├── execution/      # Trade execution gateways
│   └── strategies/     # RSRS and other strategy logic
├── scripts/            # Database initialization scripts
└── tests/              # Unit and functional tests
```

## Getting Started

### Prerequisites

*   Python 3.10+
*   Tushare Pro Token (Optional, for data fetching)
*   DeepSeek/OpenAI API Key (Optional, for LLM analysis)

### Installation

1.  Clone the repository and enter the directory.
2.  Install dependencies:
    ```bash
    pip install -e .
    # Or manually
    pip install pandas numpy sqlalchemy psycopg2-binary redis pydantic-settings tushare akshare backtrader openai loguru
    ```

### Configuration

Copy the example environment file and configure your keys:

```bash
cp .env.example .env
```

Edit `.env`:
```ini
DEEPSEEK_API_KEY=your_key
TUSHARE_TOKEN=your_token
DATABASE_URL=sqlite:///./alphaquant.db
```

### Database Initialization

Initialize the SQLite database and create tables:

```bash
python3 scripts/init_db.py
```

## Usage

### 1. Data Ingestion
Fetch historical data for a stock (e.g., Ping An Bank 000001.SZ):

```bash
PYTHONPATH=. python3 src/data_engine/tushare_loader.py
```
*(Modify the script or args to fetch different assets)*

### 2. Run Backtest
Run the RSRS strategy backtest on the fetched data:

```bash
PYTHONPATH=. python3 src/backtest/engine.py
```

### 3. LLM Analysis
(Programmatic usage)
```python
from src.analysis.llm_agent import ResearchAgent
agent = ResearchAgent()
result = agent.analyze_text("Company report text...")
print(result)
```

### 4. Trade Execution (Simulation)
Run the mock trader gateway:

```bash
PYTHONPATH=. python3 src/execution/qmt_gateway.py
```

## Testing

Run the test suite:

```bash
python3 -m pytest tests/
```

## Project Concept
Based on *Independent Developer Full-Stack Quant System Construction* (Background.md) and *Project Blueprint* (Design.md).
