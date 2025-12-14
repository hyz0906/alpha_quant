import argparse
import time
import redis
import json
from config.logging_config import setup_logging
from config.settings import settings
from src.data_engine.tushare_loader import TushareLoader
from src.data_engine.qdii_calc import QDIICalculator
from src.strategies.factors.rsrs import RSRSCalculator

logger = setup_logging()

def cmd_update_data(args):
    loader = TushareLoader()
    # Defaulting to a few interesting codes if not provided
    # 513100: Nasdaq ETF
    codes = ["000001.SZ", "512480.SH", "513100.SH"] 
    for code in codes:
        loader.sync_daily_data(code, "20230101", "20240101")

def cmd_calc_factors(args):
    logger.info("Calculating RSRS factors...")
    # Stub: In real usage, iterate all stocks in FactorData
    logger.success("Factor calculation complete (stub).")

def cmd_monitor(args):
    logger.info("Starting QDII Monitor...")
    calc = QDIICalculator()
    etf_code = "513100" # Nasdaq ETF
    
    # Redis for Alerts
    try:
        r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        r = None
        logger.warning("Redis not connected! Alerts will only be logged.")

    while True:
        try:
            premium = calc.get_realtime_premium(etf_code)
            logger.info(f"ETF: {etf_code} Premium: {premium:.4f}")
            
            if abs(premium) > 0.03:
                msg = f"High Premium Alert! {etf_code}: {premium:.2%}"
                logger.warning(msg)
                if r:
                    r.publish("alert_queue", json.dumps({
                        "type": "PREMIUM_ALERT",
                        "code": etf_code,
                        "value": premium,
                        "timestamp": time.time()
                    }))
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user.")
            break
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
            
        time.sleep(3)

def main():
    parser = argparse.ArgumentParser(description="AlphaQuant CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # update_data
    p_update = subparsers.add_parser("update_data", help="Sync historical data")
    p_update.set_defaults(func=cmd_update_data)
    
    # calc_factors
    p_calc = subparsers.add_parser("calc_factors", help="Calculate strategy factors")
    p_calc.set_defaults(func=cmd_calc_factors)
    
    # monitor
    p_monitor = subparsers.add_parser("monitor", help="Start Realtime Monitor")
    p_monitor.set_defaults(func=cmd_monitor)
    
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
        
    args.func(args)

if __name__ == "__main__":
    main()
