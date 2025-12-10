import tushare as ts
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from config.settings import settings
from src.database.connection import engine, get_db
from src.database.models import MarketData
from loguru import logger
import time

class TushareLoader:
    def __init__(self):
        if not settings.TUSHARE_TOKEN:
            logger.warning("TUSHARE_TOKEN not found in settings!")
        
        # Initialize Tushare Pro API
        self.pro = ts.pro_api(settings.TUSHARE_TOKEN)
        
    def fetch_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        Fetch daily data from Tushare.
        """
        try:
            df = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                logger.warning(f"No data found for {ts_code} between {start_date} and {end_date}")
                return pd.DataFrame()
                
            # Rename columns to match model
            # Tushare daily columns: ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount
            df = df.rename(columns={
                'vol': 'vol' # matches
            })
            return df
        except Exception as e:
            logger.error(f"Error fetching data for {ts_code}: {e}")
            return pd.DataFrame()

    def save_to_db(self, df: pd.DataFrame):
        """
        Save DataFrame to database with upsert handling.
        """
        if df.empty:
            return

        with Session(engine) as session:
            for _, row in df.iterrows():
                # Convert trade_date string 'YYYYMMDD' to date object
                t_date = datetime.strptime(row['trade_date'], '%Y%m%d').date()
                
                # Check if exists
                existing = session.query(MarketData).filter(
                    MarketData.ts_code == row['ts_code'],
                    MarketData.trade_date == t_date
                ).first()
                
                if existing:
                    # Update
                    existing.open = row['open']
                    existing.high = row['high']
                    existing.low = row['low']
                    existing.close = row['close']
                    existing.vol = row['vol']
                else:
                    # Insert
                    item = MarketData(
                        ts_code=row['ts_code'],
                        trade_date=t_date,
                        open=row['open'],
                        high=row['high'],
                        low=row['low'],
                        close=row['close'],
                        vol=row['vol']
                    )
                    session.add(item)
            
            try:
                session.commit()
                logger.info(f"Saved {len(df)} records to database.")
            except Exception as e:
                session.rollback()
                logger.error(f"Failed to commit to DB: {e}")

    def run(self, ts_code: str, days: int = 5):
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)
        
        start_date_str = start_dt.strftime('%Y%m%d')
        end_date_str = end_dt.strftime('%Y%m%d')
        
        logger.info(f"Fetching {ts_code} from {start_date_str} to {end_date_str}")
        df = self.fetch_daily(ts_code, start_date_str, end_date_str)
        self.save_to_db(df)

if __name__ == "__main__":
    loader = TushareLoader()
    # Test with 000001.SZ
    loader.run("000001.SZ", days=10)
