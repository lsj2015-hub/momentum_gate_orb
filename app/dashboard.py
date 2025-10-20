# 이 대시보드는 앞으로 우리 봇의 관제탑 역할을 하게 됩니다.

import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import streamlit as st
import pandas as pd
import asyncio
from datetime import datetime
import time

from core.engine import TradingEngine

# --- 페이지 기본 설정 ---
st.set_page_config(page_title="Momentum Gate ORB Bot", page_icon="🤖", layout="wide")

if 'engine' not in st.session_state:
  st.session_state.engine = TradingEngine()

engine = st.session_state.engine

# --- 비동기 로직 실행 ---
# Streamlit의 동기 환경에서 엔진의 비동기 함수를 실행
asyncio.run(engine.process_tick())

# --- 제목 및 UI ---
st.title("🤖 Momentum Gate ORB Trading Bot")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

col1, col2 = st.columns(2)

with col1:
  st.subheader("⚙️ Engine Status")
  st.markdown("##### **감시 종목**")
  st.code(f"{engine.target_stock} (마니커)", language="text")

  st.markdown("##### **현재 포지션**")
  if engine.position:
    entry_price = engine.position.get('entry_price', 'N/A')
    size = engine.position.get('size', 'N/A')
    st.success(f"**매수 포지션 보유 중**\n- 진입 가격: {entry_price}\n- 보유 수량: {size}")
  else:
    st.info("현재 보유 포지션 없음")

  st.markdown("##### **Kill-Switch**")
  if st.button("🚨 긴급 정지 및 모든 포지션 청산"):
    st.warning("긴급 정지 신호가 보내졌습니다! (기능 구현 예정)")

with col2:
  st.subheader("📊 Live Chart & Indicators")
  st.info("실시간 차트가 여기에 표시됩니다. (기능 구현 예정)")

st.divider()

st.subheader("📝 Trading Logs")
log_text = "\n".join(engine.logs)
st.text_area("Logs", value=log_text, height=300, disabled=True)

# --- 자동 새로고침 ---
time.sleep(60)
st.rerun()