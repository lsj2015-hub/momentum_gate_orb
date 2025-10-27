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
# print("[DASHBOARD_INIT] nest_asyncio applied.") # 로그 제거

# 프로젝트 루트 경로 추가
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# print("[DASHBOARD_INIT] Project path added.") # 로그 제거

# --- 모듈 임포트 ---
try:
    # print("[DASHBOARD_INIT] Importing TradingEngine...") # 로그 제거
    from core.engine import TradingEngine
    # print("[DASHBOARD_INIT] Importing config...") # 로그 제거
    from config.loader import config
    # print("[DASHBOARD_INIT] Imports successful.") # 로그 제거
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
# print("[DASHBOARD_INIT] Page config set.") # 로그 제거

# --- 엔진 인스턴스 관리 ---
engine = None # engine 변수 초기화
if 'engine' not in st.session_state:
    # print("[DASHBOARD_INIT] 'engine' not in session_state. Creating new instance...") # 로그 제거
    try:
        # print("[DASHBOARD_INIT] Instantiating TradingEngine()...") # 로그 제거
        engine_instance = TradingEngine()
        # print(f"[DASHBOARD_INIT] TradingEngine instantiated successfully. Initial status: {getattr(engine_instance, 'engine_status', 'N/A')}") # 로그 제거
        st.session_state.engine = engine_instance
        st.session_state.engine_thread = None
        # print("[DASHBOARD_INIT] Engine stored in session_state.") # 로그 제거
        st.info("엔진 인스턴스 생성 완료. 백그라운드 실행을 시작하세요.")
    except BaseException as e: # Catch BaseException during instantiation
        st.error(f"TradingEngine 인스턴스 생성 실패: {e}")
        st.exception(e)
        print(f"🚨🚨🚨 [CRITICAL_INIT_BASE] TradingEngine 인스턴스 생성 실패: {e}\n{traceback.format_exc()}")
        st.session_state.engine_status_override = 'ERROR'
else:
    # print("[DASHBOARD_INIT] 'engine' found in session_state.") # 로그 제거
    pass # engine이 이미 있으면 특별히 할 작업 없음

# --- 엔진 객체 가져오기 ---
if 'engine' in st.session_state:
    engine = st.session_state.engine
    # print(f"[DASHBOARD_INIT] Engine retrieved from session_state. Current status: {getattr(engine, 'engine_status', 'N/A')}") # 로그 제거
elif 'engine_status_override' in st.session_state and st.session_state.engine_status_override == 'ERROR':
     # print("[DASHBOARD_INIT] Engine creation failed, setting status to ERROR.") # 로그 제거
     pass # 오류 상태는 UI 로직에서 처리
else:
     st.error("엔진 객체를 초기화하거나 가져올 수 없습니다.")
     print("🚨🚨🚨 [CRITICAL_SESSION] 엔진 객체 초기화/검색 최종 실패.")
     st.stop()

def run_engine_in_background():
    """엔진 start() 메서드를 별도 스레드에서 실행"""
    try:
        # 새 이벤트 루프 생성 및 설정
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # engine.start() 실행 전 로그
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [DASHBOARD_THREAD] engine.start() 호출 시도...")
        loop.run_until_complete(engine.start())
        # engine.start() 정상 종료 후 로그
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [DASHBOARD_THREAD] engine.start() 정상 종료됨.")
        loop.close()
    except BaseException as e:
        # 스레드 종료 시 오류 로그를 engine.logs에 추가하고 터미널에도 출력
        error_msg = f"🚨🚨🚨 [CRITICAL] 엔진 스레드에서 처리되지 않은 심각한 오류 발생 (dashboard 스레드에서 감지): {e} 🚨🚨🚨\n{traceback.format_exc()}"
        print(error_msg) # 터미널에 먼저 출력 (engine 객체 접근 전)
        try:
            # engine 객체가 유효할 때만 add_log 사용
            if hasattr(engine, 'add_log'):
                engine.add_log(error_msg)
            else:
                print(" -> engine.add_log 호출 불가 (dashboard)") # engine 객체 접근 불가 시
        except Exception as log_e:
             print(f"로그 기록 중 추가 오류: {log_e}\n원본 오류:{error_msg}")

        # UI 업데이트를 위해 메인 스레드에서 상태 변경
        st.session_state.engine_status_override = 'ERROR' # 오류 상태 강제 설정
        st.session_state.engine_thread = None # 오류 발생 시 스레드 상태 초기화
    finally:
        # 스레드 종료 시 finally 블록 실행 로그
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [DASHBOARD_THREAD] run_engine_in_background 스레드 종료됨.")

# --- (stop_engine_background 및 나머지 UI 코드는 이전 답변과 동일하게 유지) ---
def stop_engine_background():
    """엔진 stop() 메서드 호출 (비동기 함수 호출)"""
    if engine and engine.engine_status in ['RUNNING', 'INITIALIZING']:
        st.info("엔진 종료 신호 전송 시도...")
        try:
            # stop 메서드가 async 함수이므로 asyncio.run 사용
            asyncio.run(engine.stop())
            st.info("엔진 종료 신호 전송 완료. 완료까지 잠시 기다려주세요...")
        except RuntimeError as e:
             if "cannot run loop while another loop is running" in str(e):
                 st.warning("이벤트 루프 충돌 감지. 엔진 종료 재시도 중...")
                 try:
                     loop = asyncio.get_event_loop()
                     loop.create_task(engine.stop())
                     st.info("엔진 종료 신호 (task) 전송 완료.")
                 except Exception as task_e:
                      st.error(f"엔진 종료 task 생성 실패: {task_e}")
             else:
                  st.error(f"엔진 종료 중 런타임 오류: {e}")
        except Exception as e:
            st.error(f"엔진 종료 중 예상치 못한 오류: {e}")

# --- 제목 및 UI ---
st.title("🤖 Momentum Gate ORB Trading Bot")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

col1, col2 = st.columns(2)

# 메인 스레드에서 오류 상태 반영
if 'engine_status_override' in st.session_state and st.session_state.engine_status_override == 'ERROR':
    if hasattr(engine, 'engine_status'): engine.engine_status = 'ERROR'
    del st.session_state.engine_status_override # 오류 상태 반영 후 플래그 제거

# 엔진 객체 존재 여부 확인
if not engine or not hasattr(engine, 'engine_status'):
     st.error("엔진 객체가 올바르게 초기화되지 않았습니다. 코드를 확인하세요.")
     st.stop()


with col1:
  st.subheader("⚙️ Engine Control & Status")

  # 엔진 상태 표시
  st.metric("엔진 상태", engine.engine_status)

  # 엔진 시작/종료 버튼
  if engine.engine_status in ['INITIALIZING', 'STOPPED', 'ERROR', 'KILLED'] and (st.session_state.engine_thread is None or not st.session_state.engine_thread.is_alive()):
    if st.button("🚀 엔진 시작"):
      # 스레드가 없거나 종료되었을 때만 새로 시작
      st.session_state.engine_thread = threading.Thread(target=run_engine_in_background, daemon=True)
      st.session_state.engine_thread.start()
      st.info("엔진 백그라운드 실행 시작됨...")
      time.sleep(1) # 스레드 시작 및 상태 변경 대기
      st.rerun() # 페이지 새로고침하여 상태 반영

  elif engine.engine_status in ['RUNNING', 'INITIALIZING', 'STOPPING']:
    if st.button("🛑 엔진 정지"):
      stop_engine_background()
      st.rerun() # 페이지 새로고침

  # Kill Switch 버튼
  if engine.engine_status == 'RUNNING': # 실행 중일 때만 활성화
      if st.button("🚨 긴급 정지 (Kill Switch)"):
          st.warning("긴급 정지 신호 전송! 모든 미체결 취소 및 포지션 청산을 시도합니다...")
          try:
              # execute_kill_switch는 비동기 함수
              asyncio.run(engine.execute_kill_switch())
              st.success("Kill Switch 처리 완료됨.")
          except RuntimeError as e:
              if "cannot run loop while another loop is running" in str(e):
                  try:
                      loop = asyncio.get_event_loop()
                      loop.create_task(engine.execute_kill_switch())
                      st.info("Kill Switch 신호 (task) 전송 완료.")
                  except Exception as task_e:
                      st.error(f"Kill Switch task 생성 실패: {task_e}")
              else:
                  st.error(f"Kill Switch 실행 중 런타임 오류: {e}")
          except Exception as e:
               st.error(f"Kill Switch 실행 중 오류: {e}")
          st.rerun()

  st.markdown("---")
  st.markdown("##### **스크리닝 후보 종목**")
  # candidate_stock_codes 속성 존재 확인
  if hasattr(engine, 'candidate_stocks_info') and engine.candidate_stocks_info:
    # 종목 정보를 "코드(이름)" 형식의 문자열 리스트로 변환
    display_candidates = [f"{info['stk_cd']} ({info['stk_nm']})" for info in engine.candidate_stocks_info]
    st.code('\n'.join(display_candidates), language='text') # 여러 줄로 표시
  # --- 👆 수정 끝 ---
  else:
    st.info("현재 스크리닝된 후보 종목 없음")

  st.markdown("##### **현재 포지션**")
  # positions 속성 존재 확인
  if hasattr(engine, 'positions') and engine.positions:
    st.markdown("###### 보유 종목:")
    position_details = []
    # positions 딕셔너리 순회하며 정보 표시
    for code, pos_data in engine.positions.items():
      # pos_data가 딕셔너리인지 확인
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

st.subheader("📝 Trading Logs")
# 로그가 변경되었을 수 있으므로 최신 상태 표시
# logs 속성 존재 확인
log_list = getattr(engine, 'logs', ["로그 속성 없음."]) # engine 객체에 logs 속성이 없어도 오류 방지
log_text = "\n".join(log_list)
st.text_area("Logs", value=log_text, height=300, disabled=True, key="log_area") # key 추가

# --- 자동 새로고침 (엔진 상태 및 로그 업데이트용) ---
# 엔진이 실행 중이거나 종료 중일 때만 자동 새로고침
if hasattr(engine, 'engine_status') and engine.engine_status in ['RUNNING', 'INITIALIZING', 'STOPPING']:
    # --- 👇 수정된 부분 👇 ---
    # st.session_state.engine_thread 객체가 존재하고, 살아있는지 확인
    thread_alive = st.session_state.engine_thread and st.session_state.engine_thread.is_alive()

    if thread_alive or engine.engine_status == 'STOPPING': # 스레드가 살아있거나, 종료 중일 때 새로고침
        time.sleep(5) # 새로고침 간격 (초)
        st.rerun()
    # elif 블록 수정: engine_thread 객체가 None이 아니고 (즉, 시작된 적이 있고) 죽었을 때만 오류 처리
    elif st.session_state.engine_thread is not None and not thread_alive and engine.engine_status not in ['STOPPED', 'ERROR', 'KILLED']:
    # --- 👆 수정된 부분 👆 ---
         # 스레드가 시작되었으나 예기치 않게 종료된 경우
         engine.add_log("⚠️ 엔진 스레드가 예기치 않게 종료되었습니다. 상태를 확인하세요.")
         engine.engine_status = 'ERROR'
         time.sleep(1)
         st.rerun()
# else: # 엔진 상태가 RUNNING, INITIALIZING, STOPPING이 아니면 자동 새로고침 안 함
#    pass