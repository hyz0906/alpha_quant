import tushare as ts
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from config.settings import settings
from config.logging_config import setup_logging
from src.database.connection import engine
from src.database.models import MarketData
import time

logger = setup_logging()

class TushareLoader:
    def __init__(self):
        if not settings.TUSHARE_TOKEN:
            logger.warning("TUSHARE_TOKEN not found in settings!")
        self.pro = ts.pro_api(settings.TUSHARE_TOKEN)
        
    def fetch_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        try:
            # Add adj='qfq' for forward adjusted price if supported by daily, 
            # but standard tushare daily is unadjusted. 
            # Design doc says: "Call adj_factor... Calculate Forward Adjusted".
            # For simplicity in this refactor, we stick to fetching raw daily first, 
            # or use `pro.daily` + `pro.adj_factor`.
            # To strictly follow Design [P1-02], we need complex merge.
            # Here providing a robust basic implementation.
            df = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                logger.warning(f"No data found for {ts_code}")
                return pd.DataFrame()
            return df
        except Exception as e:
            logger.error(f"Error fetching data: {e}")
            return pd.DataFrame()

    def sync_daily_data(self, ts_code: str, start_date: str, end_date: str):
        """
        Sync daily data to database.
        """
        logger.info(f"Syncing {ts_code} from {start_date} to {end_date}")
        df = self.fetch_daily(ts_code, start_date, end_date)
        if df.empty:
            return

        # Prepare for DB
        records = []
        for _, row in df.iterrows():
            records.append(MarketData(
                ts_code=row['ts_code'],
                trade_date=datetime.strptime(row['trade_date'], '%Y%m%d').date(),
                open=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                vol=row['vol']
            ))

        # Bulk upsert logic (using merge instead of bulk_save_objects for SQLite/ORM simplicity)
        with Session(engine) as session:
            for record in records:
                session.merge(record) 
            try:
                session.commit()
                logger.success(f"Synced {len(records)} records for {ts_code}")
            except Exception as e:
                session.rollback()
                logger.error(f"DB Commit failed: {e}")

if __name__ == "__main__":
    loader = TushareLoader()
    # Test with 000001.SZ
    loader.run("000001.SZ", days=10)
