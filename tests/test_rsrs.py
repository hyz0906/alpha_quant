import pytest
import pandas as pd
import numpy as np
from src.strategies.rsrs_momentum import RSRSCalculator

def test_rsrs_perfect_correlation():
    # Create synthetic data where High = 2 * Low + 10
    # Perfect linear relationship means Beta should be 2.0 and R2 should be 1.0
    N = 100
    low = np.linspace(10, 100, N)
    high = 2 * low + 10
    
    df = pd.DataFrame({'high': high, 'low': low})
    
    # Initialize calculator with small windows for testing
    calc = RSRSCalculator(n=18, m=20)
    result = calc.process(df)
    
    # Check last few rows where windows are full
    last_row = result.iloc[-1]
    
    print(last_row)
    
    # Beta should be exactly 2.0
    assert abs(last_row['rsrs_beta'] - 2.0) < 1e-5
    
    # R2 should be exactly 1.0
    assert abs(last_row['rsrs_r2'] - 1.0) < 1e-5
    
    # ZScore will vary but should be defined
    assert not np.isnan(last_row['rsrs_zscore'])

def test_rsrs_random_data():
    # Random data
    np.random.seed(42)
    N = 200
    low = np.random.rand(N) * 100
    high = low + np.random.rand(N) * 10 # High always > Low
    
    df = pd.DataFrame({'high': high, 'low': low})
    
    calc = RSRSCalculator(n=18, m=600) # m > N, so zscore might be NaN if using full rolling M
    # If m=600 > len(df)=200, rolling(600) will yield NaNs unless min_periods is set.
    # The standard rolling in pandas returns NaN if window is not full.
    # We should probably set min_periods=0 or handle it.
    # But design doc said "Z-Score of Beta over window M".
    # If M is large (600), we need 600 points.
    # Let's retry with smaller M for this test or more data.
    
    calc = RSRSCalculator(n=18, m=50)
    result = calc.process(df)
    
    assert 'rsrs_zscore' in result.columns
    assert not np.isnan(result['rsrs_zscore'].iloc[-1])
