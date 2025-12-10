import os
import datetime
import redis
import json
from loguru import logger
from config.settings import settings

class MockTrader:
    def __init__(self):
        try:
            self.redis = redis.from_url(settings.REDIS_URL)
        except Exception:
            self.redis = None
            logger.warning("Redis not available, MockTrader will run without publishing signals.")
            
        self.log_file = "logs/orders.log"
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    def buy(self, ts_code: str, amount: int):
        self._log_order("BUY", ts_code, amount)
        self._publish_order("BUY", ts_code, amount)

    def sell(self, ts_code: str, amount: int):
        self._log_order("SELL", ts_code, amount)
        self._publish_order("SELL", ts_code, amount)

    def _log_order(self, direction: str, ts_code: str, amount: int):
        timestamp = datetime.datetime.now().isoformat()
        with open(self.log_file, "a") as f:
            f.write(f"{timestamp} [{direction}] {ts_code} Vol:{amount}\n")
        logger.info(f"Order Executed: {direction} {ts_code} {amount}")

    def _publish_order(self, direction: str, ts_code: str, amount: int):
        if self.redis:
            message = {
                "timestamp": datetime.datetime.now().isoformat(),
                "direction": direction,
                "ts_code": ts_code,
                "amount": amount
            }
            try:
                self.redis.publish("trade_signal", json.dumps(message))
            except Exception as e:
                logger.error(f"Failed to publish to Redis: {e}")

if __name__ == "__main__":
    trader = MockTrader()
    trader.buy("000001.SZ", 100)
