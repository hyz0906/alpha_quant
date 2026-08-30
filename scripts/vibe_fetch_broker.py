"""
Vibe-Trading 数据桥接子进程（broker）。

为什么必须是独立进程：
  vibe-trading-ai 把顶层包 `src` 安装进 site-packages，与本项目顶层包
  `src` 同名冲突。若在 alpha_quant 根目录下直接 `from src.market_data
  import fetch_market_data`，解析到的是本项目的 src，而非 vibe 的。
  解法：本脚本由 VibeMarketLoader 以 cwd=$HOME 的子进程方式运行，
  此时 `import src` 唯一候选即 site-packages 中的 vibe-trading 版本。

输入：--codes 512480.SH 513100.SH ...  --start 2020-01-01 --end 2026-08-30
      --out-dir <alpha_quant>/data
输出：每个代码一个 CSV：<out-dir>/<code>.csv
      列：date,open,high,low,close,volume（date 为 ISO 日期，升序）
      以及一个 manifest.json 记录来源与行数。

数据链路：vibe-trading fetch_market_data，A 股回退链
  tencent(前复权) -> mootdx -> eastmoney -> baostock -> akshare -> tushare
  默认 tencent，免 token。

⚠️ 实测返回契约（vibe-trading-ai 0.1.14）：
  fetch_market_data() 返回 dict，每个代码的 value 是 **list[dict]**（不是 DataFrame），
  记录的日期字段名是 `trade_date`（不是 `date`）。另有 `_provenance` 键记录来源。
  实测 tencent/eastmoney 为前复权价，akshare 为不复权价（512480 同日收盘价
  0.482 vs 0.964），故默认链路必须走 tencent，不要混用数据源。
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

# vibe 返回的日期字段名（实测为 trade_date）
_DATE_KEYS = ("trade_date", "date", "datetime", "time")


def coerce_frame(payload: Any) -> pd.DataFrame | None:
    """把 vibe 的返回值统一成以 date 为 DatetimeIndex、列小写的 DataFrame。

    兼容三种形态：list[dict]（实测默认）、DataFrame、None/空。
    """
    if payload is None:
        return None
    if isinstance(payload, pd.DataFrame):
        df = payload.copy()
    elif isinstance(payload, (list, tuple)):
        if not payload:
            return None
        if not isinstance(payload[0], dict):
            return None
        df = pd.DataFrame(list(payload))
    else:
        return None

    if df.empty:
        return None

    # 统一列名小写
    df.columns = [str(c).strip().lower() for c in df.columns]

    # 定位日期列并设为索引
    date_col = next((c for c in _DATE_KEYS if c in df.columns), None)
    if date_col is not None:
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col).sort_index()
    elif isinstance(df.index, pd.DatetimeIndex):
        df = df.sort_index()
    else:
        return None

    df.index.name = "date"
    # 只保留 OHLCV 标准列（vibe 可能返回 amount/turnover 等附加列）
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    if not {"high", "low"}.issubset(keep):  # RSRS 必须有 high/low
        return None
    return df[keep]


def _slice_ranges(start: str, end: str, step_days: int = 365):
    """把 [start, end] 切成若干 ≤step_days 天的闭区间段。

    每段约 243 个交易日（1 年），远低于腾讯接口 500 根上限，
    从而规避 vibe-trading 分页 bug 导致的静默截断。
    """
    cur = pd.Timestamp(start)
    stop = pd.Timestamp(end)
    while cur <= stop:
        seg_end = min(cur + pd.Timedelta(days=step_days - 1), stop)
        yield cur.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")
        cur = seg_end + pd.Timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch OHLCV via vibe-trading")
    parser.add_argument("--codes", nargs="+", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--source", default="auto",
                        help="auto/tencent/mootdx/eastmoney/baostock/akshare/tushare")
    parser.add_argument("--max-rows", type=int, default=0,
                        help="0=不限制(vibe 默认 250 会触发等距降采样，务必保持 0)")
    parser.add_argument("--slice-days", type=int, default=365,
                        help="分段拉取的窗口天数，须保证段内 K 线数 < 500")
    args = parser.parse_args()

    # 关键：cwd 必须不含 src/ 目录（由调用方保证），此处再兜底移除 cwd
    # 与 ''，确保 import src 只会命中 site-packages。
    cwd_str = str(Path.cwd().resolve())
    for entry in ("", cwd_str):
        while entry in sys.path:
            sys.path.remove(entry)

    from src.market_data import fetch_market_data  # vibe-trading 的 src

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ⚠️ vibe-trading 的 tencent loader 有截断 bug：
    #   腾讯 fqkline 接口单次最多 500 根，且当窗口内 K 线数 > 500 时返回的是
    #   「end_date 往前 500 根」（末尾对齐），而 vibe 的 _fetch_one 分页却假设
    #   首页从 start_date 开始，于是 next_start > end_date 立即 break，
    #   最终任何 >500 根的请求都静默退化为「最近 500 根」。
    #   （实测：请求 2024-01-01~2026-06-30 返回 500 根，first=2024-06-06）
    #   RSRS 的 M=600 标准化窗口需要至少 600 根，故此处按年分段拉取再拼接。
    slices = list(_slice_ranges(args.start, args.end, step_days=args.slice_days))
    if len(slices) > 1:
        print(f"[broker] 分段拉取：{len(slices)} 段 ({slices[0][0]} ~ {slices[-1][1]})，"
              f"规避 vibe 500 根截断")

    collected: dict[str, list[pd.DataFrame]] = {c: [] for c in args.codes}
    provenance: dict[str, Any] = {}

    for seg_start, seg_end in slices:
        try:
            result = fetch_market_data(
                codes=list(args.codes),
                start_date=seg_start,
                end_date=seg_end,
                source=args.source,
                interval="1D",
                max_rows=args.max_rows,
                include_provenance=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[broker] 段 {seg_start}~{seg_end} 失败: {type(exc).__name__}: {exc}")
            continue

        if not isinstance(result, dict):
            continue
        prov = result.get("_provenance", {})
        if isinstance(prov, dict):
            provenance.update(prov)

        for code, payload in result.items():
            if code.startswith("_"):
                continue
            if isinstance(payload, dict) and "data" in payload:
                # cap_rows 触发降采样——分段后不应发生，发生即说明数据有诈
                print(f"[broker] ⚠️ {code} 段 {seg_start}~{seg_end} 触发 vibe 降采样"
                      f"({payload.get('policy')})，已跳过该段")
                continue
            df = coerce_frame(payload)
            if df is not None and not df.empty:
                collected.setdefault(code, []).append(df)

    manifest = {
        "start": args.start,
        "end": args.end,
        "source": args.source,
        "slices": len(slices),
        "files": {},
    }
    failed = []
    for code in args.codes:
        chunks = collected.get(code) or []
        if not chunks:
            failed.append(code)
            print(f"[broker] {code}: EMPTY/UNPARSEABLE")
            continue
        df = pd.concat(chunks)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        csv_path = out_dir / f"{code}.csv"
        df.to_csv(csv_path, index_label="date")
        src_used = provenance.get(code, {}).get("source") if isinstance(provenance, dict) else None
        manifest["files"][code] = {
            "rows": int(len(df)),
            "first": str(df.index.min().date()),
            "last": str(df.index.max().date()),
            "source_used": src_used,
        }
        print(f"[broker] {code}: {len(df)} rows -> {csv_path.name} "
              f"({df.index.min().date()} ~ {df.index.max().date()}) via {src_used}")

    manifest["failed"] = failed
    if provenance:
        manifest["provenance"] = provenance
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[broker] done. ok={len(manifest['files'])} failed={len(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
