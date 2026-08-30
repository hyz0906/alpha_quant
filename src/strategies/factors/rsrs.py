import pandas as pd
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

class RSRSCalculator:
    def __init__(self, n: int = 18, m: int = 600):
        # 构造签名与 tests/test_rsrs.py 及历史调用方（rsrs_momentum）对齐
        self.n = n
        self.m = m

    def calculate_rsrs_vectorized(self, df: pd.DataFrame, N: int = None, M: int = None) -> pd.DataFrame:
        """
        Vectorized RSRS calculation matching Design.md [P1-03].
        Input: DataFrame must contain 'high', 'low'.
        Output: DataFrame with 'rsrs_score'.
        """
        N = N or self.n
        M = M or self.m
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
        # 关键：Series 必须继承 df 的索引。此前用默认 RangeIndex 创建，
        # 与 DatetimeIndex 的 df 做列赋值/乘法时索引无法对齐，
        # 导致 rsrs_zscore / rsrs_score 整列静默变 NaN（原实现 bug，已修复）。
        beta_series = pd.Series(beta_padded, index=df.index)
        rolling_mean = beta_series.rolling(window=M).mean()
        rolling_std = beta_series.rolling(window=M).std()
        
        z_score = (beta_series - rolling_mean) / rolling_std
        # 退化情形：窗口内 beta 恒定（如完美线性 high=2*low+10）时
        # std=0 -> 0/0=NaN。约定：无波动即无偏离，z 记 0。
        zero_std = (rolling_std == 0) & beta_series.notna()
        z_score = z_score.mask(zero_std, 0.0)

        # RSRS 修正分 = ZScore * R2（RSRS-Right 标准定义）。
        # 语义统一（与 rsrs_momentum.py / Design.md [P1-03] 对齐）：
        #   rsrs_zscore 存【修正分】，即 z * r2 —— signal_generator 的
        #   ±0.7 阈值针对修正分（量级被 R2 压缩）设计。
        #   rsrs_score 为 rsrs_zscore 的别名，向后兼容旧调用方。
        df['rsrs_zscore'] = z_score * df['rsrs_r2']
        df['rsrs_score'] = df['rsrs_zscore']

        return df

    # Adapt old method signature to new logic if needed, or update callers
    def process(self, df):
        return self.calculate_rsrs_vectorized(df)
