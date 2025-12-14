import backtrader as bt

class HybridPandasData(bt.feeds.PandasData):
    """
    Hybrid Data Feed supporting:
    - rsrs_score
    - market_type (0=A-Share, 1=QDII)
    """
    lines = ('rsrs_score', 'market_type', )
    
    params = (
        ('rsrs_score', -1),
        ('market_type', -1), # Assumes column 'market_type' exists in DF
    )
