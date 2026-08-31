"""QDII 影子 IOPV 溢价计算（真实数据版，替换原 mock）。

原实现（Design P1 阶段）的 premium = 当日涨跌幅，完全错误——那只是价格波动，
不是「价格相对净值」的溢价。本版改为：

  * 官方 IOPV / 折价率：ak.fund_etf_spot_em() 的「IOPV实时估值」「基金折价率」
    （交易所/东财发布的盘中估算净值与折溢价，QDII ETF 均覆盖）。
  * 影子 IOPV（shadow IOPV）：官方 IOPV × (1 + 底层市场最新涨跌幅)。
    对美股/港股 QDII，A 股盘中官方 IOPV 常滞后于隔夜海外行情，影子 IOPV
    把「底层指数最新一跳」折进去，得到更接近真实净值的参考价。
  * 溢价 = price / iopv - 1（官方口径）与 price / shadow_iopv - 1（影子口径）。

影子调整的底层数据源：
  * 美股：index_us_stock_sina(symbol=".IXIC"/".INX") 最新收盘
  * 港股：stock_hk_index_daily_em(symbol="HSI") 最新收盘
  * 德国DAX/日经225：index_global_hist_sina(symbol="德国DAX 30种股价指数"/"日经225指数")
  * 汇率：currency_boc_sina(symbol="美元"/"欧元"/"日元"/"港币")，中行牌价

已知边界：
  * 影子 IOPV 仍是一阶近似：未计底层成分权重漂移，只做「底层市场最新一跳 +
    汇率最新一跳」的线性修正（shadow = official × (1+底层涨跌) × (1+汇率变动)）。
  * 汇率用中行每日牌价（非日内分时），周末牌价不变时 fx_chg≈0。
  * QDII 溢价套利的精确估值需日内持续喂入底层指数/期货 + 汇率，本模块
    给出的是收盘快照级别的参考，供 >3% 告警链路使用。
"""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd

warnings.filterwarnings("ignore")

# 溢价告警阈值（绝对值，%）
ALERT_THRESHOLD = 3.0

# 池内 QDII ETF -> 底层市场（用于影子 IOPV 调整）
# 字段：us=美股新浪指数 / hk=港股东财指数 / global=新浪全球指数(DAX/日经) / fx=汇率币种
QDII_UNDERLYING: dict[str, dict] = {
    "513100.SH": {"market": "美股", "us": ".IXIC", "fx": "美元", "label": "纳斯达克综合(纳指100代理)"},
    "513500.SH": {"market": "美股", "us": ".INX",  "fx": "美元", "label": "标普500"},
    "513050.SH": {"market": "美股", "us": ".IXIC", "fx": "美元", "label": "纳斯达克综合(中概代理)"},
    "513030.SH": {"market": "德国", "global": "德国DAX 30种股价指数", "fx": "欧元", "label": "德国DAX"},
    "159920.SZ": {"market": "港股", "hk": "HSI",   "fx": "港币", "label": "恒生指数"},
    "513880.SH": {"market": "日本", "global": "日经225指数", "fx": "日元", "label": "日经225"},
}


class QDIICalculator:
    """QDII 溢价计算器（真实数据版）。"""

    def __init__(self):
        self._spot = None

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------
    def fetch_spot(self) -> pd.DataFrame:
        """东财 ETF 实时行情全量表，以代码为索引。"""
        df = ak.fund_etf_spot_em()
        df["代码"] = df["代码"].astype(str)
        return df.set_index("代码")

    def latest_index_change(self, us: str | None = None, hk: str | None = None,
                            global_: str | None = None) -> float | None:
        """底层指数最新一日的涨跌幅（close[-1]/close[-2] - 1）。

        us=美股新浪指数(.IXIC/.INX)、hk=港股东财指数(HSI)、global_=新浪全球
        指数(德国DAX/日经225，用 ak.index_global_hist_sina 的「指数名称」)。
        """
        try:
            if us:
                df = ak.index_us_stock_sina(symbol=us)
                c = pd.to_numeric(df["close"], errors="coerce").dropna()
                return float(c.iloc[-1] / c.iloc[-2] - 1.0)
            if global_:
                df = ak.index_global_hist_sina(symbol=global_)
                c = pd.to_numeric(df["close"], errors="coerce").dropna()
                return float(c.iloc[-1] / c.iloc[-2] - 1.0)
            if hk:
                df = ak.stock_hk_index_daily_em(symbol=hk)
                c = pd.to_numeric(df["latest"], errors="coerce").dropna()
                return float(c.iloc[-1] / c.iloc[-2] - 1.0)
        except Exception:
            return None
        return None

    def latest_fx_change(self, symbol: str | None) -> float | None:
        """外币兑人民币汇率最新一日的变动（今日/昨日 - 1）。

        用中国银行外汇牌价（currency_boc_sina），优先「央行中间价」（仅交易日），
        全部缺失时退回「中行折算价」（含周末）。正值=外币升值（人民币贬值），
        QDII 人民币计价净值随之上升。
        """
        if not symbol:
            return None
        try:
            start = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
            end = datetime.now().strftime("%Y%m%d")
            df = ak.currency_boc_sina(symbol=symbol, start_date=start, end_date=end)
            col = "央行中间价" if "央行中间价" in df.columns else "中行折算价"
            v = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(v) < 2:
                return None
            return float(v.iloc[-1] / v.iloc[-2] - 1.0)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 计算
    # ------------------------------------------------------------------
    @staticmethod
    def premium(price: float, iopv: float) -> float | None:
        """溢价率 = price / iopv - 1。"""
        if iopv in (None, 0.0):
            return None
        return float(price / iopv - 1.0)

    def get_premiums(self, codes: list[str] | None = None) -> pd.DataFrame:
        """计算指定（或全部池内）QDII ETF 的官方/影子溢价。"""
        if self._spot is None:
            self._spot = self.fetch_spot()
        codes = codes or list(QDII_UNDERLYING.keys())
        rows = []
        for code in codes:
            meta = QDII_UNDERLYING.get(code, {})
            spot_code = code.split(".")[0]  # 东财 spot 用 6 位裸代码，无 .SH/.SZ 后缀
            if spot_code not in self._spot.index:
                continue
            row = self._spot.loc[spot_code]
            price = pd.to_numeric(row["最新价"], errors="coerce")
            iopv = pd.to_numeric(row["IOPV实时估值"], errors="coerce")
            # 东财「基金折价率」= (IOPV-价格)/IOPV（正值=折价）→ 取负得溢价率（正值=溢价）
            discount = pd.to_numeric(row["基金折价率"], errors="coerce")
            official_premium = float(-discount) if pd.notna(discount) else None
            # 影子调整：折入底层指数最新涨跌 + 汇率最新变动
            chg = self.latest_index_change(us=meta.get("us"), hk=meta.get("hk"),
                                           global_=meta.get("global"))
            fx_chg = self.latest_fx_change(meta.get("fx"))
            adj = 1.0
            if chg is not None:
                adj *= (1 + chg)
            if fx_chg is not None:
                adj *= (1 + fx_chg)
            shadow_iopv = float(iopv * adj) if (pd.notna(iopv) and (chg is not None or fx_chg is not None)) else None
            rows.append({
                "code": code,
                "name": str(row.get("名称", "")),
                "market": meta.get("market", ""),
                "underlying": meta.get("label", ""),
                "price": float(price) if pd.notna(price) else None,
                "iopv_official": float(iopv) if pd.notna(iopv) else None,
                "official_premium_pct": round(official_premium, 3) if official_premium is not None else None,
                "underlying_chg_pct": round(chg * 100, 3) if chg is not None else None,
                "fx_chg_pct": round(fx_chg * 100, 3) if fx_chg is not None else None,
                "shadow_iopv": round(shadow_iopv, 4) if shadow_iopv is not None else None,
                "shadow_premium_pct": (
                    round(self.premium(float(price), shadow_iopv) * 100, 3)
                    if shadow_iopv is not None and pd.notna(price) else None
                ),
            })
        df = pd.DataFrame(rows)
        df["alert"] = df.apply(self._alert, axis=1)
        return df

    @staticmethod
    def _alert(r: pd.Series) -> str:
        """按影子溢价（缺失则官方）打告警标签。"""
        v = r.get("shadow_premium_pct")
        if v is None:
            v = r.get("official_premium_pct")
        if v is None or pd.isna(v):
            return "—"
        if v >= ALERT_THRESHOLD:
            return "⚠️ 溢价偏高"
        if v <= -ALERT_THRESHOLD:
            return "🟢 折价"
        return "正常"


if __name__ == "__main__":
    calc = QDIICalculator()
    out = calc.get_premiums()
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print(f"数据时点：{datetime.now():%Y-%m-%d %H:%M:%S}")
    print(out.to_string(index=False))
