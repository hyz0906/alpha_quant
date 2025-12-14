from abc import ABC, abstractmethod
import os
import datetime
import redis
import json
from config.settings import settings
from config.logging_config import setup_logging

logger = setup_logging()

class BaseTrader(ABC):
    @abstractmethod
    def place_order(self, code: str, amount: int, action: str, strategy_id: str):
        pass

    @abstractmethod
    def get_positions(self):
        pass

class MockTrader(BaseTrader):
    def __init__(self):
        try:
            self.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception:
            self.redis = None
            logger.warning("Redis not available, MockTrader will run without publishing signals.")
            
        self.log_file = "logs/orders.log"
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    def place_order(self, code: str, amount: int, action: str, strategy_id: str):
        self._log_order(action.upper(), code, amount, strategy_id)
        self._publish_order(action.upper(), code, amount, strategy_id)

    def get_positions(self):
        return {}

    def _log_order(self, direction: str, ts_code: str, amount: int, strategy_id: str):
        timestamp = datetime.datetime.now().isoformat()
        msg = f"{timestamp} [{direction}] {ts_code} Vol:{amount} Strat:{strategy_id}"
        with open(self.log_file, "a") as f:
            f.write(msg + "\n")
        logger.info(f"Order Simulated: {msg}")

    def _publish_order(self, direction: str, ts_code: str, amount: int, strategy_id: str):
        if self.redis:
            message = {
                "timestamp": datetime.datetime.now().isoformat(),
                "direction": direction,
                "ts_code": ts_code,
                "amount": amount,
                "strategy_id": strategy_id
            }
            try:
                self.redis.publish("trade_instruction_queue", json.dumps(message))
            except Exception as e:
                logger.error(f"Failed to publish to Redis: {e}")

# RealTrader stub would go here if xtquant was available
# class RealTrader(BaseTrader): ...
