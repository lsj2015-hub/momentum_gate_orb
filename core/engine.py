import asyncio
import pandas as pd
import math
from datetime import datetime, timedelta
# Set, Callable 추가
from typing import Dict, List, Optional, Set, Callable
import json
import traceback # 상세 오류 로깅을 위해 추가
# 설정 로더 import
from config.loader import config
# API 게이트웨이 import
from gateway.kiwoom_api import KiwoomAPI
# 데이터 처리 및 지표 계산 import
from data.manager import preprocess_chart_data
# indicators.py 에서 필요한 모든 함수 import
from data.indicators import add_vwap, calculate_orb, add_ema, calculate_rvol, calculate_obi, get_strength
# 전략 관련 import
from strategy.momentum_orb import check_breakout_signal
from strategy.risk_manager import manage_position

class TradingEngine:
  """웹소켓 기반 실시간 다중 종목 트레이딩 로직 관장 엔진"""
  def __init__(self):
    self.config = config # 전역 config 객체 사용
    self.positions: Dict[str, Dict] = {} 
    self.logs: List[str] = [] # 최근 로그 저장 (UI 표시용)
    self.api: Optional[KiwoomAPI] = None # KiwoomAPI 인스턴스
    self._stop_event = asyncio.Event() # 엔진 종료 제어 이벤트
    screening_interval_minutes = getattr(self.config.strategy, 'screening_interval_minutes', 5)
    self.last_screening_time = datetime.now() - timedelta(minutes=screening_interval_minutes + 1)
    self.candidate_stock_codes: List[str] = []
    self.candidate_stocks_info: List[Dict[str, str]] = []
    self.realtime_data: Dict[str, Dict] = {}
    self.cumulative_volumes: Dict[str, Dict] = {}
    self.subscribed_codes: Set[str] = set()
    self.engine_status = 'STOPPED'
    self.last_stock_tick_time: Dict[str, datetime] = {}
    self._realtime_registered = False
    self._start_lock = asyncio.Lock()
    # --- 👇 VI 상태 관련 추가 ---
    self.vi_status: Dict[str, bool] = {} # {'종목코드': True/False} (True: 발동 중)
    # --- 👆 VI 상태 관련 추가 끝 ---

  def add_log(self, message: str):
    """로그 메시지를 리스트에 추가하고 터미널에도 출력"""
    log_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(log_msg)
    self.logs.insert(0, log_msg)
    if len(self.logs) > 100:
        self.logs.pop()

  # --- 👇 VI 상태 처리 함수 수정 ---
  async def _process_vi_update(self, stock_code: str, values: Dict):
      """실시간 VI 발동/해제('1h') 데이터 처리 (비동기)"""
      try:
          # --- 키움 API 문서 '1h' 응답 필드 참조 ---
          vi_status_flag = values.get('9068') # VI발동구분
          vi_type = values.get('1225')       # VI적용구분 (정적/동적/동적+정적)
          vi_direction = values.get('9069')  # 발동방향구분 (1:상승, 2:하락)
          vi_release_time_raw = values.get('1224') # VI 해제 시각 (HHMMSS)
          # --- 참조 끝 ---

          # VI 해제 시각 포맷팅 (HH:MM:SS)
          vi_release_time = f"{vi_release_time_raw[:2]}:{vi_release_time_raw[2:4]}:{vi_release_time_raw[4:]}" if vi_release_time_raw and len(vi_release_time_raw) == 6 else "N/A"

          is_vi_activated = False
          status_text = "해제" # 기본값

          # --- VI 발동/해제 판단 로직 ---
          # ❗️ 참고: 문서에 '9068'의 정확한 발동/해제 값 명시가 부족하여,
          #   일반적인 경우(값이 있으면 발동, 없거나 특정 값이면 해제)를 가정합니다.
          #   실제 API 테스트를 통해 발동/해제 시 '9068' 값을 확인하고 조정해야 합니다.
          if vi_status_flag: # 값이 존재하면 발동으로 간주 (임시 로직)
              is_vi_activated = True
              direction_text = '⬆️상승' if vi_direction == '1' else ('⬇️하락' if vi_direction == '2' else '?')
              status_text = f"발동 ({vi_type}, {direction_text})"
          # --- 판단 로직 끝 ---

          # 엔진의 VI 상태 업데이트
          self.vi_status[stock_code] = is_vi_activated
          # 로그 메시지에 해제 예정 시각 포함
          self.add_log(f"⚡️ [{stock_code}] VI 상태 업데이트: {status_text} (해제 예정: {vi_release_time})")

      except Exception as e:
          self.add_log(f"  🚨 [RT_VI] 실시간 VI({stock_code}) 처리 오류: {e}")
          self.add_log(traceback.format_exc()) # 상세 오류 로그 추가
  # --- 👆 VI 상태 처리 함수 수정 끝 ---

  # --- 👇 VI 상태 확인 함수 추가 ---
  def check_vi_status(self, stock_code: str) -> bool:
      """현재 저장된 VI 상태를 반환합니다 (True: 발동 중)."""
      return self.vi_status.get(stock_code, False)
  # --- 👆 VI 상태 확인 함수 끝 ---

  async def process_single_stock_tick(self, stock_code: str):
      """개별 종목 Tick 처리: 데이터 조회, 지표 계산, 신호 판단, 주문 실행"""
      self.add_log(f"➡️ [{stock_code}] Tick 처리 시작")
      if not self.api: self.add_log(f"  ⚠️ [{stock_code}] API 객체 없음."); return

      api = self.api
      df = None; current_price = None; current_vwap = None; obi = None; rvol = None; orderbook_data = None; strength_val = None; realtime_available = False

      try:
          # --- 1. 실시간 데이터 우선 확인 ---
          if stock_code in self.realtime_data:
              realtime_info = self.realtime_data[stock_code]; last_update_time = realtime_info.get('timestamp')
              if last_update_time and (datetime.now() - last_update_time).total_seconds() < 10:
                  current_price = realtime_info.get('last_price'); 
                  realtime_available = bool(current_price)

          # --- 2. REST API 호출 ---
          if not realtime_available or df is None: # DataFrame 없을 시에도 호출
              raw_data = await api.fetch_minute_chart(stock_code, timeframe=1)
              if raw_data and raw_data.get('return_code') == 0:
                  df = preprocess_chart_data(raw_data.get("stk_min_pole_chart_qry", []))
                  if df is not None and not df.empty:
                      if not realtime_available: current_price = df['close'].iloc[-1]
                  else: df = None
          orderbook_raw_data = await api.fetch_orderbook(stock_code)
          if orderbook_raw_data and orderbook_raw_data.get('return_code') == 0:
              orderbook_data = orderbook_raw_data

          # --- 3. 현재가 최종 확인 ---
          if current_price is None: self.add_log(f"  ⚠️ [{stock_code}] 현재가 확인 불가."); return

          # --- 4. 지표 계산 ---
          orb_levels = pd.Series({'orh': None, 'orl': None})
          current_time = pd.Timestamp.now(tz='Asia/Seoul')

          if df is not None:
              current_time = df.index[-1]
              add_vwap(df)
              add_ema(df, short_period=config.strategy.ema_short_period, long_period=config.strategy.ema_long_period)
              orb_levels = calculate_orb(df, timeframe=config.strategy.orb_timeframe)
              current_vwap = df['vwap'].iloc[-1] if 'vwap' in df.columns and not pd.isna(df['vwap'].iloc[-1]) else None
              # ✅ RVOL 계산 호출
              rvol_period = config.strategy.rvol_period
              rvol = calculate_rvol(df, window=rvol_period)
              # ✅ 체결강도 호출 (indicators.py의 임시 구현 사용)
              strength_val = get_strength(df)
              # 임시로 df에 추가 (get_strength가 df를 수정하지 않으므로)
              if strength_val is not None:
                  if 'strength' not in df.columns: df['strength'] = strength_val
                  else: df['strength'].fillna(strength_val, inplace=True)
          else: self.add_log(f"  ⚠️ [{stock_code}] DataFrame 없음. 지표 계산 제한됨.")

          # ✅ OBI 계산 호출
          if orderbook_data:
              try:
                  # --- 👇 UndefinedVariable 오류 수정 ---
                  total_bid_vol_str = orderbook_data.get('tot_buy_req') # 총매수호가총잔량
                  total_ask_vol_str = orderbook_data.get('tot_sel_req') # 총매도호가총잔량
                  # --- 👆 오류 수정 끝 ---
                  total_bid_vol = int(total_bid_vol_str.strip()) if total_bid_vol_str and total_bid_vol_str.strip().lstrip('-').isdigit() else None
                  total_ask_vol = int(total_ask_vol_str.strip()) if total_ask_vol_str and total_ask_vol_str.strip().lstrip('-').isdigit() else None
                  if total_bid_vol is not None and total_ask_vol is not None:
                     obi = calculate_obi(total_bid_vol, total_ask_vol) # ✅ 호출
                  else: self.add_log(f"  ⚠️ [{stock_code}] OBI 계산 위한 호가 잔량 추출/변환 불가.")
              except Exception as obi_e: self.add_log(f"  🚨 [{stock_code}] OBI 계산 준비 중 오류: {obi_e}")
          else: self.add_log(f"  ⚠️ [{stock_code}] 호가 데이터 없음. OBI 계산 불가.")

          # ✅ 체결강도 계산 준비
          cumulative_vols = self.cumulative_volumes.get(stock_code) # 누적량 가져오기
          strength_val = None
          if cumulative_vols:
              # ✅ get_strength 함수 호출 방식 변경 (누적량 전달)
              strength_val = get_strength(cumulative_vols['buy_vol'], cumulative_vols['sell_vol'])
          else:
              self.add_log(f"  ⚠️ [{stock_code}] 체결강도 계산 위한 누적 데이터 없음.")

          # --- 5. 필수 지표 확인 ---
          if orb_levels['orh'] is None: self.add_log(f"  ⚠️ [{stock_code}] ORH 계산 불가."); return

          # --- 로그 출력 ---
          ema_short_col = f'EMA_{config.strategy.ema_short_period}'; ema_long_col = f'EMA_{config.strategy.ema_long_period}'
          ema_short_val = df[ema_short_col].iloc[-1] if df is not None and ema_short_col in df.columns and not pd.isna(df[ema_short_col].iloc[-1]) else None
          ema_long_val = df[ema_long_col].iloc[-1] if df is not None and ema_long_col in df.columns and not pd.isna(df[ema_long_col].iloc[-1]) else None
          ema9_str = f"{ema_short_val:.2f}" if ema_short_val is not None else "N/A"
          ema20_str = f"{ema_long_val:.2f}" if ema_long_val is not None else "N/A"
          rvol_str = f"{rvol:.2f}%" if rvol is not None else "N/A"
          obi_str = f"{obi:.2f}" if obi is not None else "N/A" # indicators.py의 OBI는 %가 아님
          vwap_str = f"{current_vwap:.2f}" if current_vwap is not None else "N/A"
          orh_str = f"{orb_levels['orh']:.0f}" if orb_levels['orh'] is not None else "N/A"
          orl_str = f"{orb_levels['orl']:.0f}" if orb_levels['orl'] is not None else "N/A"
          strength_str = f"{strength_val:.2f}" if strength_val is not None else "N/A"
          self.add_log(f"📊 [{stock_code}] 현재가:{current_price:.0f}, ORH:{orh_str}, ORL:{orl_str}, VWAP:{vwap_str}, EMA({ema9_str}/{ema20_str}), RVOL:{rvol_str}, OBI:{obi_str}, Strength:{strength_str}")

          # --- 6. 전략 로직 실행 ---
          position_info = self.positions.get(stock_code)
          current_status = position_info.get('status') if position_info else 'SEARCHING'
          if current_status in ['PENDING_ENTRY', 'PENDING_EXIT']: return

          if current_status == 'SEARCHING':
              if stock_code in self.candidate_stock_codes and len(self.positions) < config.strategy.max_concurrent_positions:
                  if df is not None:
                      signal = check_breakout_signal(df, orb_levels) # ✅ 2개 인자 호출
                      rvol_ok = rvol is not None and rvol >= config.strategy.rvol_threshold
                      obi_ok = obi is not None and obi >= config.strategy.obi_threshold
                      strength_ok = strength_val is not None and strength_val >= config.strategy.strength_threshold
                      momentum_ok = ema_short_val is not None and ema_long_val is not None and ema_short_val > ema_long_val

                      if signal == "BUY" and rvol_ok and obi_ok and strength_ok and momentum_ok:
                          order_qty = await self.calculate_order_quantity(current_price)
                          if order_qty > 0:
                              self.add_log(f"🔥 [{stock_code}] 매수 신호 + 모든 필터 충족!")
                              self.add_log(f"  -> [{stock_code}] 매수 주문 API 호출 시도 ({order_qty}주)...")
                              order_result = await api.create_buy_order(stock_code, quantity=order_qty)
                              if order_result and order_result.get('return_code') == 0:
                                  order_no = order_result.get('ord_no')
                                  self.add_log(f"   ➡️ [{stock_code}] 매수 주문 접수 완료: {order_no}")
                                  self.positions[stock_code] = {
                                      'stk_cd': stock_code, 'status': 'PENDING_ENTRY', 'order_no': order_no,
                                      'original_order_qty': order_qty, 'filled_qty': 0, 'filled_value': 0.0,
                                      'entry_price': None, 'size': 0,
                                      'partial_profit_taken': False # ✅ 부분 익절 초기화
                                  }
                              else:
                                  error_msg = order_result.get('return_msg', '주문 실패') if order_result else 'API 호출 실패'
                                  self.add_log(f"   ❌ [{stock_code}] 매수 주문 실패: {error_msg}")
                      elif signal == "BUY":
                           filter_log = f"RVOL:{rvol_str}({'✅' if rvol_ok else '❌'}), OBI:{obi_str}({'✅' if obi_ok else '❌'}), Strength:{strength_str}({'✅' if strength_ok else '❌'}), Momentum:{'✅' if momentum_ok else '❌'}"
                           self.add_log(f"   ⚠️ [{stock_code}] 매수 신호 발생했으나 필터 미충족 ({filter_log}). 진입 보류.")
                  else: self.add_log(f"   ⚠️ [{stock_code}] DataFrame 없음. 진입 신호 확인 불가.")

          elif current_status == 'IN_POSITION' and position_info:
              if df is not None:
                 exit_signal = manage_position(position_info, df) # ✅ df 전달
              else: # DataFrame 없으면 고정 비율 + 부분 익절 확인
                 if position_info.get('entry_price'):
                     profit_pct = ((current_price - position_info['entry_price']) / position_info['entry_price']) * 100
                     if config.strategy.partial_take_profit_pct is not None and \
                        not position_info.get('partial_profit_taken', False) and \
                        profit_pct >= config.strategy.partial_take_profit_pct:
                         exit_signal = "PARTIAL_TAKE_PROFIT"
                     elif profit_pct >= config.strategy.take_profit_pct: exit_signal = "TAKE_PROFIT"
                     elif profit_pct <= config.strategy.stop_loss_pct: exit_signal = "STOP_LOSS"
                     else: exit_signal = None
                 else: exit_signal = None

              # --- 시간 기반 청산 ---
              TIME_STOP_HOUR = config.strategy.time_stop_hour; TIME_STOP_MINUTE = config.strategy.time_stop_minute
              if current_time.hour >= TIME_STOP_HOUR and current_time.minute >= TIME_STOP_MINUTE and exit_signal is None:
                 self.add_log(f"⏰ [{stock_code}] 시간 기반 청산 신호 발생"); exit_signal = "TIME_STOP"

              # --- 👇 VI 발동 시 청산 ---
              is_vi_activated = self.check_vi_status(stock_code) # VI 상태 확인
              if is_vi_activated and exit_signal is None:
                  self.add_log(f"⚡️ [{stock_code}] VI 발동 감지! 청산 신호 발생"); exit_signal = "VI_STOP"
              # --- 👆 VI 로직 끝 ---

              # --- 청산 신호 처리 ---
              # ✅ 부분 익절
              if exit_signal == "PARTIAL_TAKE_PROFIT":
                current_size = position_info.get('size', 0); partial_ratio = config.strategy.partial_take_profit_ratio
                size_to_sell = math.ceil(current_size * partial_ratio)
                if size_to_sell > 0 and size_to_sell < current_size :
                  self.add_log(f"💰 [{stock_code}] 부분 익절 실행 ({partial_ratio*100:.0f}%): {size_to_sell}주 매도 시도")
                  order_result = await api.create_sell_order(stock_code, size_to_sell)
                  if order_result and order_result.get('return_code') == 0:
                    order_no = order_result.get('ord_no')
                    position_info.update({
                        'status': 'PENDING_EXIT', 'order_no': order_no,
                        'exit_signal': exit_signal, 'original_size_before_exit': current_size,
                        'exit_order_qty': size_to_sell, 'filled_qty': 0, 'filled_value': 0.0
                    })
                    self.add_log(f" PARTIAL ⬅️ [{stock_code}] 부분 익절 주문 접수 완료. 상태: {position_info}")
                  else:
                    error_msg = order_result.get('return_msg', '주문 실패') if order_result else 'API 호출 실패'
                    self.add_log(f"❌ [{stock_code}] 부분 익절 주문 실패: {error_msg}")
                    position_info['status'] = 'ERROR_LIQUIDATION'
                elif size_to_sell >= current_size and current_size > 0:
                    self.add_log(f"⚠️ [{stock_code}] 부분 익절 수량({size_to_sell}) >= 보유량({current_size}). 전체 익절로 처리.")
                    exit_signal = "TAKE_PROFIT"
                else: self.add_log(f"⚠️ [{stock_code}] 부분 익절 계산 수량 0. 매도 보류.")

              # ✅ 전체 청산 (VI_STOP 포함)
              if exit_signal in ["TAKE_PROFIT", "STOP_LOSS", "EMA_CROSS_SELL", "VWAP_BREAK_SELL", "TIME_STOP", "VI_STOP"]:
                if exit_signal != "PARTIAL_TAKE_PROFIT": self.add_log(f"🎉 [{stock_code}] 전체 청산 조건 ({exit_signal}) 충족! 매도 주문 실행.")
                size_to_sell = position_info.get('size', 0)
                if size_to_sell > 0:
                    order_result = await api.create_sell_order(stock_code, size_to_sell)
                    if order_result and order_result.get('return_code') == 0:
                       order_no = order_result.get('ord_no')
                       position_info.update({
                          'status': 'PENDING_EXIT', 'order_no': order_no, 'exit_signal': exit_signal,
                          'original_size_before_exit': size_to_sell, 'exit_order_qty': size_to_sell,
                          'filled_qty': 0, 'filled_value': 0.0
                       })
                       self.add_log(f"⬅️ [{stock_code}] (전체) 청산 주문 접수 완료. 상태: {position_info}")
                    else:
                      error_msg = order_result.get('return_msg', '주문 실패') if order_result else 'API 호출 실패'
                      self.add_log(f"❌ [{stock_code}] (전체) 청산 주문 실패: {error_msg}")
                      position_info['status'] = 'ERROR_LIQUIDATION'
                else:
                    if stock_code in self.positions: del self.positions[stock_code]; await self._unsubscribe_realtime_stock(stock_code)

      except Exception as e:
          self.add_log(f"🚨 [CRITICAL] Tick 처리({stock_code}) 오류: {e} 🚨"); self.add_log(traceback.format_exc())
          if stock_code in self.positions: self.positions[stock_code]['status'] = 'ERROR_TICK'

  async def calculate_order_quantity(self, current_price: float) -> int:
      """주문 수량 계산"""
      investment_amount = config.strategy.investment_amount_per_stock
      if investment_amount <= 0: return 0
      if not self.api: return 0
      balance_info = await self.api.fetch_account_balance()
      if not balance_info or balance_info.get('return_code') != 0 or 'ord_alow_amt' not in balance_info:
          self.add_log(f"   ⚠️ [ORDER_QTY] 주문 가능 현금 조회 실패: {balance_info.get('return_msg', 'API 응답 없음')}")
          return 0
      available_cash_str = balance_info.get('ord_alow_amt', '0').lstrip('0')
      try: available_cash = int(available_cash_str) if available_cash_str else 0
      except ValueError: self.add_log(f"   ⚠️ [ORDER_QTY] 현금 변환 오류: '{available_cash_str}'"); return 0
      if available_cash < investment_amount:
          self.add_log(f"   ⚠️ [ORDER_QTY] 현금 부족 (보유:{available_cash} < 필요:{investment_amount})")
          return 0
      if current_price <= 0: self.add_log(f"   ⚠️ [ORDER_QTY] 현재가 오류 ({current_price})"); return 0
      return int(investment_amount // current_price)

  async def execute_kill_switch(self):
      """긴급 정지"""
      if self.engine_status != 'RUNNING': return
      if not self.api: return
      self.add_log("🚨 KILL Switch 발동!"); self.engine_status = 'KILLED'; self._stop_event.set()
      try:
          self.add_log("  -> [KILL] 미체결 주문 조회 및 취소 시도...")
          # pending_orders = await self.api.fetch_pending_orders() # ka10075 구현 필요
          pending_orders = [] # 임시
          if pending_orders: pass
          else: self.add_log("  - [KILL] 취소할 미체결 주문 없음.")

          positions_to_liquidate = list(self.positions.items())
          if positions_to_liquidate:
              self.add_log(f"  -> [KILL] {len(positions_to_liquidate)} 건 보유 포지션 시장가 청산 시도...")
              for stock_code, pos_info in positions_to_liquidate:
                  if pos_info.get('status') == 'IN_POSITION' and pos_info.get('size', 0) > 0:
                      quantity = pos_info['size']
                      result = await self.api.create_sell_order(stock_code, quantity)
                      if result and result.get('return_code') == 0:
                          if stock_code in self.positions: self.positions[stock_code].update({'status': 'PENDING_EXIT', 'exit_signal': 'KILL_SWITCH', 'order_no': result.get('ord_no'), 'original_size_before_exit': quantity, 'filled_qty': 0, 'filled_value': 0.0 })
                          self.add_log(f"     ✅ [KILL] 시장가 청산 주문 접수 ({stock_code} {quantity}주)")
                      else:
                          error_info = result.get('return_msg', '주문 실패') if result else 'API 호출 실패'
                          self.add_log(f"     ❌ [KILL] 시장가 청산 주문 실패 ({stock_code} {quantity}주): {error_info}")
                          if stock_code in self.positions: self.positions[stock_code]['status'] = 'ERROR_LIQUIDATION'
                  elif pos_info.get('status') in ['PENDING_ENTRY', 'PENDING_EXIT']: self.add_log(f"     ℹ️ [KILL] 주문 진행 중 포지션({stock_code})은 미체결 취소로 처리됨.")
              self.add_log("  <- [KILL] 시장가 청산 주문 접수 완료.")
          else: self.add_log("  - [KILL] 청산할 보유 포지션 없음.")
          self.add_log("🚨 Kill Switch 처리 완료. 엔진 종료 대기...")
      except Exception as e: self.add_log(f"🚨 [CRITICAL] Kill Switch 오류: {e}"); self.add_log(traceback.format_exc()); self.engine_status = 'ERROR'
      finally: await self.stop()

  def handle_realtime_data(self, ws_data: Dict):
      """웹소켓 콜백 함수"""
      try:
          trnm = ws_data.get('trnm')
          if trnm == 'REAL':
              data_type = ws_data.get('type'); values = ws_data.get('values'); item_code_raw = ws_data.get('item', '')
              if not data_type or not values: return
              stock_code = None
              if item_code_raw: stock_code = item_code_raw[1:] if item_code_raw.startswith('A') else item_code_raw; stock_code = stock_code.split('_')[0]

              # --- 👇 VI 발동/해제 ('1h') 처리 ---
              if data_type == '1h':
                  if stock_code: asyncio.create_task(self._process_vi_update(stock_code, values))
                  return
              # --- 👆 VI 처리 끝 ---
              elif data_type == '0B': # 체결
                  if stock_code: asyncio.create_task(self._process_realtime_execution(stock_code, values))
              elif data_type == '0D': # 호가
                  if stock_code: asyncio.create_task(self._process_realtime_orderbook(stock_code, values))
              elif data_type == '00': # 주문체결 응답
                  asyncio.create_task(self._process_execution_update(values))
              elif data_type == '04': # 잔고
                  if stock_code: asyncio.create_task(self._process_balance_update(stock_code, values))
          elif trnm in ['REG', 'REMOVE']: # 등록/해지 응답
              return_code_raw = ws_data.get('return_code'); return_msg = ws_data.get('return_msg', '')
              try: return_code = int(str(return_code_raw).strip())
              except: return_code = -1
              if trnm == 'REG' and not self._realtime_registered:
                  if return_code == 0: self._realtime_registered = True
                  else: self.engine_status = 'ERROR'
      except Exception as e: self.add_log(f"🚨 실시간 콜백 오류: {e}"); self.add_log(traceback.format_exc())

  async def _process_realtime_execution(self, stock_code: str, values: Dict):
      """실시간 체결(0B) 처리 및 체결강도 계산용 데이터 누적"""
      try:
          last_price_str = values.get('10') # 현재가
          exec_vol_signed_str = values.get('15') # 거래량 (+/- 포함)
          exec_time_str = values.get('20') # 체결시간 (HHMMSS)

          if not last_price_str or not exec_vol_signed_str or not exec_time_str:
              # self.add_log(f"  ⚠️ [RT_EXEC] ({stock_code}) 필수 값 누락: {values}") # 로그 너무 많을 수 있어 주석 처리
              return

          last_price = float(last_price_str.replace('+','').replace('-','').strip())
          exec_vol_signed = int(exec_vol_signed_str.strip())
          now = datetime.now()

          # --- 실시간 데이터 저장 (기존 로직) ---
          if stock_code not in self.realtime_data: self.realtime_data[stock_code] = {}
          self.realtime_data[stock_code].update({ 'last_price': last_price, 'timestamp': now })

          # --- 체결강도 계산 위한 누적량 업데이트 ---
          if stock_code not in self.cumulative_volumes:
              self.cumulative_volumes[stock_code] = {'buy_vol': 0, 'sell_vol': 0, 'timestamp': now}

          # 1분 지났으면 누적량 초기화 (최근 1분 체결강도)
          last_update = self.cumulative_volumes[stock_code]['timestamp']
          if (now - last_update).total_seconds() > 60:
              # self.add_log(f"  🔄 [{stock_code}] 체결강도 누적량 초기화 (1분 경과)") # 디버깅 시 주석 해제
              self.cumulative_volumes[stock_code] = {'buy_vol': 0, 'sell_vol': 0, 'timestamp': now}

          current_cumulative = self.cumulative_volumes[stock_code]

          if exec_vol_signed > 0: # 매수 체결
              current_cumulative['buy_vol'] += exec_vol_signed
          elif exec_vol_signed < 0: # 매도 체결
              current_cumulative['sell_vol'] += abs(exec_vol_signed)
          # 0은 무시

          current_cumulative['timestamp'] = now
          # self.add_log(f"  📊 [{stock_code}] 누적: 매수={current_cumulative['buy_vol']}, 매도={current_cumulative['sell_vol']}") # 디버깅 시 주석 해제


      except (ValueError, KeyError) as e:
          self.add_log(f"  🚨 [RT_EXEC] ({stock_code}) 데이터 처리 오류: {e}, Data: {values}")
      except Exception as e:
          self.add_log(f"  🚨 [RT_EXEC] ({stock_code}) 예상치 못한 오류: {e}")
          self.add_log(traceback.format_exc()) # 상세 오류 로깅 추가

  async def _process_realtime_orderbook(self, stock_code: str, values: Dict):
      """실시간 호가(0D) 처리"""
      try:
          ask1_str = values.get('41'); bid1_str = values.get('51'); ask1_vol_str = values.get('61'); bid1_vol_str = values.get('71')
          total_ask_vol_str = values.get('121'); total_bid_vol_str = values.get('125')
          if not ask1_str or not bid1_str: return
          ask1 = float(ask1_str.replace('+','').replace('-','').strip())
          bid1 = float(bid1_str.replace('+','').replace('-','').strip())
          ask1_vol = int(ask1_vol_str) if ask1_vol_str and ask1_vol_str.isdigit() else 0
          bid1_vol = int(bid1_vol_str) if bid1_vol_str and bid1_vol_str.isdigit() else 0
          total_ask_vol = int(total_ask_vol_str) if total_ask_vol_str and total_ask_vol_str.isdigit() else 0
          total_bid_vol = int(total_bid_vol_str) if total_bid_vol_str and total_bid_vol_str.isdigit() else 0
          if stock_code not in self.realtime_data: self.realtime_data[stock_code] = {}
          self.realtime_data[stock_code].update({ 'ask1': ask1, 'bid1': bid1, 'ask1_vol': ask1_vol, 'bid1_vol': bid1_vol, 'total_ask_vol': total_ask_vol, 'total_bid_vol': total_bid_vol, 'timestamp': datetime.now() })
      except (ValueError, KeyError): pass
      except Exception as e: self.add_log(f"  🚨 RT_OB ({stock_code}) 오류: {e}")

  async def _process_execution_update(self, exec_data: Dict):
      """실시간 주문 체결 응답(00) 처리"""
      try:
          order_no = exec_data.get('9203', '').strip(); stock_code_raw = exec_data.get('9001', '').strip()
          order_status = exec_data.get('913', '').strip()
          exec_qty = int(exec_data.get('911', '0')); exec_price = float(exec_data.get('910', '0.0'))
          unfilled_qty = int(exec_data.get('902', '0'))
          if not order_no or not stock_code_raw: return
          stock_code = stock_code_raw[1:] if stock_code_raw.startswith('A') else stock_code_raw; stock_code = stock_code.split('_')[0]

          position_info = None; target_code_for_pos = None
          for code, pos in self.positions.items():
              if pos.get('order_no') == order_no and pos.get('status') in ['PENDING_ENTRY', 'PENDING_EXIT']:
                  position_info = pos; target_code_for_pos = code; break
          if not position_info and stock_code in self.positions:
              pos = self.positions[stock_code]
              if pos.get('status') in ['IN_POSITION', 'ERROR_LIQUIDATION']: position_info = pos; target_code_for_pos = stock_code
          if not position_info or not target_code_for_pos: return

          current_pos_status = position_info.get('status')

          if current_pos_status == 'PENDING_ENTRY':
              if order_status == '체결' and exec_qty > 0:
                  filled_qty = position_info.get('filled_qty', 0) + exec_qty; filled_value = position_info.get('filled_value', 0) + (exec_qty * exec_price)
                  position_info['filled_qty'] = filled_qty; position_info['filled_value'] = filled_value
                  original_order_qty = position_info.get('original_order_qty', 0)
                  is_fully_filled = (unfilled_qty == 0 or filled_qty >= original_order_qty)
                  if is_fully_filled:
                      entry_price = filled_value / filled_qty if filled_qty > 0 else 0
                      position_info.update({'status': 'IN_POSITION', 'entry_price': entry_price, 'entry_time': datetime.now(), 'size': filled_qty, 'partial_profit_taken': False}) # ✅ partial_profit_taken
                      self.add_log(f"   ✅ [EXEC_UPDATE] 매수 완전 체결: [{target_code_for_pos}] @{entry_price:.2f}, {filled_qty}주")
                  else: self.add_log(f"   ⏳ [EXEC_UPDATE] 매수 부분 체결: [{target_code_for_pos}] 누적 {filled_qty}/{original_order_qty}")
              elif order_status in ['취소', '거부', '확인']:
                  if position_info.get('filled_qty', 0) == 0:
                       if target_code_for_pos in self.positions: del self.positions[target_code_for_pos]; await self._unsubscribe_realtime_stock(target_code_for_pos)
                  else:
                      entry_price = position_info['filled_value'] / position_info['filled_qty']
                      position_info.update({'status': 'IN_POSITION', 'entry_price': entry_price, 'entry_time': datetime.now(), 'size': position_info['filled_qty'], 'partial_profit_taken': False})
                      self.add_log(f"   ⚠️ [EXEC_UPDATE] 매수 부분 체결 후 주문 실패/취소: [{target_code_for_pos}] {position_info['filled_qty']}주 보유")

          elif current_pos_status == 'PENDING_EXIT':
               exit_signal_type = position_info.get('exit_signal'); exit_order_qty = position_info.get('exit_order_qty', 0); original_size = position_info.get('original_size_before_exit', 0)
               if order_status == '체결' and exec_qty > 0:
                  filled_qty = position_info.get('filled_qty', 0) + exec_qty; filled_value = position_info.get('filled_value', 0) + (exec_qty * exec_price)
                  position_info['filled_qty'] = filled_qty; position_info['filled_value'] = filled_value
                  is_fully_filled = (unfilled_qty == 0 or filled_qty >= exit_order_qty)
                  if is_fully_filled:
                      exit_price = filled_value / filled_qty if filled_qty > 0 else 0; entry_price = position_info.get('entry_price', 0)
                      profit = (exit_price - entry_price) * filled_qty if entry_price else 0
                      profit_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price and entry_price != 0 else 0
                      # ✅ 부분 익절 완료 처리
                      if exit_signal_type == "PARTIAL_TAKE_PROFIT":
                          remaining_size = original_size - filled_qty
                          if remaining_size > 0:
                              position_info.update({'status': 'IN_POSITION', 'size': remaining_size, 'partial_profit_taken': True, 'order_no': None, 'exit_signal': None, 'exit_order_qty': 0, 'filled_qty': 0, 'filled_value': 0.0}) # ✅ partial_profit_taken=True
                              self.add_log(f"   ✅ [EXEC_UPDATE] 부분 익절 완료: [{target_code_for_pos}] {filled_qty}주 매도. 남은 수량: {remaining_size}, P/L={profit:.2f}({profit_pct:.2f}%)")
                          else:
                              self.add_log(f"   ✅ [EXEC_UPDATE] 부분 익절 후 전량 매도 완료: [{target_code_for_pos}] {filled_qty}주, P/L={profit:.2f}({profit_pct:.2f}%)")
                              if target_code_for_pos in self.positions: del self.positions[target_code_for_pos]; await self._unsubscribe_realtime_stock(target_code_for_pos)
                      else: # ✅ 전체 청산 완료
                          self.add_log(f"   ✅ [EXEC_UPDATE] (전체) 청산 완료: [{target_code_for_pos}] @{exit_price:.2f}, {filled_qty}주, P/L={profit:.2f}({profit_pct:.2f}%), 사유={exit_signal_type}")
                          if target_code_for_pos in self.positions: del self.positions[target_code_for_pos]; await self._unsubscribe_realtime_stock(target_code_for_pos)
                  else: self.add_log(f"   ⏳ [EXEC_UPDATE] 매도 부분 체결: [{target_code_for_pos}] 누적 {filled_qty}/{exit_order_qty}")
               elif order_status in ['취소', '거부', '확인']:
                   remaining_size_after_cancel = original_size - position_info.get('filled_qty', 0)
                   self.add_log(f"   ⚠️ [EXEC_UPDATE] 매도 주문 취소/거부/확인 ({order_status}): [{target_code_for_pos}] 미체결={unfilled_qty}, 계산잔량={remaining_size_after_cancel}")
                   if remaining_size_after_cancel > 0:
                       position_info['status'] = 'IN_POSITION'; position_info['size'] = remaining_size_after_cancel
                       if exit_signal_type == "PARTIAL_TAKE_PROFIT": position_info['partial_profit_taken'] = False # ✅ False 유지
                       position_info.pop('order_no', None); position_info.pop('exit_signal', None)
                       self.add_log(f"     -> [{target_code_for_pos}] 상태 복구: IN_POSITION, 수량={remaining_size_after_cancel}")
                   else:
                       if target_code_for_pos in self.positions: self.positions.pop(target_code_for_pos, None); await self._unsubscribe_realtime_stock(target_code_for_pos)

      except Exception as e: self.add_log(f"🚨 EXEC_UPDATE 오류: {e}"); self.add_log(traceback.format_exc())

  async def _process_balance_update(self, stock_code: str, balance_data: Dict):
      """실시간 잔고(04) 처리"""
      try:
          account_no = balance_data.get('9201', ''); current_size_str = balance_data.get('930', ''); avg_price_str = balance_data.get('931', '')
          if not account_no: return
          current_size = int(current_size_str) if current_size_str else 0
          avg_price = float(avg_price_str) if avg_price_str else 0.0
          position_info = self.positions.get(stock_code); current_pos_status = position_info.get('status') if position_info else None

          if position_info and current_pos_status in ['IN_POSITION', 'PENDING_EXIT']:
              engine_size = position_info.get('size', 0)
              if engine_size != current_size: position_info['size'] = current_size
              if current_pos_status == 'PENDING_EXIT' and current_size == 0:
                  if stock_code in self.positions: del self.positions[stock_code]; await self._unsubscribe_realtime_stock(stock_code)
          elif not position_info and current_size > 0:
              self.positions[stock_code] = { 'stk_cd': stock_code, 'entry_price': avg_price, 'size': current_size, 'status': 'IN_POSITION', 'entry_time': datetime.now(), 'filled_qty': current_size, 'filled_value': avg_price * current_size, 'partial_profit_taken': False } # ✅ partial_profit_taken
              if stock_code not in self.subscribed_codes: await self._subscribe_realtime_stock(stock_code)
          elif position_info and current_pos_status != 'PENDING_ENTRY' and current_size == 0:
              if stock_code in self.positions: del self.positions[stock_code]; await self._unsubscribe_realtime_stock(stock_code)
      except (ValueError, KeyError): pass
      except Exception as e: self.add_log(f"🚨 BALANCE_UPDATE 오류: {e}"); self.add_log(traceback.format_exc())

  # --- 👇 구독 관리 함수들 ('1h' 추가) ---
  async def _update_realtime_subscriptions(self, target_codes: List[str]):
      """실시간 데이터 구독/해지 관리 (0B, 0D, 1h 포함)"""
      if not self.api: return
      current_subscribed = self.subscribed_codes; target_set = set(target_codes)
      codes_to_keep = target_set | set(self.positions.keys())
      to_subscribe = target_set - current_subscribed; to_unsubscribe = current_subscribed - codes_to_keep
      TR_TYPES_PER_STOCK = ['0B', '0D', '1h'] # ✅ '1h' 추가
      if to_subscribe:
          sub_list = list(to_subscribe)
          tr_ids = [t for _ in sub_list for t in TR_TYPES_PER_STOCK]; tr_keys = [c for c in sub_list for _ in TR_TYPES_PER_STOCK]
          self.add_log(f"  ➕ [WS_SUB] 구독 추가: {sub_list} (Types: {TR_TYPES_PER_STOCK})")
          await self.api.register_realtime(tr_ids=tr_ids, tr_keys=tr_keys); self.subscribed_codes.update(to_subscribe)
      if to_unsubscribe:
          unsub_list = list(to_unsubscribe)
          tr_ids = [t for _ in unsub_list for t in TR_TYPES_PER_STOCK]; tr_keys = [c for c in unsub_list for _ in TR_TYPES_PER_STOCK]
          self.add_log(f"  ➖ [WS_SUB] 구독 해지: {unsub_list} (Types: {TR_TYPES_PER_STOCK})")
          await self.api.unregister_realtime(tr_ids=tr_ids, tr_keys=tr_keys); self.subscribed_codes.difference_update(to_unsubscribe)
          for code in to_unsubscribe: self.vi_status.pop(code, None) # ✅ VI 상태 제거

  async def _unsubscribe_realtime_stock(self, stock_code: str):
      """특정 종목 구독 해지 (0B, 0D, 1h)"""
      if self.api and stock_code in self.subscribed_codes:
          TR_TYPES_PER_STOCK = ['0B', '0D', '1h'] # ✅ '1h' 추가
          tr_ids = TR_TYPES_PER_STOCK; tr_keys = [stock_code] * len(TR_TYPES_PER_STOCK)
          self.add_log(f"  ➖ [WS_SUB] 구독 해지: {stock_code} (Types: {TR_TYPES_PER_STOCK})")
          await self.api.unregister_realtime(tr_ids=tr_ids, tr_keys=tr_keys)
          self.subscribed_codes.discard(stock_code)
          self.vi_status.pop(stock_code, None) # ✅ VI 상태 제거

  async def _subscribe_realtime_stock(self, stock_code: str):
      """특정 종목 구독 추가 (0B, 0D, 1h)"""
      if self.api and stock_code not in self.subscribed_codes:
          TR_TYPES_PER_STOCK = ['0B', '0D', '1h'] # ✅ '1h' 추가
          tr_ids = TR_TYPES_PER_STOCK; tr_keys = [stock_code] * len(TR_TYPES_PER_STOCK)
          self.add_log(f"  ➕ [WS_SUB] 구독 추가 (복구): {stock_code} (Types: {TR_TYPES_PER_STOCK})")
          await self.api.register_realtime(tr_ids=tr_ids, tr_keys=tr_keys)
          self.subscribed_codes.add(stock_code)
  # --- 👆 구독 관리 함수들 수정 끝 ---

  async def run_screening(self) -> List[str]:
      """거래 대상 종목 스크리닝"""
      self.add_log("🔍 [SCREEN] 종목 스크리닝 시작...")
      if not self.api: self.add_log("⚠️ [SCREEN] API 객체 없음."); return []
      try:
          self.add_log("  -> [SCREEN] 거래량 급증 API 호출 시도...")
          params = {
              'mrkt_tp': '000', 'sort_tp': '2', 'tm_tp': '1',
              'tm': str(config.strategy.screening_surge_timeframe_minutes),
              'trde_qty_tp': str(config.strategy.screening_min_volume_threshold).zfill(5), # zfill(5)
              'stk_cnd': '14', # ETF 제외 (14)
              'pric_tp': '8', # 1000원 이상
              'stex_tp': '3'  # 통합
          }
          candidate_stocks_raw = await self.api.fetch_volume_surge_rank(**params)

          if not candidate_stocks_raw or candidate_stocks_raw.get('return_code') != 0:
              error_msg = candidate_stocks_raw.get('return_msg', 'API 응답 없음') if candidate_stocks_raw else 'API 호출 실패'
              self.add_log(f"⚠️ [SCREEN] 스크리닝 API 오류: {error_msg}")
              self.candidate_stock_codes = []; self.candidate_stocks_info = []
              return []

          surge_list = candidate_stocks_raw.get('trde_qty_sdnin', [])
          self.add_log(f"  <- [SCREEN] API 응답 수신 (결과 수: {len(surge_list)})")
          if not surge_list: self.add_log("⚠️ [SCREEN] 스크리닝 결과 없음."); return []

          candidate_stocks_intermediate = []
          for s in surge_list:
              stk_cd_raw = s.get('stk_cd'); stk_nm = s.get('stk_nm'); cur_prc_str = s.get('cur_prc'); sdnin_rt_str = s.get('sdnin_rt')
              if not stk_cd_raw or not stk_nm or not cur_prc_str or not sdnin_rt_str: continue
              stk_cd = stk_cd_raw.strip().split('_')[0] # _NX, _AL 제거
              try:
                  cur_prc = float(cur_prc_str.replace('+','').replace('-','').strip())
                  sdnin_rt = float(sdnin_rt_str.strip())
                  if cur_prc >= config.strategy.screening_min_price and sdnin_rt >= config.strategy.screening_min_surge_rate:
                      candidate_stocks_intermediate.append({'stk_cd': stk_cd, 'stk_nm': stk_nm, 'sdnin_rt': sdnin_rt})
              except ValueError: continue
          candidate_stocks_intermediate.sort(key=lambda x: x['sdnin_rt'], reverse=True)
          self.candidate_stocks_info = candidate_stocks_intermediate[:config.strategy.max_target_stocks]
          self.candidate_stock_codes = [s['stk_cd'] for s in self.candidate_stocks_info]

          target_stocks_display = [f"{s['stk_cd']}({s['stk_nm']})" for s in self.candidate_stocks_info] # 1. 리스트를 먼저 생성
          if target_stocks_display: self.add_log(f"🎯 [SCREEN] 스크리닝 완료. 후보: {target_stocks_display}") # 2. f-string에 변수 전달
          else: self.add_log("ℹ️ [SCREEN] 최종 후보 종목 없음.")
          return self.candidate_stock_codes
      
      except Exception as e: self.add_log(f"🚨 [CRITICAL] 스크리닝 오류: {e}"); self.add_log(traceback.format_exc()); return []

  async def process_all_stocks_tick(self, current_time: datetime):
      """모든 대상 종목 Tick 처리"""
      codes_to_process = set(self.candidate_stock_codes) | set(self.positions.keys())
      if not codes_to_process: return
      tick_interval_seconds = config.strategy.tick_interval_seconds
      tasks = []
      for stock_code in list(codes_to_process):
          last_tick = self.last_stock_tick_time.get(stock_code)
          if last_tick and (current_time - last_tick).total_seconds() < tick_interval_seconds: continue
          if not self.api: break
          tasks.append(asyncio.create_task(self.process_single_stock_tick(stock_code)))
          self.last_stock_tick_time[stock_code] = current_time
          await asyncio.sleep(0.1) # API 호출 간격
      if tasks: await asyncio.gather(*tasks)

  async def start(self):
      """엔진 시작"""
      async with self._start_lock:
          if self.engine_status in ['INITIALIZING', 'RUNNING']: return
          self.engine_status = 'INITIALIZING'; self._realtime_registered = False; self._stop_event.clear()
          self.add_log("🚀 엔진 시작...");
          if self.api: await self.api.close();
          self.api = KiwoomAPI() # ✅ self.api에 할당

      try:
          self.add_log("  -> [START] 웹소켓 연결 시도...")
          connected = await self.api.connect_websocket(self.handle_realtime_data)
          if not connected: raise ConnectionError("웹소켓 연결 실패")

          self.add_log("⏳ [START] 기본 TR 등록 응답 대기...")
          await asyncio.wait_for(self._wait_for_registration(), timeout=10.0)
          self.add_log("✅ [START] 기본 TR 등록 확인 완료.")

          self.engine_status = 'RUNNING'; self.add_log("✅ 메인 루프 시작.")
          self.candidate_stock_codes = []; self.candidate_stocks_info = []

          while not self._stop_event.is_set():
              if self.engine_status != 'RUNNING': break
              current_time = datetime.now()
              screening_interval = config.strategy.screening_interval_minutes * 60
              should_screen = (current_time - self.last_screening_time).total_seconds() >= screening_interval
              max_positions = config.strategy.max_concurrent_positions
              if len(self.positions) < max_positions and should_screen:
                  new_candidates = await self.run_screening(); self.last_screening_time = current_time
                  await self._update_realtime_subscriptions(new_candidates)
              await self.process_all_stocks_tick(current_time)
              await asyncio.sleep(1)
          self.add_log("✅ 메인 루프 정상 종료.")
      except (asyncio.CancelledError, ConnectionError, asyncio.TimeoutError) as ce: self.add_log(f"ℹ️ 엔진 루프 중단: {ce}"); self.engine_status = 'ERROR'
      except Exception as e: self.add_log(f"🚨 [CRITICAL] 엔진 메인 루프 예외: {e} 🚨"); self.add_log(traceback.format_exc()); self.engine_status = 'ERROR'
      finally: 
        self.add_log("🚪 [FINALLY] 엔진 종료 처리 시작..."); 
        async with self._start_lock: await self.shutdown()

  async def _wait_for_registration(self):
      """기본 TR 등록 완료 대기"""
      while not self._realtime_registered and self.engine_status != 'ERROR': await asyncio.sleep(0.1)
      if not self._realtime_registered and self.engine_status != 'ERROR': raise asyncio.TimeoutError("Registration flag timeout")
      elif self.engine_status == 'ERROR': self.add_log("   -> _wait: 엔진 에러 감지.")
      elif self._realtime_registered: self.add_log("   -> _wait: 등록 플래그 True 확인.")

  async def stop(self):
      """엔진 종료 신호"""
      if self.engine_status not in ['STOPPING', 'STOPPED', 'KILLED']: self.add_log("🛑 엔진 종료 신호 수신..."); self._stop_event.set()

  async def shutdown(self):
      """엔진 자원 정리"""
      if self.engine_status in ['STOPPED', 'KILLED']: return
      if self.engine_status != 'STOPPING': self.add_log("🛑 엔진 종료(Shutdown) 절차 시작..."); self.engine_status = 'STOPPING'
      self._stop_event.set()

      if self.api and self.subscribed_codes:
          codes_to_unregister = list(self.subscribed_codes)
          if codes_to_unregister:
              TR_TYPES_PER_STOCK = ['0B', '0D', '1h'] # ✅ '1h' 포함
              tr_ids = [t for _ in codes_to_unregister for t in TR_TYPES_PER_STOCK]
              tr_keys = [c for c in codes_to_unregister for _ in TR_TYPES_PER_STOCK]
              self.add_log(f"  -> [SHUTDOWN] 실시간 구독 해지: {codes_to_unregister}")
              await self.api.unregister_realtime(tr_ids=tr_ids, tr_keys=tr_keys)
              self.subscribed_codes.clear(); self.vi_status.clear() # ✅ VI 상태 클리어

      if self.api: await self.api.close(); self.api = None
      self._realtime_registered = False; self.engine_status = 'STOPPED'; self.add_log("🛑 엔진 종료 완료.")

  async def execute_kill_switch(self):
      """긴급 정지"""
      if self.engine_status != 'RUNNING': return
      if not self.api: return
      self.add_log("🚨 KILL Switch 발동!"); self.engine_status = 'KILLED'; self._stop_event.set()
      try:
          self.add_log("  -> [KILL] 미체결 주문 조회 및 취소 시도...")
          # pending_orders = await self.api.fetch_pending_orders() # ka10075 구현 필요
          pending_orders = [] # 임시
          if pending_orders: pass
          else: self.add_log("  - [KILL] 취소할 미체결 주문 없음.")

          positions_to_liquidate = list(self.positions.items())
          if positions_to_liquidate:
              self.add_log(f"  -> [KILL] {len(positions_to_liquidate)} 건 보유 포지션 시장가 청산 시도...")
              for stock_code, pos_info in positions_to_liquidate:
                  if pos_info.get('status') == 'IN_POSITION' and pos_info.get('size', 0) > 0:
                      quantity = pos_info['size']
                      result = await self.api.create_sell_order(stock_code, quantity)
                      if result and result.get('return_code') == 0:
                          if stock_code in self.positions: self.positions[stock_code].update({'status': 'PENDING_EXIT', 'exit_signal': 'KILL_SWITCH', 'order_no': result.get('ord_no'), 'original_size_before_exit': quantity, 'filled_qty': 0, 'filled_value': 0.0 })
                          self.add_log(f"     ✅ [KILL] 시장가 청산 주문 접수 ({stock_code} {quantity}주)")
                      else:
                          error_info = result.get('return_msg', '주문 실패') if result else 'API 호출 실패'
                          self.add_log(f"     ❌ [KILL] 시장가 청산 주문 실패 ({stock_code} {quantity}주): {error_info}")
                          if stock_code in self.positions: self.positions[stock_code]['status'] = 'ERROR_LIQUIDATION'
                  elif pos_info.get('status') in ['PENDING_ENTRY', 'PENDING_EXIT']: self.add_log(f"     ℹ️ [KILL] 주문 진행 중 포지션({stock_code})은 미체결 취소로 처리됨.")
              self.add_log("  <- [KILL] 시장가 청산 주문 접수 완료.")
          else: self.add_log("  - [KILL] 청산할 보유 포지션 없음.")
          self.add_log("🚨 Kill Switch 처리 완료. 엔진 종료 대기...")
      except Exception as e: self.add_log(f"🚨 [CRITICAL] Kill Switch 오류: {e}"); self.add_log(traceback.format_exc()); self.engine_status = 'ERROR'
      finally: await self.stop()