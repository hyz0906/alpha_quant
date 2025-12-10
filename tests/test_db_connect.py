import pytest
from sqlalchemy import text
from src.database.connection import get_db, engine, Base
from src.database.models import MarketData
from datetime import date

# Setup/Teardown
@pytest.fixture(scope="module")
def db():
    # Create tables
    Base.metadata.create_all(bind=engine)
    yield next(get_db())
    # Drop tables (optional, or rely on internal rollback if using transaction) (using sqlite file so maybe not needed to drop if we want persistence, but for test maybe good)
    # Base.metadata.drop_all(bind=engine)

def test_db_connection(db):
    result = db.execute(text("SELECT 1"))
    assert result.scalar() == 1

def test_market_data_crud(db):
    # Create
    item = MarketData(
        ts_code="000001.SZ",
        trade_date=date(2023, 1, 1),
        open=10.0,
        high=10.5,
        low=9.5,
        close=10.2,
        vol=10000.0
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    
    # Read
    read_item = db.query(MarketData).filter(MarketData.ts_code == "000001.SZ").first()
    assert read_item is not None
    assert read_item.close == 10.2

    # Cleanup
    db.delete(item)
    db.commit()
