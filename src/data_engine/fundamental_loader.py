#!/usr/bin/env python3
"""基本面/估值数据加载器（akshare，免 token）。

数据源与覆盖（2026-08 实测）：
  * 中证指数官方 `stock_zh_index_value_csindex(code)`：
      覆盖中证/上证指数，返回最新约 20 个交易日的「市盈率1/2 + 股息率1/2」。
      —— 用于 A 股权益 ETF 的估值快照（单截面 PE / 股息率）。
  * 乐咕乐股 `stock_index_pe_lg(symbol)` / `stock_index_pb_lg(symbol)`：
      约 1 年日频 PE / PB（滚动市盈率等），仅覆盖少数宽基指数
      （沪深300/中证500/上证50/中证红利…）。—— 用于短窗口估值分位。
  * 债券收益率 `bond_zh_us_rate()`：中国 10 年期国债收益率，作债券 carry 参考。

⚠️ 已知局限（重要）：
  * 免费 akshare 无「多年点-in-time 指数 PE/PB/股息率历史」，无法支撑标准
    截面 IC 时间序列检验（因子诊断需要因子 × 未来收益的多年面板）。
  * 全历史需 Tushare Pro（指数估值日频接口，需 token）或中证指数官网
    历史导出。本模块先落地可获取的「快照 + 1 年短窗」，接口已按可替换设计。
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Optional

import akshare as ak
import pandas as pd

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FUND_DIR = PROJECT_ROOT / "data" / "fundamental"


# --------------------------------------------------------------------------- #
# ETF -> 底层指数映射（A 股权益子集；债券/商品/跨境无 PE 概念，另表处理）
# --------------------------------------------------------------------------- #
# code: (csindex 代码, 中文名, 乐咕 symbol 或 None)
ETF_INDEX_MAP: dict[str, tuple[str, str, Optional[str]]] = {
    "510300.SH": ("000300", "沪深300", "沪深300"),
    "510500.SH": ("000905", "中证500", "中证500"),
    "159915.SZ": ("399006", "创业板指", "创业板"),   # csindex 404 → 乐咕兜底
    "512010.SH": ("000933", "中证医药", None),
    "159928.SZ": ("000932", "中证消费", None),
    "512880.SH": ("399975", "证券公司", None),
    "512660.SH": ("399967", "中证军工", None),
}

# 债券 / 商品 / 跨境：无 PE，用替代 carry/估值口径（快照仅标注）
NON_EQUITY_NOTE: dict[str, str] = {
    "511010.SH": "国债ETF——carry=10年期国债收益率",
    "511180.SH": "可转债ETF——carry=可转债YTM/纯债溢价",
    "518880.SH": "黄金ETF——无carry，估值看实际利率/金价分位",
    "159985.SZ": "豆粕ETF——商品无PE，看期限结构/展期收益",
    "159981.SZ": "能源化工ETF——商品无PE，看期限结构/展期收益",
    "513100.SH": "纳指100ETF——美股估值(PE)",
    "513500.SH": "标普500ETF——美股估值(PE)",
    "513050.SH": "中概互联ETF——美股/港股中概估值",
    "513880.SH": "日经ETF——日本估值(PE)",
    "513030.SH": "德国30ETF——德国估值(PE)",
    "159920.SZ": "恒生ETF——港股估值(PE)",
}


def fetch_csindex_snapshot(code: str) -> dict:
    """中证指数官方估值快照：返回最新一日的 PE1/PE2/股息率1/股息率2。"""
    df = ak.stock_zh_index_value_csindex(symbol=code)
    r = df.iloc[-1]
    return {
        "date": str(r["日期"]),
        "pe1": float(r["市盈率1"]),      # 静态市盈率（总股本加权，亏损剔除）
        "pe2": float(r["市盈率2"]),      # 滚动市盈率（TTM）
        "dy1": float(r["股息率1"]),      # 股息率（%）
        "dy2": float(r["股息率2"]),
    }


def fetch_lg_pe(symbol: str) -> pd.DataFrame:
    """乐咕乐股近 1 年日频市盈率（滚动市盈率 = TTM）。返回 [日期, pe]。"""
    df = ak.stock_index_pe_lg(symbol=symbol)
    out = pd.DataFrame({
        "date": pd.to_datetime(df["日期"]),
        "pe": pd.to_numeric(df["滚动市盈率"], errors="coerce"),
    }).dropna()
    return out.set_index("date").sort_index()


def fetch_lg_pb(symbol: str) -> pd.DataFrame:
    """乐咕乐股近 1 年日频市净率。返回 [日期, pb]。"""
    df = ak.stock_index_pb_lg(symbol=symbol)
    pb_col = "市净率" if "市净率" in df.columns else df.columns[-1]
    out = pd.DataFrame({
        "date": pd.to_datetime(df["日期"]),
        "pb": pd.to_numeric(df[pb_col], errors="coerce"),
    }).dropna()
    return out.set_index("date").sort_index()


def fetch_bond_yield_10y() -> float:
    """中国 10 年期国债收益率（最新，%），债券 carry 参考。"""
    df = ak.bond_zh_us_rate(start_date="20250101")
    # 精确取「中国国债收益率10年」列（宽表里还有美国各期限，勿用 max）
    col = "中国国债收益率10年"
    if col not in df.columns:
        # 兜底：找同时含「中国」与「10年」的列
        col = next((c for c in df.columns if "中国" in c and "10年" in c), None)
    if col is None:
        raise KeyError("未找到中国10年期国债收益率列")
    return float(pd.to_numeric(df[col], errors="coerce").dropna().iloc[-1])


def fetch_valuation_snapshot() -> pd.DataFrame:
    """抓取 A 股权益子集的最新估值快照（PE + 股息率 + 乐咕短窗分位）。"""
    rows = []
    for etf, (cs_code, name, lg_sym) in ETF_INDEX_MAP.items():
        rec = {"etf": etf, "index": name, "csindex": cs_code}
        try:
            snap = fetch_csindex_snapshot(cs_code)
            rec.update(pe=snap["pe2"], pe_static=snap["pe1"],
                       dividend_yield=snap["dy1"], snap_date=snap["date"])
        except Exception as e:  # 创业板 399006 在 csindex 404
            rec.update(pe=None, pe_static=None, dividend_yield=None,
                       snap_date=None, csindex_err=str(e)[:40])

        # 乐咕 1 年 PE/PB → 当前分位（仅在能拿到时）
        rec["pe_pct_1y"] = None
        rec["pb_pct_1y"] = None
        if lg_sym:
            try:
                pe_hist = fetch_lg_pe(lg_sym)
                rec["pe_pct_1y"] = float((pe_hist["pe"] <= pe_hist["pe"].iloc[-1]).mean())
            except Exception:
                pass
            try:
                pb_hist = fetch_lg_pb(lg_sym)
                rec["pb_pct_1y"] = float((pb_hist["pb"] <= pb_hist["pb"].iloc[-1]).mean())
            except Exception:
                pass
        rows.append(rec)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    snap = fetch_valuation_snapshot()
    print(snap.to_string(index=False))
    snap.to_csv(FUND_DIR / "valuation_snapshot.csv", index=False, encoding="utf-8")
    print(f"\n已缓存 {FUND_DIR / 'valuation_snapshot.csv'}")
