#!/usr/bin/env python3
"""基本面/估值数据加载器（akshare，免 token）。

数据源与覆盖（2026-08 实测）：
  * 中证指数官方 `stock_zh_index_value_csindex(code)`：
      覆盖中证/上证指数，返回最新约 20 个交易日的「市盈率1/2 + 股息率1/2」。
      —— 用于 A 股权益 ETF 的估值快照（单截面 PE / 股息率）。
  * 乐咕乐股 `stock_index_pe_lg(symbol)` / `stock_index_pb_lg(symbol)`：
      月频完整历史（2005 至今约 250 个月，非「近 1 年」），仅覆盖少数宽基
      （沪深300/中证500/上证50）。—— 用于全历史估值分位 + 时序估值择时。
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
    """乐咕乐股市盈率完整历史（月频，2005 至今，非「近 1 年」）。返回 [日期, pe]。

    注意：乐咕 index-basic-pe API 返回的是月频完整历史（约 250 个月），
    仅覆盖少数宽基（上证50/沪深300/中证500）。pe=滚动市盈率(TTM)。
    """
    df = ak.stock_index_pe_lg(symbol=symbol)
    out = pd.DataFrame({
        "date": pd.to_datetime(df["日期"]),
        "pe": pd.to_numeric(df["滚动市盈率"], errors="coerce"),
    }).dropna()
    return out.set_index("date").sort_index()


def fetch_lg_pb(symbol: str) -> pd.DataFrame:
    """乐咕乐股市净率完整历史（月频，2005 至今）。返回 [日期, pb]。"""
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

        # 乐咕完整历史 PE/PB → 当前值在全历史的分位（仅在能拿到时）
        # 注意：这是「2005 至今完整历史」分位，不是「近 1 年」分位。
        rec["pe_pct_hist"] = None
        rec["pb_pct_hist"] = None
        if lg_sym:
            try:
                pe_hist = fetch_lg_pe(lg_sym)
                rec["pe_pct_hist"] = float((pe_hist["pe"] <= pe_hist["pe"].iloc[-1]).mean())
            except Exception:
                pass
            try:
                pb_hist = fetch_lg_pb(lg_sym)
                rec["pb_pct_hist"] = float((pb_hist["pb"] <= pb_hist["pb"].iloc[-1]).mean())
            except Exception:
                pass
        rows.append(rec)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# 乐咕乐股月频估值历史（2005 至今）——免费源唯一可得的「多年点-in-time」估值序列
# --------------------------------------------------------------------------- #
# 实测（2026-08-31）：乐咕 index-basic-pe/pb 仅覆盖少数宽基（上证50/沪深300/中证500
# 有数据，其余 9 个宽基/风格指数返回空）。数据为月频（月末），2005 年至今约 250 个月。
# 用途：时序估值择时检验（单标的「估值分位 → 未来收益」），无法做多标的截面 IC。
LEGU_BROAD_INDEXES: dict[str, str] = {
    "上证50": "510050.SH",
    "沪深300": "510300.SH",
    "中证500": "510500.SH",
}


def fetch_lg_monthly(symbol: str) -> pd.DataFrame:
    """乐咕乐股月频估值历史（完整，2005 至今）。返回 [close, pe_ttm, pe_lyr]。

    index 为月末日期。pe_ttm=滚动市盈率(TTM)，pe_lyr=静态市盈率(LYR)，
    close=指数收盘点位（用于算未来收益）。
    """
    df = ak.stock_index_pe_lg(symbol=symbol)
    # 注意：用 .to_numpy() 传裸值，避免 pd.DataFrame(dict, index=...) 按索引对齐
    # 导致全 NaN 的坑（dict 内 Series 是 RangeIndex，index 是日期索引，对齐失败）。
    out = pd.DataFrame({
        "close": pd.to_numeric(df["指数"], errors="coerce").to_numpy(),
        "pe_ttm": pd.to_numeric(df["滚动市盈率"], errors="coerce").to_numpy(),
        "pe_lyr": pd.to_numeric(df["静态市盈率"], errors="coerce").to_numpy(),
    }, index=pd.to_datetime(df["日期"]))
    return out.dropna(subset=["pe_ttm"]).sort_index()


# --------------------------------------------------------------------------- #
# Tushare Pro 指数估值历史（index_dailybasic，日频 PE/PB，需 2000 积分）
# --------------------------------------------------------------------------- #
# index_dailybasic 返回指数日频 PE(TTM)/PB/市值，是「多年 × 多标的」截面 IC 的
# 正确数据源；但需 2000 积分。低积分 token 会抛 code=40203 无权限。
# ts_code 后缀（.SH/.SZ）为按代码段推断（000xxx=上交所、399xxx=深交所），
# 待 token 有权限后可用 index_basic 校验修正。
TS_INDEX_MAP: dict[str, str] = {
    "510300.SH": "000300.SH",   # 沪深300
    "510500.SH": "000905.SH",   # 中证500
    "159915.SZ": "399006.SZ",   # 创业板指
    "512010.SH": "000933.SH",   # 中证医药
    "159928.SZ": "000932.SH",   # 中证消费
    "512880.SH": "399975.SZ",   # 证券公司
    "512660.SH": "399967.SZ",   # 中证军工
}


def fetch_tushare_index_dailybasic(etf_code: str, start: str, end: str) -> pd.DataFrame:
    """Tushare 指数估值日频历史（pe_ttm/pb/市值）。需 2000 积分，否则抛权限错误。

    参数 start/end 为 YYYY-MM-DD。返回 index=date 的 [pe_ttm, pb, total_mv, float_mv]。
    """
    import tushare as ts
    from config.settings import settings
    if not settings.TUSHARE_TOKEN:
        raise RuntimeError("未配置 TUSHARE_TOKEN（.env）")
    ts_code = TS_INDEX_MAP.get(etf_code)
    if ts_code is None:
        raise KeyError(f"{etf_code} 无 Tushare 指数映射")
    pro = ts.pro_api(settings.TUSHARE_TOKEN)
    df = pro.index_dailybasic(ts_code=ts_code, start_date=start.replace("-", ""),
                              end_date=end.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    out = df[["trade_date", "pe_ttm", "pb", "total_mv", "float_mv"]].copy()
    out["date"] = pd.to_datetime(out["trade_date"])
    return out.set_index("date").sort_index()


if __name__ == "__main__":
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    snap = fetch_valuation_snapshot()
    print(snap.to_string(index=False))
    snap.to_csv(FUND_DIR / "valuation_snapshot.csv", index=False, encoding="utf-8")
    print(f"\n已缓存 {FUND_DIR / 'valuation_snapshot.csv'}")
