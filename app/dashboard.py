# app/dashboard.py 수정

import sys
import os
import streamlit as st
import pandas as pd
import asyncio
from datetime import datetime
import time

# 프로젝트 루트 경로 추가 (기존 코드 유지)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# 필요한 모듈 import
from core.engine import TradingEngine
from config.loader import config 
from gateway.kiwoom_api import KiwoomAPI

# Streamlit 환경에서 asyncio 중첩 실행 허용 (기존 코드 유지)
import nest_asyncio
nest_asyncio.apply()

# --- 페이지 기본 설정 ---
st.set_page_config(page_title="Momentum Gate ORB Bot", page_icon="🤖", layout="wide")

# --- 엔진 초기화 ---
# session_state에 엔진 인스턴스가 없으면 새로 생성하고 초기화
if 'engine' not in st.session_state:
  engine = TradingEngine(config) # config 객체를 전달하여 엔진 생성
  st.session_state.engine = engine
  st.info("엔진 인스턴스 생성 완료. 초기 종목 탐색을 시작합니다...")

  # initialize_session 비동기 호출을 위한 별도 함수 정의
  async def init_engine():
    api = KiwoomAPI() # API 인스턴스 생성
    try:
      await engine.initialize_session(api) # 생성된 api 객체 전달
    except Exception as e:
      st.error(f"엔진 초기화 중 오류 발생: {e}") # Streamlit UI에 오류 표시
      engine.add_log(f"🚨 엔진 초기화 중 오류 발생: {e}") # 엔진 로그에도 기록
    finally:
      await api.close() # API 연결 종료

  # 비동기 함수 실행
  asyncio.run(init_engine())
  st.rerun() # 초기화 후 화면 새로고침

engine = st.session_state.engine # session_state에서 엔진 가져오기

# --- 비동기 로직 실행 (매 틱 처리) ---
# Streamlit 환경에서 엔진의 비동기 함수 실행 (기존 코드 유지)
try:
    asyncio.run(engine.process_tick())
except Exception as e:
    st.error(f"틱 처리 중 오류 발생: {e}")
    engine.add_log(f"🚨 틱 처리 중 오류 발생: {e}")

# --- 제목 및 UI ---
st.title("🤖 Momentum Gate ORB Trading Bot")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# --- 상태 표시 컬럼 ---
col1, col2 = st.columns(2)

with col1:
  st.subheader("⚙️ Engine Status")
  current_status = engine.position.get('status', 'N/A')
  st.metric("현재 상태", current_status)

  st.markdown("##### **감시/보유 종목**")
  # 상태에 따라 표시할 종목 결정
  display_stock = engine.position.get('stk_cd') if current_status in ['IN_POSITION', 'PENDING_EXIT'] else engine.target_stock

  if display_stock:
      # 엔진에 저장된 종목명 사용, 없으면 빈 문자열
      stock_name = engine.target_stock_name if engine.target_stock_name else ""
      # "종목코드(종목명)" 형식으로 표시
      st.code(f"{display_stock}({stock_name})", language="text")
  else:
      st.info("대상 종목 없음")


  st.markdown("##### **현재 포지션**")
  if current_status == 'IN_POSITION':
    entry_price = engine.position.get('entry_price', 'N/A')
    size = engine.position.get('size', 'N/A')
    # 현재가 및 평가손익 계산 (process_tick에서 가져온 데이터를 사용하거나, 실시간 조회 필요)
    # 여기서는 간단히 표시만 함
    st.success(f"**매수 포지션 보유 중**\n- 진입 가격: {entry_price}\n- 보유 수량: {size}")
    # st.metric("현재가", current_price) # current_price 변수 필요
    # st.metric("평가손익", f"{pnl_pct:.2f}%") # pnl_pct 변수 필요
  elif current_status in ['PENDING_ENTRY', 'PENDING_EXIT']:
    st.warning(f"주문 처리 대기 중... (주문번호: {engine.position.get('order_no', 'N/A')})")
  else:
    st.info("현재 보유 포지션 없음")

  st.markdown("##### **Kill-Switch**")
  if st.button("🚨 긴급 정지 및 모든 포지션 청산"):
    st.warning("긴급 정지 신호를 보냅니다...")
    # Kill Switch 비동기 호출
    async def run_kill_switch():
        await engine.execute_kill_switch()
    asyncio.run(run_kill_switch())
    st.rerun() # 상태 변경 반영을 위해 새로고침


with col2:
  st.subheader("📊 Live Chart & Indicators")
  st.info("실시간 차트가 여기에 표시됩니다. (기능 구현 예정)")

st.divider()

# --- 로그 표시 ---
st.subheader("📝 Trading Logs")
log_text = "\n".join(engine.logs)
st.text_area("Logs", value=log_text, height=300, disabled=True, key="log_area") # key 추가

# --- 자동 새로고침 ---
# 주의: Streamlit Cloud 등 배포 환경에서는 sleep 사용에 제약이 있을 수 있음
# 로컬 테스트 용도로 사용
REFRESH_INTERVAL = 60 # 초 단위
time.sleep(REFRESH_INTERVAL)
st.rerun()