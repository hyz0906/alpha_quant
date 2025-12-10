from sqlalchemy import Column, String, Date, Float, Integer, Text, Boolean, Index
from src.database.connection import Base

class MarketData(Base):
    __tablename__ = "market_data"

    ts_code = Column(String, primary_key=True, index=True)
    trade_date = Column(Date, primary_key=True, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    vol = Column(Float)
    rsrs_beta = Column(Float, nullable=True, index=True)
    rsrs_r2 = Column(Float, nullable=True)
    rsrs_zscore = Column(Float, nullable=True)

class ReportSentiment(Base):
    __tablename__ = "report_sentiment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String, index=True)
    publish_date = Column(Date, index=True)
    title = Column(String)
    sentiment_score = Column(Float) # Range [-1.0, 1.0]
    summary = Column(Text)
    key_risks = Column(Text) # JSON string or delimited
    growth_logic = Column(Text) # JSON string or delimited
