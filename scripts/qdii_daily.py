#!/usr/bin/env python3
"""QDII 溢价套利每日定时任务编排（§7.18 / §9 产品化落地）。

收盘后（15:30）由 crontab 触发，按序执行两件事并把结果落日志：

  1. 监控快照 `qdii_monitor.py`  —— 当前截面官方/影子 IOPV 溢价告警
     -> runs/qdii_premium.md / .json
  2. 套利回测 `qdii_backtest.py --refresh` —— 重拉历史溢价序列（追加最新数据点）
     并重算「溢价回避 / 折价买入」策略绩效
     -> runs/qdii_backtest.md / .json + data/fundamental/qdii_premium_*.csv

设计要点：
  * 每步用 subprocess 独立进程跑，任一步失败不影响另一步（错误隔离）。
  * 日志追加到 logs/qdii_daily.log（时间戳 + 每步成败 + 摘要）。
  * 交易日 15:30 触发（crontab: `30 15 * * 1-5 ...`），节假日会空跑（数据无变化，
    无害）。

用法：python3 scripts/qdii_daily.py [--skip-backtest]
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
    args = parser.parse_args()

    log("=" * 72)
    log("QDII 每日定时任务启动")

    rc1 = run_step("QDII 溢价监控快照", "qdii_monitor.py", [])

    rc2 = 0
    if not args.skip_backtest:
        rc2 = run_step("QDII 套利回测刷新", "qdii_backtest.py", ["--refresh"])

    ok = rc1 == 0 and rc2 == 0
    log(f"全部结束: {'全部成功' if ok else '存在失败(监控 rc=%d 回测 rc=%d)' % (rc1, rc2)}")
    log("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
