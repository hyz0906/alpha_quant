import backtrader as bt
import pandas as pd
from datetime import datetime
from tushare import pro_api
from src.database.connection import get_db, engine
from src.database.models import MarketData
from sqlalchemy.orm import Session
from src.strategies.rsrs_momentum import RSRSCalculator
from loguru import logger

# Custom Feed
class PandasDataPlus(bt.feeds.PandasData):
    lines = ('rsrs_zscore', 'rsrs_beta', )
    params = (
        ('rsrs_zscore', -1),
        ('rsrs_beta', -1),
    )

# Strategy
class RSRSStrategy(bt.Strategy):
    params = (
        ('buy_threshold', 0.7),
        ('sell_threshold', -0.7),
    )

    def log(self, txt, dt=None):
        dt = dt or self.datas[0].datetime.date(0)
        logger.info(f'{dt.isoformat()} {txt}')

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.rsrs_zscore = self.datas[0].rsrs_zscore
        self.order = None

    def next(self):
        if self.order:
            return

        # Check if we are in the market
        if not self.position:
            # Buy signal
            if self.rsrs_zscore[0] > self.params.buy_threshold:
                self.log(f'BUY CREATE, {self.dataclose[0]:.2f}, Score: {self.rsrs_zscore[0]:.2f}')
                self.order = self.buy()
        else:
            # Sell signal
            if self.rsrs_zscore[0] < self.params.sell_threshold:
                self.log(f'SELL CREATE, {self.dataclose[0]:.2f}, Score: {self.rsrs_zscore[0]:.2f}')
                self.order = self.sell()

class BacktestEngine:
    def __init__(self, ts_code: str, start_date: str, end_date: str):
        self.ts_code = ts_code
        self.start_date = start_date
        self.end_date = end_date
        self.cerebro = bt.Cerebro()

    def load_data(self):
        # Fetch data from DB
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
                    # Calculate RSRS on the fly if not in DB, but better to have it pre-calculated
                    # Here we assume it is populated or we calc it now
                    'high_raw': row.high,
                    'low_raw': row.low
                })
            
            if not data:
                logger.warning("No data found in DB for backtest.")
                return

            df = pd.DataFrame(data)
            df.set_index('datetime', inplace=True)
            
            # Calculate RSRS if missing (in a real flow, this might be a separate ETL step)
            # We'll run calculator here to ensure columns exist
            calc = RSRSCalculator()
            df = calc.process(df)
            
            # Fill NaNs for Backtrader
            df = df.fillna(0)
            
            # Add to Cerebro
            data_feed = PandasDataPlus(dataname=df)
            self.cerebro.adddata(data_feed)

    def run(self):
        self.cerebro.addstrategy(RSRSStrategy)
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
