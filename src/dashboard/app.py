import streamlit as st
import pandas as pd
import time
from sqlalchemy.orm import Session
from src.database.connection import engine
from src.database.models import MarketData, ReportSentiment, FactorData
from src.data_engine.qdii_calc import QDIICalculator

st.set_page_config(page_title="AlphaQuant Dashboard", layout="wide")

def page_monitor():
    st.header("🌍 Global Market Monitor (QDII)")
    
    # Auto-refresh logic (mimicked with rerun button or st.empty)
    if st.button("Refresh Premium"):
        st.cache_data.clear()

    calc = QDIICalculator()
    etfs = {
        "Nasdaq 100": "513100.SH",
        "S&P 500": "513500.SH",
        "Semiconductor": "512480.SH" # A-Share example for contrast
    }

    data = []
    for name, code in etfs.items():
        premium = calc.get_realtime_premium(code)
        nav = calc.get_last_nav(code)
        
        # Color logic
        status = "Normal"
        if premium > 0.03: status = "⚠️ High Premium"
        elif premium < -0.01: status = "🟢 Discount"
        
        data.append({
            "Name": name,
            "Code": code,
            "Est. Premium": f"{premium:.2%}",
            "Last NAV": nav,
            "Status": status
        })
    
    df = pd.DataFrame(data)
    st.table(df)

def page_backtest():
    st.header("📈 Backtest Viewer")
    st.info("Run backtest via CLI and save results to DB or Pickle to view here.")
    
    # Simple DB query to show recent factors
    with Session(engine) as session:
        factors = session.query(FactorData).order_by(FactorData.trade_date.desc()).limit(50).all()
        if factors:
            df = pd.DataFrame([{
                "Date": f.trade_date,
                "Code": f.ts_code,
                "RSRS Z-Score": f.rsrs_zscore,
                "R2": f.rsrs_r2
            } for f in factors])
            
            st.subheader("Recent RSRS Factors")
            st.dataframe(df)
            
            # Simple Chart
            chart_data = df.set_index("Date")[["RSRS Z-Score"]]
            st.line_chart(chart_data)

def page_reports():
    st.header("🤖 AI Research Analyst")
    
    with Session(engine) as session:
        reports = session.query(ReportSentiment).order_by(ReportSentiment.publish_date.desc()).limit(20).all()
        
        if not reports:
            st.warning("No analyzed reports found.")
            return

        for r in reports:
            with st.expander(f"{r.publish_date} | {r.ts_code} | Sentiment: {r.sentiment_score}"):
                st.write(f"**Title**: {r.title}")
                st.write(f"**Summary**: {r.summary}")
                st.write(f"**Key Risks**: {r.key_risks}")
                st.metric("Sentiment Score", value=r.sentiment_score)

def main():
    st.sidebar.title("AlphaQuant Pro")
    page = st.sidebar.radio("Navigation", ["Market Monitor", "Backtest Viewer", "AI Reports"])
    
    if page == "Market Monitor":
        page_monitor()
    elif page == "Backtest Viewer":
        page_backtest()
    elif page == "AI Reports":
        page_reports()

if __name__ == "__main__":
    main()
