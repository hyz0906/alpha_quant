import pytest
from src.data_engine.tushare_loader import TushareLoader
from src.database.connection import get_db, engine
from src.database.models import MarketData
from sqlalchemy.orm import Session
from sqlalchemy import text
from unittest.mock import MagicMock, patch
import pandas as pd

def test_tushare_loader_mock():
    # Mock Tushare API to avoid needing a real token during CI/Test if not provided
    with patch('tushare.pro_api') as mock_pro:
        mock_api = MagicMock()
        mock_pro.return_value = mock_api
        
        # Mock Response
        mock_df = pd.DataFrame({
            'ts_code': ['000001.SZ'],
            'trade_date': ['20230101'],
            'open': [10.0],
            'high': [10.5],
            'low': [9.5],
            'close': [10.2],
            'vol': [1000.0]
        })
        mock_api.daily.return_value = mock_df
        
        loader = TushareLoader()
        loader.run("000001.SZ", days=1)
        
        # Verify DB
        with Session(engine) as session:
            item = session.query(MarketData).filter(MarketData.ts_code == '000001.SZ', MarketData.trade_date == '2023-01-01').first()
            assert item is not None
            assert item.close == 10.2

            # Clean up
            session.delete(item)
            session.commit()
