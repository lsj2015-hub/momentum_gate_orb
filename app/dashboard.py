# [수정 파일: app/dashboard.py]
import sys
import os
import streamlit as st
import asyncio
from datetime import datetime
import time
import threading
import nest_asyncio
import traceback

nest_asyncio.apply()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from core.engine import TradingEngine
    from config.loader import config # config는 기본값 로드에 사용
except ImportError as e:
    st.error(f"필수 모듈 임포트 실패: {e}. 경로 설정을 확인하세요.")
    print(f"🚨🚨🚨 [CRITICAL_IMPORT] 필수 모듈 임포트 실패: {e}\n{traceback.format_exc()}")
    st.stop()
except BaseException as e: 
    st.error(f"초기화 중 예상치 못한 오류 (Import 단계): {e}")
    print(f"🚨🚨🚨 [CRITICAL_IMPORT_BASE] 예상치 못한 오류 (Import 단계): {e}\n{traceback.format_exc()}")
    st.stop()

st.set_page_config(page_title="Momentum Gate ORB Bot", page_icon="🤖", layout="wide")

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
            # nest_asyncio가 적용되었으므로 asyncio.run() 대신 get_event_loop().run_until_complete() 사용
            loop = asyncio.get_event_loop()
            if loop.is_running():
                st.warning("이벤트 루프가 이미 실행 중입니다. Task로 종료를 시도합니다.")
                loop.create_task(engine.stop())
            else:
                loop.run_until_complete(engine.stop())
            st.info("엔진 종료 신호 전송 완료. 완료까지 잠시 기다려주세요...")
        except RuntimeError as e:
             st.error(f"엔진 종료 중 런타임 오류: {e}")
        except Exception as e: st.error(f"엔진 종료 중 예상치 못한 오류: {e}")


# --- 👇 사이드바: 전략 설정 ---
st.sidebar.header("⚙️ Strategy Settings (실시간 적용)")
st.sidebar.warning("설정 변경 후 'Apply Settings' 버튼을 눌러야 엔진에 반영됩니다. 반영된 설정은 **다음 신규 진입/스크리닝**부터 적용됩니다.")

if engine:
    # --- 탭(Tabs)을 사용하여 설정 구분 ---
    tab1, tab2, tab3 = st.sidebar.tabs(["📈 진입/청산", "💰 자금 관리", "🔍 스크리닝"])

    with tab1:
        st.markdown("#### 진입 및 청산 조건")
        orb_tf = st.slider(
            "ORB Timeframe (minutes)",
            min_value=5,
            max_value=60,
            value=engine.orb_timeframe, 
            step=5,
            help="ORB(시가 돌파) 범위를 계산할 개장 후 시간(분)입니다. [기본값: 15]"
        )
        breakout_buf = st.number_input(
            "Breakout Buffer (%)",
            min_value=0.0,
            max_value=5.0,
            value=engine.breakout_buffer, 
            step=0.05,
            format="%.2f",
            help="ORB 고가(ORH)를 돌파했다고 판단하기 위한 추가 버퍼(%)입니다. [기본값: 0.15]"
        )
        tp_pct = st.number_input(
            "Take Profit (%)",
            min_value=0.1,
            max_value=20.0, 
            value=engine.take_profit_pct, 
            step=0.1,
            format="%.2f",
            help="포지션 진입 가격 대비 목표 익절 수익률(%)입니다. [기본값: 2.5]"
        )
        sl_pct = st.number_input(
            "Stop Loss (%)",
            min_value=-20.0, 
            max_value=-0.1, 
            value=engine.stop_loss_pct, 
            step=-0.1, 
            format="%.2f",
            help="포지션 진입 가격 대비 허용 손실률(%)입니다. (음수) [기본값: -1.0]"
        )

    with tab2:
        st.markdown("#### 자금 및 포지션 관리")
        invest_amt = st.number_input(
            "종목당 투자 금액 (원)",
            min_value=50000,
            max_value=10000000, # 최대 1천만원 (필요시 조정)
            value=engine.investment_amount_per_stock,
            step=50000,
            help=f"한 종목 신규 진입 시 사용할 고정 투자 금액(원)입니다. [기본값: {config.strategy.investment_amount_per_stock}]"
        )
        max_pos = st.slider(
            "최대 동시 보유 종목 수",
            min_value=1,
            max_value=20,
            value=engine.max_concurrent_positions,
            step=1,
            help=f"동시에 'IN_POSITION' 상태로 보유할 수 있는 최대 종목 수입니다. [기본값: {config.strategy.max_concurrent_positions}]"
        )

    with tab3:
        st.markdown("#### 스크리닝 (종목 탐색) 조건")
        max_targets = st.slider(
            "최대 스크리닝 후보 수",
            min_value=1,
            max_value=20,
            value=engine.max_target_stocks,
            step=1,
            help=f"스크리닝 결과에서 상위 N개의 종목만 실시간 감시 대상으로 등록합니다. [기본값: {config.strategy.max_target_stocks}]"
        )
        screen_interval = st.slider(
            "스크리닝 주기 (분)",
            min_value=1,
            max_value=60,
            value=engine.screening_interval_minutes,
            step=1,
            help=f"새로운 종목을 탐색하는 스크리닝 로직의 실행 주기(분)입니다. [기본값: {config.strategy.screening_interval_minutes}]"
        )
        screen_surge_time = st.slider(
            "거래량 급증 비교 시간 (분)",
            min_value=1,
            max_value=30,
            value=engine.screening_surge_timeframe_minutes,
            step=1,
            help=f"거래량 급증률 계산 시 비교할 시간(N분 전 대비)입니다. [기본값: {config.strategy.screening_surge_timeframe_minutes}]"
        )
        screen_min_vol = st.number_input(
            "최소 거래량 기준 (만 주)",
            min_value=0,
            max_value=1000,
            value=engine.screening_min_volume_threshold,
            step=10,
            help=f"스크리닝 시 최소 거래량 조건 (단위: 만 주). 예: 10 -> 100,000주 [기본값: {config.strategy.screening_min_volume_threshold}]"
        )
        screen_min_price = st.number_input(
            "최소 가격 기준 (원)",
            min_value=100,
            max_value=50000,
            value=engine.screening_min_price,
            step=100,
            help=f"스크리닝 시 최소 주가 조건 (원). [기본값: {config.strategy.screening_min_price}]"
        )
        screen_min_surge = st.number_input(
            "최소 거래량 급증률 (%)",
            min_value=50.0,
            max_value=1000.0,
            value=engine.screening_min_surge_rate,
            step=10.0,
            format="%.1f",
            help=f"스크리닝 시 N분 전 대비 최소 거래량 급증률(%) 조건. [기본값: {config.strategy.screening_min_surge_rate}]"
        )

    # 설정값 업데이트 버튼 (탭 밖에 위치)
    if st.sidebar.button("Apply Settings"):
        try:
            engine.update_strategy_settings({
                # Tab 1
                'orb_timeframe': orb_tf,
                'breakout_buffer': breakout_buf,
                'take_profit_pct': tp_pct,
                'stop_loss_pct': sl_pct,
                # Tab 2
                'investment_amount_per_stock': invest_amt,
                'max_concurrent_positions': max_pos,
                # Tab 3
                'max_target_stocks': max_targets,
                'screening_interval_minutes': screen_interval,
                'screening_surge_timeframe_minutes': screen_surge_time,
                'screening_min_volume_threshold': screen_min_vol,
                'screening_min_price': screen_min_price,
                'screening_min_surge_rate': screen_min_surge,
            })
            st.sidebar.success("✅ 설정이 엔진에 반영되었습니다!")
            st.rerun() # 설정 적용 후 화면 즉시 갱신
        except Exception as e:
            st.sidebar.error(f"설정 적용 실패: {e}")
else:
    st.sidebar.error("엔진이 초기화되지 않아 설정을 표시할 수 없습니다.")
# --- 👆 사이드바 끝 ---

# --- 제목 및 UI ---
st.title("🤖 Momentum Gate ORB Trading Bot")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

col1, col2 = st.columns(2)

# 메인 스레드에서 오류 상태 반영
if 'engine_status_override' in st.session_state and st.session_state.engine_status_override == 'ERROR':
    if hasattr(engine, 'engine_status'): engine.engine_status = 'ERROR'
    del st.session_state.engine_status_override

if not engine or not hasattr(engine, 'engine_status'):
     st.error("엔진 객체가 올바르게 초기화되지 않았습니다. 코드를 확인하세요.")
     st.stop()


with col1:
  st.subheader("⚙️ Engine Control & Status")
  st.metric("엔진 상태", engine.engine_status)

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

  if engine.engine_status == 'RUNNING':
      if st.button("🚨 긴급 정지 (Kill Switch)"):
          st.warning("긴급 정지 신호 전송! 모든 미체결 취소 및 포지션 청산을 시도합니다...")
          try:
              # nest_asyncio가 적용되었으므로 asyncio.run() 대신 get_event_loop().run_until_complete() 사용
              loop = asyncio.get_event_loop()
              if loop.is_running():
                  st.warning("이벤트 루프가 이미 실행 중입니다. Task로 Kill Switch를 시도합니다.")
                  loop.create_task(engine.execute_kill_switch())
              else:
                  loop.run_until_complete(engine.execute_kill_switch())
              st.success("Kill Switch 처리 완료됨.")
          except RuntimeError as e:
              st.error(f"Kill Switch 실행 중 런타임 오류: {e}")
          except Exception as e: st.error(f"Kill Switch 실행 중 오류: {e}")
          st.rerun()

  st.markdown("---")
  
  # --- 👇 현재 설정 표시 (두 섹션으로 분리) ---
  st.markdown("##### **Current Strategy (Entry/Exit)**")
  if engine:
      st.markdown(f"- ORB Timeframe: **{engine.orb_timeframe} 분** | Buffer: **{engine.breakout_buffer:.2f} %**")
      st.markdown(f"- Take Profit: **{engine.take_profit_pct:.2f} %** | Stop Loss: **{engine.stop_loss_pct:.2f} %**")

  st.markdown("##### **Current Screening & Capital**")
  if engine:
      st.markdown(f"- 투자금(종목당): **{engine.investment_amount_per_stock:,} 원**")
      st.markdown(f"- 최대 보유: **{engine.max_concurrent_positions} 종목** | 최대 후보: **{engine.max_target_stocks} 종목**")
      st.markdown(f"- 스크리닝 주기: **{engine.screening_interval_minutes} 분**")
      st.markdown(f"<small>  (조건) 급증시간: {engine.screening_surge_timeframe_minutes}분 | "
                  f"최소거래(만): {engine.screening_min_volume_threshold} | "
                  f"최소가: {engine.screening_min_price}원 | "
                  f"최소급증률: {engine.screening_min_surge_rate:.1f}%</small>", unsafe_allow_html=True)
      
  st.markdown("##### **스크리닝 후보 종목**")
  if hasattr(engine, 'candidate_stocks_info') and engine.candidate_stocks_info:
    display_candidates = [f"{info['stk_cd']} ({info['stk_nm']})" for info in engine.candidate_stocks_info]
    st.code('\n'.join(display_candidates), language='text')
  else:
    st.info("현재 스크리닝된 후보 종목 없음")

  st.markdown("##### **현재 포지션**")
  if hasattr(engine, 'positions') and engine.positions:
    st.markdown("###### 보유 종목:")
    position_details = []
    for code, pos_data in engine.positions.items():
      if isinstance(pos_data, dict) and pos_data.get('status') != 'CLOSED': # 닫힌 포지션 제외
          entry_price = pos_data.get('entry_price', 'N/A')
          size = pos_data.get('size', 'N/A')
          status = pos_data.get('status', 'N/A')
          # [신규] 포지션에 고정된 TP/SL 값 표시
          tp = pos_data.get('target_profit_pct', 'N/A')
          sl = pos_data.get('stop_loss_pct', 'N/A')
          position_details.append(
              f"- **{code}**: {size}주 @ {entry_price} (상태: {status})\n"
              f"  - `TP: {tp}% / SL: {sl}%`"
          )
      elif isinstance(pos_data, dict) and pos_data.get('status') == 'CLOSED':
          pass # 닫힌 포지션은 표시 안함
      else:
           position_details.append(f"- **{code}**: 데이터 형식 오류 ({type(pos_data)})")
    
    if position_details:
        st.markdown("\n".join(position_details))
    else:
        st.info("현재 보유 포지션 없음")
  else:
    st.info("현재 보유 포지션 없음")


with col2:
  st.subheader("📊 Live Chart & Indicators")
  st.info("실시간 차트가 여기에 표시됩니다. (기능 구현 예정)")

st.divider()

st.subheader("📝 Trading Logs")
log_list = getattr(engine, 'logs', ["엔진 로그를 가져올 수 없습니다."])

log_text = "\n".join(log_list)
st.text_area("Logs", value=log_text, height=300, disabled=True, key="log_area") 

if hasattr(engine, 'engine_status') and engine.engine_status in ['RUNNING', 'INITIALIZING', 'STOPPING']:
    thread_alive = st.session_state.engine_thread and st.session_state.engine_thread.is_alive()
    if thread_alive or engine.engine_status == 'STOPPING':
        time.sleep(5) 
        st.rerun()
    elif st.session_state.engine_thread is not None and not thread_alive and engine.engine_status not in ['STOPPED', 'ERROR', 'KILLED']:
         engine.add_log("⚠️ 엔진 스레드가 예기치 않게 종료되었습니다. 상태를 확인하세요.")
         engine.engine_status = 'ERROR'
         time.sleep(1)
         st.rerun()