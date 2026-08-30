"""
历史遗留模块：与 factors/rsrs.py 重复的 RSRSCalculator。

两个实现曾因 rsrs_zscore 语义不一致（raw z vs 修正分 z*r2）产生分歧，
现已统一：canonical 实现在 src/strategies/factors/rsrs.py，
本模块仅作薄别名重导出，保证旧 import 路径不失效。
"""
from src.strategies.factors.rsrs import RSRSCalculator

__all__ = ["RSRSCalculator"]
