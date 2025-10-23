import asyncio
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import json
import traceback # 상세 오류 로깅을 위해 추가

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
# from strategy.screener import find_momentum_stocks # 스크리닝 로직은 run_screening 내부에 구현

# Pydantic 모델 정의 (설정 구조를 명확히 하기 위해)
from pydantic import BaseModel

# class EngineConfig(BaseModel):
#     # tick_interval_seconds: int = 60 # Tick 처리 주기는 process_all_stocks_tick 에서 관리
#     screening_interval_minutes: int = 5 # 스크리닝 주기 (분)

# class StrategyConfig(BaseModel):
#     orb_timeframe: int = 15
#     breakout_buffer: float = 0.15
#     max_target_stocks: int = 5       # 스크리닝 후 최대 관심 종목 수
#     max_concurrent_positions: int = 3 # 동시에 보유할 최대 종목 수
    # risk_manager 에서 사용할 설정 추가 (config/loader.py 에서 로드)
    # take_profit_pct: float = 2.5
    # stop_loss_pct: float = -1.0

# class Config(BaseModel):
#     engine: EngineConfig = EngineConfig()
#     strategy: StrategyConfig = StrategyConfig()
    # kiwoom 설정은 config/loader.py 에서 로드된 것을 사용

# 전역 설정 객체 (config/loader.py 에서 로드된 것을 사용하도록 가정)
# config 객체는 config/loader.py 에서 로드된 전역 객체를 사용합니다.

class TradingEngine:
  """웹소켓 기반 실시간 다중 종목 트레이딩 로직 관장 엔진"""
  def __init__(self):
    # loader.py 에서 로드된 전역 config 객체 사용
    self.config = config
    # 종목 코드를 키로 사용하는 포지션 관리 딕셔너리
    # 예: {'005930': {'status': 'IN_POSITION', 'entry_price': 70000, 'size': 10, ...}, ...}
    self.positions: Dict[str, Dict] = {}
    self.logs: List[str] = []
    self.api: Optional[KiwoomAPI] = None
    self._stop_event = asyncio.Event()
    # 마지막 스크리닝 시간 (첫 스크리닝 즉시 실행되도록 과거 시간으로 초기화)
    self.last_screening_time = datetime.now() - timedelta(minutes=self.config.engine.screening_interval_minutes + 1)
    # 스크리닝 결과(후보 종목 코드 리스트) 저장
    self.candidate_stock_codes: List[str] = []
    # 엔진 전체 상태 관리 (INITIALIZING, RUNNING, STOPPING, STOPPED, ERROR)
    self.engine_status = 'INITIALIZING'
    # 각 종목별 마지막 Tick 처리 시간을 저장하는 딕셔너리
    self.last_stock_tick_time: Dict[str, datetime] = {}

  def add_log(self, message: str):
    """로그 메시지를 추가하고 화면에 출력합니다."""
    log_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(log_msg)
    self.logs.insert(0, log_msg)
    if len(self.logs) > 100: self.logs.pop() # 로그 최대 100개 유지

  async def start(self):
    """엔진 시작 및 메인 루프 실행"""
    self.api = KiwoomAPI()
    self.engine_status = 'INITIALIZING' # 엔진 상태 초기화
    self.add_log("🚀 엔진 시작...")
    try:
      # 웹소켓 연결 및 실시간 데이터 핸들러 등록
      connected = await self.api.connect_websocket(self.handle_realtime_data)
      if not connected:
        self.add_log("❌ 웹소켓 연결 실패. 엔진 시작 불가.")
        self.engine_status = 'ERROR'; return

      self.engine_status = 'RUNNING' # 웹소켓 연결 성공 후 RUNNING 상태로 변경

      # 스크리닝 결과를 저장할 변수 초기화
      self.candidate_stock_codes: List[str] = []

      # 메인 루프 시작
      while not self._stop_event.is_set():
        current_time = datetime.now()

        # 1. 주기적 스크리닝 실행 조건 확인
        should_screen = (current_time - self.last_screening_time).total_seconds() >= self.config.engine.screening_interval_minutes * 60
        # 보유 포지션 수가 최대치 미만이고, 스크리닝 시간 도래 시
        if len(self.positions) < self.config.strategy.max_concurrent_positions and should_screen:
            self.candidate_stock_codes = await self.run_screening()
            self.last_screening_time = current_time # 마지막 스크리닝 시간 업데이트

        # 2. 보유/관심 종목 Tick 처리 (매 루프마다 실행, 내부에서 API 호출 간격 조절)
        await self.process_all_stocks_tick(current_time)

        # CPU 사용량 조절 및 루프 지연 (예: 1초)
        await asyncio.sleep(1)

    except asyncio.CancelledError:
      self.add_log("ℹ️ 엔진 메인 루프 취소됨.")
    except Exception as e:
      self.add_log(f"🚨 엔진 메인 루프 예외: {e}")
      self.add_log(traceback.format_exc()) # 상세 오류 로그 추가
      self.engine_status = 'ERROR' # 오류 발생 시 상태 변경
    finally:
      await self.shutdown() # 최종 종료 처리

  async def stop(self):
    """엔진 종료 신호 설정"""
    if self.engine_status not in ['STOPPING', 'STOPPED']:
        self.add_log("🛑 엔진 종료 신호 수신..."); self._stop_event.set()

  async def shutdown(self):
      """엔진 종료 처리 (자원 해제)"""
      if self.engine_status == 'STOPPED': return # 이미 종료됨
      self.add_log("🛑 엔진 종료 절차 시작...")
      self.engine_status = 'STOPPING' # 종료 중 상태
      self._stop_event.set() # 메인 루프 확실히 종료
      if self.api:
          await self.api.close() # API 자원 해제 (웹소켓 포함)
      self.engine_status = 'STOPPED' # 최종 정지 상태
      self.add_log("🛑 엔진 종료 완료.")

  # --- 실시간 데이터 처리 콜백 ---
  def handle_realtime_data(self, data: Dict):
    """웹소켓으로부터 실시간 데이터를 받아 해당 처리 함수 호출"""
    try:
        header = data.get('header', {})
        body_str = data.get('body')

        # body가 문자열이면 json 파싱 시도, 이미 dict면 그대로 사용
        body = {}
        if isinstance(body_str, str):
            if not body_str.strip(): return # 빈 문자열 무시
            try: body = json.loads(body_str)
            except json.JSONDecodeError: print(f"⚠️ 실시간 body 파싱 실패: {body_str[:100]}..."); return
        elif isinstance(body_str, dict):
            body = body_str # 이미 dict 형태인 경우 (API 수정 시 대비)
        else:
             print(f"⚠️ 알 수 없는 body 타입: {type(body_str)}")
             return

        if not header or not body: return # 필요한 데이터 없으면 무시

        tr_id = header.get('tr_id')
        if tr_id == '00': # 주문 체결
            # 비동기 함수는 create_task로 백그라운드 실행
            asyncio.create_task(self._process_execution_update(body))
        elif tr_id == '04': # 잔고 변경
            asyncio.create_task(self._process_balance_update(body))
        # 필요한 다른 실시간 TR ID 처리 추가 가능 (예: '0B' 주식체결 등)

    except Exception as e:
        self.add_log(f"🚨 실시간 데이터 처리 오류(콜백): {e} | Data: {str(data)[:200]}...")
        self.add_log(traceback.format_exc())

  # --- 실시간 데이터 처리 상세 ---
  async def _process_execution_update(self, exec_data: Dict):
    """실시간 주문 체결(TR ID: 00) 데이터 처리"""
    try:
        # 키움 API 문서(p.416) 기준 필드명 사용
        order_no = exec_data.get('9203')
        exec_qty_str = exec_data.get('911'); exec_qty = int(exec_qty_str) if exec_qty_str and exec_qty_str.isdigit() else 0
        exec_price_str = exec_data.get('910'); exec_price = 0.0
        order_status = exec_data.get('913') # 예: 접수, 확인, 체결, 취소, 거부
        unfilled_qty_str = exec_data.get('902'); unfilled_qty = int(unfilled_qty_str) if unfilled_qty_str and unfilled_qty_str.isdigit() else 0
        stock_code_raw = exec_data.get('9001')
        order_type = exec_data.get('905') # +매수, -매도

        # 필수 데이터 누락 시 처리 중단
        if not all([order_no, order_status, stock_code_raw]):
             self.add_log(f"⚠️ 주문 체결 데이터 누락: {exec_data}")
             return

        # 종목코드 정리 (앞 'A' 제거)
        stock_code = stock_code_raw[1:] if stock_code_raw.startswith('A') else stock_code_raw

        # 체결 데이터인 경우에만 체결가 파싱
        if order_status == '체결' and exec_price_str:
            try: exec_price = float(exec_price_str.replace('+', '').replace('-', ''))
            except ValueError: self.add_log(f"⚠️ 체결가 파싱 오류: {exec_price_str}"); return
        else:
            exec_price = 0.0 # 체결이 아니면 체결가는 0

        # 현재 관리중인 포지션 정보 가져오기
        position_info = self.positions.get(stock_code)

        # 현재 엔진에서 관리하는 주문이 아니면 무시 (다른 경로 주문 등)
        if not position_info or position_info.get('order_no') != order_no:
            # self.add_log(f"ℹ️ 관련 없는 주문 체결 수신: {order_no} / {stock_code}")
            return

        self.add_log(f"⚡️ 주문({order_no}) 상태={order_status}, 종목={stock_code}, 체결량={exec_qty}, 체결가={exec_price}, 미체결량={unfilled_qty}")

        # --- 상태별 체결 처리 ---
        current_pos_status = position_info.get('status')

        # [1. PENDING_ENTRY: 매수 주문 처리]
        if current_pos_status == 'PENDING_ENTRY':
            if order_status == '체결' and exec_qty > 0 and exec_price > 0:
                # 부분 또는 완전 체결: 체결 정보 누적
                filled_qty = position_info.get('filled_qty', 0) + exec_qty
                filled_value = position_info.get('filled_value', 0) + (exec_qty * exec_price)
                position_info['filled_qty'] = filled_qty
                position_info['filled_value'] = filled_value

                if unfilled_qty == 0: # 완전 체결 완료
                    entry_price = filled_value / filled_qty if filled_qty > 0 else position_info.get('order_price', 0) # 주문 시 가격 사용 (만약 대비)
                    position_info.update({
                        'status': 'IN_POSITION',
                        'entry_price': entry_price,
                        'size': filled_qty,
                        'entry_time': datetime.now(),
                        'order_no': None, # 주문 완료
                        'order_qty': None,
                        'filled_qty': None, # 임시 필드 제거
                        'filled_value': None })
                    self.add_log(f"✅ (WS) 매수 완전 체결: [{stock_code}] 진입가={entry_price:.2f}, 수량={filled_qty}")
                else: # 부분 체결 진행 중
                    self.add_log(f"⏳ (WS) 매수 부분 체결: [{stock_code}] 누적 {filled_qty}/{position_info.get('order_qty')}")

            elif order_status in ['취소', '거부', '확인']: # 주문 종료 (취소, 거부, 최종 확인)
                filled_qty = position_info.get('filled_qty', 0)
                if filled_qty > 0: # 부분 체결 후 종료된 경우
                     entry_price = position_info.get('filled_value', 0) / filled_qty if filled_qty > 0 else position_info.get('order_price', 0)
                     self.add_log(f"⚠️ (WS) 매수 주문({order_no}) 부분 체결({filled_qty}) 후 종료({order_status}).")
                     position_info.update({
                         'status': 'IN_POSITION', 'entry_price': entry_price, 'size': filled_qty,
                         'entry_time': datetime.now(), 'order_no': None, 'order_qty': None,
                         'filled_qty': None, 'filled_value': None })
                else: # 완전 미체결 상태에서 종료된 경우
                    self.add_log(f"❌ (WS) 매수 주문({order_no}) 실패/취소: {order_status}. 포지션 제거.")
                    self.positions.pop(stock_code, None) # 포지션 목록에서 제거

        # [2. PENDING_EXIT: 매도 주문 처리]
        elif current_pos_status == 'PENDING_EXIT':
             if order_status == '체결' and exec_qty > 0 and exec_price > 0:
                # 부분 또는 완전 체결: 체결 정보 누적
                filled_qty = position_info.get('filled_qty', 0) + exec_qty
                filled_value = position_info.get('filled_value', 0) + (exec_qty * exec_price)
                position_info['filled_qty'] = filled_qty
                position_info['filled_value'] = filled_value
                original_size = position_info.get('original_size_before_exit', position_info.get('size', 0)) # 청산 시작 전 수량

                if unfilled_qty == 0: # 완전 체결 (청산 완료)
                    exit_price = filled_value / filled_qty if filled_qty > 0 else 0
                    entry_price = position_info.get('entry_price', 0)
                    profit = (exit_price - entry_price) * filled_qty if entry_price else 0
                    profit_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price and entry_price != 0 else 0

                    self.add_log(f"✅ (WS) 매도 완전 체결 (청산): [{stock_code}] 청산가={exit_price:.2f}, 수량={filled_qty}, 실현손익={profit:.2f} ({profit_pct:.2f}%), 사유={position_info.get('exit_signal')}")
                    self.positions.pop(stock_code, None) # 포지션 목록에서 제거
                else: # 부분 체결 진행 중
                    self.add_log(f"⏳ (WS) 매도 부분 체결: [{stock_code}] 누적 {filled_qty}/{original_size}")
                    # 부분 청산 시 size 업데이트 (선택적) - 현재는 완전 청산만 가정
                    # position_info['size'] = original_size - filled_qty

             elif order_status in ['취소', '거부', '확인']: # 주문 종료
                 filled_qty = position_info.get('filled_qty', 0)
                 original_size = position_info.get('original_size_before_exit', position_info.get('size', 0))
                 remaining_size = original_size - filled_qty

                 if remaining_size > 0 : # 부분 체결 후 종료
                      self.add_log(f"⚠️ (WS) 매도 주문({order_no}) 부분 체결({filled_qty}) 후 종료({order_status}). [{stock_code}] {remaining_size}주 잔여.")
                      # 부분 청산 처리: 남은 수량으로 포지션 업데이트하고 IN_POSITION 복귀
                      position_info.update({'size': remaining_size, 'status': 'IN_POSITION', 'order_no': None,
                                           'order_qty': None, 'filled_qty': None, 'filled_value': None,
                                           'original_size_before_exit': None, 'exit_signal': None}) # 임시 필드 제거
                 else: # 전량 미체결 또는 전량 체결 후 종료 상태 수신
                     if filled_qty == original_size: # 전량 체결 후 최종 상태('확인' 등)
                         self.add_log(f"ℹ️ (WS) 매도 주문({order_no}) 전량 체결 후 최종 상태({order_status}) 수신. 포지션 제거.")
                         self.positions.pop(stock_code, None) # 포지션 목록에서 제거
                     else: # 전량 미체결 상태에서 종료
                         self.add_log(f"❌ (WS) 매도 주문({order_no}) 실패/취소: {order_status}. IN_POSITION 복귀.")
                         position_info.update({'status': 'IN_POSITION', 'order_no': None, 'order_qty': None,
                                               'filled_qty': None, 'filled_value': None,
                                               'original_size_before_exit': None, 'exit_signal': None}) # 임시 필드 제거

    except Exception as e:
        self.add_log(f"🚨 주문 체결 처리 오류(_process_execution_update): {e} | Data: {str(exec_data)[:200]}...")
        self.add_log(traceback.format_exc())


  async def _process_balance_update(self, balance_data: Dict):
      """실시간 잔고(TR ID: 04) 데이터 처리."""
      try:
          # 키움 API 문서(p.420) 기준 필드명 사용
          stock_code_raw = balance_data.get('9001')
          current_size_str = balance_data.get('930') # 보유수량
          avg_price_str = balance_data.get('931')    # 매입단가 (평균)

          if not stock_code_raw or current_size_str is None or avg_price_str is None: return

          stock_code = stock_code_raw[1:] if stock_code_raw.startswith('A') else stock_code_raw
          try:
              current_size = int(current_size_str)
              avg_price = float(avg_price_str)
          except ValueError:
              self.add_log(f"⚠️ 잔고 데이터 숫자 변환 오류: size='{current_size_str}', avg_price='{avg_price_str}'")
              return

          position_info = self.positions.get(stock_code)
          current_pos_status = position_info.get('status') if position_info else None

          # [1. 엔진이 관리하는 포지션에 대한 업데이트]
          if position_info and current_pos_status in ['IN_POSITION', 'PENDING_EXIT']:
              pos_size = position_info.get('size')
              # 수량 변경 시 로그 및 업데이트 (평균 단가는 업데이트하지 않음)
              if pos_size is not None and pos_size != current_size:
                  self.add_log(f"🔄 (WS 잔고) 수량 변경 감지: [{stock_code}], 엔진:{pos_size} -> 잔고:{current_size}")
                  position_info['size'] = current_size

              # IN_POSITION 상태에서 잔고가 0이 되면 청산된 것으로 간주 (PENDING_EXIT에서 체결 못받은 경우 대비)
              if current_size == 0 and current_pos_status == 'IN_POSITION':
                  self.add_log(f"ℹ️ (WS 잔고) 잔고 0 확인 ({stock_code}). 포지션 제거.")
                  self.positions.pop(stock_code, None)

          # [2. 상태 불일치 감지 및 처리]
          # 엔진은 포지션 없다고 인식하는데 잔고가 있는 경우 (외부 요인 또는 오류 복구)
          elif not position_info and current_size > 0:
               # 스크리닝 대상이었거나, 과거 보유 이력이 있는 종목인지 등 추가 조건 부여 가능
               self.add_log(f"⚠️ 엔진 상태와 불일치하는 잔고({stock_code}, {current_size}주 @ {avg_price}) 발견. 상태 강제 업데이트.")
               self.positions[stock_code] = {
                   'status': 'IN_POSITION', 'stk_cd': stock_code, 'size': current_size,
                   'entry_price': avg_price, 'entry_time': datetime.now() # 진입 시간은 현재로 기록
               }

          # 엔진은 보유 중으로 아는데 잔고가 0으로 오는 경우 (외부 청산 등)
          elif position_info and current_pos_status == 'IN_POSITION' and current_size == 0:
              self.add_log(f"⚠️ 엔진 상태(IN_POSITION)와 불일치하는 잔고 0 ({stock_code}) 발견. 포지션 강제 제거.")
              self.positions.pop(stock_code, None)

      except Exception as e:
          self.add_log(f"🚨 잔고 처리 오류(_process_balance_update): {e} | Data: {str(balance_data)[:200]}...")
          self.add_log(traceback.format_exc())


  # --- 종목 선정 ---
  async def run_screening(self) -> List[str]:
    """거래 대상 종목 스크리닝 실행하고 종목 코드 리스트 반환"""
    self.add_log("🔍 종목 스크리닝 시작...")
    if not self.api:
      self.add_log("⚠️ API 객체 없음. 스크리닝 불가.")
      return []

    try:
      # 거래량 급증 API 호출 (kiwoom_api.py에 정의된 함수 사용)
      candidate_stocks_raw = await self.api.fetch_volume_surge_stocks(market_type="000") # 예: 전체 시장

      if not candidate_stocks_raw:
        self.add_log("⚠️ 스크리닝 결과 없음.")
        return [] # 빈 리스트 반환

      # 필요한 정보(종목코드, 급증률 등) 추출 및 필터링
      candidate_stocks = []
      for s in candidate_stocks_raw:
          stk_cd_raw = s.get('stk_cd')
          if not stk_cd_raw: continue # 종목 코드 없으면 제외

          # '_AL' 접미사 제거 (SOR 주문 결과로 추정)
          stk_cd = stk_cd_raw[:-3] if stk_cd_raw.endswith('_AL') else stk_cd_raw

          try:
              cur_prc_str = s.get('cur_prc','0').replace('+','').replace('-','')
              sdnin_rt_str = s.get('sdnin_rt', '0.0')
              # 필수 필드가 숫자로 변환 가능한지 확인
              if cur_prc_str and sdnin_rt_str:
                  candidate_stocks.append({
                      'stk_cd': stk_cd,
                      'cur_prc': float(cur_prc_str),
                      'sdnin_rt': float(sdnin_rt_str)
                  })
              else:
                   self.add_log(f"⚠️ 스크리닝 데이터 값 누락 또는 형식 오류: {s}")
          except ValueError:
              self.add_log(f"⚠️ 스크리닝 데이터 숫자 변환 오류: {s}")
              continue # 변환 오류 시 해당 종목 제외

      # 급증률 기준으로 내림차순 정렬
      candidate_stocks.sort(key=lambda x: x['sdnin_rt'], reverse=True)

      # 설정된 최대 개수만큼 종목 코드만 추출
      target_stock_codes = [s['stk_cd'] for s in candidate_stocks[:self.config.strategy.max_target_stocks]]

      if target_stock_codes:
        self.add_log(f"🎯 스크리닝 완료. 후보: {target_stock_codes}")
      else:
        self.add_log("ℹ️ 스크리닝 결과 후보 종목 없음.")

      return target_stock_codes # 종목 코드 리스트 반환

    except Exception as e:
      self.add_log(f"🚨 스크리닝 오류: {e}")
      self.add_log(traceback.format_exc())
      # self.engine_status = 'ERROR' # 스크리닝 오류 시 엔진 상태 변경 고려
      return [] # 오류 시 빈 리스트 반환


  # --- 주기적 작업 (진입/청산 조건 확인) ---
  async def process_all_stocks_tick(self, current_time: datetime):
    """스크리닝된 후보 종목과 보유 종목 전체에 대해 Tick 처리 실행 (동시 실행)"""
    if self.engine_status != 'RUNNING': return

    # 1. 처리 대상 종목 목록 생성 (보유 종목 + 스크리닝 후보 중 미보유)
    stocks_to_process = set(self.positions.keys()) # 현재 보유 종목
    if self.candidate_stock_codes:
        # 스크리닝 후보 중 아직 보유하지 않은 종목 추가
        stocks_to_process.update([code for code in self.candidate_stock_codes if code not in self.positions])

    if not stocks_to_process: return # 처리할 종목 없음

    # self.add_log(f"⚙️ Tick 처리 시작 (대상: {list(stocks_to_process)})")

    # 각 종목별 Tick 처리 비동기 실행
    tasks = []
    # API 요청 간격을 조절하기 위한 변수
    api_call_delay = 0.2 # 예: 각 API 호출 사이에 0.2초 지연 (키움증권 제한 고려)
    processed_count = 0

    for code in list(stocks_to_process): # 순회 중 변경될 수 있으므로 list() 사용
        # 마지막 처리 시간 확인 (예: 60초 간격)
        last_processed = self.last_stock_tick_time.get(code)
        if last_processed is None or (current_time - last_processed).total_seconds() >= 60:
            # API 호출 전에 지연 추가
            if processed_count > 0:
                await asyncio.sleep(api_call_delay)

            tasks.append(self.process_single_stock_tick(code))
            self.last_stock_tick_time[code] = current_time # 처리 시간 업데이트
            processed_count += 1

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True) # 여러 종목 동시 처리
        # 결과 확인 (오류 로깅 등)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # asyncio.gather에서 task를 특정하기 어려우므로, 오류 발생 사실만 로깅
                self.add_log(f"🚨 Tick 처리 중 gather 오류 발생: {result}")
        # self.add_log(f"Tick 처리 완료 ({len(tasks)}개 종목 시도)")


  async def process_single_stock_tick(self, stock_code: str):
    """개별 종목에 대한 데이터 조회, 지표 계산, 신호 확인, 주문 실행"""
    # API 객체가 없거나 엔진이 실행 중 상태가 아니면 함수 종료
    if not self.api or not self.engine_status == 'RUNNING': return

    # 해당 종목의 현재 포지션 정보 가져오기 (없으면 None)
    position_info = self.positions.get(stock_code)
    # 포지션 정보가 있으면 해당 상태 사용, 없으면 'SEARCHING'(탐색) 상태로 간주
    current_status = position_info.get('status') if position_info else 'SEARCHING'

    # PENDING_ENTRY(매수 주문 접수), PENDING_EXIT(매도 주문 접수) 상태에서는
    # 새로운 Tick 처리를 하지 않고 실시간 체결 콜백을 기다림
    if current_status in ['PENDING_ENTRY', 'PENDING_EXIT']:
        # self.add_log(f"⏳ [{stock_code}] 주문 체결 대기 중...") # 로그가 너무 자주 찍히는 것을 방지하기 위해 주석 처리
        return

    try:
      # --- 1. 데이터 조회 및 지표 계산 ---
      # 해당 종목의 최신 1분봉 데이터 조회 요청
      raw_data = await self.api.fetch_minute_chart(stock_code, timeframe=1)
      # 데이터 조회 실패 시 함수 종료
      if not (raw_data and raw_data.get("stk_min_pole_chart_qry")): return

      # 조회된 데이터를 DataFrame으로 변환 및 전처리
      df = preprocess_chart_data(raw_data["stk_min_pole_chart_qry"])
      # 데이터 변환 실패 시 함수 종료
      if df is None or df.empty: return

      # VWAP(거래량가중평균가) 지표 계산 및 DataFrame에 추가
      add_vwap(df)
      # ORB(시가 범위 고가/저가) 계산
      orb_levels = calculate_orb(df, timeframe=self.config.strategy.orb_timeframe)
      # 현재가 (DataFrame의 마지막 종가)
      current_price = df['close'].iloc[-1]
      # 현재 VWAP (DataFrame의 마지막 VWAP 값, 없으면 None)
      current_vwap = df['VWAP'].iloc[-1] if 'VWAP' in df.columns and not pd.isna(df['VWAP'].iloc[-1]) else None

      # --- 2. 상태별 로직 수행 ---

      # [ 상태 1: SEARCHING - 신규 진입 조건 확인 ]
      if current_status == 'SEARCHING':
        # 현재 보유 종목 수가 설정된 최대 보유 가능 종목 수 이상이면 신규 진입 불가
        if len(self.positions) >= self.config.strategy.max_concurrent_positions: return

        # ORB 돌파 매수 신호 확인
        signal = check_breakout_signal(current_price, orb_levels, self.config.strategy.breakout_buffer)
        if signal == "BUY":
          self.add_log(f"🔥 [{stock_code}] 매수 신호! (현재가 {current_price}, ORH {orb_levels.get('orh','N/A')})")

          # --- 👇 주문 수량 결정 로직 시작 👇 ---
          order_quantity = 0
          # 설정 파일에서 종목당 투자 금액 가져오기 (없으면 기본 100만원)
          investment_amount_per_stock = getattr(self.config.strategy, 'investment_amount_per_stock', 1_000_000)

          # 예수금 정보 조회 (API 호출)
          balance_info = await self.api.fetch_account_balance()
          if balance_info:
              # 주문가능현금 ('ord_alowa') 키로 조회 시도
              available_cash_str = balance_info.get('ord_alowa')
              if available_cash_str:
                  try:
                      available_cash = int(available_cash_str)
                      # 주문 가능 현금이 투자 예정 금액보다 많고, 현재가가 0보다 클 때 수량 계산
                      if available_cash >= investment_amount_per_stock and current_price > 0:
                          order_quantity = int(investment_amount_per_stock // current_price) # 정수 수량 계산
                          self.add_log(f"   - 주문 가능 현금: {available_cash}, 투자 예정: {investment_amount_per_stock}, 계산된 수량: {order_quantity}주")
                      else:
                           self.add_log(f"   - 주문 불가: 현금 부족 또는 현재가 오류 (가능현금: {available_cash}, 현재가: {current_price})")
                  except ValueError:
                       self.add_log(f"   - 주문 불가: 주문 가능 현금 파싱 오류 ({available_cash_str})")
              else:
                  self.add_log(f"   - 주문 불가: API 응답에서 주문 가능 현금('ord_alowa') 찾을 수 없음")
          else:
               self.add_log("   - 주문 불가: 예수금 정보 조회 실패")
          # --- 👆 주문 수량 결정 로직 끝 👆 ---

          # 계산된 주문 수량이 0보다 크면 매수 주문 실행
          if order_quantity > 0:
            order_result = await self.api.create_buy_order(stock_code, quantity=order_quantity)
            # 주문 요청 성공 시
            if order_result and order_result.get('return_code') == 0:
              # 포지션 상태를 PENDING_ENTRY(매수 주문 접수)로 변경하고 체결 대기
              self.positions[stock_code] = {
                  'status': 'PENDING_ENTRY', # 상태 변경
                  'order_no': order_result.get('ord_no'), # 접수된 주문 번호 저장
                  'order_qty': order_quantity,      # 주문 수량
                  'order_price': current_price,     # 주문 시점 가격 (참고용)
                  'stk_cd': stock_code,           # 종목 코드
                  'filled_qty': 0,                # 체결 수량 (초기화)
                  'filled_value': 0.0             # 체결 금액 합계 (초기화)
              }
              self.add_log(f"➡️ [{stock_code}] 매수 주문 접수: {order_result.get('ord_no')}")
            # 주문 요청 실패 시
            else:
                self.add_log(f"❌ [{stock_code}] 매수 주문 실패: {order_result}")
                # 실패 시 포지션 상태 변경 없이 다음 Tick에서 재시도 (또는 다른 처리)
          # 주문 수량이 0이면 실행 안 함
          else:
               self.add_log(f"   - [{stock_code}] 주문 수량이 0이므로 매수 주문 실행 안 함.")

      # [ 상태 2: IN_POSITION - 청산 조건 확인 ]
      elif current_status == 'IN_POSITION' and position_info: # 포지션 정보가 있을 때만 실행
        # 익절/손절 조건 확인 (risk_manager.py의 함수 호출)
        signal = manage_position(position_info, current_price, current_vwap)
        # 청산 신호("TAKE_PROFIT", "STOP_LOSS", "VWAP_STOP_LOSS" 등)가 발생했을 경우
        if signal:
          log_prefix = "💰" if signal == "TAKE_PROFIT" else "🛑" # 로그 아이콘 설정
          self.add_log(f"{log_prefix} 청산 신호({signal})! [{stock_code}] 매도 주문 실행 (현재가 {current_price}).")
          # 보유 수량 가져오기
          order_quantity = position_info.get('size', 0)
          # 보유 수량이 0보다 크면 매도 주문 실행
          if order_quantity > 0:
            order_result = await self.api.create_sell_order(stock_code, quantity=order_quantity)
            # 주문 요청 성공 시
            if order_result and order_result.get('return_code') == 0:
              # 포지션 상태를 PENDING_EXIT(매도 주문 접수)로 변경하고 체결 대기
              self.positions[stock_code].update({
                  'status': 'PENDING_EXIT', # 상태 변경
                  'order_no': order_result.get('ord_no'), # 접수된 주문 번호 저장
                  'order_qty': order_quantity,      # 주문 수량
                  'exit_signal': signal,            # 청산 사유 기록
                  'filled_qty': 0,                # 체결 수량 (초기화)
                  'filled_value': 0.0,            # 체결 금액 합계 (초기화)
                  'original_size_before_exit': order_quantity # 부분 체결 관리를 위해 원래 수량 기록
              })
              self.add_log(f"⬅️ [{stock_code}] 매도 주문 접수: {order_result.get('ord_no')}")
            # 주문 요청 실패 시
            else:
                self.add_log(f"❌ [{stock_code}] 매도 주문 실패: {order_result}")
                # 실패 시 포지션 상태 변경 없이 다음 Tick에서 재시도 (또는 다른 처리)

    # Tick 처리 중 발생한 예외 처리
    except Exception as e:
      self.add_log(f"🚨 Tick 처리 중 오류 발생 ({stock_code}): {e}")
      self.add_log(traceback.format_exc()) # 상세 오류 로그 출력
      # 오류 발생 시 해당 종목을 포지션 목록에서 제거하거나 상태를 ERROR로 변경하는 등의 처리 추가 가능
      # 예: self.positions.pop(stock_code, None)

  # --- Kill Switch ---
  async def execute_kill_switch(self):
    """긴급 정지: 미체결 주문 취소 및 모든 보유 포지션 시장가 청산"""
    if self.engine_status in ['STOPPING', 'STOPPED', 'KILLED']: return # 이미 종료 중이거나 완료됨
    self.add_log("🚨 KILL SWITCH 발동!")
    if not self.api: self.add_log("⚠️ API 객체 없음."); return

    original_engine_status = self.engine_status
    self.engine_status = 'STOPPING' # 상태 변경 (긴급 정지 중)

    try:
        # 1. 미체결 주문 취소 (PENDING 상태인 모든 종목)
        pending_orders = []
        for code, pos in list(self.positions.items()): # 순회 중 변경될 수 있으므로 list 사용
            if pos.get('status') in ['PENDING_ENTRY', 'PENDING_EXIT'] and pos.get('order_no'):
                pending_orders.append({'code': code, 'order_no': pos['order_no']})

        if pending_orders:
            self.add_log(f"  - 미체결 주문 {len(pending_orders)}건 취소 시도...")
            cancel_tasks = [self.api.cancel_order(order['order_no'], order['code'], 0) for order in pending_orders]
            cancel_results = await asyncio.gather(*cancel_tasks, return_exceptions=True)
            for i, result in enumerate(cancel_results):
                order = pending_orders[i]
                if isinstance(result, Exception) or (result and result.get('return_code') != 0):
                    self.add_log(f"  ⚠️ 주문({order['order_no']}/{order['code']}) 취소 실패/오류: {result}")
                    # 취소 실패 시 해당 포지션 상태를 ERROR로 변경하거나 재시도 로직 필요
                else:
                     self.add_log(f"  ✅ 주문({order['order_no']}/{order['code']}) 취소 성공/요청됨.")
                     # 취소 성공 시 해당 포지션 상태를 SEARCHING 또는 IN_POSITION(부분체결 후 취소)으로 되돌림
                     if order['code'] in self.positions:
                         pos_info = self.positions[order['code']]
                         if pos_info['status'] == 'PENDING_ENTRY':
                             if pos_info.get('filled_qty', 0) > 0: # 부분 체결 후 취소
                                 entry_price = pos_info.get('filled_value', 0) / pos_info['filled_qty']
                                 pos_info.update({'status': 'IN_POSITION', 'entry_price': entry_price, 'size': pos_info['filled_qty'], 'order_no': None})
                             else: # 완전 미체결 후 취소
                                 self.positions.pop(order['code'], None) # 포지션 제거
                         elif pos_info['status'] == 'PENDING_EXIT':
                             # 매도 취소 시 IN_POSITION 복귀
                             pos_info.update({'status': 'IN_POSITION', 'order_no': None})

            await asyncio.sleep(2.0) # 취소 처리 및 잔고 반영 대기 시간 증가

        # 2. 보유 포지션 시장가 청산 (IN_POSITION 또는 PENDING_EXIT 실패 후 남은 잔량)
        positions_to_liquidate = []
        for code, pos in list(self.positions.items()): # 순회 중 삭제될 수 있으므로 list() 사용
             # IN_POSITION 상태이거나, 취소 실패/부분체결 등으로 size가 남은 경우
             if pos.get('size', 0) > 0 and pos.get('status') != 'PENDING_ENTRY': # PENDING_ENTRY는 아직 주식 없음
                 positions_to_liquidate.append({'code': code, 'qty': pos['size']})

        if positions_to_liquidate:
            self.add_log(f"  - 보유 포지션 {len(positions_to_liquidate)}건 시장가 청산 시도...")
            sell_tasks = [self.api.create_sell_order(pos['code'], quantity=pos['qty']) for pos in positions_to_liquidate]
            sell_results = await asyncio.gather(*sell_tasks, return_exceptions=True)
            for i, result in enumerate(sell_results):
                pos_info = positions_to_liquidate[i]
                if isinstance(result, Exception) or (result and result.get('return_code') != 0):
                    self.add_log(f"  ❌ 시장가 청산 실패 ({pos_info['code']} {pos_info['qty']}주): {result}")
                    # 실패 시 ERROR 상태로 남겨둘지 결정 필요
                    if pos_info['code'] in self.positions: self.positions[pos_info['code']]['status'] = 'ERROR_LIQUIDATION'
                else:
                    self.add_log(f"  ✅ 시장가 청산 주문 접수 ({pos_info['code']} {pos_info['qty']}주): {result.get('ord_no')}")
                    # 청산 주문 접수 성공 시, 상태를 PENDING_EXIT으로 변경하여 체결 확인 유도
                    if pos_info['code'] in self.positions:
                        self.positions[pos_info['code']]['status'] = 'PENDING_EXIT'
                        self.positions[pos_info['code']]['order_no'] = result.get('ord_no')
                        self.positions[pos_info['code']]['exit_signal'] = 'KILL_SWITCH'
                        self.positions[pos_info['code']]['original_size_before_exit'] = pos_info['qty'] # 청산 시작 수량 기록
        else:
            self.add_log("  - 청산할 보유 포지션 없음.")

        self.add_log("🚨 Kill Switch 처리 완료. 엔진 종료됩니다.")
        await self.stop() # 메인 루프 종료 신호

    except Exception as e:
        self.add_log(f"🚨 Kill Switch 처리 중 심각한 오류: {e}")
        self.add_log(traceback.format_exc())
        self.engine_status = 'ERROR' # 오류 발생 시 상태 변경
        await self.stop() # 오류 시에도 종료 시도
    finally:
         # Kill Switch가 성공적으로 모든 포지션을 청산했는지 여부와 관계없이 Killed 상태로 변경
         self.engine_status = 'KILLED'