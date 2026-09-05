#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三层组合最终验证：把三轮各自验证过的旋钮叠加，给出可直接落地的档位表。

已验证的四个正交旋钮（均通过 IS→OOS 检验）：
  1. QDII 灵敏度 z_hi 2.0→1.5、floor 1.0%→0.5%   （夏普 +0.10，OOS +0.12）
  2. 再平衡频率 月频→季频                          （夏普 +0.05，换手 -0.23）
  3. 门控空缺资金 现金→货币腿（月频锁定）           （年化 +0.11pp）
  4. 收益档位 VOL_FLOOR_ANN 2.5%→4%/6%/8%         （年化 +1.2~3.1pp，夏普 -0.06~-0.15）

另备风险档位旋钮 p（w ∝ 1/σ^p，1.0→1.4）：降收益、大幅抬夏普降回撤。

用法：python3 scripts/tune_final.py
输出：runs/tune_final.md / .json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tune_three_layer import BASE, KEYS, evaluate, load_panel, fmt_table, row  # noqa: E402
from tune_round3 import build_W, group_expo, GROUPS                          # noqa: E402
import risk_parity as rp                                                     # noqa: E402

TD = rp.TRADING_DAYS

# 公共改进：季频 + QDII 灵敏度 + 月频货币腿（不含收益档位调整）
COMMON = dict(freq="Q", z_hi=1.5, q_floor=0.5, cash_mmf=True)

CANDIDATES = {
    "BASE 现役（对照）": dict(BASE),
    "L1 不加杠杆·稳健": dict(COMMON, p=1.2),
    "L2 不加杠杆·均衡": dict(COMMON, p=1.0),
    "L3 收益档 +4%": dict(COMMON, p=1.0, vf=0.04),
    "L4 收益档 +6%": dict(COMMON, p=1.0, vf=0.06),
    "L5 收益档 +8%": dict(COMMON, p=1.0, vf=0.08),
    "L6 收益档+6% & 夏普档p=1.2": dict(COMMON, p=1.2, vf=0.06),
}

NOTES = {
    "BASE 现役（对照）": "现役：月频 / z=2.0 / floor=1% / 空仓持现金 / 地板2.5%",
    "L1 不加杠杆·稳健": "波罚指数 p=1.2，降风险优先（回撤 <4%）",
    "L2 不加杠杆·均衡": "只叠加三项免费改进，收益档不动",
    "L3 收益档 +4%": "地板 2.5%→4%，年化 +1.2pp 量级",
    "L4 收益档 +6%": "地板 2.5%→6%，年化 +2.4pp 量级，回撤扩至 ~7%",
    "L5 收益档 +8%": "地板 2.5%→8%，年化 +3.1pp 量级，回撤扩至 ~8%",
    "L6 收益档+6% & 夏普档p=1.2": "收益档与夏普档对冲，回撤回到 6% 内",
}


def main() -> int:
    panel = load_panel()
    years = len(panel) / TD
    print(f"[final] 面板 {panel.index.min().date()} ~ {panel.index.max().date()} "
          f"({years:.2f} 年)")

    rows, nets, expos = [], {}, {}
    for name, cfg in CANDIDATES.items():
        m = evaluate(panel, build_W(panel, cfg))
        nets[name] = m["net"]
        expos[name] = group_expo(build_W(panel, cfg))
        rows.append(row(name, m))
        print(f"[final] {name:24s} 年化 {m['ann_ret']*100:+.2f}% "
              f"夏普 {m['sharpe']:.2f} 回撤 {m['max_dd']*100:.1f}% "
              f"换手 {m['turnover']:.2f} | IS {m['is_sharpe']:.2f} "
              f"OOS {m['oos_sharpe']:.2f}")

    # 分年
    names = list(nets.keys())
    ymat = pd.DataFrame({k: v.groupby(v.index.year).apply(lambda x: (1 + x).prod() - 1)
                         for k, v in nets.items()})

    L = ["# 三层组合参数调优：最终档位表（实验分支 exp/new-strategy）", "",
         f"- 样本 {panel.index.min().date()} ~ {panel.index.max().date()}"
         f"（{len(panel)} 交易日 / {years:.2f} 年），18 只异构池，单边成本 0.15%",
         "- IS（样本内）= ~2023-06，OOS（样本外）= 2023-07 ~。"
         "**OOS ≥ IS 才认**，避免只报样本内最优。", "",
         "## 1. 最终候选对比", ""] + fmt_table(rows, KEYS) + [
        "", "## 2. 方案说明", ""]
    for k, v in NOTES.items():
        L.append(f"- **{k}**：{v}")
    L += ["", "## 3. 资产大类平均暴露", "",
          "| 方案 | " + " | ".join(GROUPS.keys()) + " | 合计 |",
          "| --- | " + " | ".join(["---"] * len(GROUPS)) + " | --- |"]
    for k, g in expos.items():
        tot = sum(g.values())
        L.append(f"| {k} | " + " | ".join(f"{g[x]*100:.1f}%" for x in GROUPS)
                 + f" | {tot*100:.1f}% |")
    L += ["", "## 4. 分年净收益", "",
          "| 年份 | " + " | ".join(names) + " |",
          "| --- | " + " | ".join(["---"] * len(names)) + " |"]
    for y in ymat.index:
        L.append(f"| {y} | " + " | ".join(
            (f"{ymat.loc[y, k]*100:+.1f}%" if pd.notna(ymat.loc[y, k]) else "—")
            for k in names) + " |")

    (ROOT / "runs" / "tune_final.md").write_text("\n".join(L), encoding="utf-8")
    (ROOT / "runs" / "tune_final.json").write_text(
        json.dumps({"rows": rows, "exposure": expos}, ensure_ascii=False,
                   indent=2, default=str), encoding="utf-8")
    print("\n[final] 报告已写出: " + str(ROOT / "runs" / "tune_final.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
