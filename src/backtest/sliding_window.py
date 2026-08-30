"""
SlidingWindowRunner：滑动窗口（默认半年）多策略回测。

动机：单一回测窗口的结果高度依赖起止点——上次整段回测 2024-01~2026-06
恰好覆盖牛市，总收益 +182.78% 但相对基准超额仅 +6.20%。按半年切窗逐段
评估，能暴露策略在不同市场周期（熊市/震荡/牛市）下的分段表现与一致性，
回答「超额是不是只在某一段行情里赚的」。

窗口语义：
  - 窗口按自然月对齐（如 2023-01-01 ~ 2023-06-30），默认 step = window，
    即不重叠的连续分段；step < window 时为重叠滚动窗（此时窗口间有重叠，
    跨窗复利不再有意义，summary 中会标注 compound_valid=False）。
  - 每个窗口独立跑一次 vibe 回测：config.start_date = 窗口起点，初始资金
    重置；warmup 数据通过缓存注入提供（帧内容自 fetch_start 起、长于窗口），
    signal_engine 的 EVAL_START 门控保证窗口之前不交易。各窗口的评估指标
    由 exporter.write_evaluation_metrics() 按窗口切片计算。

数据策略（重要）：
  全部窗口共享同一份 data/*.csv，开跑前统一拉一次。不能把数据拉取交给
  每个窗口的 exporter.ensure_data 各自处理——broker 的 to_csv 是整段覆写
  而非增量合并，前一个窗口较短的拉取区间会把后续窗口需要的数据截掉，
  引发逐窗反复重拉。

输出：
  runs/<base>/w<idx>_<label>_<strategy>/   每窗口每策略的完整回测产物
  runs/<base>/summary.json                 机器可读聚合
  runs/<base>/summary.md                   人读对比表（窗口×策略矩阵）

用法：
  python3 main.py sliding_backtest \
      --codes 512480.SH,513100.SH,588000.SH \
      --start 2023-01-01 --end 2026-06-30 \
      --strategies rsrs_rotation,rsrs_timing,equal_weight
"""
import calendar
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from loguru import logger

from src.backtest.vibe_exporter import VibeBacktestExporter, DATA_DIR

# 本文件位于 <root>/src/backtest/，故 root = parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = PROJECT_ROOT / "runs"

DEFAULT_STRATEGIES = ("rsrs_rotation", "rsrs_timing", "equal_weight")


# ----------------------------------------------------------------------
# 窗口生成
# ----------------------------------------------------------------------
def _add_months(d: date, k: int) -> date:
    """日期按自然月加 k 个月（日溢出时钳到月末）。"""
    y = d.year + (d.month - 1 + k) // 12
    m = (d.month - 1 + k) % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def build_windows(
    start: str,
    end: str,
    window_months: int = 6,
    step_months: int | None = None,
    allow_partial: bool = False,
) -> list[tuple[date, date]]:
    """生成 [(eval_start, eval_end), ...] 闭区间窗口列表。

    默认只收完整窗口；allow_partial=True 时末尾不足一个完整窗口的
    部分窗口也纳入（截到 end）。
    """
    start_d = pd.Timestamp(start).date()
    end_d = pd.Timestamp(end).date()
    step = step_months or window_months
    if window_months < 1 or step < 1:
        raise ValueError("window_months/step_months 必须 >= 1")

    windows: list[tuple[date, date]] = []
    i = 0
    while True:
        ws = _add_months(start_d, i * step)
        if ws > end_d:
            break
        we = _add_months(ws, window_months) - timedelta(days=1)
        if we > end_d:
            if allow_partial and ws < end_d:
                windows.append((ws, end_d))
            break
        windows.append((ws, we))
        if we == end_d:
            break
        i += 1
    return windows


# ----------------------------------------------------------------------
# 运行器
# ----------------------------------------------------------------------
class SlidingWindowRunner:
    def __init__(
        self,
        codes: list[str],
        start: str,
        end: str,
        window_months: int = 6,
        step_months: int | None = None,
        strategies=DEFAULT_STRATEGIES,
        run_base: str = "sliding",
        initial_cash: float = 100_000.0,
        top_k: int = 2,
        n: int = 18,
        m: int = 600,
        min_score: float = 0.0,
        warmup_years: int = 3,
        fetch_start: str | None = None,
        allow_partial: bool = False,
    ):
        self.codes = list(codes)
        self.window_months = window_months
        self.step_months = step_months
        self.strategies = [s for s in strategies if s]
        self.run_base = run_base
        self.initial_cash = initial_cash
        self.top_k = top_k
        self.n, self.m = n, m
        self.min_score = min_score
        self.allow_partial = allow_partial

        self.windows = build_windows(
            start, end, window_months, step_months, allow_partial)
        if not self.windows:
            raise ValueError(
                f"[{start} ~ {end}] 无法切出任何窗口 "
                f"(window={window_months}m, allow_partial={allow_partial})")

        self.global_eval_start = self.windows[0][0]
        self.global_eval_end = self.windows[-1][1]
        if fetch_start is None:
            fetch_start = (
                pd.Timestamp(self.global_eval_start)
                - pd.Timedelta(days=365 * warmup_years)
            ).strftime("%Y-%m-%d")
        self.fetch_start = str(fetch_start)

        self.summary_dir = RUNS_DIR / run_base
        self._failed: list[str] = []

    # ------------------------------------------------------------------
    # 全局一次性确保数据
    # ------------------------------------------------------------------
    def ensure_global_data(self):
        """确保 CSV 覆盖 [fetch_start, global_eval_end]，不足则拉一次。

        注意：若标的上市晚于 fetch_start，拉取后 CSV 起点仍晚于
        fetch_start——这是数据本身的边界，不视为失败（RSRS warmup 相应
        缩短，signal_engine 对 NaN 分数有优雅降级）。
        """
        from src.data_engine.vibe_market_loader import VibeMarketLoader

        ge = str(self.global_eval_end)
        # fetch_start 可能落在节假日（如元旦），首个 K 线日期会晚于它；
        # 给 10 天容差，否则每次运行都会触发无谓的全量重拉。
        gs_tol = (
            pd.Timestamp(self.fetch_start) + pd.Timedelta(days=10)
        ).date()
        need = []
        for code in self.codes:
            csv = DATA_DIR / f"{code}.csv"
            ok = False
            if csv.exists():
                try:
                    df = pd.read_csv(csv, parse_dates=["date"])
                    ok = (
                        not df.empty
                        and df["date"].min().date() <= gs_tol
                        and str(df["date"].max().date()) >= ge
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[sliding] {csv} 读取失败: {exc}")
            if not ok:
                need.append(code)
        if need:
            logger.info(f"[sliding] 统一拉取 {need} "
                        f"[{self.fetch_start} ~ {ge}]（整段覆写 CSV）")
            VibeMarketLoader().fetch_and_sync(need, self.fetch_start, ge)
        else:
            logger.info("[sliding] 现有 CSV 已覆盖全部窗口，跳过拉取")

    # ------------------------------------------------------------------
    # 主流程：逐窗口 × 逐策略
    # ------------------------------------------------------------------
    def run(self) -> dict:
        self.ensure_global_data()
        results: dict[tuple[int, str], dict] = {}

        for wi, (ws, we) in enumerate(self.windows):
            label = f"{ws.year}H{1 if ws.month <= 6 else 2}"
            for strat in self.strategies:
                name = f"{self.run_base}/w{wi + 1:02d}_{label}_{strat}"
                exporter = VibeBacktestExporter(
                    run_name=name,
                    codes=self.codes,
                    eval_start=str(ws),
                    eval_end=str(we),
                    fetch_start=self.fetch_start,
                    initial_cash=self.initial_cash,
                    top_k=self.top_k,
                    n=self.n,
                    m=self.m,
                    min_score=self.min_score,
                    strategy=strat,
                )
                try:
                    rc = exporter.run(ensure_data_first=False)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[sliding] {name} 异常: {exc}")
                    rc = -1
                mfile = exporter.run_dir / "evaluation_metrics.json"
                if rc == 0 and mfile.exists():
                    results[(wi, strat)] = json.loads(
                        mfile.read_text(encoding="utf-8"))
                    m = results[(wi, strat)]
                    logger.success(
                        f"[sliding] w{wi + 1:02d} {label} {strat}: "
                        f"总收益 {m['total_return']:+.2%} | "
                        f"超额 {m.get('excess_return', float('nan')):+.2%} | "
                        f"回撤 {m['max_drawdown']:.2%}")
                else:
                    self._failed.append(name)
                    logger.error(f"[sliding] {name} 失败 (rc={rc})")

        return self.summarize(results)

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------
    def summarize(self, results: dict) -> dict:
        n_win = len(self.windows)
        step = self.step_months or self.window_months
        compound_valid = step >= self.window_months  # 重叠窗复利无意义

        window_rows = []
        for wi, (ws, we) in enumerate(self.windows):
            row = {"idx": wi + 1, "start": str(ws), "end": str(we),
                   "strategies": {}}
            for s in self.strategies:
                m = results.get((wi, s))
                if m:
                    row["strategies"][s] = {
                        "total_return": m.get("total_return"),
                        "annual_return": m.get("annual_return"),
                        "sharpe": m.get("sharpe"),
                        "max_drawdown": m.get("max_drawdown"),
                        "excess_return": m.get("excess_return"),
                    }
            window_rows.append(row)

        by_strategy = {}
        for s in self.strategies:
            ms = [results[(wi, s)] for wi in range(n_win)
                  if (wi, s) in results]
            if not ms:
                continue
            trs = [float(m["total_return"]) for m in ms]
            shs = [float(m["sharpe"]) for m in ms]
            mdds = [float(m["max_drawdown"]) for m in ms]
            exs = [float(m["excess_return"]) for m in ms
                   if m.get("excess_return") is not None]
            compound = 1.0
            for t in trs:
                compound *= (1.0 + t)
            by_strategy[s] = {
                "windows_completed": len(ms),
                "compound_return": compound - 1.0,
                "mean_window_return": sum(trs) / len(trs),
                "best_window": max(trs),
                "worst_window": min(trs),
                "positive_window_rate": sum(1 for t in trs if t > 0) / len(trs),
                "mean_sharpe": sum(shs) / len(shs),
                "worst_max_drawdown": min(mdds),
                "mean_excess_return": (sum(exs) / len(exs)) if exs else None,
                "positive_excess_rate": (
                    sum(1 for e in exs if e > 0) / len(exs)) if exs else None,
            }

        summary = {
            "run_base": self.run_base,
            "codes": self.codes,
            "window_months": self.window_months,
            "step_months": step,
            "compound_valid": compound_valid,
            "fetch_start": self.fetch_start,
            "n_windows": n_win,
            "windows": window_rows,
            "by_strategy": by_strategy,
            "failed_runs": self._failed,
        }

        self.summary_dir.mkdir(parents=True, exist_ok=True)
        (self.summary_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (self.summary_dir / "summary.md").write_text(
            self._render_md(summary), encoding="utf-8")
        logger.success(f"[sliding] summary -> {self.summary_dir / 'summary.md'}")
        return summary

    # ------------------------------------------------------------------
    # Markdown 渲染
    # ------------------------------------------------------------------
    @staticmethod
    def _fmt(v, pct=True) -> str:
        if v is None:
            return "—"
        return f"{v:+.2%}" if pct else f"{v:.2f}"

    def _render_md(self, s: dict) -> str:
        strategies = [x for x in self.strategies if x in s["by_strategy"]]
        lines = [f"# 滑动窗口回测汇总（{s['run_base']}）", ""]
        lines.append(f"- 标的：{', '.join(s['codes'])}")
        lines.append(f"- 窗口：{s['window_months']} 个月 × "
                     f"{s['n_windows']} 段（步长 {s['step_months']} 个月）")
        lines.append(f"- warmup 数据起点：{s['fetch_start']}")
        if not s["compound_valid"]:
            lines.append("- ⚠️ 窗口存在重叠，跨窗复利仅供粗略参考")
        if s["failed_runs"]:
            lines.append(f"- ⚠️ 失败 run：{', '.join(s['failed_runs'])}")
        lines.append("")

        # 表 1：各窗口总收益
        lines.append("## 各窗口总收益")
        lines.append("| 窗口 | 区间 | " + " | ".join(strategies) + " |")
        lines.append("|" + "---|" * (2 + len(strategies)))
        for w in s["windows"]:
            cells = [f"{w['idx']:02d}", f"{w['start']}~{w['end']}"]
            for st in strategies:
                d = w["strategies"].get(st)
                cells.append(self._fmt(d["total_return"]) if d else "—")
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

        # 表 2：各窗口超额（相对窗口内等权买入持有基准，由 write_evaluation_metrics 自算。
        # equal_weight 策略本身即该基准的实盘化，其超额应 ≈0，可作为基准口径的一致性校验）
        lines.append("## 各窗口超额收益（相对窗口等权买入持有基准）")
        lines.append("| 窗口 | " + " | ".join(strategies) + " |")
        lines.append("|" + "---|" * (1 + len(strategies)))
        for w in s["windows"]:
            cells = [f"{w['idx']:02d}"]
            for st in strategies:
                d = w["strategies"].get(st)
                cells.append(self._fmt(d.get("excess_return")) if d else "—")
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

        # 表 3：策略聚合
        lines.append("## 策略聚合（跨全部窗口）")
        lines.append("| 策略 | 窗口数 | 跨窗复利 | 平均单窗 | 最差单窗 | "
                     "单窗胜率 | 平均夏普 | 最深回撤 | 平均超额 | 超额胜率 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for st in strategies:
            d = s["by_strategy"][st]
            lines.append(
                f"| {st} | {d['windows_completed']} "
                f"| {self._fmt(d['compound_return'])} "
                f"| {self._fmt(d['mean_window_return'])} "
                f"| {self._fmt(d['worst_window'])} "
                f"| {d['positive_window_rate']:.0%} "
                f"| {d['mean_sharpe']:.2f} "
                f"| {self._fmt(d['worst_max_drawdown'])} "
                f"| {self._fmt(d['mean_excess_return'])} "
                f"| {(d['positive_excess_rate'] or 0):.0%} |"
            )
        lines.append("")
        lines.append("> 判读：主动策略（rsrs_rotation / rsrs_timing）若"
                     "「跨窗复利 / 最差单窗 / 超额胜率」不明显优于 equal_weight，"
                     "则策略有效性存疑，超额大概率来自 beta。")
        return "\n".join(lines)
