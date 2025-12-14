import sys
import os
import pandas as pd
from datetime import datetime
# Add project root to path
sys.path.append(os.getcwd())

from src.strategies.factors.rsrs import RSRSCalculator
from src.strategies.signal_generator import SignalGenerator
from src.execution.qmt_client import MockTrader
from src.database.models import FactorData, ReportSentiment, MarketData
from src.database.connection import engine
from sqlalchemy.orm import Session
from loguru import logger

def verify_system_flow():
    logger.info("Step 1: Mocking Data in DB")
    ts_code = "TEST001.SZ"
    today = datetime.now().date()
    
    # 1. Insert Mock Market Data (Enough for RSRS N=18)
    data = []
    for i in range(20):
        data.append(MarketData(
            ts_code=ts_code,
            trade_date=datetime(2023, 1, i+1).date(),
            open=10+i, high=12+i, low=9+i, close=11+i, vol=1000
        ))
    
    with Session(engine) as session:
        session.query(MarketData).filter(MarketData.ts_code==ts_code).delete()
        session.query(FactorData).filter(FactorData.ts_code==ts_code).delete()
        session.query(ReportSentiment).filter(ReportSentiment.ts_code==ts_code).delete()
        
        # Add Market Data
        for d in data: session.merge(d)
        
        # Add Mock Report Sentiment
        session.add(ReportSentiment(
            ts_code=ts_code,
            publish_date=today,
            title="Test Report",
            sentiment_score=0.8, # Very positive
            summary="Bullish",
            key_risks="None",
            growth_logic="Testing"
        ))
        session.commit()
        
    logger.info("Step 2: Calculating RSRS")
    # Load data
    with Session(engine) as session:
        query = session.query(MarketData).filter(
            MarketData.ts_code == ts_code
        ).order_by(MarketData.trade_date)
        df = pd.read_sql(query.statement, session.bind)
    
    # Calculate
    calc = RSRSCalculator()
    df_res = calc.calculate_rsrs_vectorized(df)
    
    # Save Factor (Mocking the save process)
    last_row = df_res.iloc[-1]
    # Manually inserting into FactorData for the test
    with Session(engine) as session:
        session.add(FactorData(
            ts_code=ts_code,
            trade_date=today,
            rsrs_zscore=0.8, # forcing > 0.7 for signal
            rsrs_r2=0.8,
            rsrs_beta=1.0
        ))
        session.commit()

    logger.info("Step 3: Generating Signal (Fusion)")
    gen = SignalGenerator()
    # Mocking date as today
    # Note: SignalGenerator logic queries DB.
    result = gen.generate_signal(ts_code, today)
    logger.info(f"Signal Result: {result}")
    
    if result['signal'] == "STRONG_BUY":
        logger.success("Signal Fusion Logic Verified: STRONG_BUY")
    else:
        logger.error(f"Signal Fusion Failed. Expected STRONG_BUY, got {result['signal']}")
        return

    logger.info("Step 4: Executing Trade via MockTrader")
    trader = MockTrader()
    trader.place_order(ts_code, 100, "BUY", "RSRS_SENTIMENT_FUSION")
    
    # Verify Log
    if os.path.exists("logs/orders.log"):
        with open("logs/orders.log", "r") as f:
            content = f.read()
            if "TEST001.SZ" in content and "BUY" in content:
                logger.success("Order Logged Successfully.")
            else:
                logger.error("Order not found in logs.")
    else:
        logger.error("Log file not created.")

if __name__ == "__main__":
    verify_system_flow()
