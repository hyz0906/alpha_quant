from sqlalchemy.orm import Session
from src.database.connection import engine
from src.database.models import FactorData, ReportSentiment
from loguru import logger

class SignalGenerator:
    def __init__(self):
        pass

    def generate_signal(self, ts_code: str, trade_date: str):
        """
        Fusion logic: RSRS + Sentiment
        """
        with Session(engine) as session:
            # 1. Get latest RSRS
            # Note: In real app, query by trade_date. Here getting latest for simplicity.
            rsrs_rec = session.query(FactorData).filter(
                FactorData.ts_code == ts_code
            ).order_by(FactorData.trade_date.desc()).first()

            # 2. Get latest Sentiment (valid for e.g. 7 days)
            sent_rec = session.query(ReportSentiment).filter(
                ReportSentiment.ts_code == ts_code
            ).order_by(ReportSentiment.publish_date.desc()).first()

            if not rsrs_rec:
                return "NO_DATA"

            rsrs_score = rsrs_rec.rsrs_zscore # Using zscore as the proxy for 'score' as per our implementation
            sentiment = sent_rec.sentiment_score if sent_rec else 0.0

            # 3. Fusion Logic [P3-03]
            signal = "HOLD/SELL"
            if rsrs_score > 0.7 and sentiment > 0.2:
                signal = "STRONG_BUY"
            elif rsrs_score > 0.7 and sentiment < -0.2:
                signal = "DIVERGENCE_WATCH"
            
            return {
                "ts_code": ts_code,
                "date": trade_date,
                "signal": signal,
                "rsrs": rsrs_score,
                "sentiment": sentiment
            }
