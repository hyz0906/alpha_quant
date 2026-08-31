#!/usr/bin/env python3
"""抓取蛋卷 VIP 估值表历史面板（source=lsd「螺丝钉估值表」）。

背景（§7.23 探索过程）：
  * 公开接口 /djapi/index_eva/dj 只有单截面（63 指数，免登录）；
  * /djapi/index_eva/detail/{code} 也只是单指数当前快照（多一个 peg）；
  * /djapi/index_eva/dj/history 需登录，但登录后仍 500（参数不明，放弃）；
  * **真正带历史的是 /djapi/fundx/base/vip/valuation/show/detail**：
    参数 `id=N` 直接返回第 N 期的整张估值表（62 个指数的 pe/pb/roe/
    股息率/盈利收益率 + 5y/10y 分位 + 场内/场外基金代码）。
    id 与 date 单调递增（id=1 → 2019-08-20 共 10 只；id=10444 → 2026-08-31
    共 62 只），中间偶发空洞（同一天但 valuations 为空，跳过即可）。
    ⚠️ `source=lsd` 与 `id` 互斥——带 source 时 id 被忽略、恒返回最新期。
       所以遍历历史只能**只用 id、不带 source**。

输出：
  data/fundamental/danjuan_valuation_panel.csv  长表（date, index_code, ...）
  data/fundamental/danjuan_valuation_panel.json 抓取元信息（id↔date 映射等）

用法：
  python3 scripts/danjuan_panel_fetch.py                 # 增量抓取（默认）
  python3 scripts/danjuan_panel_fetch.py --workers 8
  python3 scripts/danjuan_panel_fetch.py --id-from 1 --id-to 10444

Cookie：读 data/.danjuan_cookie（已在 .gitignore）或环境变量 DANJUAN_COOKIE。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FUND_DIR = ROOT / "data" / "fundamental"
OUT_CSV = FUND_DIR / "danjuan_valuation_panel.csv"
OUT_META = FUND_DIR / "danjuan_valuation_panel.json"
COOKIE_FILE = ROOT / "data" / ".danjuan_cookie"

API = "https://danjuanfunds.com/djapi/fundx/base/vip/valuation/show/detail"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

FIELDS = ["pe", "pb", "roe", "dividend_yield", "profit_yield",
          "pb_percent_r5y", "pb_percent_r10y",
          "pe_percent_r5y", "pe_percent_r10y"]


def load_cookie() -> str:
    if os.environ.get("DANJUAN_COOKIE"):
        return os.environ["DANJUAN_COOKIE"].strip()
    if COOKIE_FILE.exists():
        return COOKIE_FILE.read_text(encoding="utf-8").strip()
    sys.exit("缺少 cookie：请写 data/.danjuan_cookie 或设 DANJUAN_COOKIE 环境变量")


def fetch_one(i: int, cookie: str, retries: int = 3):
    """返回 (id, date, rows) 或 (id, None, None) 表示空洞/失败。"""
    url = f"{API}?id={i}"
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": "https://danjuanfunds.com/",
                "Accept": "application/json, text/plain, */*",
                "Cookie": cookie,
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                j = json.loads(r.read().decode("utf-8", "ignore"))
            d = j.get("data") or {}
            vals = d.get("valuations") or []
            if not vals or not d.get("time"):
                return i, None, None
            rows = []
            for v in vals:
                row = {"date": d["time"], "period_id": i,
                       "index_code": v.get("index_code"),
                       "index_name": v.get("index_name"),
                       "inside_fund": v.get("inside_fund"),
                       "outside_fund": v.get("outside_fund"),
                       "valuation_status": v.get("valuation_status")}
                for f in FIELDS:
                    row[f] = v.get(f)
                rows.append(row)
            return i, d["time"], rows
        except Exception:
            if k == retries - 1:
                return i, None, None
            time.sleep(0.6 * (k + 1) + random.random() * 0.4)
    return i, None, None


def latest_id(cookie: str) -> int:
    req = urllib.request.Request(f"{API}?source=lsd&category_code=6", headers={
        "User-Agent": UA, "Referer": "https://danjuanfunds.com/",
        "Accept": "application/json, text/plain, */*", "Cookie": cookie})
    with urllib.request.urlopen(req, timeout=25) as r:
        j = json.loads(r.read().decode("utf-8", "ignore"))
    return int(j["data"]["id"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--id-from", type=int, default=1)
    ap.add_argument("--id-to", type=int, default=None,
                    help="默认自动探测最新期 id")
    ap.add_argument("--chunk", type=int, default=1000,
                    help="每抓取多少个 id 落一次盘（断点续传）")
    args = ap.parse_args()

    cookie = load_cookie()
    id_to = args.id_to or latest_id(cookie)
    print(f"抓取范围：id {args.id_from} ~ {id_to}（共 {id_to-args.id_from+1} 期），"
          f"{args.workers} 并发")

    # 断点续传：已有 CSV 则跳过已完成的 date（按期粒度不严谨，改用 period_id 集合）
    done_ids = set()
    all_rows = []
    if OUT_CSV.exists():
        try:
            old = pd.read_csv(OUT_CSV, dtype={"index_code": str})
            done_ids = set(old["period_id"].dropna().astype(int).unique())
            all_rows = old.to_dict("records")
            print(f"断点续传：已存在 {len(done_ids)} 期、{len(all_rows)} 行")
        except Exception as e:
            print(f"旧文件读取失败（{e}），从头抓取")

    todo = [i for i in range(args.id_from, id_to + 1) if i not in done_ids]
    print(f"待抓取 {len(todo)} 期")

    id_date = {}
    n_ok = n_empty = n_fail = 0
    t0 = time.time()

    for start in range(0, len(todo), args.chunk):
        batch = todo[start:start + args.chunk]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(fetch_one, i, cookie): i for i in batch}
            for fut in as_completed(futs):
                i, dt, rows = fut.result()
                if dt is None:
                    # 区分空洞（成功但无数据）与失败：再探一次不可区分，按空洞计
                    n_empty += 1
                    continue
                id_date[i] = dt
                all_rows.extend(rows)
                n_ok += 1
        # 每批落盘
        df = pd.DataFrame(all_rows)
        df = df.drop_duplicates(subset=["period_id", "index_code"])
        df = df.sort_values(["date", "index_code"])
        df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
        el = time.time() - t0
        done = start + len(batch)
        print(f"  [{done}/{len(todo)}] 有效 {n_ok} / 空洞 {n_empty} | "
              f"累计 {len(df)} 行 | {el:.0f}s | 预计剩余 "
              f"{el/max(done,1)*(len(todo)-done):.0f}s")
        time.sleep(0.5)

    df = pd.DataFrame(all_rows).drop_duplicates(
        subset=["period_id", "index_code"]).sort_values(["date", "index_code"])
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    meta = {
        "source": "danjuanfunds.com /djapi/fundx/base/vip/valuation/show/detail",
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "id_range": [args.id_from, id_to],
        "n_periods": int(df["period_id"].nunique()),
        "n_index": int(df["index_code"].nunique()),
        "date_min": str(df["date"].min()),
        "date_max": str(df["date"].max()),
        "n_rows": int(len(df)),
        "n_empty_periods": n_empty,
        "id_date_map": {str(k): v for k, v in sorted(id_date.items())},
        "note": ("VIP 会员数据，仅供个人研究，勿分发；source 与 id 互斥，"
                 "遍历历史只能只用 id"),
    }
    OUT_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n完成：{meta['n_periods']} 期 / {meta['n_index']} 个指数 / "
          f"{meta['n_rows']} 行，{meta['date_min']} ~ {meta['date_max']}")
    print(f"输出：{OUT_CSV}")


if __name__ == "__main__":
    main()
