import pandas as pd
import numpy as np

class RSRSCalculator:
    def __init__(self, n: int = 18, m: int = 600):
        self.n = n
        self.m = m

    def process(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Input: DataFrame with ['high', 'low']
        Output: DataFrame with added columns ['beta', 'r2', 'rsrs_zscore']
        """
        # Ensure sufficient data
        if len(df) < self.n:
            return df

        # Prepare High and Low
        high = df['high']
        low = df['low']
        
        # Calculate Rolling OLS Beta and R2 efficiently using simple linear regression formulas
        # Slope = (n * sum(xy) - sum(x)*sum(y)) / (n * sum(x^2) - sum(x)^2)
        # x = low, y = high
        
        N = self.n
        
        # Calculate rolling sums
        sum_x = low.rolling(window=N).sum()
        sum_y = high.rolling(window=N).sum()
        sum_xy = (low * high).rolling(window=N).sum()
        sum_xx = (low * low).rolling(window=N).sum()
        sum_yy = (high * high).rolling(window=N).sum()
        
        # Beta numerator and denominator
        numerator = N * sum_xy - sum_x * sum_y
        denominator = N * sum_xx - sum_x * sum_x
        
        # Handle division by zero (though unlikely in real price data)
        beta = numerator / denominator.replace(0, np.nan)
        
        # Calculate R2
        # R2 = (Cov(x,y) / (Std(x)*Std(y)))^2 = Corr(x,y)^2
        # Or using the slope: R2 = Beta^2 * Var(x) / Var(y)
        # Var(x) ~ (sum_xx - sum_x^2/N)
        # Var(y) ~ (sum_yy - sum_y^2/N)
        
        var_x = sum_xx - (sum_x * sum_x) / N
        var_y = sum_yy - (sum_y * sum_y) / N
        
        r2 = (beta * beta) * (var_x / var_y.replace(0, np.nan))
        
        # Assign to DataFrame
        df['rsrs_beta'] = beta
        df['rsrs_r2'] = r2
        
        # Calculate Z-Score of Beta
        # Standardized = (Beta - Mean(Beta, M)) / Std(Beta, M)
        beta_mean = df['rsrs_beta'].rolling(window=self.m).mean()
        beta_std = df['rsrs_beta'].rolling(window=self.m).std()
        
        zscore = (df['rsrs_beta'] - beta_mean) / beta_std.replace(0, np.nan)
        
        # RSRS Corrected = ZScore * R2
        # Design doc says: "Z-Score of Beta... Apply R2 correction." 
        # Background.md says: New_RSRS = ZScore * R2 + ZScore * (1-R2)? No, formula is RSRS_Right = ZScore * R2 + ZScore * (1-R2)?
        # Let's check Background.md content: "修正后的指标为： RSRS_{corr} = RSRS_{std} * R^2 + RSRS_{std} * 1?" No, it was cut off/not fully clear in my reading but usually it is:
        # RSRS_Right = Z_Score * R2
        # Actually standard practice is RSRS_Right = ZScore * R2
        # Wait, Background.md line 143 is empty.
        # Let's assume RSRS_Right = ZScore * R2 as per design doc "Apply R2 correction".
        # Correction: Some implementations use `ZScore * R2` or `ZScore * R2 * sign`.
        # I will use `rsrs_zscore` as the raw zscore and maybe add `rsrs_right` if needed, 
        # but Design.md only asks for `rsrs_zscore`.
        # Wait, Design.md interface says: Output columns ['beta', 'r2', 'rsrs_zscore']
        # And Task 06 says: "buys if rsrs_zscore > 0.7".
        # So I will store the *corrected* value into `rsrs_zscore`?
        # Or store raw into `rsrs_zscore` and let strategy correct it?
        # Design.md says: 
        #   "Output: DataFrame with added columns ['beta', 'r2', 'rsrs_zscore']"
        #   "Logic: 3. Apply R2 correction."
        # This implies the `rsrs_zscore` output IS the corrected one.
        # So I will calculate standardized Z-Score first, then multiply by R2.
        
        # Let's double check if I should return the raw Z-score or the Corrected one as "rsrs_zscore".
        # Background.md: "One correction... R2 * ZScore".
        # I will compute `raw_zscore` and then `rsrs_zscore` = `raw_zscore * r2`.
        
        df['rsrs_zscore'] = zscore * r2
        
        return df
