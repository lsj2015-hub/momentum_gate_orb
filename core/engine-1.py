# engine.py
import asyncio
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import json
import traceback # 상세 오류 로깅을 위해 추가
import httpx
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

class TradingEngine:
  """웹소켓 기반 실시간 다중 종목 트레이딩 로직 관장 엔진"""
  def __init__(self):
    self.config = config
    self.positions: Dict[str, Dict] = {}
    self.logs: List[str] = []
    self.api: Optional[KiwoomAPI] = None
    self._stop_event = asyncio.Event()
    screening_interval_minutes = getattr(self.config.strategy, 'screening_interval_minutes', 5)
    self.last_screening_time = datetime.now() - timedelta(minutes=screening_interval_minutes + 1)
    self.candidate_stock_codes: List[str] = [] # 종목 코드 리스트 (기존 로직 호환용)
    self.candidate_stocks_info: List[Dict[str, str]] = [] # {'stk_cd': '000000', 'stk_nm': '종목명'} 형식 리스트 추가
    self.engine_status = 'STOPPED'
    self.last_stock_tick_time: Dict[str, datetime] = {}
    self._realtime_registered = False
    # --- 시작 로직 동시성 제어를 위한 Lock 추가 ---
    self._start_lock = asyncio.Lock()

  def add_log(self, message: str):
    """로그 메시지를 리스트에 추가하고 터미널에도 출력"""
    log_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(log_msg)
    self.logs.insert(0, log_msg)
    if len(self.logs) > 100: self.logs.pop()

  async def start(self):
    """엔진 시작: API 인스턴스 생성, 웹소켓 연결, 메인 루프 실행"""
    # --- Lock을 사용하여 start 로직 진입 제어 ---
    async with self._start_lock:
      # --- Lock 획득 후 상태 재확인 ---
      if self.engine_status in ['INITIALIZING', 'RUNNING']:
          self.add_log(f"⚠️ [START] 엔진이 이미 '{self.engine_status}' 상태입니다. 추가 시작 요청 무시 (Lock 확인).")
          return

      # --- 상태 변경 및 초기화 ---
      self.engine_status = 'INITIALIZING'
      self._realtime_registered = False
      self._stop_event.clear() # 종료 이벤트 초기화
      self.add_log("🚀 엔진 시작 (Lock 획득)...")

      # --- 기존 API 인스턴스 정리 및 새 인스턴스 생성 ---
      if self.api:
          await self.api.close()
      self.api = KiwoomAPI()

    # --- Lock 외부에서 API 연결 및 TR 등록 시도 (Lock을 길게 잡지 않음) ---
    try:
      self.add_log("  -> [START] 웹소켓 연결 시도...")
      # connect_websocket 내부에서 LOGIN 및 '00', '04' TR 등록 시도
      connected = await self.api.connect_websocket(self.handle_realtime_data)
      if not connected:
        self.add_log("❌ [START] 웹소켓 연결 실패. 엔진 시작 불가.")
        self.engine_status = 'ERROR'
        await self.shutdown(); return

      # --- TR 등록 성공 응답 대기 (이 부분은 유지) ---
      try:
          self.add_log("⏳ [START] 실시간 TR 등록 응답 대기 중...") # 대기 시작 로그 추가
          await asyncio.wait_for(self._wait_for_registration(), timeout=10.0)
          self.add_log("✅ [START] 실시간 TR 등록 응답 수신 확인 완료.") # 성공 로그 추가
      except asyncio.TimeoutError:
          self.add_log("🚨 [START] 실시간 TR 등록 응답 시간 초과. 엔진 시작 실패.")
          await self.api.disconnect_websocket()
          self.engine_status = 'ERROR'
          await self.shutdown(); return
      # --- TR 등록 대기 끝 ---

      # --- 메인 루프 시작 ---
      self.engine_status = 'RUNNING' # TR 등록 성공 확인 후 RUNNING 상태로 변경
      self.add_log("✅ 메인 루프 시작.")
      self.candidate_stock_codes: List[str] = []

      while not self._stop_event.is_set():
        # --- 엔진 상태 확인 (중간에 ERROR로 변경될 수 있음) ---
        if self.engine_status != 'RUNNING':
            self.add_log(f"⚠️ [LOOP] 엔진 상태가 RUNNING이 아님({self.engine_status}). 메인 루프 중단.")
            break

        current_time = datetime.now()

        # --- 스크리닝 로직 ---
        screening_interval_minutes = getattr(self.config.strategy, 'screening_interval_minutes', 5)
        screening_interval = screening_interval_minutes * 60
        should_screen = (current_time - self.last_screening_time).total_seconds() >= screening_interval
        max_positions = getattr(self.config.strategy, 'max_concurrent_positions', 5)

        if len(self.positions) < max_positions and should_screen:
            self.add_log("  -> [LOOP] 스크리닝 실행 시작...")
            self.candidate_stock_codes = await self.run_screening()
            self.last_screening_time = current_time
            self.add_log("  <- [LOOP] 스크리닝 실행 완료.")

        # --- Tick 처리 로직 ---
        await self.process_all_stocks_tick(current_time)

        await asyncio.sleep(1)

      self.add_log("✅ 메인 루프 정상 종료됨.") # 종료 사유 명확화

    except asyncio.CancelledError:
      self.add_log("ℹ️ 엔진 메인 루프 강제 취소됨.")
      self.engine_status = 'STOPPED' # 상태 명시적 변경
    except Exception as e:
      self.add_log(f"🚨🚨🚨 [CRITICAL] 엔진 메인 루프에서 처리되지 않은 심각한 예외 발생: {e} 🚨🚨🚨")
      self.add_log(traceback.format_exc())
      self.engine_status = 'ERROR' # 오류 발생 시 상태 변경
    finally:
      self.add_log("🚪 [FINALLY] 엔진 종료 처리 시작...")
      # --- 종료 시에도 Lock을 사용하여 shutdown 중복 방지 ---
      async with self._start_lock:
          await self.shutdown()

  async def _wait_for_registration(self):
      """_realtime_registered 플래그가 True가 되거나 엔진 상태가 ERROR가 될 때까지 기다립니다."""
      while not self._realtime_registered and self.engine_status != 'ERROR': # 에러 상태에서도 대기 중단
          await asyncio.sleep(0.1)
      # --- 👇 등록 실패 시 명확한 예외 발생 ---
      if not self._realtime_registered and self.engine_status != 'ERROR':
          self.add_log("   -> _wait_for_registration: 등록 플래그 False이고 에러 상태 아님 -> TimeoutError 발생시킴") # 디버깅 로그
          raise asyncio.TimeoutError("Registration flag was not set by callback")
      elif self.engine_status == 'ERROR':
          self.add_log("   -> _wait_for_registration: 엔진 에러 상태 감지 -> 대기 중단") # 디버깅 로그
          # 에러 상태에서는 TimeoutError를 발생시키지 않고 그냥 종료되도록 함 (start 함수에서 처리)
      elif self._realtime_registered:
          self.add_log("   -> _wait_for_registration: 등록 플래그 True 확인 -> 대기 종료") # 디버깅 로그
      # --- 👆 예외 발생 로직 수정 끝 👆 ---

  async def stop(self):
    """엔진 종료 신호 설정"""
    if self.engine_status not in ['STOPPING', 'STOPPED', 'KILLED']:
        self.add_log("🛑 엔진 종료 신호 수신...")
        self._stop_event.set() # 메인 루프 종료 요청

  async def shutdown(self):
      """엔진 관련 자원 정리 (웹소켓 해제, API 클라이언트 종료)"""
      # --- 이미 종료되었거나 종료 중이면 실행 안 함 ---
      if self.engine_status in ['STOPPED', 'KILLED']:
          # self.add_log("ℹ️ [SHUTDOWN] 이미 종료되었거나 종료된 상태입니다.") # 로그 너무 많아짐
          return
      if self.engine_status != 'STOPPING': # stop()을 거치지 않고 직접 호출될 경우 대비
        self.add_log("🛑 엔진 종료 절차 시작...")
        self.engine_status = 'STOPPING'

      self._stop_event.set() # 메인 루프 확실히 종료되도록

      if self.api:
          self.add_log("  -> [SHUTDOWN] API 자원 해제 시도...")
          await self.api.close()
          self.add_log("  <- [SHUTDOWN] API 자원 해제 완료.")
          self.api = None # 인스턴스 참조 제거

      self._realtime_registered = False
      self.engine_status = 'STOPPED' # 최종 상태 변경
      self.add_log("🛑 엔진 종료 완료.")

  def handle_realtime_data(self, ws_data: Dict):
        """웹소켓으로부터 실시간 데이터를 받아 해당 처리 함수 호출 (콜백 방식)"""
        try:
            trnm = ws_data.get('trnm')
            # --- 👇 디버깅 로그 레벨 조정 (DEBUG -> INFO) ---
            self.add_log(f"   [INFO] handle_realtime_data 수신: trnm='{trnm}', data='{str(ws_data)[:150]}...'")
            # --- 👆 로그 레벨 조정 끝 ---

            # 실시간 데이터 처리 ('REAL')
            if trnm == 'REAL':
                data_type = ws_data.get('type')
                values = ws_data.get('values')
                item_code = ws_data.get('item')

                if not data_type or not values:
                    self.add_log(f"  ⚠️ [WS_REAL] 실시간 데이터 항목 형식 오류: {ws_data}")
                    return

                if data_type == '00':
                    asyncio.create_task(self._process_execution_update(values))
                elif data_type == '04':
                    asyncio.create_task(self._process_balance_update(values))
                else:
                    self.add_log(f"  ℹ️ [WS_REAL] 처리 로직 없는 실시간 데이터 수신: type={data_type}, item={item_code}")

            # 등록/해지 응답 처리 ('REG', 'REMOVE')
            elif trnm in ['REG', 'REMOVE']:
                return_code_raw = ws_data.get('return_code')
                return_msg = ws_data.get('return_msg', '메시지 없음')
                return_code = -1 # 기본값 오류

                # --- 👇 디버깅 로그 레벨 조정 (DEBUG -> INFO) ---
                self.add_log(f"   [INFO] {trnm} 응답 처리 시작: raw_code='{return_code_raw}', msg='{return_msg}'")
                # --- 👆 로그 레벨 조정 끝 ---

                # return_code_raw 타입 확인 및 변환
                if isinstance(return_code_raw, str) and return_code_raw.strip().isdigit():
                    try:
                       return_code = int(return_code_raw.strip())
                       # --- 👇 디버깅 로그 레벨 조정 ---
                       self.add_log(f"      [INFO] return_code 문자열 -> 정수 변환: {return_code}")
                       # --- 👆 로그 레벨 조정 끝 ---
                    except ValueError:
                       self.add_log(f"      [WARN] return_code 문자열 -> 정수 변환 실패: '{return_code_raw}'")
                       return_code = -2
                elif isinstance(return_code_raw, int):
                    return_code = return_code_raw
                    # --- 👇 디버깅 로그 레벨 조정 ---
                    self.add_log(f"      [INFO] return_code는 이미 정수: {return_code}")
                    # --- 👆 로그 레벨 조정 끝 ---
                else:
                    self.add_log(f"      [WARN] return_code 타입이 문자열(숫자) 또는 정수가 아님: {type(return_code_raw)}")
                    return_code = -3

                self.add_log(f"📬 WS 응답 ({trnm}): code={return_code_raw}(parsed:{return_code}), msg='{return_msg}'")

                # REG 성공/실패 처리
                if trnm == 'REG':
                    # --- 👇 디버깅 로그 레벨 조정 ---
                    if return_code == 0:
                        self.add_log("      [INFO] return_code가 0이므로 성공 처리 시도...")
                        if not self._realtime_registered:
                            self._realtime_registered = True
                            self.add_log("✅ [START] 웹소켓 실시간 TR 등록 **성공** 확인 (플래그 설정).")
                        else:
                             self.add_log("ℹ️ [WS_REG] 이미 등록된 TR에 대한 성공 응답 수신 (플래그 무시).")
                    else:
                         self.add_log(f"      [INFO] return_code가 {return_code}이므로 실패 처리 시도...")
                         self.add_log(f"🚨🚨🚨 [CRITICAL] 실시간 TR 등록 실패 응답 수신! (code={return_code}). 엔진 에러 상태로 변경.")
                         self.engine_status = 'ERROR'
                    # --- 👆 로그 레벨 조정 끝 ---

            elif trnm == 'LOGIN': pass
            elif trnm in ['PING', 'PONG']: pass
            else:
               self.add_log(f"ℹ️ 처리되지 않은 WS 메시지 수신 (TRNM: {trnm}): {str(ws_data)[:200]}...")

        except Exception as e:
            self.add_log(f"🚨🚨🚨 [CRITICAL] 실시간 데이터 처리 콜백 함수 오류: {e} | Data: {str(ws_data)[:200]}... 🚨🚨🚨")
            self.add_log(traceback.format_exc())

  async def _process_execution_update(self, exec_data: Dict):
    """실시간 주문 체결(TR ID: 00) 데이터 처리 (비동기)"""
    try:
        order_no = exec_data.get('9203')
        exec_qty_str = exec_data.get('911');
        exec_price_str = exec_data.get('910');
        order_status = exec_data.get('913')
        unfilled_qty_str = exec_data.get('902');
        stock_code_raw = exec_data.get('9001')
        order_side_code = exec_data.get('907')
        total_order_qty_str = exec_data.get('900')

        if not all([order_no, order_status, stock_code_raw, order_side_code, total_order_qty_str]):
             self.add_log(f"   ⚠️ [EXEC_UPDATE] 필수 데이터 누락: {exec_data}")
             return

        stock_code = stock_code_raw[1:] if stock_code_raw.startswith('A') else stock_code_raw
        order_side = "BUY" if order_side_code == "2" else "SELL"
        try:
            exec_qty = int(exec_qty_str.strip()) if exec_qty_str and exec_qty_str.strip() else 0
            unfilled_qty = int(unfilled_qty_str.strip()) if unfilled_qty_str and unfilled_qty_str.strip() else 0
            total_order_qty = int(total_order_qty_str.strip()) if total_order_qty_str and total_order_qty_str.strip() else 0
            exec_price = float(exec_price_str.replace('+', '').replace('-', '').strip()) if exec_price_str and exec_price_str.strip() else 0.0
        except ValueError:
            self.add_log(f"   ⚠️ [EXEC_UPDATE] 숫자 변환 오류: qty='{exec_qty_str}', price='{exec_price_str}', unfilled='{unfilled_qty_str}', total='{total_order_qty_str}'")
            return

        position_info = None
        for code, pos in self.positions.items():
            if code == stock_code and pos.get('order_no') == order_no:
                position_info = pos
                break

        if not position_info:
            return

        self.add_log(f"   ⚡️ [EXEC_UPDATE] 주문({order_no}) 상태={order_status}, 종목={stock_code}, 체결량={exec_qty}, 체결가={exec_price}, 미체결량={unfilled_qty}")
        current_pos_status = position_info.get('status')

        if current_pos_status == 'PENDING_ENTRY':
            if order_status == '체결' and exec_qty > 0 and exec_price > 0:
                filled_qty = position_info.get('filled_qty', 0) + exec_qty
                filled_value = position_info.get('filled_value', 0) + (exec_qty * exec_price)
                position_info['filled_qty'] = filled_qty
                position_info['filled_value'] = filled_value

                if unfilled_qty == 0:
                    entry_price = filled_value / filled_qty if filled_qty > 0 else position_info.get('order_price', 0)
                    position_info.update({
                        'status': 'IN_POSITION', 'entry_price': entry_price, 'size': filled_qty,
                        'entry_time': datetime.now(),
                        'order_no': None, 'order_qty': None, 'order_price': None,
                        'filled_qty': None, 'filled_value': None
                    })
                    self.add_log(f"   ✅ [EXEC_UPDATE] 매수 완전 체결: [{stock_code}] 진입가={entry_price:.2f}, 수량={filled_qty}")
                else:
                    self.add_log(f"   ⏳ [EXEC_UPDATE] 매수 부분 체결: [{stock_code}] 누적 {filled_qty}/{total_order_qty}")

            elif order_status in ['취소', '거부', '확인']:
                filled_qty = position_info.get('filled_qty', 0)
                if filled_qty > 0:
                     entry_price = position_info.get('filled_value', 0) / filled_qty if filled_qty > 0 else position_info.get('order_price', 0)
                     self.add_log(f"   ⚠️ [EXEC_UPDATE] 매수 주문({order_no}) 부분 체결({filled_qty}) 후 종료({order_status}). 포지션 확정.")
                     position_info.update({
                         'status': 'IN_POSITION', 'entry_price': entry_price, 'size': filled_qty,
                         'entry_time': datetime.now(),
                         'order_no': None, 'order_qty': None, 'order_price': None,
                         'filled_qty': None, 'filled_value': None
                     })
                else:
                    self.add_log(f"   ❌ [EXEC_UPDATE] 매수 주문({order_no}) 실패/취소: {order_status}. 포지션 제거.")
                    self.positions.pop(stock_code, None)

        elif current_pos_status == 'PENDING_EXIT':
             if order_status == '체결' and exec_qty > 0 and exec_price > 0:
                filled_qty = position_info.get('filled_qty', 0) + exec_qty
                filled_value = position_info.get('filled_value', 0) + (exec_qty * exec_price)
                position_info['filled_qty'] = filled_qty
                position_info['filled_value'] = filled_value
                original_size = position_info.get('original_size_before_exit', position_info.get('size', 0))

                if unfilled_qty == 0:
                    # filled_qty는 이미 이 시점까지 누적된 체결량이므로 정확합니다.
                    exit_price = filled_value / filled_qty if filled_qty > 0 else 0
                    entry_price = position_info.get('entry_price', 0)
                    # ✅ 실현 손익 계산 시에도 정확한 filled_qty 사용 확인
                    profit = (exit_price - entry_price) * filled_qty if entry_price else 0
                    profit_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price and entry_price != 0 else 0
                    # ✅ 로그 출력 시에도 정확한 filled_qty 변수 사용 확인
                    self.add_log(f"   ✅ [EXEC_UPDATE] 매도 완전 체결 (청산): [{stock_code}] 청산가={exit_price:.2f}, 수량={filled_qty}, 실현손익={profit:.2f} ({profit_pct:.2f}%), 사유={position_info.get('exit_signal')}")
                    self.positions.pop(stock_code, None)
                else:
                    self.add_log(f"   ⏳ [EXEC_UPDATE] 매도 부분 체결: [{stock_code}] 누적 {filled_qty}/{original_size}")

             elif order_status in ['취소', '거부', '확인']:
                 filled_qty = position_info.get('filled_qty', 0)
                 original_size = position_info.get('original_size_before_exit', position_info.get('size', 0))
                 remaining_size = original_size - filled_qty

                 if remaining_size > 0 :
                      self.add_log(f"   ⚠️ [EXEC_UPDATE] 매도 주문({order_no}) 부분 체결({filled_qty}) 후 종료({order_status}). [{stock_code}] {remaining_size}주 잔여. IN_POSITION 복귀.")
                      position_info.update({
                          'size': remaining_size, 'status': 'IN_POSITION',
                          'order_no': None, 'order_qty': None, 'filled_qty': None, 'filled_value': None,
                          'original_size_before_exit': None, 'exit_signal': None
                      })
                 else:
                     if filled_qty == original_size:
                         self.add_log(f"   ℹ️ [EXEC_UPDATE] 매도 주문({order_no}) 전량 체결 후 최종 상태({order_status}) 수신. 포지션 제거.")
                         self.positions.pop(stock_code, None)
                     else:
                         self.add_log(f"   ❌ [EXEC_UPDATE] 매도 주문({order_no}) 실패/취소: {order_status}. IN_POSITION 복귀.")
                         position_info.update({
                             'status': 'IN_POSITION',
                             'order_no': None, 'order_qty': None, 'filled_qty': None, 'filled_value': None,
                             'original_size_before_exit': None, 'exit_signal': None
                         })
    except Exception as e:
        self.add_log(f"🚨🚨🚨 [CRITICAL] 주문 체결 처리(_process_execution_update) 중 심각한 오류: {e} | Data: {str(exec_data)[:200]}... 🚨🚨🚨")
        self.add_log(traceback.format_exc())

  async def _process_balance_update(self, balance_data: Dict):
      """실시간 잔고(TR ID: 04) 데이터 처리 (비동기)"""
      try:
          stock_code_raw = balance_data.get('9001')
          current_size_str = balance_data.get('930') # 보유수량
          avg_price_str = balance_data.get('931')    # 매입단가

          if not stock_code_raw or current_size_str is None or avg_price_str is None:
              return

          stock_code = stock_code_raw[1:] if stock_code_raw.startswith('A') else stock_code_raw

          try:
              current_size = int(current_size_str.strip()) if current_size_str and current_size_str.strip() else 0
              avg_price = float(avg_price_str.strip()) if avg_price_str and avg_price_str.strip() else 0.0
          except ValueError:
              self.add_log(f"  ⚠️ [BALANCE_UPDATE] 잔고 숫자 변환 오류: size='{current_size_str}', avg_price='{avg_price_str}'")
              return

          position_info = self.positions.get(stock_code)
          current_pos_status = position_info.get('status') if position_info else None

          if position_info and current_pos_status in ['IN_POSITION', 'PENDING_EXIT'] and current_size > 0:
              pos_size = position_info.get('size')
              pos_entry_price = position_info.get('entry_price')
              if pos_size is not None and pos_size != current_size:
                  self.add_log(f"  🔄 [BALANCE_UPDATE] 수량 불일치 감지 (Case 1): [{stock_code}], 엔진:{pos_size} != 잔고:{current_size}. 엔진 상태 동기화.")
                  position_info['size'] = current_size
              if pos_entry_price is not None and abs(pos_entry_price - avg_price) > 0.01:
                   self.add_log(f"  🔄 [BALANCE_UPDATE] 평균단가 불일치 감지 (Case 1): [{stock_code}], 엔진:{pos_entry_price:.2f} != 잔고:{avg_price:.2f}. 엔진 상태 동기화.")
                   position_info['entry_price'] = avg_price

          elif not position_info and current_size > 0:
               self.add_log(f"  ⚠️ [BALANCE_UPDATE] 불일치 잔고({stock_code}, {current_size}주 @ {avg_price}) 발견 (Case 2). 엔진 상태 강제 업데이트.")
               self.positions[stock_code] = {
                   'status': 'IN_POSITION', 'stk_cd': stock_code, 'size': current_size,
                   'entry_price': avg_price, 'entry_time': datetime.now()
               }

          elif position_info and current_pos_status in ['IN_POSITION', 'PENDING_EXIT'] and current_size == 0:
              self.add_log(f"  ℹ️ [BALANCE_UPDATE] 잔고 0 확인 ({stock_code}, 상태: {current_pos_status}). 엔진 포지션 제거 (Case 3).")
              self.positions.pop(stock_code, None)

      except Exception as e:
          self.add_log(f"🚨🚨🚨 [CRITICAL] 잔고 처리(_process_balance_update) 중 심각한 오류: {e} | Data: {str(balance_data)[:200]}... 🚨🚨🚨")
          self.add_log(traceback.format_exc())

  async def run_screening(self) -> List[str]: # 반환 타입은 종목 코드 리스트 유지
    """거래 대상 종목 스크리닝 실행하고 종목 코드 리스트 반환 (종목명 정보도 저장)"""
    self.add_log("🔍 [SCREEN] 종목 스크리닝 시작...")
    if not self.api: self.add_log("⚠️ [SCREEN] API 객체 없음."); return []
    try:
      self.add_log("  -> [SCREEN] 거래량 급증 API 호출 시도...")
      params = {
          'mrkt_tp': '000', 'sort_tp': '2', 'tm_tp': '1',
          'tm': str(getattr(self.config.strategy, 'screening_surge_timeframe_minutes', 5)),
          'trde_qty_tp': str(getattr(self.config.strategy, 'screening_min_volume_threshold', 10)).zfill(5),
          'stk_cnd': '14', 'pric_tp': '8', 'stex_tp': '3'
      }
      candidate_stocks_raw = await self.api.fetch_volume_surge_rank(**params)

      if not candidate_stocks_raw or candidate_stocks_raw.get('return_code') != 0:
          error_msg = candidate_stocks_raw.get('return_msg', 'API 응답 없음') if candidate_stocks_raw else 'API 호출 실패'
          self.add_log(f"⚠️ [SCREEN] 스크리닝 API 오류 또는 결과 없음: {error_msg}")
          self.candidate_stock_codes = [] # 후보 리스트 초기화
          self.candidate_stocks_info = [] # 정보 리스트 초기화
          return []

      surge_list = candidate_stocks_raw.get('trde_qty_sdnin', [])
      self.add_log(f"  <- [SCREEN] 거래량 급증 API 응답 수신 (결과 수: {len(surge_list)})")

      if not surge_list:
          self.add_log("⚠️ [SCREEN] 스크리닝 결과 없음.")
          self.candidate_stock_codes = [] # 후보 리스트 초기화
          self.candidate_stocks_info = [] # 정보 리스트 초기화
          return []

      candidate_stocks_intermediate = [] # 필터링 중간 결과 저장
      for s in surge_list:
          stk_cd_raw = s.get('stk_cd'); stk_nm = s.get('stk_nm')
          cur_prc_str = s.get('cur_prc'); sdnin_rt_str = s.get('sdnin_rt')
          if not stk_cd_raw or not stk_nm or not cur_prc_str or not sdnin_rt_str: continue

          stk_cd = stk_cd_raw.strip()
          # _NX, _AL 제거 (필요시)
          if stk_cd.endswith('_NX'): stk_cd = stk_cd[:-3]
          if stk_cd.endswith('_AL'): stk_cd = stk_cd[:-3]

          try:
              cur_prc = float(cur_prc_str.replace('+','').replace('-','').strip())
              sdnin_rt = float(sdnin_rt_str.strip())
              min_price = getattr(self.config.strategy, 'screening_min_price', 1000)
              min_surge_rate = getattr(self.config.strategy, 'screening_min_surge_rate', 100.0)
              if cur_prc >= min_price and sdnin_rt >= min_surge_rate:
                  candidate_stocks_intermediate.append({'stk_cd': stk_cd, 'stk_nm': stk_nm, 'sdnin_rt': sdnin_rt})
          except ValueError:
              self.add_log(f"  ⚠️ [SCREEN] 숫자 변환 오류 (스크리닝 필터링): {s}")
              continue

      candidate_stocks_intermediate.sort(key=lambda x: x['sdnin_rt'], reverse=True)
      max_targets = getattr(self.config.strategy, 'max_target_stocks', 5)

      # --- 👇 최종 후보 리스트 업데이트 ---
      self.candidate_stocks_info = candidate_stocks_intermediate[:max_targets] # 종목 정보 저장
      self.candidate_stock_codes = [s['stk_cd'] for s in self.candidate_stocks_info] # 종목 코드만 따로 저장
      # --- 👆 최종 후보 리스트 업데이트 끝 ---

      # --- 👇 로그 출력 형식 변경 ---
      target_stocks_display = [f"{s['stk_cd']}({s['stk_nm']})" for s in self.candidate_stocks_info]
      if target_stocks_display:
          self.add_log(f"🎯 [SCREEN] 스크리닝 완료. 후보: {target_stocks_display}")
      else:
          self.add_log("ℹ️ [SCREEN] 최종 후보 종목 없음.")

      return self.candidate_stock_codes # 기존처럼 종목 코드 리스트 반환
      # --- 👆 로그 출력 형식 변경 끝 ---

    except Exception as e:
        self.add_log(f"🚨🚨🚨 [CRITICAL] 스크리닝 중 심각한 오류 발생: {e} 🚨🚨🚨")
        self.add_log(traceback.format_exc())
        self.candidate_stock_codes = [] # 후보 리스트 초기화
        self.candidate_stocks_info = [] # 정보 리스트 초기화
        return []

  async def process_all_stocks_tick(self, current_time: datetime):
      """스크리닝된 후보 종목과 보유 종목 전체에 대해 Tick 처리 실행 (순차 실행, API 호출 간격 조절)"""
      if self.engine_status != 'RUNNING': return

      stocks_to_process = set(self.positions.keys())
      if self.candidate_stock_codes:
          stocks_to_process.update([code for code in self.candidate_stock_codes if code not in self.positions])

      if not stocks_to_process: return

      stocks_to_run_this_tick = []
      tick_interval_seconds = getattr(self.config.strategy, 'tick_interval_seconds', 5)

      for code in list(stocks_to_process):
          last_processed = self.last_stock_tick_time.get(code)
          if last_processed is None or (current_time - last_processed).total_seconds() >= tick_interval_seconds:
              stocks_to_run_this_tick.append(code)

      if not stocks_to_run_this_tick: return

      self.add_log(f"⚙️ [TICK_ALL] 순차 처리 시작 (대상: {stocks_to_run_this_tick})")
      processed_count = 0
      api_call_delay = getattr(self.api, 'REQUEST_DELAY', 1.1) if self.api else 1.1

      for code in stocks_to_run_this_tick:
          if processed_count > 0:
              self.add_log(f"    - [TICK_ALL] 다음 API 호출 전 {api_call_delay:.1f}초 대기...")
              await asyncio.sleep(api_call_delay)

          try:
              await self.process_single_stock_tick(code)
              self.last_stock_tick_time[code] = current_time
              processed_count += 1
          except httpx.HTTPStatusError as http_err:
              self.add_log(f"🚨 [TICK_ALL] HTTP 오류 발생 ({code}): {http_err.response.status_code} - {http_err.response.text[:100]}...")
          except Exception as e:
              self.add_log(f"🚨🚨🚨 [CRITICAL] 개별 Tick 처리 중 심각한 오류 발생 ({code}): {e} 🚨🚨🚨")
              self.add_log(traceback.format_exc())

      self.add_log(f"⚙️ [TICK_ALL] 순차 처리 완료 ({processed_count}/{len(stocks_to_run_this_tick)}개 종목 시도)")

  async def process_single_stock_tick(self, stock_code: str):
      """개별 종목에 대한 데이터 조회, 지표 계산, 신호 확인, 주문 실행"""
      if not self.api or not self.engine_status == 'RUNNING': return

      position_info = self.positions.get(stock_code)
      current_status = position_info.get('status') if position_info else 'SEARCHING'

      if current_status in ['PENDING_ENTRY', 'PENDING_EXIT']:
          return

      try:
          raw_data = await self.api.fetch_minute_chart(stock_code, timeframe=1)
          if not raw_data: return
          if raw_data.get('return_code') != 0:
              self.add_log(f"    ⚠️ [{stock_code}] 분봉 조회 API 오류: code={raw_data.get('return_code')}, msg={raw_data.get('return_msg')}")
              return
          chart_data_list = raw_data.get("stk_min_pole_chart_qry")
          if not chart_data_list or not isinstance(chart_data_list, list):
              self.add_log(f"    ⚠️ [{stock_code}] 분봉 데이터 형식 오류 또는 없음: {str(raw_data)[:100]}...")
              return

          df = None
          try:
              df = preprocess_chart_data(chart_data_list)
          except Exception as preproc_e:
              self.add_log(f"    🚨 [{stock_code}] 데이터 전처리 중 오류: {preproc_e}")
              self.add_log(traceback.format_exc())
              return
          if df is None or df.empty: return

          current_price = None; current_vwap = None; orb_levels = None
          try:
              add_vwap(df)
              orb_timeframe = getattr(self.config.strategy, 'orb_timeframe', 15)
              orb_levels = calculate_orb(df, timeframe=orb_timeframe)
              current_price = df['close'].iloc[-1]
              current_vwap = df['VWAP'].iloc[-1] if 'VWAP' in df.columns and not pd.isna(df['VWAP'].iloc[-1]) else None
          except Exception as indi_e:
              self.add_log(f"    🚨 [{stock_code}] 지표 계산 중 오류: {indi_e}")
              self.add_log(traceback.format_exc())
              return

          if current_price is None or orb_levels is None or orb_levels.get('orh') is None:
              self.add_log(f"    ⚠️ [{stock_code}] 필수 지표(현재가/ORH) 계산 실패 또는 값 없음. Tick 처리 중단.")
              return

          # === 상태별 로직 수행 ===
          if current_status == 'SEARCHING':
            max_concurrent_positions = getattr(self.config.strategy, 'max_concurrent_positions', 5)
            if len(self.positions) >= max_concurrent_positions: return

            breakout_buffer = getattr(self.config.strategy, 'breakout_buffer', 0.15)
            signal = check_breakout_signal(current_price, orb_levels, breakout_buffer)

            if signal == "BUY":
              self.add_log(f"🔥 [{stock_code}] 매수 신호! (현재가 {current_price:.0f}, ORH {orb_levels.get('orh','N/A'):.0f}, Buffer {breakout_buffer}%)")
              order_quantity = 0

              # --- 주문 수량 계산 로직 ---
              investment_amount_per_stock = getattr(self.config.strategy, 'investment_amount_per_stock', 1_000_000)
              # --- 👇 디버깅 로그 추가 ---
              self.add_log(f"    [DEBUG_ORDER_QTY] 설정된 투자 금액: {investment_amount_per_stock}")
              # --- 👆 디버깅 로그 추가 끝 ---

              balance_info = await self.api.fetch_account_balance()

              if balance_info and balance_info.get('return_code') == 0:
                  available_cash_str = balance_info.get('ord_alow_amt')
                  # --- 👇 디버깅 로그 추가 ---
                  self.add_log(f"    [DEBUG_ORDER_QTY] API 예수금 응답 중 주문가능금액(str): '{available_cash_str}'")
                  # --- 👆 디버깅 로그 추가 끝 ---

                  if available_cash_str and available_cash_str.strip():
                      try:
                          available_cash = int(available_cash_str)
                          # --- 👇 디버깅 로그 추가 ---
                          self.add_log(f"    [DEBUG_ORDER_QTY] 변환된 주문 가능 현금(int): {available_cash}")
                          self.add_log(f"    [DEBUG_ORDER_QTY] 비교: available_cash({available_cash}) >= investment_amount_per_stock({investment_amount_per_stock}) ?")
                          # --- 👆 디버깅 로그 추가 끝 ---

                          if available_cash >= investment_amount_per_stock and current_price is not None and current_price > 0:
                              # --- 👇 디버깅 로그 추가 ---
                              self.add_log(f"    [DEBUG_ORDER_QTY] 수량 계산 시도: investment({investment_amount_per_stock}) // current_price({current_price})")
                              # --- 👆 디버깅 로그 추가 끝 ---
                              order_quantity = int(investment_amount_per_stock // current_price)
                              self.add_log(f"     - 주문 가능 현금: {available_cash:,}, 투자 예정: {investment_amount_per_stock:,}, 계산된 수량: {order_quantity}주")
                          else:
                              # --- 👇 디버깅 로그 강화 ---
                              reason = []
                              if available_cash < investment_amount_per_stock: reason.append(f"현금 부족({available_cash} < {investment_amount_per_stock})")
                              if current_price is None: reason.append("현재가 없음")
                              elif current_price <= 0: reason.append(f"현재가 오류({current_price})")
                              self.add_log(f"     - 주문 불가 사유: {', '.join(reason)}")
                              # --- 👆 디버깅 로그 강화 끝 ---
                      except ValueError:
                          self.add_log(f"     - 주문 불가: 주문 가능 현금('ord_alow_amt') 파싱 오류 ({available_cash_str})")
                  else:
                      self.add_log(f"     - 주문 불가: API 응답에서 주문 가능 현금('ord_alow_amt') 찾을 수 없음")
              else:
                  error_msg = balance_info.get('return_msg', 'API 응답 없음') if balance_info else 'API 호출 실패'
                  self.add_log(f"     - 주문 불가: 예수금 정보 조회 실패 ({error_msg})")
              # --- 주문 수량 계산 로직 끝 ---

              if order_quantity > 0:
                self.add_log(f"    -> [{stock_code}] 매수 주문 API 호출 시도 ({order_quantity}주)...")
                order_result = await self.api.create_buy_order(stock_code, quantity=order_quantity)

                if order_result and order_result.get('return_code') == 0:
                  order_no = order_result.get('ord_no')
                  self.positions[stock_code] = {
                      'status': 'PENDING_ENTRY', 'order_no': order_no,
                      'order_qty': order_quantity, 'order_price': current_price,
                      'stk_cd': stock_code, 'filled_qty': 0, 'filled_value': 0.0
                  }
                  self.add_log(f"    ➡️ [{stock_code}] 매수 주문 접수 완료: {order_no}")
                else:
                    error_msg = order_result.get('return_msg', 'API 응답 없음') if order_result else 'API 호출 실패'
                    self.add_log(f"    ❌ [{stock_code}] 매수 주문 실패: {error_msg}")
              else:
                self.add_log(f"     - [{stock_code}] 주문 수량이 0이므로 매수 주문 실행 안 함.")

          elif current_status == 'IN_POSITION' and position_info:
            signal = manage_position(position_info, current_price)
            if signal:
              log_prefix = "💰" if signal == "TAKE_PROFIT" else "🛑"
              self.add_log(f"{log_prefix} 청산 신호({signal})! [{stock_code}] 매도 주문 실행 (현재가 {current_price:.0f}).")
              order_quantity = position_info.get('size', 0)

              if order_quantity > 0:
                self.add_log(f"    -> [{stock_code}] 매도 주문 API 호출 시도 ({order_quantity}주)...")
                order_result = await self.api.create_sell_order(stock_code, quantity=order_quantity)

                if order_result and order_result.get('return_code') == 0:
                  order_no = order_result.get('ord_no')
                  self.positions[stock_code].update({
                      'status': 'PENDING_EXIT', 'order_no': order_no,
                      'order_qty': order_quantity, 'exit_signal': signal,
                      'filled_qty': 0, 'filled_value': 0.0,
                      'original_size_before_exit': order_quantity
                  })
                  self.add_log(f"    ⬅️ [{stock_code}] 매도 주문 접수 완료: {order_no}")
                else:
                    error_msg = order_result.get('return_msg', 'API 응답 없음') if order_result else 'API 호출 실패'
                    self.add_log(f"    ❌ [{stock_code}] 매도 주문 실패: {error_msg}")
              else:
                self.add_log(f"    ⚠️ [{stock_code}] 보유 수량이 0이므로 매도 주문 실행 안 함.")

      except Exception as e:
        self.add_log(f"🚨🚨🚨 [CRITICAL] 개별 Tick 처리({stock_code}) 중 예상치 못한 심각한 오류 발생: {e} 🚨🚨🚨")
        self.add_log(traceback.format_exc())

  async def execute_kill_switch(self):
      """긴급 정지: 미체결 주문 취소 및 보유 포지션 시장가 청산"""
      if self.engine_status in ['STOPPING', 'STOPPED', 'KILLED']: return
      self.add_log("🚨 KILL SWITCH 발동! 모든 주문 취소 및 포지션 청산 시작...")
      if not self.api: self.add_log("⚠️ [KILL] API 객체 없음. Kill Switch 실행 불가."); return

      original_engine_status = self.engine_status
      self.engine_status = 'STOPPING'

      try:
          pending_orders = []
          for code, pos in list(self.positions.items()):
              if pos.get('status') in ['PENDING_ENTRY', 'PENDING_EXIT'] and pos.get('order_no'):
                  pending_orders.append({'code': code, 'order_no': pos['order_no'], 'status_before': pos['status']})

          if pending_orders:
              self.add_log(f"  -> [KILL] 미체결 주문 {len(pending_orders)}건 취소 시도...")
              cancel_tasks = [self.api.cancel_order(order['order_no'], order['code'], 0) for order in pending_orders]
              cancel_results = await asyncio.gather(*cancel_tasks, return_exceptions=True)

              for i, result in enumerate(cancel_results):
                  order = pending_orders[i]
                  if isinstance(result, Exception) or (result and result.get('return_code') != 0):
                      error_info = str(result) if isinstance(result, Exception) else result.get('return_msg', 'Unknown Error')
                      self.add_log(f"  ⚠️ [KILL] 주문({order['order_no']}/{order['code']}) 취소 실패/오류: {error_info}")
                  else:
                      self.add_log(f"  ✅ [KILL] 주문({order['order_no']}/{order['code']}) 취소 성공/요청됨.")
                      if order['code'] in self.positions:
                          pos_info = self.positions[order['code']]
                          if order['status_before'] == 'PENDING_ENTRY':
                              if pos_info.get('filled_qty', 0) > 0:
                                  entry_price = pos_info.get('filled_value', 0) / pos_info['filled_qty'] if pos_info.get('filled_qty', 0) > 0 else 0
                                  pos_info.update({'status': 'IN_POSITION', 'entry_price': entry_price, 'size': pos_info['filled_qty'], 'order_no': None})
                                  self.add_log(f"     -> [{order['code']}] 부분 매수({pos_info['size']}주) 후 취소. 포지션 확정.")
                              else:
                                  self.positions.pop(order['code'], None)
                                  self.add_log(f"     -> [{order['code']}] 완전 미체결 매수 취소. 포지션 제거.")
                          elif order['status_before'] == 'PENDING_EXIT':
                              if pos_info.get('filled_qty', 0) > 0:
                                  original_size = pos_info.get('original_size_before_exit', pos_info.get('size',0))
                                  remaining_size = original_size - pos_info['filled_qty']
                                  if remaining_size > 0:
                                      pos_info.update({'status': 'IN_POSITION', 'size': remaining_size, 'order_no': None})
                                      self.add_log(f"     -> [{order['code']}] 부분 매도({pos_info['filled_qty']}주) 후 취소. {remaining_size}주 포지션 복귀.")
                                  else:
                                      self.positions.pop(order['code'], None)
                                      self.add_log(f"     -> [{order['code']}] 전량 매도 후 취소 응답? 포지션 제거.")
                              else:
                                  pos_info.update({'status': 'IN_POSITION', 'order_no': None})
                                  self.add_log(f"     -> [{order['code']}] 완전 미체결 매도 취소. 포지션 복귀.")

              self.add_log("  <- [KILL] 미체결 주문 취소 처리 완료. (잠시 대기 후 잔고 확인 및 청산 진행)")
              await asyncio.sleep(2.0)
          else:
              self.add_log("  - [KILL] 취소할 미체결 주문 없음.")

          positions_to_liquidate = []
          for code, pos in list(self.positions.items()):
              if pos.get('size', 0) > 0 and pos.get('status') == 'IN_POSITION':
                  positions_to_liquidate.append({'code': code, 'qty': pos['size']})

          if positions_to_liquidate:
              self.add_log(f"  -> [KILL] 보유 포지션 {len(positions_to_liquidate)}건 시장가 청산 시도...")
              sell_tasks = [self.api.create_sell_order(pos['code'], quantity=pos['qty']) for pos in positions_to_liquidate]
              sell_results = await asyncio.gather(*sell_tasks, return_exceptions=True)

              for i, result in enumerate(sell_results):
                  pos_info = positions_to_liquidate[i]
                  if isinstance(result, Exception) or (result and result.get('return_code') != 0):
                      error_info = str(result) if isinstance(result, Exception) else result.get('return_msg', 'Unknown Error')
                      self.add_log(f"  ❌ [KILL] 시장가 청산 실패 ({pos_info['code']} {pos_info['qty']}주): {error_info}")
                      if pos_info['code'] in self.positions:
                          self.positions[pos_info['code']]['status'] = 'ERROR_LIQUIDATION'
                  else:
                      order_no = result.get('ord_no', 'N/A')
                      self.add_log(f"  ✅ [KILL] 시장가 청산 주문 접수 ({pos_info['code']} {pos_info['qty']}주): {order_no}")
                      if pos_info['code'] in self.positions:
                          self.positions[pos_info['code']].update({
                              'status': 'PENDING_EXIT',
                              'order_no': order_no,
                              'exit_signal': 'KILL_SWITCH', # 청산 사유
                              'original_size_before_exit': pos_info['qty'] # 원래 수량 기록
                          })
              self.add_log("  <- [KILL] 시장가 청산 주문 접수 완료.")
          else:
              self.add_log("  - [KILL] 청산할 보유 포지션 없음.")

          # --- Kill Switch 완료 후 엔진 종료 신호 ---
          self.add_log("🚨 Kill Switch 처리 완료. 엔진을 종료합니다.")
          await self.stop() # 메인 루프 종료 요청

      except Exception as e:
          self.add_log(f"🚨🚨🚨 [CRITICAL] Kill Switch 처리 중 심각한 오류 발생: {e} 🚨🚨🚨")
          self.add_log(traceback.format_exc())
          self.engine_status = 'ERROR' # 오류 상태로 변경
          await self.stop() # 메인 루프 종료 요청
      finally:
          # Kill Switch 실행 완료 후 상태를 KILLED로 최종 변경
          self.engine_status = 'KILLED'
          self.add_log("🚪 [KILL SWITCH FINALLY] Kill Switch 종료 처리 완료.")