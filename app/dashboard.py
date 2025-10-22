# app/dashboard.py

import sys
import os
import streamlit as st
import asyncio
from datetime import datetime
import time
import nest_asyncio # Streamlit 환경에서 asyncio 루프 중첩 허용
import pandas as pd # 평가 손익 계산 위해 추가

# --- 프로젝트 경로 설정 ---
# 현재 파일의 디렉토리 기준 상위 폴더를 sys.path에 추가
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.engine import TradingEngine
from config.loader import config # config 객체 임포트
# ❗️ KiwoomAPI 임포트 추가 (현재가 조회를 위해)
from gateway.kiwoom_api import KiwoomAPI

# --- 페이지 기본 설정 ---
st.set_page_config(page_title="Momentum Gate ORB Bot", page_icon="🤖", layout="wide")

# --- Streamlit 세션 상태 초기화 ---
# 앱이 처음 실행될 때 또는 새로고침될 때 엔진 객체를 생성하고 초기화 상태를 관리합니다.
if 'engine' not in st.session_state:
  st.session_state.engine = TradingEngine(config) # 엔진 생성 시 config 전달
  st.session_state.initialized = False
  st.session_state.logs = [] # UI 업데이트를 위한 로그 저장 공간
  st.session_state.current_price = None # 현재가 저장 변수 추가

engine = st.session_state.engine

# --- 실시간 현재가 조회 함수 ---
async def fetch_current_price(api: KiwoomAPI, stock_code: str):
  """지정된 종목의 현재가를 조회합니다."""
  # ka10001 (주식기본정보요청) API 활용 예시
  # 또는 더 가벼운 API (예: 실시간 시세 수신) 사용 고려
  stock_info = await api.fetch_stock_info(stock_code)
  if stock_info and stock_info.get("return_code") == 0:
    price_str = stock_info.get('cur_prc', '0') # 현재가 필드는 'cur_prc'
    try:
      # '+' 또는 '-' 부호 제거 후 float 변환
      return float(price_str.replace('+', '').replace('-', ''))
    except ValueError:
      return None
  return None

# --- 비동기 로직 실행 함수 ---
async def run_engine_operations():
  """엔진 초기화, 틱 처리, 현재가 조회 비동기 함수"""
  if not st.session_state.initialized:
    await engine.initialize_session()
    st.session_state.initialized = True # 초기화 완료 플래그 설정
  
  # ❗️ process_tick 전에 API 객체를 생성하도록 순서 변경
  api_instance = KiwoomAPI()
  try:
    await engine.process_tick() # 엔진 틱 처리
    # ❗️ 포지션 보유 중일 때만 현재가 조회
    if engine.position.get('status') == 'IN_POSITION' and engine.position.get('stk_cd'):
      st.session_state.current_price = await fetch_current_price(api_instance, engine.position['stk_cd'])
    else:
      st.session_state.current_price = None # 포지션 없으면 현재가 초기화
  finally:
    await api_instance.close() # API 세션 정리
    
  st.session_state.logs = engine.logs[:] # 엔진 로그 복사

# --- Streamlit 앱에서 비동기 함수 실행 ---
try:
  # nest_asyncio 적용 (Streamlit 환경에서 중첩 루프 문제 해결)
  nest_asyncio.apply()
  asyncio.run(run_engine_operations())
except Exception as e:
  st.error(f"엔진 실행 중 오류 발생: {e}")

# --- 제목 및 UI ---
st.title("🤖 Momentum Gate ORB Trading Bot")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

col1, col2 = st.columns(2)

with col1:
  st.subheader("⚙️ Engine Status")
  st.markdown("##### **대상 종목**")
  # 엔진의 target_stock 값이 None일 경우 대비
  target_display = engine.target_stock if engine.target_stock else "선정 대기 중"
  st.code(f"{target_display}", language="text") # 종목명은 별도 조회 필요 시 추가

  st.markdown("##### **현재 상태**")
  st.info(f"**{engine.position.get('status', 'N/A')}**") # 현재 엔진 상태 표시

  st.markdown("##### **현재 포지션**")
  # position 상태가 IN_POSITION일 때만 상세 정보 표시
  if engine.position.get('status') == 'IN_POSITION':
    entry_price = engine.position.get('entry_price', 0.0)
    size = engine.position.get('size', 0)
    stk_cd = engine.position.get('stk_cd', 'N/A')
    current_price = st.session_state.current_price # 세션 상태에서 현재가 가져오기

    # ▼▼▼ 평가 손익 계산 및 표시 로직 수정 ▼▼▼
    if current_price is not None and entry_price > 0 and size > 0:
      profit_loss = (current_price - entry_price) * size
      profit_loss_pct = ((current_price - entry_price) / entry_price) * 100
      
      # 포맷팅을 최종 출력 문자열 내에서 안전하게 수행
      profit_loss_str = f"{profit_loss:,.0f} 원"
      profit_loss_pct_str = f"{profit_loss_pct:.2f} %"

      if profit_loss_pct > 0:
        profit_display = f":green[▲ {profit_loss_pct_str} ({profit_loss_str})]"
      elif profit_loss_pct < 0:
        profit_display = f":red[▼ {profit_loss_pct_str} ({profit_loss_str})]"
      else:
        profit_display = f"{profit_loss_pct_str} ({profit_loss_str})"
    else:
      # 현재가 조회 중이거나, 진입 가격/수량이 유효하지 않으면 N/A 표시
      profit_display = "N/A (현재가 조회 중...)"
    # ▲▲▲ 평가 손익 계산 및 표시 로직 수정 ▲▲▲

    st.success(f"**종목: {stk_cd}**\n- 진입 가격: {entry_price:,.0f}\n- 보유 수량: {size}\n- 현재가: {current_price:,.0f if current_price else '조회 중'}\n- **평가 손익:** {profit_display}")
  else:
    st.info("현재 보유 포지션 없음")

  st.markdown("##### **Kill-Switch**")
  if st.button("🚨 긴급 정지 및 모든 포지션 청산"):
    st.warning("긴급 정지 신호가 보내졌습니다! (기능 구현 예정)") #

with col2:
  st.subheader("📊 Live Chart & Indicators")
  st.info("실시간 차트가 여기에 표시됩니다. (기능 구현 예정)") #

st.divider()

st.subheader("📝 Trading Logs")
# 엔진 내부 로그 대신 st.session_state에 복사된 로그 사용
log_text = "\n".join(st.session_state.logs)
st.text_area("Logs", value=log_text, height=300, disabled=True) #

# --- 자동 새로고침 ---
# Streamlit 앱이 자동으로 60초마다 재실행되도록 설정
time.sleep(60)
st.rerun()