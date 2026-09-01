#!/usr/bin/env python3
"""QDII 溢价套利每日定时任务编排（§7.18 / §7.20 / §9 产品化落地）。

晚间（21:30）由 crontab 触发，按序执行四件事并把结果落日志：

  1. 监控快照 `qdii_monitor.py`  —— 当前截面官方/影子 IOPV 溢价告警
     -> runs/qdii_premium.md / .json
  2. 套利回测 `qdii_backtest.py --refresh` —— 重拉历史溢价序列（追加最新数据点）
     并重算「溢价回避 / 折价买入」策略绩效
     -> runs/qdii_backtest.md / .json + data/fundamental/qdii_premium_*.csv
  3. 三层组合实盘信号 `portfolio_live.py` —— 刷新 18 只 ETF 收盘价（vibe
     tencent 前复权链增量）+ 乐咕 PB，按 §7.20 回测口径输出「明日目标持仓」
     与动作清单（月频再平衡 + 每日 QDII 门控刷新）
     -> runs/portfolio_live.md / .json
  4. 模拟盘对账 `paper_trading.py reconcile` —— 20 万模拟盘按最新收盘价估值，
     与第 3 步目标权重对账，输出偏差与调仓建议、追加当日净值
     -> runs/paper_trading.md + data/paper_nav.csv（账本 data/paper_ledger.json）

设计要点：
  * 每步用 subprocess 独立进程跑，任一步失败不影响另一步（错误隔离）。
  * 日志追加到 logs/qdii_daily.log（时间戳 + 每步成败 + 摘要）。
  * 交易日 21:30 触发（crontab: `30 21 * * 1-5 ...`），节假日会空跑（数据无变化，
    无害）。
  * 21:30 而非 15:30 的原因：东财晚间才更新 QDII 当日净值（15:30 时欧美腿
    净值只到 T-2、亚洲腿 T-1，gate_lag_test 实测实盘夏普 1.31→~1.0）；
    21:30 时净值已补到 T-1~T（滞后降到 0~1 天）。代价：监控告警从盘中变盘后。
  * 第 3 步依赖第 2 步刚刷新的 QDII 溢价缓存，顺序不可调换。
  * 第 4 步依赖第 3 步的 portfolio_live.json（目标权重），但缺失时只出账本
    视图不崩溃（降级容忍）。

用法：python3 scripts/qdii_daily.py [--skip-backtest] [--skip-portfolio] [--skip-paper]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
LOG = ROOT / "logs" / "qdii_daily.log"


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_step(name: str, script: str, args: list[str]) -> int:
    """子进程跑一个脚本，返回退出码（不抛异常）。"""
    cmd = [sys.executable, str(SCRIPTS / script)] + args
    log(f"开始 {name}: {' '.join(cmd)}")
    t0 = time.time()
    try:
        cp = subprocess.run(
            cmd, cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        out = cp.stdout.strip()
        # 只回显尾部关键行，避免日志膨胀
        tail = "\n".join(out.splitlines()[-12:]) if out else "(无输出)"
        status = "成功" if cp.returncode == 0 else f"失败(exit={cp.returncode})"
        log(f"结束 {name}: {status}，耗时 {time.time()-t0:.0f}s")
        if tail:
            for ln in tail.splitlines():
                log(f"  | {ln}")
        return cp.returncode
    except Exception as e:
        log(f"结束 {name}: 异常 {type(e).__name__}: {e}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="QDII 溢价套利每日定时任务")
    parser.add_argument("--skip-backtest", action="store_true",
                        help="只跑监控快照，跳过回测刷新")
    parser.add_argument("--skip-portfolio", action="store_true",
                        help="跳过三层组合实盘信号（第 3 步）")
    parser.add_argument("--skip-paper", action="store_true",
                        help="跳过模拟盘对账（第 4 步）")
    args = parser.parse_args()

    log("=" * 72)
    log("QDII 每日定时任务启动")

    rc1 = run_step("QDII 溢价监控快照", "qdii_monitor.py", [])

    rc2 = 0
    if not args.skip_backtest:
        rc2 = run_step("QDII 套利回测刷新", "qdii_backtest.py", ["--refresh"])

    rc3 = 0
    if not args.skip_portfolio:
        rc3 = run_step("三层组合实盘信号", "portfolio_live.py", [])

    rc4 = 0
    if not args.skip_paper:
        rc4 = run_step("模拟盘对账", "paper_trading.py", ["reconcile"])

    ok = rc1 == 0 and rc2 == 0 and rc3 == 0 and rc4 == 0
    log(f"全部结束: {'全部成功' if ok else '存在失败(监控 rc=%d 回测 rc=%d 组合 rc=%d 对账 rc=%d)' % (rc1, rc2, rc3, rc4)}")
    log("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
