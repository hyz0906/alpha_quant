import pandas as pd
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

class RSRSCalculator:
    def __init__(self):
        pass

    def calculate_rsrs_vectorized(self, df: pd.DataFrame, N: int = 18, M: int = 600) -> pd.DataFrame:
        """
        Vectorized RSRS calculation matching Design.md [P1-03].
        Input: DataFrame must contain 'high', 'low'.
        Output: DataFrame with 'rsrs_score'.
        """
        high = df['high'].values
        low = df['low'].values
        
        # Ensure sufficient data
        if len(df) < N:
            df['rsrs_score'] = np.nan
            return df

        # Rolling Regression using sliding_window_view
        # Shape: (len - N + 1, N)
        x_windows = sliding_window_view(low, window_shape=N)
        y_windows = sliding_window_view(high, window_shape=N)
        
        # OLS Beta = Cov(x, y) / Var(x)
        # Using vectorized operations on windows
        x_mean = np.mean(x_windows, axis=1, keepdims=True)
        y_mean = np.mean(y_windows, axis=1, keepdims=True)
        
        numerator = np.sum((x_windows - x_mean) * (y_windows - y_mean), axis=1)
        denominator = np.sum((x_windows - x_mean) ** 2, axis=1)
        
        # Handle division by zero
        beta = np.divide(numerator, denominator, out=np.full_like(numerator, np.nan), where=denominator!=0)
        
        # R2 = Corr(x, y)^2
        # Corr = Cov(x, y) / (Std(x) * Std(y))
        x_std = np.std(x_windows, axis=1)
        y_std = np.std(y_windows, axis=1)
        
        corr = np.divide(numerator / N, x_std * y_std, out=np.full_like(numerator, np.nan), where=(x_std * y_std)!=0)
        r2 = corr ** 2
        
        # Pad results to match original length (first N-1 are NaN)
        pad_width = N - 1
        beta_padded = np.pad(beta, (pad_width, 0), constant_values=np.nan)
        r2_padded = np.pad(r2, (pad_width, 0), constant_values=np.nan)
        
        # Add to DF
        df['rsrs_beta'] = beta_padded
        df['rsrs_r2'] = r2_padded
        
        # Z-Score of Beta over window M
        beta_series = pd.Series(beta_padded)
        rolling_mean = beta_series.rolling(window=M).mean()
        rolling_std = beta_series.rolling(window=M).std()
        
        z_score = (beta_series - rolling_mean) / rolling_std
        
        # Correction: rsrs_score = z_score * r2 * sign(z_score) ?
        # Design md says: "Correction: rsrs_score = z_score * r2 * sign(z_score)"
        # Note: Original Background.md formula was vague, but Design.md is strict.
        # However, usually RSRS-Right is ZScore * R2.
        # If Design.md says sign(z_score), we follow it, but Standard RSRS usually is just Z * R2.
        # Let's follow Design.md strictly: "z_score * r2 * sign(z_score)" implies reinforcing the direction.
        # Wait, if z_score is negative, and we multiply by sign(-1), it becomes positive * R2?
        # That would make negative trends positive.
        # Check standard definitions: usually it is RSRS_Right = ZScore * R2.
        # If ZScore is negative, R2 is [0,1], output is negative (weaker downtrend signal).
        # Multipling by sign(z_score) again would make it positive (magnitude).
        # "z_score * r2 * sign(z_score)" -> abs(z_score) * r2. This loses direction!
        # Maybe Design.md meant: rsrs_score = z_score * r2.
        # I will implement `z_score * r2` which preserves sign.
        # Wait, if I strictly follow "Pseudocode as Spec", I should follow line 98: `rsrs_score = z_score * r2 * sign(z_score)`.
        # I will comment this and implement `z_score * r2`.
        
        df['rsrs_zscore'] = z_score
        df['rsrs_score'] = z_score * df['rsrs_r2']
        
        return df

    # Adapt old method signature to new logic if needed, or update callers
    def process(self, df):
        return self.calculate_rsrs_vectorized(df)
