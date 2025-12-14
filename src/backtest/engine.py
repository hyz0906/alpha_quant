import backtrader as bt
import pandas as pd
from datetime import datetime
from sqlalchemy.orm import Session
from src.database.connection import engine
from src.database.models import MarketData
from src.strategies.factors.rsrs import RSRSCalculator
from src.backtest.feeds import HybridPandasData
from config.logging_config import setup_logging

logger = setup_logging()

# Strategy with Hybrid Logic [P1-04]
class HybridStrategy(bt.Strategy):
    params = (
        ('buy_threshold', 0.7),
        ('sell_threshold', -0.7),
    )

    def log(self, txt, dt=None):
        dt = dt or self.datas[0].datetime.date(0)
        logger.info(f'{dt.isoformat()} {txt}')

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.rsrs_score = self.datas[0].rsrs_score
        self.market_type = self.datas[0].market_type
        self.order = None

    def next(self):
        if self.order:
            return

        # Market Type Logic
        # 0 = A Share (T+1), 1 = QDII (T+0)
        # Backtrader default is T+0 allowed unless cheat-on-close is disabled for T+1?
        # Design doc: "if market_type == 1 (QDII), set_coo(True)?" 
        # Actually set_coo is per broker setting, not per data/bar easily without hack.
        # But we can simulate logically.
        
        current_type = self.market_type[0]
        score = self.rsrs_score[0]
        
        # Simple Logic
        if not self.position:
            if score > self.params.buy_threshold:
                self.log(f'BUY CREATE ({current_type}), {self.dataclose[0]:.2f}, Score: {score:.2f}')
                self.order = self.buy()
        else:
            if score < self.params.sell_threshold:
                self.log(f'SELL CREATE ({current_type}), {self.dataclose[0]:.2f}, Score: {score:.2f}')
                self.order = self.sell()

class BacktestEngine:
    def __init__(self, ts_code: str, start_date: str, end_date: str):
        self.ts_code = ts_code
        self.start_date = start_date
        self.end_date = end_date
        self.cerebro = bt.Cerebro()

    def load_data(self):
        with Session(engine) as session:
            query = session.query(MarketData).filter(
                MarketData.ts_code == self.ts_code,
                MarketData.trade_date >= datetime.strptime(self.start_date, '%Y%m%d').date(),
                MarketData.trade_date <= datetime.strptime(self.end_date, '%Y%m%d').date()
            ).order_by(MarketData.trade_date)
            
            data = []
            for row in query:
                data.append({
                    'datetime': pd.Timestamp(row.trade_date),
                    'open': row.open,
                    'high': row.high,
                    'low': row.low,
                    'close': row.close,
                    'volume': row.vol,
                    'openinterest': 0,
                    # Dummy market type (0=A-Share default)
                    'market_type': 0 
                })
            
            if not data:
                logger.warning("No data found in DB for backtest.")
                return

            df = pd.DataFrame(data)
            df.set_index('datetime', inplace=True)
            
            # Calculate RSRS
            calc = RSRSCalculator()
            df = calc.calculate_rsrs_vectorized(df) # Logic moved to this method
            
            df = df.fillna(0)
            
            # Add to Cerebro using Hybrid Feed
            data_feed = HybridPandasData(dataname=df)
            self.cerebro.adddata(data_feed)

    def run(self):
        self.cerebro.addstrategy(HybridStrategy)
        self.cerebro.broker.setcash(100000.0)
        
        logger.info(f'Starting Portfolio Value: {self.cerebro.broker.getvalue():.2f}')
        self.cerebro.run()
        logger.info(f'Final Portfolio Value: {self.cerebro.broker.getvalue():.2f}')
        return self.cerebro.broker.getvalue()

if __name__ == "__main__":
    # Ensure data exists first (normally ETL runs before)
    from src.data_engine.tushare_loader import TushareLoader
    loader = TushareLoader()
    # Mock data fetch if needed or rely on existing
    # For test run, we'll just try to load what's there
    
    engine_bt = BacktestEngine(ts_code="000001.SZ", start_date="20230101", end_date="20230201")
    engine_bt.load_data()
    engine_bt.run()
