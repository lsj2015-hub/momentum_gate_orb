# app/dashboard.py
import sys
import os
import streamlit as st
import asyncio
from datetime import datetime
import time
import threading
import nest_asyncio
import traceback

# nest_asyncio 적용
nest_asyncio.apply()

# 프로젝트 루트 경로 추가
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# --- 모듈 임포트 ---
try:
    from core.engine import TradingEngine
    from config.loader import config
except ImportError as e:
    st.error(f"필수 모듈 임포트 실패: {e}. 경로 설정을 확인하세요.")
    print(f"🚨🚨🚨 [CRITICAL_IMPORT] 필수 모듈 임포트 실패: {e}\n{traceback.format_exc()}")
    st.stop()
except BaseException as e: # Catch BaseException during import
    st.error(f"초기화 중 예상치 못한 오류 (Import 단계): {e}")
    print(f"🚨🚨🚨 [CRITICAL_IMPORT_BASE] 예상치 못한 오류 (Import 단계): {e}\n{traceback.format_exc()}")
    st.stop()

# --- 페이지 기본 설정 ---
st.set_page_config(page_title="Momentum Gate ORB Bot", page_icon="🤖", layout="wide")

# --- 엔진 인스턴스 관리 ---
engine = None
if 'engine' not in st.session_state:
    try:
        engine_instance = TradingEngine()
        st.session_state.engine = engine_instance
        st.session_state.engine_thread = None
        st.info("엔진 인스턴스 생성 완료. 백그라운드 실행을 시작하세요.")
    except BaseException as e:
        st.error(f"TradingEngine 인스턴스 생성 실패: {e}")
        st.exception(e)
        print(f"🚨🚨🚨 [CRITICAL_INIT_BASE] TradingEngine 인스턴스 생성 실패: {e}\n{traceback.format_exc()}")
        st.session_state.engine_status_override = 'ERROR'

# --- 엔진 객체 가져오기 ---
if 'engine' in st.session_state:
    engine = st.session_state.engine
elif 'engine_status_override' in st.session_state and st.session_state.engine_status_override == 'ERROR':
     pass
else:
     st.error("엔진 객체를 초기화하거나 가져올 수 없습니다.")
     print("🚨🚨🚨 [CRITICAL_SESSION] 엔진 객체 초기화/검색 최종 실패.")
     st.stop()

def run_engine_in_background():
    """엔진 start() 메서드를 별도 스레드에서 실행"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [DASHBOARD_THREAD] engine.start() 호출 시도...")
        loop.run_until_complete(engine.start())
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [DASHBOARD_THREAD] engine.start() 정상 종료됨.")
        loop.close()
    except BaseException as e:
        error_msg = f"🚨🚨🚨 [CRITICAL] 엔진 스레드에서 처리되지 않은 심각한 오류 발생 (dashboard 스레드에서 감지): {e} 🚨🚨🚨\n{traceback.format_exc()}"
        print(error_msg)
        try:
            if hasattr(engine, 'add_log'): engine.add_log(error_msg)
            else: print(" -> engine.add_log 호출 불가 (dashboard)")
        except Exception as log_e: print(f"로그 기록 중 추가 오류: {log_e}\n원본 오류:{error_msg}")
        st.session_state.engine_status_override = 'ERROR'
        st.session_state.engine_thread = None
    finally:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [DASHBOARD_THREAD] run_engine_in_background 스레드 종료됨.")

def stop_engine_background():
    """엔진 stop() 메서드 호출 (비동기 함수 호출)"""
    if engine and engine.engine_status in ['RUNNING', 'INITIALIZING']:
        st.info("엔진 종료 신호 전송 시도...")
        try:
            asyncio.run(engine.stop())
            st.info("엔진 종료 신호 전송 완료. 완료까지 잠시 기다려주세요...")
        except RuntimeError as e:
             if "cannot run loop while another loop is running" in str(e):
                 st.warning("이벤트 루프 충돌 감지. 엔진 종료 재시도 중...")
                 try:
                     loop = asyncio.get_event_loop()
                     loop.create_task(engine.stop())
                     st.info("엔진 종료 신호 (task) 전송 완료.")
                 except Exception as task_e: st.error(f"엔진 종료 task 생성 실패: {task_e}")
             else: st.error(f"엔진 종료 중 런타임 오류: {e}")
        except Exception as e: st.error(f"엔진 종료 중 예상치 못한 오류: {e}")

# --- 제목 및 UI ---
st.title("🤖 Momentum Gate ORB Trading Bot")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

col1, col2 = st.columns(2)

# 메인 스레드에서 오류 상태 반영
if 'engine_status_override' in st.session_state and st.session_state.engine_status_override == 'ERROR':
    if hasattr(engine, 'engine_status'): engine.engine_status = 'ERROR'
    del st.session_state.engine_status_override

# 엔진 객체 존재 여부 확인
if not engine or not hasattr(engine, 'engine_status'):
     st.error("엔진 객체가 올바르게 초기화되지 않았습니다. 코드를 확인하세요.")
     st.stop()


with col1:
  st.subheader("⚙️ Engine Control & Status")
  st.metric("엔진 상태", engine.engine_status)

  # 엔진 시작/종료 버튼
  if engine.engine_status in ['INITIALIZING', 'STOPPED', 'ERROR', 'KILLED'] and (st.session_state.engine_thread is None or not st.session_state.engine_thread.is_alive()):
    if st.button("🚀 엔진 시작"):
      st.session_state.engine_thread = threading.Thread(target=run_engine_in_background, daemon=True)
      st.session_state.engine_thread.start()
      st.info("엔진 백그라운드 실행 시작됨...")
      time.sleep(1)
      st.rerun()

  elif engine.engine_status in ['RUNNING', 'INITIALIZING', 'STOPPING']:
    if st.button("🛑 엔진 정지"):
      stop_engine_background()
      st.rerun()

  # Kill Switch 버튼
  if engine.engine_status == 'RUNNING':
      if st.button("🚨 긴급 정지 (Kill Switch)"):
          st.warning("긴급 정지 신호 전송! 모든 미체결 취소 및 포지션 청산을 시도합니다...")
          try:
              asyncio.run(engine.execute_kill_switch())
              st.success("Kill Switch 처리 완료됨.")
          except RuntimeError as e:
              if "cannot run loop while another loop is running" in str(e):
                  try:
                      loop = asyncio.get_event_loop()
                      loop.create_task(engine.execute_kill_switch())
                      st.info("Kill Switch 신호 (task) 전송 완료.")
                  except Exception as task_e: st.error(f"Kill Switch task 생성 실패: {task_e}")
              else: st.error(f"Kill Switch 실행 중 런타임 오류: {e}")
          except Exception as e: st.error(f"Kill Switch 실행 중 오류: {e}")
          st.rerun()

  st.markdown("---")
  st.markdown("##### **스크리닝 후보 종목**")
  # --- 👇 수정된 부분 👇 ---
  if hasattr(engine, 'candidate_stocks_info') and engine.candidate_stocks_info:
    display_candidates = [f"{info['stk_cd']} ({info['stk_nm']})" for info in engine.candidate_stocks_info]
    st.code('\n'.join(display_candidates), language='text')
  # --- 👆 수정된 부분 👆 ---
  else:
    st.info("현재 스크리닝된 후보 종목 없음")

  st.markdown("##### **현재 포지션**")
  if hasattr(engine, 'positions') and engine.positions:
    st.markdown("###### 보유 종목:")
    position_details = []
    for code, pos_data in engine.positions.items():
      if isinstance(pos_data, dict):
          entry_price = pos_data.get('entry_price', 'N/A')
          size = pos_data.get('size', 'N/A')
          status = pos_data.get('status', 'N/A')
          position_details.append(f"- **{code}**: {size}주 @ {entry_price} (상태: {status})")
      else:
           position_details.append(f"- **{code}**: 데이터 형식 오류 ({type(pos_data)})")
    st.markdown("\n".join(position_details))
  else:
    st.info("현재 보유 포지션 없음")

with col2:
  st.subheader("📊 Live Chart & Indicators")
  st.info("실시간 차트가 여기에 표시됩니다. (기능 구현 예정)")

st.divider()

# --- 👇 로그 표시 부분 (기존 코드 유지) 👇 ---
st.subheader("📝 Trading Logs")
# 로그가 변경되었을 수 있으므로 최신 상태 표시
# logs 속성 존재 확인
log_list = getattr(engine, 'logs', ["엔진 로그를 가져올 수 없습니다."])

# --- 👇 디버깅 코드 추가 ---
st.write(f"--- DEBUG: 현재 로그 개수: {len(log_list)} ---")
if log_list:
    st.write(f"--- DEBUG: 최신 로그 샘플: {log_list[0][:100]}... ---") # 너무 길지 않게 일부만 표시
# --- 👆 디버깅 코드 추가 끝 ---

log_text = "\n".join(log_list)
st.text_area("Logs", value=log_text, height=300, disabled=True, key="log_area") # key는 이미 있음

# --- 자동 새로고침 ---
if hasattr(engine, 'engine_status') and engine.engine_status in ['RUNNING', 'INITIALIZING', 'STOPPING']:
    thread_alive = st.session_state.engine_thread and st.session_state.engine_thread.is_alive()
    if thread_alive or engine.engine_status == 'STOPPING':
        time.sleep(5) # 새로고침 간격 (초)
        st.rerun()
    elif st.session_state.engine_thread is not None and not thread_alive and engine.engine_status not in ['STOPPED', 'ERROR', 'KILLED']:
         engine.add_log("⚠️ 엔진 스레드가 예기치 않게 종료되었습니다. 상태를 확인하세요.")
         engine.engine_status = 'ERROR'
         time.sleep(1)
         st.rerun()