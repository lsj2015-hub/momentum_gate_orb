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
import pandas as pd
import plotly.graph_objects as go
import json

nest_asyncio.apply()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from core.engine import TradingEngine
    from config.loader import config
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

# --- 제목 ---
st.title("🤖 Momentum Gate ORB Trading Bot")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# --- col1, col2 레이아웃을 st.tabs로 변경 ---
tab_engine, tab_chart, tab_performance = st.tabs([
    "⚙️ Engine & Positions", 
    "📊 Live Chart", 
    "📈 Performance"
])

# 메인 스레드에서 오류 상태 반영
if 'engine_status_override' in st.session_state and st.session_state.engine_status_override == 'ERROR':
    if hasattr(engine, 'engine_status'): engine.engine_status = 'ERROR'
    del st.session_state.engine_status_override

if not engine or not hasattr(engine, 'engine_status'):
     st.error("엔진 객체가 올바르게 초기화되지 않았습니다. 코드를 확인하세요.")
     st.stop()


# --- 탭 1: 엔진 컨트롤 및 포지션 ---
with tab_engine:
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
          tp = pos_data.get('target_profit_pct', 'N/A')
          sl = pos_data.get('stop_loss_pct', 'N/A')
          position_details.append(
              f"- **{code}**: {size}주 @ {entry_price} (상태: {status})\n"
              f"  - `TP: {tp}% / SL: {sl}%`"
          )
      elif isinstance(pos_data, dict) and pos_data.get('status') == 'CLOSED':
          pass 
      else:
           position_details.append(f"- **{code}**: 데이터 형식 오류 ({type(pos_data)})")
    
    if position_details:
        st.markdown("\n".join(position_details))
    else:
        st.info("현재 보유 포지션 없음")
  else:
    st.info("현재 보유 포지션 없음")


# --- 탭 2: 실시간 차트 ---
with tab_chart:
  st.subheader("📊 Live Chart & Indicators")
  
  if engine and hasattr(engine, 'subscribed_codes') and engine.subscribed_codes:
    
    chartable_stocks = list(engine.subscribed_codes)
    
    display_names = []
    if hasattr(engine, 'candidate_stocks_info') and engine.candidate_stocks_info:
        name_map = {info['stk_cd']: info['stk_nm'] for info in engine.candidate_stocks_info}
        for code in chartable_stocks:
            if code in engine.positions and 'stk_nm' in engine.positions[code]:
                name = engine.positions[code]['stk_nm']
            else:
                name = name_map.get(code, code) 
            display_names.append(f"{code} ({name})")
    else:
        display_names = chartable_stocks

    if not display_names:
        st.info("감시 중인 종목이 없습니다.")
    else:
        selected_display_name = st.selectbox("차트 조회 종목 선택", options=display_names)
        selected_stock_code = selected_display_name.split(" ")[0] 

        df = engine.ohlcv_data.get(selected_stock_code)
        orb_data = engine.orb_levels.get(selected_stock_code)
        pos_data = engine.positions.get(selected_stock_code)

        if df is None or df.empty:
            st.info(f"[{selected_stock_code}] 1분봉 데이터 로딩 중입니다. 잠시 후 새로고침 됩니다...")
        else:
            fig = go.Figure()

            fig.add_trace(go.Candlestick(
                x=df.index,
                open=df['open'], high=df['high'],
                low=df['low'], close=df['close'],
                name=f"{selected_stock_code} 1m"
            ))

            if 'vwap' in df.columns:
                fig.add_trace(go.Scatter(
                    x=df.index, y=df['vwap'],
                    mode='lines', name='VWAP',
                    line=dict(color='orange', width=1)
                ))
            
            ema_short_col = f'EMA_{engine.config.strategy.ema_short_period}'
            ema_long_col = f'EMA_{engine.config.strategy.ema_long_period}'
            if ema_short_col in df.columns:
                 fig.add_trace(go.Scatter(
                    x=df.index, y=df[ema_short_col],
                    mode='lines', name=f'EMA({engine.config.strategy.ema_short_period})',
                    line=dict(color='cyan', width=1)
                ))
            if ema_long_col in df.columns:
                 fig.add_trace(go.Scatter(
                    x=df.index, y=df[ema_long_col],
                    mode='lines', name=f'EMA({engine.config.strategy.ema_long_period})',
                    line=dict(color='purple', width=1)
                ))

            if orb_data:
                if orb_data.get('orh') is not None:
                    fig.add_hline(y=orb_data['orh'], line_width=1.5, line_dash="dash", line_color="red",
                                  annotation_text="ORH", annotation_position="bottom right")
                if orb_data.get('orl') is not None:
                    fig.add_hline(y=orb_data['orl'], line_width=1.5, line_dash="dash", line_color="blue",
                                  annotation_text="ORL", annotation_position="top right")

            if pos_data and pos_data.get('entry_time') and pos_data.get('entry_price'):
                entry_time = pd.to_datetime(pos_data['entry_time'])
                entry_price = pos_data['entry_price']
                
                if entry_time >= df.index.min() and entry_time <= df.index.max():
                    fig.add_trace(go.Scatter(
                        x=[entry_time],
                        y=[entry_price],
                        mode='markers',
                        name='Buy Entry',
                        marker_symbol='triangle-up',
                        marker_color='green',
                        marker_size=15
                    ))

            fig.update_layout(
                title=f"[{selected_stock_code}] 1-Min Chart & Indicators",
                xaxis_title="Time",
                yaxis_title="Price",
                xaxis_rangeslider_visible=False, 
                margin=dict(l=20, r=20, t=50, b=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
            )

            st.plotly_chart(fig, use_container_width=True)

  else:
    st.info("엔진이 실행되면 여기에 감시 대상 종목이 표시됩니다.")


# --- 탭 3: 성과 분석 ---
def load_and_analyze_trades() -> pd.DataFrame:
    """trades_history.jsonl 파일을 로드하고 PnL을 계산합니다."""
    HISTORY_FILE = "trades_history.jsonl"
    if not os.path.exists(HISTORY_FILE):
        return pd.DataFrame() # 파일이 없으면 빈 DataFrame 반환

    try:
        # 1. 파일 로드
        trade_df = pd.read_json(HISTORY_FILE, lines=True, dtype={'stk_cd': str})
        if trade_df.empty:
            return pd.DataFrame()
        
        # 2. 데이터 정제 및 PnL 계산
        # engine.py에서 'original_size_before_exit'는 청산 주문 시점의 총 보유량 (즉, 총 매수량)
        # 'filled_value'는 총 매도 금액 (부분 청산 포함 누적)
        # 'entry_price'는 평균 매수 단가
        
        # entry_price가 None인 경우(체결 전 오류 등)를 대비
        trade_df = trade_df.dropna(subset=['entry_price'])
        
        trade_df['buy_cost'] = trade_df['entry_price'] * trade_df['original_size_before_exit']
        trade_df['pnl'] = trade_df['filled_value'] - trade_df['buy_cost']
        
        # pnl_pct 계산 (buy_cost가 0인 경우 방지)
        trade_df['pnl_pct'] = trade_df.apply(
            lambda row: (row['pnl'] / row['buy_cost']) * 100 if row['buy_cost'] != 0 else 0,
            axis=1
        )

        # 시간 변환 (차트용)
        trade_df['entry_time'] = pd.to_datetime(trade_df['entry_time'])
        trade_df = trade_df.sort_values(by='entry_time')
        
        # 누적 손익
        trade_df['cumulative_pnl'] = trade_df['pnl'].cumsum()
        
        return trade_df

    except Exception as e:
        # st.error는 메인 스레드에서만 호출 가능하므로, 여기서는 print로 대체
        print(f"🚨 [DASHBOARD] 매매 이력 파일({HISTORY_FILE}) 로드 또는 분석 중 오류: {e}")
        return pd.DataFrame()

with tab_performance:
    st.subheader("📈 Performance Analysis (From `trades_history.jsonl`)")

    # 1. 위에서 정의한 헬퍼 함수 호출
    trade_df = load_and_analyze_trades()

    if trade_df.empty:
        st.info("아직 완료된 매매 이력(`trades_history.jsonl`)이 없습니다.")
    else:
        # 2. KPI 계산
        total_pnl = trade_df['pnl'].sum()
        total_trades = len(trade_df)
        
        winning_trades = trade_df[trade_df['pnl'] > 0]
        losing_trades = trade_df[trade_df['pnl'] <= 0] # 본전 포함
        
        win_rate = (len(winning_trades) / total_trades) * 100 if total_trades > 0 else 0
        
        total_profit = winning_trades['pnl'].sum()
        total_loss = losing_trades['pnl'].abs().sum()
        
        profit_factor = total_profit / total_loss if total_loss > 0 else 999.0 # 0으로 나누기 방지
        
        avg_profit = winning_trades['pnl'].mean()
        avg_loss = losing_trades['pnl'].mean()

        # 3. KPI 시각화 (st.metric)
        kpi_cols = st.columns(5)
        kpi_cols[0].metric("총 실현 손익 (원)", f"{total_pnl:,.0f}")
        kpi_cols[1].metric("총 거래 횟수", f"{total_trades} 회")
        kpi_cols[2].metric("승률 (%)", f"{win_rate:.2f}")
        kpi_cols[3].metric("손익비 (Profit Factor)", f"{profit_factor:.2f}")
        kpi_cols[4].metric("평균 손익 (원)", f"{trade_df['pnl'].mean():,.0f}")

        st.markdown(f" (평균 수익: `{avg_profit:,.0f} 원` | 평균 손실: `{avg_loss:,.0f} 원`)")

        st.markdown("---")
        
        # 4. 누적 손익 그래프
        st.subheader("Cumulative PnL")
        # entry_time을 인덱스로 사용해야 line_chart가 시간순으로 올바르게 표시
        chart_df = trade_df.set_index('entry_time')
        st.line_chart(chart_df['cumulative_pnl'], use_container_width=True)
        
        # 5. 매매 이력 테이블
        st.subheader("Trade History")
        st.dataframe(trade_df[[
            'stk_cd', 'entry_time', 'exit_signal', 
            'entry_price', 'buy_cost', 'filled_value', 
            'pnl', 'pnl_pct'
        ]].sort_values(by='entry_time', ascending=False), use_container_width=True)

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