"""
VibeBacktestExporter：把 AlphaQuant 的 RSRS 轮动策略导出为 vibe-trading
回测 run_dir 并驱动其 runner 执行。

替代关系（Design.md [P1-04] 的实现调整）：
  原：Backtrader（backtrader 包未安装，src/backtest/engine.py 不可运行）
  新：vibe-trading backtest.runner + ChinaAEngine
      - ChinaAEngine 内置 A 股规则：T+1、禁做空、100 股整手、涨跌停
      - ETF 免印花税：config 显式 stamp_tax=0（默认 0.0005 会系统性低估收益）

工作原理（免 API key 的关键）：
  1. 数据由 VibeMarketLoader（tencent 免费链、前复权）预先拉好，落 data/*.csv
  2. 本模块把 CSV 注入 vibe 的 loader cache，通道为 CACHE_SOURCE="akshare"
  3. runner 的 config 声明 source=CACHE_SOURCE，于是 akshare loader 的
     cached_loader_fetch 先查缓存，全命中则全程不触碰 API、不需要 token；
     akshare 与 tushare 一样路由到 ChinaAEngine（A 股规则）
     ⚠️ 通道不能用 tushare：其 is_available() 要求 TUSHARE_TOKEN，无 token
        时 registry 直接跳过它，缓存永远读不到，引擎会真调 akshare 拿
        **不复权**数据（实测 512480 同日收盘 0.352 vs 0.703）。
  4. RSRS 轮动信号以内联 signal_engine.py 提供（vibe AST 沙箱合规：
     无顶层可执行语句、无文件 I/O、无网络调用）

子进程隔离（src 包名冲突）：
  vibe-trading 把顶层包 src/backtest 装进 site-packages，与本项目 src 同名。
  runner 一律以 cwd=$HOME 的子进程运行，且通过
  VIBE_TRADING_ALLOWED_RUN_ROOTS 把本项目 runs/ 目录加入其白名单。
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
from loguru import logger

# 本文件位于 <root>/src/backtest/，故 root = parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
RUNS_DIR = PROJECT_ROOT / "runs"
CACHE_ROOT = DATA_DIR / "vibe_cache"

# 与 vibe backtest/loaders/base.py 的 _LOADER_CACHE_VERSION 保持一致
_LOADER_CACHE_VERSION = 4

# ⚠️ 缓存注入的通道必须是 akshare，不能用 tushare：
#   vibe 的 registry 只把 is_available() 为真的 source 纳入回退链，而
#   tushare.is_available() 要求配置 TUSHARE_TOKEN。无 token 时 tushare 直接
#   被跳过（日志 "tushare is unavailable, falling back to akshare"），其缓存
#   永远读不到，引擎会真的去调 akshare 拿**不复权**数据。
#   akshare.is_available() 仅检查包是否安装 → 恒真，缓存可被命中。
#   数据本身仍是 tencent 前复权（由 fetch 阶段决定），通道只是投递管道。
CACHE_SOURCE = "akshare"


# ----------------------------------------------------------------------
# vibe loader cache 注入（key 逻辑复刻自 vibe backtest/loaders/base.py）
# ----------------------------------------------------------------------
def _norm_date(value: str) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _loader_cache_key(symbol: str, start_date: str, end_date: str,
                      source: str = CACHE_SOURCE) -> str:
    payload = {
        "version": _LOADER_CACHE_VERSION,
        "source": source,
        "symbol": str(symbol),
        "timeframe": "1D",
        "start_date": _norm_date(start_date),
        "end_date": _norm_date(end_date),
        "fields": [],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def inject_loader_cache(symbol: str, df: pd.DataFrame,
                        start_date: str, end_date: str,
                        cache_root: Path = CACHE_ROOT) -> Path:
    """把日线 DataFrame 写成 vibe loader 的缓存条目（通道见 CACHE_SOURCE）。

    df: index=datetime(trade_date), columns=[open,high,low,close,volume]
    缓存 key 的日期 = 回测 config 的日期；帧内容可长于该区间（warmup）。
    """
    key = _loader_cache_key(symbol, start_date, end_date, CACHE_SOURCE)
    dest_dir = cache_root / CACHE_SOURCE
    dest_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = dest_dir / f"{key}.parquet"

    frame = df[["open", "high", "low", "close", "volume"]].copy()
    frame.index.name = "trade_date"
    cache_frame = frame.reset_index()
    cache_frame.to_parquet(parquet_path, engine="pyarrow", index=False)

    metadata = {
        "version": _LOADER_CACHE_VERSION,
        "index_columns": ["trade_date"],
        "index_names": ["trade_date"],
        "columns_name": None,
        "index_dtypes": [str(frame.index.dtype)],
    }
    meta_path = parquet_path.with_suffix(parquet_path.suffix + ".json")
    meta_path.write_text(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    logger.info(f"[exporter] cache injected: {symbol} "
                f"[{frame.index.min().date()} ~ {frame.index.max().date()}] "
                f"({len(frame)} bars)")
    return parquet_path


# ----------------------------------------------------------------------
# signal_engine.py 模板（RSRS 轮动；vibe AST 沙箱合规）
# ----------------------------------------------------------------------
SIGNAL_ENGINE_TEMPLATE = '''"""AlphaQuant RSRS 轮动信号引擎（由 vibe_exporter 生成）。

截面轮动：对每个标的计算 RSRS 修正分（beta 的 M 期 Z-Score * R2），
按当日截面排名取 top_k 等权；低于 min_score 的标的不入选。
输出目标权重 Dict[code, pd.Series]，引擎自动 shift 1 bar（next-bar-open）
并归一化 sum(abs)<=1。
"""


class SignalEngine:
    N = __N__
    M = __M__
    MIN_PERIODS = __MIN_PERIODS__
    TOP_K = __TOP_K__
    MIN_SCORE = __MIN_SCORE__
    # 评估起点：早于该日的信号一律置 0（空仓）。
    # vibe 0.1.14 不支持 config.evaluation_start_date，回测日期轴取自
    # data_map 的并集（含 warmup 段），只能在此处挡住 warmup 期的交易，
    # 否则 RSRS 窗口不足时的失真信号会污染净值曲线。
    EVAL_START = "__EVAL_START__"

    def generate(self, data_map):
        import numpy as np
        import pandas as pd

        n = self.N
        m = self.M
        min_periods = self.MIN_PERIODS
        scores = {}
        for code, df in data_map.items():
            scores[code] = self._rsrs_score(df, n, m, min_periods)
        if not scores:
            return {}

        panel = pd.DataFrame(scores).sort_index()
        # 缺数据的标的填充极小值：排名靠后且低于 min_score，不会入选
        panel = panel.fillna(-1e9)
        rank = panel.rank(axis=1, ascending=False, method="first")
        mask = panel.ge(self.MIN_SCORE) & (rank <= self.TOP_K)
        weights = mask.astype(float)
        row_sums = weights.sum(axis=1)
        weights = weights.div(row_sums.replace(0, float("nan")), axis=0)
        weights = weights.fillna(0.0)

        # 评估起点之前强制空仓：warmup 段不参与交易
        if self.EVAL_START:
            cutoff = pd.Timestamp(self.EVAL_START)
            weights.loc[weights.index < cutoff] = 0.0

        out = {}
        for code in data_map:
            out[code] = weights[code].reindex(data_map[code].index).fillna(0.0)
        return out

    def _rsrs_score(self, df, n, m, min_periods):
        """向量化 RSRS：sliding_window_view OLS + M 期 Z-Score + R2 修正。"""
        import numpy as np
        import pandas as pd

        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        if len(df) < n + 2:
            return pd.Series(float("nan"), index=df.index)

        x_win = np.lib.stride_tricks.sliding_window_view(low, n)
        y_win = np.lib.stride_tricks.sliding_window_view(high, n)
        x_mean = x_win.mean(axis=1, keepdims=True)
        y_mean = y_win.mean(axis=1, keepdims=True)
        numerator = ((x_win - x_mean) * (y_win - y_mean)).sum(axis=1)
        denominator = ((x_win - x_mean) ** 2).sum(axis=1)
        beta = np.divide(
            numerator, denominator,
            out=np.full_like(numerator, float("nan")),
            where=denominator != 0,
        )
        x_std = x_win.std(axis=1)
        y_std = y_win.std(axis=1)
        corr = np.divide(
            numerator / n, x_std * y_std,
            out=np.full_like(numerator, float("nan")),
            where=(x_std * y_std) != 0,
        )
        r2 = corr ** 2

        pad = np.full(n - 1, float("nan"))
        beta_pad = np.concatenate([pad, beta])
        r2_pad = np.concatenate([pad, r2])
        beta_s = pd.Series(beta_pad, index=df.index)
        r2_s = pd.Series(r2_pad, index=df.index)
        roll_mean = beta_s.rolling(m, min_periods=min_periods).mean()
        roll_std = beta_s.rolling(m, min_periods=min_periods).std()
        z = (beta_s - roll_mean) / roll_std.replace(0, float("nan"))
        return z * r2_s
'''


# 周频轮动：由日频模板派生，仅在「归一化之后、EVAL_START 置零之前」插入
# 周频化步骤——每周首个交易日的目标权重持有整周，其余日期沿用上值。
# 派生而非复制，保证两份模板的 RSRS 核心永不漂移；锚点串若随模板改动
# 失效，下面的 assert 会在 import 时立刻暴露。
_WEEKLY_ANCHOR = '''        # 评估起点之前强制空仓：warmup 段不参与交易
        if self.EVAL_START:'''
_WEEKLY_INSERT = '''        # 周频化：每周首个交易日才调仓，目标权重持有整周
        period = weights.index.to_period("W")
        is_first = pd.Series(~period.duplicated(), index=weights.index)
        weights = weights.where(is_first).ffill().fillna(0.0)

'''
assert SIGNAL_ENGINE_TEMPLATE.count(_WEEKLY_ANCHOR) == 1, \
    "周频模板锚点在日频模板中不唯一，派生失败"
WEEKLY_ENGINE_TEMPLATE = SIGNAL_ENGINE_TEMPLATE.replace(
    'AlphaQuant RSRS 轮动信号引擎',
    'AlphaQuant RSRS 周频轮动信号引擎',
).replace(
    _WEEKLY_ANCHOR,
    _WEEKLY_INSERT + _WEEKLY_ANCHOR,
)
assert WEEKLY_ENGINE_TEMPLATE != SIGNAL_ENGINE_TEMPLATE

# 月频轮动：由周频模板再派生——仅把「周频化」的 to_period("W") 换成
# to_period("M")，其余（RSRS 核心 + EVAL_START 置零）完全一致。锚点串若随
# 周频模板改动失效，下面的 assert 会在 import 时立刻暴露。
_MONTHLY_ANCHOR = '        period = weights.index.to_period("W")'
assert WEEKLY_ENGINE_TEMPLATE.count(_MONTHLY_ANCHOR) == 1, \
    "月频模板锚点在周频模板中不唯一，派生失败"
MONTHLY_ENGINE_TEMPLATE = WEEKLY_ENGINE_TEMPLATE.replace(
    'AlphaQuant RSRS 周频轮动信号引擎',
    'AlphaQuant RSRS 月频轮动信号引擎',
).replace(
    '        # 周频化：每周首个交易日才调仓，目标权重持有整周',
    '        # 月频化：每月首个交易日才调仓，目标权重持有整月',
).replace(
    _MONTHLY_ANCHOR,
    '        period = weights.index.to_period("M")',
)
assert MONTHLY_ENGINE_TEMPLATE != WEEKLY_ENGINE_TEMPLATE


TIMING_ENGINE_TEMPLATE = '''"""AlphaQuant RSRS 择时信号引擎（由 vibe_exporter 生成）。

单标的滞回择时（hysteresis）：RSRS 修正分上穿 ENTRY 开仓、下穿 EXIT 平仓，
两阈值之间维持原状态，避免边界抖动导致频繁换手。
每个标的独立择时，持仓时分配 1/n_codes 资金（可叠加，最多满仓）。
状态机在整条数据轴（含 warmup）上运行，评估起点前的仓位被置零但状态保留，
窗口首日的持仓状态由此前真实数据决定，无前视偏差。
"""


class SignalEngine:
    N = __N__
    M = __M__
    MIN_PERIODS = __MIN_PERIODS__
    ENTRY = __ENTRY__
    EXIT = __EXIT__
    EVAL_START = "__EVAL_START__"

    def generate(self, data_map):
        import pandas as pd

        n_codes = max(len(data_map), 1)
        unit = 1.0 / n_codes
        out = {}
        for code, df in data_map.items():
            score = self._rsrs_score(df, self.N, self.M, self.MIN_PERIODS)
            weights = pd.Series(0.0, index=score.index)
            state = False
            for i, v in enumerate(score.to_numpy()):
                if pd.notna(v):
                    if not state and v > self.ENTRY:
                        state = True
                    elif state and v < self.EXIT:
                        state = False
                weights.iloc[i] = unit if state else 0.0
            # 评估起点之前强制空仓：warmup 段不参与交易
            if self.EVAL_START:
                cutoff = pd.Timestamp(self.EVAL_START)
                weights.loc[weights.index < cutoff] = 0.0
            out[code] = weights
        return out

    def _rsrs_score(self, df, n, m, min_periods):
        """向量化 RSRS：sliding_window_view OLS + M 期 Z-Score + R2 修正。"""
        import numpy as np
        import pandas as pd

        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        if len(df) < n + 2:
            return pd.Series(float("nan"), index=df.index)

        x_win = np.lib.stride_tricks.sliding_window_view(low, n)
        y_win = np.lib.stride_tricks.sliding_window_view(high, n)
        x_mean = x_win.mean(axis=1, keepdims=True)
        y_mean = y_win.mean(axis=1, keepdims=True)
        numerator = ((x_win - x_mean) * (y_win - y_mean)).sum(axis=1)
        denominator = ((x_win - x_mean) ** 2).sum(axis=1)
        beta = np.divide(
            numerator, denominator,
            out=np.full_like(numerator, float("nan")),
            where=denominator != 0,
        )
        x_std = x_win.std(axis=1)
        y_std = y_win.std(axis=1)
        corr = np.divide(
            numerator / n, x_std * y_std,
            out=np.full_like(numerator, float("nan")),
            where=(x_std * y_std) != 0,
        )
        r2 = corr ** 2

        pad = np.full(n - 1, float("nan"))
        beta_pad = np.concatenate([pad, beta])
        r2_pad = np.concatenate([pad, r2])
        beta_s = pd.Series(beta_pad, index=df.index)
        r2_s = pd.Series(r2_pad, index=df.index)
        roll_mean = beta_s.rolling(m, min_periods=min_periods).mean()
        roll_std = beta_s.rolling(m, min_periods=min_periods).std()
        z = (beta_s - roll_mean) / roll_std.replace(0, float("nan"))
        return z * r2_s
'''

EQUAL_WEIGHT_ENGINE_TEMPLATE = '''"""AlphaQuant 等权持有基准信号引擎（由 vibe_exporter 生成）。

被动对照：全部候选标的等权买入持有，不做任何择时。
用途：回答「策略的超额是 alpha 还是 beta」——若择时/轮动的跨窗表现
跑不赢等权持有，则主动策略没有存在价值。
"""


class SignalEngine:
    EVAL_START = "__EVAL_START__"

    def generate(self, data_map):
        import pandas as pd

        n = max(len(data_map), 1)
        out = {}
        for code, df in data_map.items():
            weights = pd.Series(1.0 / n, index=df.index, dtype=float)
            # 评估起点之前强制空仓：与主动策略同一起跑线
            if self.EVAL_START:
                cutoff = pd.Timestamp(self.EVAL_START)
                weights.loc[weights.index < cutoff] = 0.0
            out[code] = weights
        return out
'''

# ----------------------------------------------------------------------
# 策略注册表：策略名 -> signal_engine 模板
# 新策略接入：实现一个 AST 沙箱合规模板（无顶层可执行语句/文件 IO/网络）
# 并在此注册，即可被 backtest --strategy 与 sliding_backtest 使用。
# 渲染时未出现在模板中的占位符 replace 为无害 no-op。
# ----------------------------------------------------------------------
STRATEGY_TEMPLATES: dict[str, str] = {
    "rsrs_rotation": SIGNAL_ENGINE_TEMPLATE,   # 截面轮动：top_k 等权，日频调仓
    "rsrs_rotation_weekly": WEEKLY_ENGINE_TEMPLATE,  # 同上，周频调仓
    "rsrs_rotation_monthly": MONTHLY_ENGINE_TEMPLATE,  # 同上，月频调仓（降换手）
    "rsrs_timing": TIMING_ENGINE_TEMPLATE,     # 单标的滞回择时
    "equal_weight": EQUAL_WEIGHT_ENGINE_TEMPLATE,  # 被动等权对照
}


# ----------------------------------------------------------------------
# 导出器主体
# ----------------------------------------------------------------------
class VibeBacktestExporter:
    def __init__(
        self,
        run_name: str,
        codes: list[str],
        eval_start: str,       # 回测评估起点 YYYY-MM-DD
        eval_end: str,         # 回测评估终点（须早于今天，缓存才生效）
        fetch_start: str = None,  # 数据起点（含 warmup；默认 eval_start 前 3 年）
        initial_cash: float = 100_000.0,
        top_k: int = 2,
        n: int = 18,
        m: int = 600,
        min_periods: int = 60,
        min_score: float = 0.0,
        strategy: str = "rsrs_rotation",
        entry_threshold: float = 0.7,   # rsrs_timing 开仓阈值
        exit_threshold: float = -0.7,   # rsrs_timing 平仓阈值
        commission_rate: float = 0.00025,
        commission_min: float = 5.0,
    ):
        if strategy not in STRATEGY_TEMPLATES:
            raise ValueError(
                f"未知策略 {strategy!r}；可选: {sorted(STRATEGY_TEMPLATES)}")
        self.strategy = strategy
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.run_name = run_name
        self.codes = list(codes)
        self.eval_start = eval_start
        self.eval_end = eval_end
        if fetch_start is None:
            # RSRS M=600 约需 2.5 年 lookback，取 3 年缓冲
            fetch_start = (
                pd.Timestamp(eval_start) - pd.Timedelta(days=365 * 3)
            ).strftime("%Y-%m-%d")
        self.fetch_start = fetch_start
        self.initial_cash = initial_cash
        self.top_k = top_k
        self.n, self.m = n, m
        self.min_periods, self.min_score = min_periods, min_score
        self.commission_rate = commission_rate
        self.commission_min = commission_min

        self.run_dir = RUNS_DIR / run_name
        self.cache_root = CACHE_ROOT

    # ------------------------------------------------------------------
    # 步骤 1：确保本地 CSV 数据存在（必要时在线拉取）
    # ------------------------------------------------------------------
    def ensure_data(self):
        from src.data_engine.vibe_market_loader import VibeMarketLoader

        loader = VibeMarketLoader()
        missing = []
        for code in self.codes:
            csv = DATA_DIR / f"{code}.csv"
            need_fetch = True
            if csv.exists():
                df = pd.read_csv(csv, parse_dates=["date"])
                if not df.empty and str(df["date"].min().date()) <= self.fetch_start \
                        and str(df["date"].max().date()) >= self.eval_end:
                    need_fetch = False
            if need_fetch:
                missing.append(code)
        if missing:
            logger.info(f"[exporter] fetching {missing} "
                        f"[{self.fetch_start} ~ {self.eval_end}] via vibe tencent chain")
            loader.fetch_and_sync(missing, self.fetch_start, self.eval_end)

    # ------------------------------------------------------------------
    # 步骤 2：CSV -> vibe loader cache 注入
    # ------------------------------------------------------------------
    def inject_cache(self):
        injected = []
        for code in self.codes:
            csv = DATA_DIR / f"{code}.csv"
            if not csv.exists():
                raise FileNotFoundError(
                    f"{csv} 不存在；请先运行 ensure_data()/fetch_data"
                )
            df = pd.read_csv(csv, parse_dates=["date"])
            df = df.sort_values("date").set_index("date")
            if "volume" not in df.columns:
                df["volume"] = 0.0
            # 缓存 key 日期必须 = 回测 config 的 [start_date, end_date]
            # （即评估窗口 eval_start~eval_end），否则 runner 按 config 日期
            # 查缓存会 miss。而帧内容仍是完整 CSV（含 fetch_start 起的 warmup），
            # 因为 cached_loader_fetch 命中后 `return cached` 不做日期裁剪，
            # signal_engine 因此能在评估首日就拿到 M=600 的 RSRS 窗口。
            path = inject_loader_cache(
                code, df, self.eval_start, self.eval_end, self.cache_root
            )
            injected.append(path)
        return injected

    # ------------------------------------------------------------------
    # 步骤 3：生成 run_dir（config.json + code/signal_engine.py）
    # ------------------------------------------------------------------
    def build_run_dir(self) -> Path:
        code_dir = self.run_dir / "code"
        code_dir.mkdir(parents=True, exist_ok=True)

        config = {
            "codes": self.codes,
            # ⚠️ 安装的 vibe-trading 0.1.14 不支持 evaluation_start_date
            #    （该字段属于 ~/source/Vibe-Trading 的新版源码），写了会被静默
            #    忽略，导致回测从 start_date 起就在 warmup 期交易——RSRS 的
            #    M=600 窗口不足时信号失真，实测 2022 年产生 -42.58% 的污染收益。
            #    故 start_date 直接取评估起点 eval_start；warmup 数据通过
            #    缓存注入提供（帧内容长于 key 区间，见 inject_cache）。
            "start_date": self.eval_start,
            "end_date": self.eval_end,
            # source 必须与缓存注入通道一致（CACHE_SOURCE），否则缓存不命中、
            # 引擎会去真实调 akshare 拿不复权数据。akshare 同样路由 ChinaAEngine。
            "source": CACHE_SOURCE,
            "interval": "1D",
            "engine": "daily",
            "initial_cash": self.initial_cash,
            # ⚠️ 必须用 "hold" 而非 "rebalance"：
            #   rebalance 模式每天把持仓拉回目标权重，并对整篮做严格资金预检
            #   （projected_capital < -1e-9 即抛 "insufficient capital"）。
            #   ETF 轮动遇到 T+1 时，当日买入的仓位无法卖出（can_execute=False
            #   → reductions 为空），而新标的的开仓成本仍在，必然资金为负而崩溃。
            #   hold 模式只在方向变化时平仓重开，且买不起时按统一比例缩放篮子，
            #   实测可正常跑完。
            "position_adjustment": "hold",
            # A 股交易规则（ChinaAEngine 读取 config 这些字段）
            "commission_rate": self.commission_rate,
            "commission_min": self.commission_min,
            "stamp_tax": 0,  # ETF 免印花税（默认 0.0005 仅适用股票）
        }
        (self.run_dir / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        engine_src = (
            STRATEGY_TEMPLATES[self.strategy]
            .replace("__N__", str(self.n))
            .replace("__M__", str(self.m))
            .replace("__MIN_PERIODS__", str(self.min_periods))
            .replace("__TOP_K__", str(self.top_k))
            .replace("__MIN_SCORE__", str(self.min_score))
            .replace("__ENTRY__", str(self.entry_threshold))
            .replace("__EXIT__", str(self.exit_threshold))
            .replace('"__EVAL_START__"', repr(str(self.eval_start)))
        )
        (code_dir / "signal_engine.py").write_text(engine_src, encoding="utf-8")
        logger.success(f"[exporter] run_dir ready: {self.run_dir} "
                       f"(strategy={self.strategy})")
        return self.run_dir

    # ------------------------------------------------------------------
    # 步骤 4：驱动 vibe backtest.runner 子进程
    # ------------------------------------------------------------------
    def run(self, ensure_data_first: bool = True) -> int:
        if ensure_data_first:
            self.ensure_data()
        self.inject_cache()
        self.build_run_dir()

        env = os.environ.copy()
        # 开启 vibe loader cache 并指向本项目的注入目录
        env["VIBE_TRADING_DATA_CACHE"] = "true"
        env["VIBE_TRADING_DATA_CACHE_ROOT"] = str(self.cache_root)
        # 把本项目 runs/ 加入 runner 的运行目录白名单
        env["VIBE_TRADING_ALLOWED_RUN_ROOTS"] = str(RUNS_DIR)
        env["PYTHONUTF8"] = "1"

        cmd = [sys.executable, "-m", "backtest.runner", str(self.run_dir)]
        logger.info(f"[exporter] launching: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            cwd=str(Path.home()),  # 关键：避开本项目 src 包名冲突
            env=env,
            text=True,
            timeout=900,
        )
        if proc.returncode != 0:
            logger.error(f"[exporter] runner exited {proc.returncode}")
            return proc.returncode

        self.write_evaluation_metrics()
        logger.success(f"[exporter] backtest done, results in {self.run_dir}")
        return proc.returncode

    # ------------------------------------------------------------------
    # 步骤 5：按评估窗口重算指标
    # ------------------------------------------------------------------
    def write_evaluation_metrics(self) -> dict | None:
        """按 [eval_start, eval_end] 双边裁剪重算窗口指标。

        两个必须处理的口径问题（实测 2026-08-31 滑动窗口回测中发现）：
        1. 引擎的回测轴 = 注入帧全长：config 的 start/end_date 只决定
           loader 缓存 key，**不裁剪净值曲线**——曲线从帧首一直延伸到帧末。
           若只按 eval_start 裁剪起点，算出的是「窗口起点→帧末」的收益
           （半年窗口被算成 3.5 年，+187% 的假收益）。
        2. 引擎内置 benchmark_equity 同样是全帧口径（帧首→帧末），
           与窗口不可比。这里改用自算基准：窗口内各标的等权买入持有
           （close 首→末），语义明确且与策略同窗。
        """
        eq_path = self.run_dir / "artifacts" / "equity.csv"
        if not eq_path.exists():
            logger.warning(f"[exporter] {eq_path} 缺失，跳过评估窗口指标")
            return None

        df = pd.read_csv(eq_path, parse_dates=["timestamp"]).set_index("timestamp")
        df = df.sort_index()
        cutoff = pd.Timestamp(self.eval_start)
        end_cutoff = pd.Timestamp(self.eval_end) + pd.Timedelta(days=1)
        win = df[(df.index >= cutoff) & (df.index < end_cutoff)]
        if win.empty or len(win) < 2:
            logger.warning("[exporter] 评估窗口无数据，跳过指标重算")
            return None

        eq = win["equity"]
        total = float(eq.iloc[-1] / eq.iloc[0] - 1)
        years = max(len(eq) / 252.0, 1e-9)
        annual = float((1.0 + total) ** (1.0 / years) - 1.0) if total > -1 else -1.0

        ret = eq.pct_change().dropna()
        vol = float(ret.std() * (252 ** 0.5)) if len(ret) > 1 else 0.0
        sharpe = float(ret.mean() / ret.std() * (252 ** 0.5)) if ret.std() > 0 else 0.0
        downside = ret[ret < 0]
        sortino = (
            float(ret.mean() / downside.std() * (252 ** 0.5))
            if len(downside) > 1 and downside.std() > 0 else 0.0
        )
        mdd = float((eq / eq.cummax() - 1.0).min())

        metrics = {
            "eval_start": str(win.index.min().date()),
            "eval_end": str(win.index.max().date()),
            "bars": int(len(win)),
            "years": round(years, 4),
            "final_value": float(eq.iloc[-1]),
            "total_return": total,
            "annual_return": annual,
            "annualized_vol": vol,
            "sharpe": sharpe,
            "sortino": sortino,
            "max_drawdown": mdd,
            "calmar": float(annual / abs(mdd)) if mdd < 0 else 0.0,
            "avg_invested": (
                float(win["active_ret"].notna().mean())
                if "active_ret" in win.columns else None
            ),
        }

        # 基准：窗口内等权买入持有（自算；内置 benchmark_equity 为全帧口径，
        # 与窗口不可比）
        bm_total = self._equal_weight_benchmark(
            win.index.min(), win.index.max())
        if bm_total is not None:
            metrics["benchmark_return"] = bm_total
            metrics["excess_return"] = total - bm_total

        out_path = self.run_dir / "evaluation_metrics.json"
        out_path.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            f"[exporter] 评估窗口 [{metrics['eval_start']} ~ {metrics['eval_end']}] "
            f"总收益 {total:+.2%} | 年化 {annual:+.2%} | 夏普 {sharpe:.2f} | "
            f"最大回撤 {mdd:.2%}"
        )
        return metrics

    def _equal_weight_benchmark(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> float | None:
        """[start, end] 内各标的等权买入持有收益（close 首→末的均值）。"""
        rets = []
        for code in self.codes:
            csv = DATA_DIR / f"{code}.csv"
            if not csv.exists():
                continue
            df = pd.read_csv(csv, parse_dates=["date"]).set_index("date")
            df = df.sort_index()
            seg = df[(df.index >= start) & (df.index <= end)]
            if len(seg) < 2 or seg["close"].iloc[0] <= 0:
                continue
            rets.append(float(seg["close"].iloc[-1] / seg["close"].iloc[0] - 1.0))
        if not rets:
            return None
        return sum(rets) / len(rets)
