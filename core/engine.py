import asyncio
import pandas as pd
from datetime import datetime, timedelta # timedelta import 확인
from typing import Dict, List, Optional
import json # json import 추가

# 설정 로더 import
from config.loader import config
# API 게이트웨이 import
from gateway.kiwoom_api import KiwoomAPI
# 데이터 처리 및 지표 계산 import
from data.manager import preprocess_chart_data
from data.indicators import add_vwap, calculate_orb
# 전략 관련 import
from strategy.momentum_orb import check_breakout_signal
from strategy.risk_manager import manage_position
from strategy.screener import find_momentum_stocks

class TradingEngine:
  """웹소켓 기반 실시간 트레이딩 로직 관장 엔진"""
  def __init__(self, config_obj):
    self.config = config_obj
    self.target_stock: Optional[str] = None
    self.target_stock_name: Optional[str] = None
    # status: INITIALIZING, SEARCHING, NO_TARGET, PENDING_ENTRY, IN_POSITION, PENDING_EXIT, KILLED, ERROR
    self.position: Dict[str, any] = {'status': 'INITIALIZING'}
    self.logs: List[str] = []
    self.api: Optional[KiwoomAPI] = None
    self._stop_event = asyncio.Event()
    self.last_tick_time = datetime.now() - timedelta(seconds=61) # 첫 틱 즉시 실행

  def add_log(self, message: str):
    log_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(log_msg)
    self.logs.insert(0, log_msg)
    if len(self.logs) > 100: self.logs.pop()

  async def start(self):
    """엔진 시작 및 메인 루프 실행"""
    self.api = KiwoomAPI()
    self.position['status'] = 'INITIALIZING'
    self.add_log("🚀 엔진 시작...")
    try:
      connected = await self.api.connect_websocket(self.handle_realtime_data)
      if not connected:
        self.add_log("❌ 웹소켓 연결 실패. 엔진 시작 불가."); self.position['status'] = 'ERROR'; return

      await self.initialize_session(self.api)

      while not self._stop_event.is_set():
        current_time = datetime.now()
        current_status = self.position.get('status')

        # 주기적 작업 (틱 처리)
        if current_status not in ['INITIALIZING', 'ERROR', 'KILLED', 'NO_TARGET'] and \
           (current_time - self.last_tick_time).total_seconds() >= 60: # 60초 간격
            await self.process_tick()
            self.last_tick_time = current_time

        # 대상 없음 상태에서 재시도
        elif current_status == 'NO_TARGET' and \
             (current_time - self.last_tick_time).total_seconds() >= 300: # 5분 간격
             self.add_log("ℹ️ 대상 종목 없음. 스크리닝 재시도...")
             await self.initialize_session(self.api)
             self.last_tick_time = current_time

        await asyncio.sleep(1) # 루프 지연

    except asyncio.CancelledError: self.add_log("ℹ️ 엔진 메인 루프 취소됨.")
    except Exception as e: self.add_log(f"🚨 엔진 메인 루프 예외: {e}"); self.position['status'] = 'ERROR'
    finally:
      self.add_log("🛑 엔진 종료 절차 시작...")
      if self.api: await self.api.close()
      self.add_log("🛑 엔진 종료 완료.")

  async def stop(self):
    self.add_log("🛑 엔진 종료 신호 수신..."); self._stop_event.set()

  # --- 실시간 데이터 처리 콜백 ---
  def handle_realtime_data(self, data: Dict):
    try:
        header = data.get('header', {})
        body_str = data.get('body')
        if not header or not body_str: return # PONG 등 무시

        try: body = json.loads(body_str)
        except: print(f"⚠️ 실시간 body 파싱 실패: {body_str}"); return

        tr_id = header.get('tr_id')
        if tr_id == '00': asyncio.create_task(self._process_execution_update(body))
        elif tr_id == '04': asyncio.create_task(self._process_balance_update(body))

    except Exception as e: print(f"🚨 실시간 데이터 처리 오류(콜백): {e} | 데이터: {data}")

  # --- 실시간 데이터 처리 상세 ---
  async def _process_execution_update(self, exec_data: Dict):
    try:
        # 키움 API 문서(p.416) 기준 필드명 사용
        order_no = exec_data.get('9203')
        exec_qty_str = exec_data.get('911'); exec_qty = int(exec_qty_str) if exec_qty_str else 0
        exec_price_str = exec_data.get('910'); exec_price = 0.0
        order_status = exec_data.get('913')
        unfilled_qty_str = exec_data.get('902'); unfilled_qty = int(unfilled_qty_str) if unfilled_qty_str else 0
        stock_code_raw = exec_data.get('9001')
        order_type = exec_data.get('905') # +매수, -매도

        if not all([order_no, order_status, stock_code_raw]): return
        stock_code = stock_code_raw[1:] if stock_code_raw.startswith('A') else stock_code_raw
        if exec_price_str:
            try: exec_price = float(exec_price_str.replace('+', '').replace('-', ''))
            except ValueError: self.add_log(f"⚠️ 체결가 오류: {exec_price_str}"); return

        current_order_no = self.position.get('order_no')
        if not current_order_no or current_order_no != order_no: return # 관심 주문 아님

        self.add_log(f"⚡️ 주문({order_no}) 상태={order_status}, 종목={stock_code}, 체결량={exec_qty}, 체결가={exec_price}, 미체결량={unfilled_qty}")

        # PENDING_ENTRY: 매수 체결 처리
        if self.position.get('status') == 'PENDING_ENTRY':
            if order_status == '체결':
                filled_qty = self.position.get('filled_qty', 0) + exec_qty
                filled_value = self.position.get('filled_value', 0) + (exec_qty * exec_price)
                self.position['filled_qty'] = filled_qty
                self.position['filled_value'] = filled_value

                if unfilled_qty == 0: # 완전 체결
                    entry_price = filled_value / filled_qty if filled_qty > 0 else 0
                    self.position.update({
                        'status': 'IN_POSITION', 'entry_price': entry_price, 'size': filled_qty,
                        'entry_time': datetime.now(), 'order_no': None, 'order_qty': None,
                        'filled_qty': None, 'filled_value': None })
                    self.add_log(f"✅ (WS) 매수 완전 체결: 진입가={entry_price:.2f}, 수량={filled_qty}")
                else: self.add_log(f"⏳ (WS) 매수 부분 체결: 누적 {filled_qty}/{self.position.get('order_qty')}")

            elif order_status in ['취소', '거부', '확인']:
                filled_qty = self.position.get('filled_qty', 0)
                if filled_qty > 0: # 부분 체결 후 종료
                     entry_price = self.position.get('filled_value', 0) / filled_qty
                     self.add_log(f"⚠️ (WS) 매수 주문({order_no}) 부분 체결({filled_qty}) 후 종료({order_status}).")
                     self.position.update({ 'status': 'IN_POSITION', 'entry_price': entry_price, 'size': filled_qty,
                                            'entry_time': datetime.now(), 'order_no': None, 'order_qty': None,
                                            'filled_qty': None, 'filled_value': None })
                else: # 완전 미체결 후 종료
                    self.add_log(f"❌ (WS) 매수 주문({order_no}) 실패: {order_status}. SEARCHING 복귀.")
                    self.position = {'status': 'SEARCHING', 'stk_cd': None}; self.target_stock = None

        # PENDING_EXIT: 매도 체결 처리
        elif self.position.get('status') == 'PENDING_EXIT':
             if order_status == '체결':
                filled_qty = self.position.get('filled_qty', 0) + exec_qty
                filled_value = self.position.get('filled_value', 0) + (exec_qty * exec_price)
                self.position['filled_qty'] = filled_qty
                self.position['filled_value'] = filled_value

                if unfilled_qty == 0: # 완전 체결 (청산 완료)
                    exit_price = filled_value / filled_qty if filled_qty > 0 else 0
                    entry_price = self.position.get('entry_price', 0)
                    profit = (exit_price - entry_price) * filled_qty if entry_price else 0
                    profit_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price and entry_price != 0 else 0

                    self.add_log(f"✅ (WS) 매도 완전 체결 (청산): 청산가={exit_price:.2f}, 수량={filled_qty}, 실현손익={profit:.2f} ({profit_pct:.2f}%), 사유={self.position.get('exit_signal')}")
                    self.position = {'status': 'SEARCHING', 'stk_cd': None}; self.target_stock = None
                else: self.add_log(f"⏳ (WS) 매도 부분 체결: 누적 {filled_qty}/{self.position.get('order_qty')}")

             elif order_status in ['취소', '거부', '확인']:
                 filled_qty = self.position.get('filled_qty', 0)
                 remaining_size = self.position.get('size', 0) - filled_qty
                 if remaining_size > 0 :
                      self.add_log(f"⚠️ (WS) 매도 주문({order_no}) 부분 체결({filled_qty}) 후 종료({order_status}). {remaining_size}주 잔여.")
                      self.position.update({'size': remaining_size, 'status': 'IN_POSITION', 'order_no': None,
                                           'order_qty': None, 'filled_qty': None, 'filled_value': None})
                 else: # 전량 미체결 또는 전량 체결 후 종료 상태 수신
                     if filled_qty == self.position.get('size', 0): # 전량 체결 후 종료 상태 ('확인' 등)
                         self.add_log(f"ℹ️ (WS) 매도 주문({order_no}) 전량 체결 후 최종 상태({order_status}) 수신. SEARCHING 전환.")
                         self.position = {'status': 'SEARCHING', 'stk_cd': None}; self.target_stock = None
                     else: # 전량 미체결 상태에서 종료
                         self.add_log(f"❌ (WS) 매도 주문({order_no}) 실패: {order_status}. IN_POSITION 복귀.")
                         self.position.update({'status': 'IN_POSITION', 'order_no': None, 'order_qty': None,
                                               'filled_qty': None, 'filled_value': None})

    except Exception as e:
        self.add_log(f"🚨 주문 체결 처리 오류(_process_execution_update): {e} | Data: {exec_data}")


  async def _process_balance_update(self, balance_data: Dict):
      """실시간 잔고(TR ID: 04) 데이터 처리."""
      try:
          # 키움 API 문서(p.420) 기준 필드명 사용
          stock_code_raw = balance_data.get('9001')
          current_size_str = balance_data.get('930')
          avg_price_str = balance_data.get('931')

          if not stock_code_raw or current_size_str is None or avg_price_str is None: return

          stock_code = stock_code_raw[1:] if stock_code_raw.startswith('A') else stock_code_raw
          current_size = int(current_size_str)
          avg_price = float(avg_price_str)

          # 현재 관리 중인 포지션과 관련된 업데이트인지 확인
          if self.position.get('stk_cd') == stock_code and self.position.get('status') in ['IN_POSITION', 'PENDING_EXIT']:
              pos_size = self.position.get('size')
              # 수량 변경 시 로그 및 업데이트
              if pos_size is not None and pos_size != current_size:
                  self.add_log(f"🔄 (WS) 잔고 수량 변경 감지: {stock_code}, {pos_size} -> {current_size}")
                  self.position['size'] = current_size
                  # 평단은 잔고 업데이트 시 변경하지 않음 (체결 시점 기준 유지)

              # IN_POSITION 상태에서 잔고가 0이 되면 청산된 것으로 간주
              if current_size == 0 and self.position.get('status') == 'IN_POSITION':
                  self.add_log(f"ℹ️ (WS) 잔고 0 확인 ({stock_code}). SEARCHING 상태 전환.")
                  self.position = {'status': 'SEARCHING', 'stk_cd': None}; self.target_stock = None

          # 상태 불일치 감지 (SEARCHING인데 잔고 발견)
          elif self.position.get('status') == 'SEARCHING' and stock_code == self.target_stock and current_size > 0:
               self.add_log(f"⚠️ 엔진 상태(SEARCHING)와 불일치하는 잔고({stock_code}, {current_size}주) 발견. 상태 강제 업데이트.")
               self.position.update({'status': 'IN_POSITION', 'stk_cd': stock_code, 'size': current_size,
                                     'entry_price': avg_price, 'entry_time': datetime.now() })

      except Exception as e:
          self.add_log(f"🚨 잔고 처리 오류(_process_balance_update): {e} | Data: {balance_data}")


  # --- 종목 선정 ---
  async def initialize_session(self, api: KiwoomAPI):
    """장 시작 또는 재시작 시 거래 대상 종목 선정"""
    self.add_log("🚀 세션 초기화 및 종목 스크리닝...")
    try:
      selected_stock_code = await find_momentum_stocks(api)
      if selected_stock_code:
        self.target_stock = selected_stock_code
        stock_info = await api.fetch_stock_info(self.target_stock)
        self.target_stock_name = stock_info['stk_nm'] if stock_info and 'stk_nm' in stock_info else None
        log_name = f"({self.target_stock_name})" if self.target_stock_name else ""
        self.add_log(f"✅ 대상 종목 선정: {self.target_stock}{log_name}")
        self.position['status'] = 'SEARCHING'
      else:
        self.target_stock = None; self.target_stock_name = None
        self.position['status'] = 'NO_TARGET'; self.add_log("⚠️ 거래 대상 종목 없음.")
    except Exception as e:
      self.add_log(f"🚨 스크리닝 오류: {e}")
      self.target_stock = None; self.target_stock_name = None; self.position['status'] = 'ERROR'

  # --- 주기적 작업 (진입/청산 조건 확인) ---
  async def process_tick(self):
    current_status = self.position.get('status')
    if current_status not in ['SEARCHING', 'IN_POSITION']: return
    effective_stk_cd = self.position.get('stk_cd') if current_status == 'IN_POSITION' else self.target_stock
    if not effective_stk_cd or not self.api : return

    try: # API 호출 오류 대비
        raw_data = await self.api.fetch_minute_chart(effective_stk_cd, timeframe=1)
        if not (raw_data and raw_data.get("stk_min_pole_chart_qry")): return # 데이터 없으면 조용히 종료

        df = preprocess_chart_data(raw_data["stk_min_pole_chart_qry"])
        if df is None or df.empty: return

        add_vwap(df)
        orb_levels = calculate_orb(df, timeframe=self.config.strategy.orb_timeframe)
        current_price = df['close'].iloc[-1]
        current_vwap = df['VWAP'].iloc[-1] if 'VWAP' in df.columns and not pd.isna(df['VWAP'].iloc[-1]) else None

        # --- 상태별 로직 ---
        if current_status == 'SEARCHING':
            signal = check_breakout_signal(current_price, orb_levels, self.config.strategy.breakout_buffer)
            if signal == "BUY":
                self.add_log(f"🔥 [{effective_stk_cd}] 매수 신호! (현재가 {current_price})")
                order_quantity = 1 # TODO: 자금 관리
                if order_quantity > 0:
                    order_result = await self.api.create_buy_order(effective_stk_cd, quantity=order_quantity)
                    if order_result and order_result.get('return_code') == 0:
                        self.position.update({
                            'status': 'PENDING_ENTRY', 'order_no': order_result.get('ord_no'),
                            'order_qty': order_quantity, 'order_price': current_price, 'stk_cd': effective_stk_cd,
                            'filled_qty': 0, 'filled_value': 0.0 }) # 부분 체결 추적 필드 추가
                        self.add_log(f"➡️ [{effective_stk_cd}] 매수 주문 접수: {order_result.get('ord_no')}")

        elif current_status == 'IN_POSITION':
            signal = manage_position(self.position, current_price, current_vwap)
            if signal:
                log_prefix = "💰" if signal == "TAKE_PROFIT" else "🛑"
                self.add_log(f"{log_prefix} 청산 신호({signal})! [{effective_stk_cd}] 매도 주문 실행.")
                order_quantity = self.position.get('size', 0)
                if order_quantity > 0:
                    order_result = await self.api.create_sell_order(effective_stk_cd, quantity=order_quantity)
                    if order_result and order_result.get('return_code') == 0:
                        self.position.update({
                            'status': 'PENDING_EXIT', 'order_no': order_result.get('ord_no'),
                            'order_qty': order_quantity, 'exit_signal': signal,
                            'filled_qty': 0, 'filled_value': 0.0 }) # 부분 체결 추적 필드 추가
                        self.add_log(f"⬅️ [{effective_stk_cd}] 매도 주문 접수: {order_result.get('ord_no')}")
    except Exception as e:
        self.add_log(f"🚨 Tick 처리 중 오류 발생 ({effective_stk_cd}): {e}")


  # --- Kill Switch ---
  async def execute_kill_switch(self):
    self.add_log("🚨 KILL SWITCH 발동!")
    if not self.api: self.add_log("⚠️ API 객체 없음."); return
    try:
        original_status = self.position.get('status')
        order_no = self.position.get('order_no')
        stk_cd = self.position.get('stk_cd')
        order_qty = self.position.get('order_qty', 0)
        position_size = self.position.get('size', 0)

        # 미체결 주문 취소
        if original_status in ['PENDING_ENTRY', 'PENDING_EXIT'] and order_no and stk_cd:
            self.add_log(f"  - 미체결 주문({order_no}) 취소 시도...")
            cancel_result = await self.api.cancel_order(order_no, stk_cd, 0) # 수량 0: 잔량 전체
            if cancel_result and cancel_result.get('return_code') == 0: self.add_log(f"  ✅ 주문({order_no}) 취소 성공.")
            else: self.add_log(f"  ⚠️ 주문({order_no}) 취소 실패/불필요.")

        await asyncio.sleep(1.5) # 취소 처리 및 잔고 반영 대기

        # 최신 잔고 기준 확인 (잔고 업데이트 콜백이 처리했을 수 있음)
        stk_cd_to_liquidate = self.position.get('stk_cd')
        qty_to_liquidate = self.position.get('size', 0)

        # 보유 포지션 청산
        if stk_cd_to_liquidate and qty_to_liquidate > 0:
             self.add_log(f"  - 보유 포지션({stk_cd_to_liquidate}, {qty_to_liquidate}주) 시장가 청산 시도...")
             sell_result = await self.api.create_sell_order(stk_cd_to_liquidate, quantity=qty_to_liquidate)
             if sell_result and sell_result.get('return_code') == 0:
                 self.add_log(f"  ✅ 시장가 청산 주문 접수 완료: {sell_result.get('ord_no')}")
             else:
                 self.add_log(f"  ❌ 시장가 청산 주문 실패: {sell_result}"); self.position['status'] = 'ERROR'; return
        else: self.add_log("  - 청산할 보유 포지션 없음.")

        self.add_log("  - 엔진 정지(KILLED) 및 루프 종료.")
        self.position = {'status': 'KILLED'}; self.target_stock = None
        await self.stop()
    except Exception as e: self.add_log(f"🚨 Kill Switch 오류: {e}"); self.position['status'] = 'ERROR'
    finally: self.add_log("🚨 Kill Switch 처리 완료.")