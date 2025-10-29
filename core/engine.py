import asyncio
import pandas as pd
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
# indicators.py 에서 add_vwap, calculate_orb 임포트
from data.indicators import add_vwap, calculate_orb, add_ema, calculate_rvol, calculate_obi
# 전략 관련 import
from strategy.momentum_orb import check_breakout_signal
from strategy.risk_manager import manage_position

class TradingEngine:
  """웹소켓 기반 실시간 다중 종목 트레이딩 로직 관장 엔진"""
  def __init__(self):
    self.config = config # 전역 config 객체 사용
    self.positions: Dict[str, Dict] = {} # {'종목코드': {'stk_cd': ..., 'entry_price': ..., 'size': ..., 'status': 'IN_POSITION'|'PENDING_ENTRY'|'PENDING_EXIT', 'order_no': ..., ...}}
    self.logs: List[str] = [] # 최근 로그 저장 (UI 표시용)
    self.api: Optional[KiwoomAPI] = None # KiwoomAPI 인스턴스
    self._stop_event = asyncio.Event() # 엔진 종료 제어 이벤트
    # 스크리닝 주기 설정 가져오기 (strategy 섹션에서)
    screening_interval_minutes = getattr(self.config.strategy, 'screening_interval_minutes', 5)
    # 마지막 스크리닝 시간 (시작 시 즉시 실행되도록 과거 시간으로 초기화)
    self.last_screening_time = datetime.now() - timedelta(minutes=screening_interval_minutes + 1)
    self.candidate_stock_codes: List[str] = [] # 스크리닝 결과 (종목 코드 리스트)
    self.candidate_stocks_info: List[Dict[str, str]] = [] # 스크리닝 결과 (코드+이름 딕셔너리 리스트)
    # --- 실시간 데이터 저장소 및 구독 관리 ---
    self.realtime_data: Dict[str, Dict] = {} # {'종목코드': {'last_price': ..., 'ask1': ..., 'bid1': ..., 'timestamp': ...}}
    self.subscribed_codes: Set[str] = set() # 실시간 구독 중인 종목 코드 집합
    # --- 추가 끝 ---
    self.engine_status = 'STOPPED' # 엔진 상태 (STOPPED, INITIALIZING, RUNNING, STOPPING, ERROR, KILLED)
    self.last_stock_tick_time: Dict[str, datetime] = {} # 종목별 마지막 Tick 처리 시간
    self._realtime_registered = False # 기본 실시간 TR(00, 04) 등록 완료 여부 플래그
    self._start_lock = asyncio.Lock() # 엔진 시작/종료 동시성 제어 Lock

  def add_log(self, message: str):
    """로그 메시지를 리스트에 추가하고 터미널에도 출력"""
    log_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(log_msg) # 터미널 출력
    self.logs.insert(0, log_msg) # 리스트 맨 앞에 추가 (최신 로그가 위로)
    if len(self.logs) > 100: # 로그 최대 개수 제한 (예: 100개)
        self.logs.pop() # 가장 오래된 로그 제거

  async def process_tick(self):
    """매 틱마다 API 인스턴스를 생성하고 작업을 수행한 후 정리합니다."""
    self.add_log("새로운 틱 처리 시작")

    # ⭐️ 매 틱마다 새로운 API 클라이언트 생성
    api = KiwoomAPI()
    try:
      # 1. 데이터 수집 및 가공
      # 대상 종목의 1분봉 데이터를 가져옵니다.
      raw_data = await api.fetch_minute_chart(self.target_stock, timeframe=1)
      if not (raw_data and raw_data.get("stk_min_pole_chart_qry")):
        self.add_log("❗️ 데이터를 가져오지 못했습니다.")
        return # 데이터 없으면 처리 중단

      # API 응답 데이터를 DataFrame으로 변환하고 전처리합니다.
      df = preprocess_chart_data(raw_data["stk_min_pole_chart_qry"])
      if df is None or df.empty:
        self.add_log("❗️ 데이터프레임 변환에 실패했습니다.")
        return # DataFrame 변환 실패 시 처리 중단

      # 2. 보조지표 계산
      add_vwap(df) # VWAP 계산 및 추가
      # --- EMA 계산 로직 추가 ---
      add_ema(df) # EMA(9, 20) 계산 및 추가
      # --------------------------
      orb_levels = calculate_orb(df, timeframe=config.strategy.orb_timeframe) # ORB 계산
      current_price = df['close'].iloc[-1] # 현재가 추출

      # 로그에 주요 정보 출력
      # EMA 값도 로그에 추가 (옵션) - 마지막 EMA 값 확인
      ema9 = df['EMA_9'].iloc[-1] if 'EMA_9' in df.columns else 'N/A'
      ema20 = df['EMA_20'].iloc[-1] if 'EMA_20' in df.columns else 'N/A'
      self.add_log(f"현재가: {current_price}, ORH: {orb_levels['orh']}, ORL: {orb_levels['orl']}, EMA9: {ema9:.2f}, EMA20: {ema20:.2f}")

      # 3. 포지션 상태에 따른 의사결정
      if not self.position: # 현재 포지션이 없으면
        # 매수 신호 확인
        signal = check_breakout_signal(
          current_price, orb_levels, config.strategy.breakout_buffer
        )
        if signal == "BUY":
          self.add_log("🔥 매수 신호 포착! 주문을 실행합니다.")
          # 시장가 매수 주문 실행 (예: 1주)
          order_result = await api.create_buy_order(self.target_stock, quantity=1)
          if order_result and order_result.get('return_code') == 0:
            # 주문 성공 시 포지션 상태 업데이트
            self.position = {
              'stk_cd': self.target_stock,
              'entry_price': current_price,
              'size': 1,
              'order_no': order_result.get('ord_no') # 주문 번호 저장 (필요시)
            }
            self.add_log(f"➡️ 포지션 진입 완료: {self.position}")
          # else: # 주문 실패 처리 (필요시 로그 추가 등)
          #   self.add_log(f"❌ 매수 주문 실패: {order_result}")

      else: # 현재 포지션이 있으면
        # 익절 또는 손절 신호 확인
        signal = manage_position(self.position, current_price)
        if signal in ["TAKE_PROFIT", "STOP_LOSS"]:
          self.add_log(f"🎉 {signal} 조건 충족! 매도 주문을 실행합니다.")
          # 시장가 매도 주문 실행 (보유 수량만큼)
          order_result = await api.create_sell_order(self.target_stock, self.position.get('size', 0))
          if order_result and order_result.get('return_code') == 0:
            # 주문 성공 시 포지션 상태 초기화
            self.add_log(f"⬅️ 포지션 청산 완료: {self.position}")
            self.position = {} # 포지션 없음 상태로 변경
          # else: # 주문 실패 처리
          #   self.add_log(f"❌ 매도 주문 실패: {order_result}")

    finally:
      # ⭐️ 작업이 끝나면 항상 API 클라이언트를 닫아줍니다.
      await api.close()
      self.add_log("틱 처리 완료 및 API 연결 종료")

  async def start(self):
    """엔진 시작: API 인스턴스 생성, 웹소켓 연결, 메인 루프 실행"""
    async with self._start_lock: # 시작 로직 Lock
        if self.engine_status in ['INITIALIZING', 'RUNNING']:
            self.add_log(f"⚠️ [START] 엔진 이미 '{self.engine_status}' 상태. 추가 시작 요청 무시.")
            return

        self.engine_status = 'INITIALIZING'
        self._realtime_registered = False # 플래그 초기화
        self._stop_event.clear() # 종료 이벤트 초기화
        self.add_log("🚀 엔진 시작...")

        # 기존 자원 정리 (재시작 시)
        if self.api: await self.api.close()
        self.api = KiwoomAPI() # 새 API 인스턴스 생성

    # --- Lock 외부에서 API 연결 및 TR 등록 시도 (네트워크 I/O) ---
    try:
        self.add_log("  -> [START] 웹소켓 연결 시도...")
        # 웹소켓 연결 시 handle_realtime_data 콜백 함수 전달
        connected = await self.api.connect_websocket(self.handle_realtime_data)
        if not connected:
            self.add_log("❌ [START] 웹소켓 연결 실패. 엔진 시작 불가.")
            self.engine_status = 'ERROR'
            await self.shutdown(); return # 즉시 종료

        # --- 기본 TR(00, 04) 등록 성공 응답 대기 ---
        try:
            self.add_log("⏳ [START] 기본 실시간 TR('00', '04') 등록 응답 대기 중...")
            # _wait_for_registration 함수가 플래그 상태를 확인하며 대기
            await asyncio.wait_for(self._wait_for_registration(), timeout=10.0)
            self.add_log("✅ [START] 기본 실시간 TR 등록 응답 수신 확인 완료.")
        except asyncio.TimeoutError:
            self.add_log("🚨 [START] 기본 실시간 TR 등록 응답 시간 초과. 엔진 시작 실패.")
            if self.api: await self.api.disconnect_websocket() # 웹소켓 연결 해제
            self.engine_status = 'ERROR'
            await self.shutdown(); return # 즉시 종료
        # --- TR 등록 대기 끝 ---

        # --- 메인 루프 시작 ---
        self.engine_status = 'RUNNING'
        self.add_log("✅ 메인 루프 시작.")
        self.candidate_stock_codes = [] # 후보 목록 초기화 (시작 시)
        self.candidate_stocks_info = []

        while not self._stop_event.is_set(): # 종료 이벤트 발생 시 루프 탈출
            if self.engine_status != 'RUNNING':
                self.add_log(f"⚠️ [LOOP] 엔진 상태({self.engine_status}) 변경 감지. 메인 루프 중단.")
                break # 에러/종료 상태면 루프 중단

            current_time = datetime.now()

            # --- 스크리닝 로직 ---
            screening_interval_minutes = getattr(self.config.strategy, 'screening_interval_minutes', 5)
            screening_interval = screening_interval_minutes * 60
            should_screen = (current_time - self.last_screening_time).total_seconds() >= screening_interval
            max_positions = getattr(self.config.strategy, 'max_concurrent_positions', 3) # 설정 파일 값 사용

            # 현재 포지션 수가 최대치 미만이고 스크리닝 시간이 되었을 때 실행
            if len(self.positions) < max_positions and should_screen:
                self.add_log("  -> [LOOP] 스크리닝 실행 시작...")
                new_candidates = await self.run_screening() # 종목 코드 리스트 반환
                self.last_screening_time = current_time # 마지막 스크리닝 시간 업데이트
                self.add_log("  <- [LOOP] 스크리닝 실행 완료.")
                # 스크리닝 결과로 실시간 구독 업데이트
                await self._update_realtime_subscriptions(new_candidates)

            # --- Tick 처리 로직 ---
            # 스크리닝된 후보 종목 + 현재 보유 종목에 대해 Tick 처리 시도
            await self.process_all_stocks_tick(current_time)

            await asyncio.sleep(1) # 메인 루프 주기 (1초)

        self.add_log("✅ 메인 루프 정상 종료됨 (stop_event 설정).")

    except asyncio.CancelledError:
        self.add_log("ℹ️ 엔진 메인 루프 강제 취소됨.")
        self.engine_status = 'STOPPED' # 상태 변경
    except Exception as e:
        self.add_log(f"🚨🚨🚨 [CRITICAL] 엔진 메인 루프에서 처리되지 않은 심각한 예외 발생: {e} 🚨🚨🚨")
        self.add_log(traceback.format_exc())
        self.engine_status = 'ERROR' # 에러 상태로 변경
    finally:
        self.add_log("🚪 [FINALLY] 엔진 종료 처리 시작...")
        async with self._start_lock: # 종료도 Lock 사용 (시작 로직과의 충돌 방지)
            await self.shutdown() # 자원 정리 함수 호출

  async def _wait_for_registration(self):
      """_realtime_registered 플래그가 True가 되거나 엔진 상태가 ERROR가 될 때까지 대기"""
      while not self._realtime_registered and self.engine_status != 'ERROR':
          await asyncio.sleep(0.1) # 짧은 간격으로 상태 확인
      # 루프 탈출 조건 확인
      if not self._realtime_registered and self.engine_status != 'ERROR':
          # 정상 종료되었는데 플래그가 설정되지 않은 경우 (Timeout 직전)
          self.add_log("   -> _wait_for_registration: Timeout 직전, 등록 플래그 False 감지.")
          raise asyncio.TimeoutError("Registration flag was not set by callback")
      elif self.engine_status == 'ERROR':
          self.add_log("   -> _wait_for_registration: 엔진 에러 상태 감지 -> 대기 중단")
      elif self._realtime_registered:
          self.add_log("   -> _wait_for_registration: 등록 플래그 True 확인 -> 대기 종료")

  async def stop(self):
    """엔진 종료 신호 설정 (메인 루프 중단 요청)"""
    if self.engine_status not in ['STOPPING', 'STOPPED', 'KILLED']:
        self.add_log("🛑 엔진 종료 신호 수신...")
        self._stop_event.set() # 메인 루프 종료 이벤트 설정

  async def shutdown(self):
      """엔진 관련 자원(API 연결 등) 정리"""
      if self.engine_status in ['STOPPED', 'KILLED']: return # 이미 종료/킬 상태면 중복 실행 방지
      if self.engine_status != 'STOPPING': # stop()을 거치지 않고 직접 호출될 경우 대비
        self.add_log("🛑 엔진 종료(Shutdown) 절차 시작...")
        self.engine_status = 'STOPPING'

      self._stop_event.set() # 확실히 종료 이벤트 설정

      # --- 종료 시 모든 실시간 구독 해지 ---
      if self.api and self.subscribed_codes:
          codes_to_unregister = list(self.subscribed_codes)
          if codes_to_unregister:
              # 종목당 '0B', '0D' 두 개 TR 해지
              tr_ids = ['0B', '0D'] * len(codes_to_unregister)
              tr_keys = [code for code in codes_to_unregister for _ in range(2)]
              self.add_log(f"  -> [SHUTDOWN] 실시간 구독 해지 시도: {codes_to_unregister}")
              await self.api.unregister_realtime(tr_ids=tr_ids, tr_keys=tr_keys)
              self.subscribed_codes.clear() # 해지 후 목록 비우기

      # --- API 객체 종료 ---
      if self.api:
          self.add_log("  -> [SHUTDOWN] API 자원 해제 시도...")
          await self.api.close() # 웹소켓 및 HTTP 클라이언트 종료
          self.add_log("  <- [SHUTDOWN] API 자원 해제 완료.")
          self.api = None # 참조 제거

      self._realtime_registered = False # 플래그 초기화
      self.engine_status = 'STOPPED' # 최종 상태 변경
      self.add_log("🛑 엔진 종료 완료.")

  async def _update_realtime_subscriptions(self, target_codes: List[str]):
      """스크리닝 결과를 바탕으로 실시간 데이터 구독/해지 관리"""
      if not self.api: return # API 객체 없으면 종료
      current_subscribed = self.subscribed_codes
      target_set = set(target_codes) # 스크리닝 결과 (종목 코드 Set)

      # 새로 구독해야 할 종목 (타겟 O, 현재 구독 X)
      to_subscribe = target_set - current_subscribed
      # 더 이상 필요 없는 종목 (타겟 X, 현재 구독 O, 보유 X)
      # 보유 중인 종목은 청산 시 해지되므로 여기서는 제외
      to_unsubscribe = current_subscribed - target_set - set(self.positions.keys())

      # 신규 구독 요청
      if to_subscribe:
          sub_list = list(to_subscribe)
          tr_ids = ['0B', '0D'] * len(sub_list)
          tr_keys = [code for code in sub_list for _ in range(2)]
          self.add_log(f"  ➕ [WS_SUB] 실시간 구독 추가: {sub_list}")
          await self.api.register_realtime(tr_ids=tr_ids, tr_keys=tr_keys)
          self.subscribed_codes.update(to_subscribe) # 구독 목록 업데이트

      # 구독 해지 요청
      if to_unsubscribe:
          unsub_list = list(to_unsubscribe)
          tr_ids = ['0B', '0D'] * len(unsub_list)
          tr_keys = [code for code in unsub_list for _ in range(2)]
          self.add_log(f"  ➖ [WS_SUB] 실시간 구독 해지: {unsub_list}")
          await self.api.unregister_realtime(tr_ids=tr_ids, tr_keys=tr_keys)
          self.subscribed_codes.difference_update(to_unsubscribe) # 구독 목록 업데이트

  def handle_realtime_data(self, ws_data: Dict):
      """웹소켓으로부터 실시간 데이터를 받아 해당 처리 함수 호출 (콜백)"""
      try:
          trnm = ws_data.get('trnm')
          # 실시간 데이터 상세 로깅 (필요시 주석 해제)
          # self.add_log(f"   [INFO] handle_realtime_data 수신: trnm='{trnm}', data='{str(ws_data)[:150]}...'")

          if trnm == 'REAL':
              data_type = ws_data.get('type')
              values = ws_data.get('values')
              # --- 👇 item_code_raw 정의 수정 ---
              item_code_raw = ws_data.get('item', '') # 키움은 종목코드에 'A' prefix 붙임, 없으면 빈 문자열
              # --- 👆 수정 끝 ---

              if not data_type or not values: # item_code_raw는 00, 04의 경우 없을 수 있음
                  self.add_log(f"  ⚠️ [WS_REAL] 실시간 데이터 항목 형식 오류 (type/values 누락): {ws_data}")
                  return

              # --- 종목 코드 정제 (item_code_raw가 있을 때만) ---
              stock_code = None
              if item_code_raw:
                  stock_code = item_code_raw[1:] if item_code_raw.startswith('A') else item_code_raw
                  if stock_code.endswith(('_NX', '_AL')): stock_code = stock_code[:-3]
              # --- 정제 끝 ---

              # 데이터 타입별 처리 함수 호출 (비동기 Task 생성)
              if data_type == '0B': # 주식 체결
                  if stock_code: # 종목 코드가 있을 때만 처리
                      asyncio.create_task(self._process_realtime_execution(stock_code, values))
              elif data_type == '0D': # 주식 호가 잔량
                  if stock_code:
                      asyncio.create_task(self._process_realtime_orderbook(stock_code, values))
              elif data_type == '00': # 주문 체결 응답
                  asyncio.create_task(self._process_execution_update(values))
              elif data_type == '04': # 잔고 업데이트
                  if stock_code: # 잔고 업데이트도 stock_code 필요
                      asyncio.create_task(self._process_balance_update(stock_code, values)) # stock_code 전달

          elif trnm in ['REG', 'REMOVE']: # 등록/해지 응답 처리
              return_code_raw = ws_data.get('return_code')
              return_msg = ws_data.get('return_msg', '메시지 없음')
              try: return_code = int(str(return_code_raw).strip())
              except: return_code = -1 # 변환 실패 시 오류 코드로 간주
              self.add_log(f"📬 WS 응답 ({trnm}): code={return_code}, msg='{return_msg}'")

              # 기본 TR 등록 성공/실패 플래그 처리 (start 함수 대기용)
              if trnm == 'REG' and not self._realtime_registered:
                  if return_code == 0:
                      self._realtime_registered = True
                      self.add_log("✅ [START] 웹소켓 기본 TR 등록 **성공** 확인 (플래그 설정).")
                  else:
                      self.add_log(f"🚨🚨🚨 [CRITICAL] 기본 TR 등록 실패 응답 수신! (code={return_code}). 엔진 에러 상태로 변경.")
                      self.engine_status = 'ERROR' # 에러 상태로 변경하여 start 대기 해제

          # LOGIN, PING, PONG 등은 api.py에서 처리하거나 여기서 무시
          elif trnm not in ['LOGIN', 'PING', 'PONG', 'SYSTEM']:
             self.add_log(f"ℹ️ 처리되지 않은 WS 메시지 수신 (TRNM: {trnm}): {str(ws_data)[:200]}...")

      except Exception as e:
          self.add_log(f"🚨🚨🚨 [CRITICAL] 실시간 데이터 처리 콜백 함수 오류: {e} | Data: {str(ws_data)[:200]}... 🚨🚨🚨")
          self.add_log(traceback.format_exc()) # 상세 오류 스택 출력

  async def _process_realtime_execution(self, stock_code: str, values: Dict):
      """실시간 체결(0B) 데이터 처리 및 저장 (비동기)"""
      try:
          exec_time_str = values.get('20') # 체결시간 (HHMMSS)
          last_price_str = values.get('10') # 현재가 (부호포함)
          exec_vol_str = values.get('15') # 거래량 (부호포함, +:매수체결, -:매도체결)

          if not exec_time_str or not last_price_str or not exec_vol_str:
              self.add_log(f"  ⚠️ [RT_EXEC] 실시간 체결({stock_code}) 데이터 누락: {values}")
              return

          last_price = float(last_price_str.replace('+','').replace('-','').strip())
          exec_vol_signed = int(exec_vol_str.strip())

          if stock_code not in self.realtime_data: self.realtime_data[stock_code] = {}
          self.realtime_data[stock_code].update({
              'last_price': last_price,
              'last_exec_time': exec_time_str,
              'last_exec_volume': abs(exec_vol_signed),
              'last_exec_side': 'BUY' if exec_vol_signed > 0 else ('SELL' if exec_vol_signed < 0 else 'UNKNOWN'),
              'timestamp': datetime.now()
          })
      except ValueError:
          self.add_log(f"  ⚠️ [RT_EXEC] 실시간 체결({stock_code}) 숫자 변환 오류: price='{last_price_str}', vol='{exec_vol_str}'")
      except Exception as e:
          self.add_log(f"  🚨 [RT_EXEC] 실시간 체결({stock_code}) 처리 오류: {e}")

  async def _process_realtime_orderbook(self, stock_code: str, values: Dict):
      """실시간 호가(0D) 데이터 처리 및 저장 (비동기)"""
      try:
          ask1_str = values.get('41'); bid1_str = values.get('51')
          ask1_vol_str = values.get('61'); bid1_vol_str = values.get('71')
          total_ask_vol_str = values.get('121'); total_bid_vol_str = values.get('125')

          if not ask1_str or not bid1_str:
              self.add_log(f"  ⚠️ [RT_OB] 실시간 호가({stock_code}) 데이터 누락 (ask1/bid1): {values}")
              return

          ask1 = float(ask1_str.replace('+','').replace('-','').strip())
          bid1 = float(bid1_str.replace('+','').replace('-','').strip())
          ask1_vol = int(ask1_vol_str.strip()) if ask1_vol_str and ask1_vol_str.strip().isdigit() else 0
          bid1_vol = int(bid1_vol_str.strip()) if bid1_vol_str and bid1_vol_str.strip().isdigit() else 0
          total_ask_vol = int(total_ask_vol_str.strip()) if total_ask_vol_str and total_ask_vol_str.strip().isdigit() else 0
          total_bid_vol = int(total_bid_vol_str.strip()) if total_bid_vol_str and total_bid_vol_str.strip().isdigit() else 0

          if stock_code not in self.realtime_data: self.realtime_data[stock_code] = {}
          self.realtime_data[stock_code].update({
              'ask1': ask1, 'bid1': bid1,
              'ask1_vol': ask1_vol, 'bid1_vol': bid1_vol,
              'total_ask_vol': total_ask_vol, 'total_bid_vol': total_bid_vol,
              'timestamp': datetime.now()
          })
      except ValueError:
          self.add_log(f"  ⚠️ [RT_OB] 실시간 호가({stock_code}) 숫자 변환 오류: ask='{ask1_str}', bid='{bid1_str}', ask_vol='{ask1_vol_str}', bid_vol='{bid1_vol_str}'")
      except Exception as e:
          self.add_log(f"  🚨 [RT_OB] 실시간 호가({stock_code}) 처리 오류: {e}")

  async def _process_execution_update(self, exec_data: Dict):
      """실시간 주문 체결(TR ID: 00) 데이터 처리 (비동기)"""
      try:
          account_no = exec_data.get('9201', '').strip()
          order_no = exec_data.get('9203', '').strip()
          # --- 👇 stock_code_raw 정의 ---
          stock_code_raw = exec_data.get('9001', '').strip() # 값 가져오기
          # --- 👆 정의 끝 ---
          order_status = exec_data.get('913', '').strip()
          exec_qty_str = exec_data.get('911', '').strip()
          exec_price_str = exec_data.get('910', '').strip()
          unfilled_qty_str = exec_data.get('902', '').strip()
          order_side = exec_data.get('905', '').strip()

          if not account_no or not order_no or not stock_code_raw:
              self.add_log(f"  ⚠️ [EXEC_UPDATE] 주문 체결 데이터 누락 (계좌/주문번호/종목코드): {exec_data}")
              return

          stock_code = stock_code_raw[1:] if stock_code_raw.startswith('A') else stock_code_raw
          if stock_code.endswith(('_NX', '_AL')): stock_code = stock_code[:-3]

          try: exec_qty = int(exec_qty_str) if exec_qty_str else 0
          except ValueError: exec_qty = 0
          try: exec_price = float(exec_price_str) if exec_price_str else 0.0
          except ValueError: exec_price = 0.0
          try: unfilled_qty = int(unfilled_qty_str) if unfilled_qty_str else 0
          except ValueError: unfilled_qty = 0

          self.add_log(f"   ⚡️ [EXEC_UPDATE] 주문({order_no}) 상태={order_status}, 종목={stock_code}, 체결량={exec_qty}, 체결가={exec_price}, 미체결량={unfilled_qty}")

          position_info = None
          target_code_for_pos = None
          for code, pos in self.positions.items():
              if pos.get('order_no') == order_no:
                  position_info = pos
                  target_code_for_pos = code
                  break

          if not position_info and stock_code in self.positions:
              pos = self.positions[stock_code]
              if pos.get('status') in ['IN_POSITION', 'ERROR_LIQUIDATION']:
                   self.add_log(f"  ⚠️ [EXEC_UPDATE] 주문({order_no})의 포지션 정보 없음. 종목코드({stock_code})로 IN_POSITION 포지션 찾음. 청산 체결일 수 있음.")
                   position_info = pos
                   target_code_for_pos = stock_code

          if not position_info or not target_code_for_pos:
              return

          current_pos_status = position_info.get('status')

          if current_pos_status == 'PENDING_ENTRY':
              if order_status == '체결' and exec_qty > 0 and exec_price > 0:
                  filled_qty = position_info.get('filled_qty', 0) + exec_qty
                  filled_value = position_info.get('filled_value', 0) + (exec_qty * exec_price)
                  position_info['filled_qty'] = filled_qty
                  position_info['filled_value'] = filled_value

                  if unfilled_qty == 0:
                      entry_price = filled_value / filled_qty if filled_qty > 0 else 0
                      position_info.update({
                          'status': 'IN_POSITION',
                          'entry_price': entry_price,
                          'entry_time': datetime.now(),
                          'size': filled_qty,
                      })
                      self.add_log(f"   ✅ [EXEC_UPDATE] 매수 완전 체결: [{target_code_for_pos}] 진입가={entry_price:.2f}, 수량={filled_qty}")
                  else:
                      self.add_log(f"   ⏳ [EXEC_UPDATE] 매수 부분 체결: [{target_code_for_pos}] 누적 {filled_qty}/{position_info.get('original_order_qty', '?')}")

              elif order_status in ['취소', '거부', '확인']:
                  self.add_log(f"   ❌ [EXEC_UPDATE] 매수 주문 실패/취소 ({order_status}): [{target_code_for_pos}] 주문번호 {order_no}")
                  self.positions.pop(target_code_for_pos, None)
                  await self._unsubscribe_realtime_stock(target_code_for_pos)

          elif current_pos_status == 'PENDING_EXIT':
               if order_status == '체결' and exec_qty > 0 and exec_price > 0:
                  filled_qty = position_info.get('filled_qty', 0) + exec_qty
                  filled_value = position_info.get('filled_value', 0) + (exec_qty * exec_price)
                  position_info['filled_qty'] = filled_qty
                  position_info['filled_value'] = filled_value

                  # --- 👇 original_size 정의 ---
                  original_size = position_info.get('original_size_before_exit', '?') # 변수 정의
                  # --- 👆 정의 끝 ---

                  if unfilled_qty == 0:
                      exit_price = filled_value / filled_qty if filled_qty > 0 else 0
                      entry_price = position_info.get('entry_price', 0)
                      profit = (exit_price - entry_price) * filled_qty if entry_price else 0
                      profit_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price and entry_price != 0 else 0
                      self.add_log(f"   ✅ [EXEC_UPDATE] 매도 완전 체결 (청산): [{target_code_for_pos}] 청산가={exit_price:.2f}, 수량={filled_qty}, 실현손익={profit:.2f} ({profit_pct:.2f}%), 사유={position_info.get('exit_signal')}")
                      self.positions.pop(target_code_for_pos, None)
                      await self._unsubscribe_realtime_stock(target_code_for_pos)
                  else:
                      # --- 👇 로그 변수 수정 ---
                      self.add_log(f"   ⏳ [EXEC_UPDATE] 매도 부분 체결: [{target_code_for_pos}] 누적 {filled_qty}/{original_size}")
                      # --- 👆 수정 끝 ---

               elif order_status in ['취소', '거부', '확인']:
                   # --- 👇 remaining_size_after_cancel 정의 ---
                   remaining_size_after_cancel = position_info.get('original_size_before_exit', 0) - position_info.get('filled_qty', 0)
                   # --- 👆 정의 끝 ---
                   self.add_log(f"   ⚠️ [EXEC_UPDATE] 매도 주문 취소/거부/확인 ({order_status}): [{target_code_for_pos}] 주문번호 {order_no}, 미체결 잔량={unfilled_qty}, 엔진 계산 잔량={remaining_size_after_cancel}")
                   if unfilled_qty > 0 and remaining_size_after_cancel > 0:
                       position_info['status'] = 'IN_POSITION'
                       position_info['size'] = remaining_size_after_cancel
                       position_info.pop('order_no', None)
                       self.add_log(f"     -> [{target_code_for_pos}] 상태 복구: IN_POSITION, 수량={remaining_size_after_cancel}")
                   else:
                       self.add_log(f"     -> [{target_code_for_pos}] 포지션 제거 및 구독 해지")
                       if target_code_for_pos in self.positions:
                          self.positions.pop(target_code_for_pos, None)
                          await self._unsubscribe_realtime_stock(target_code_for_pos)

      except Exception as e:
          self.add_log(f"🚨🚨🚨 [CRITICAL] 주문 체결 처리(_process_execution_update) 중 심각한 오류: {e} | Data: {str(exec_data)[:200]}... 🚨🚨🚨")
          self.add_log(traceback.format_exc())

  async def _process_balance_update(self, stock_code: str, balance_data: Dict):
  # --- 👆 수정 끝 ---
      """실시간 잔고(TR ID: 04) 데이터 처리 (비동기)"""
      try:
          account_no = balance_data.get('9201', '').strip()
          # --- 👇 stock_code_raw 정의 (values['9001'] 대신 전달받은 stock_code 사용) ---
          # stock_code_raw = balance_data.get('9001', '').strip() # 제거
          # --- 👆 정의 끝 ---
          current_size_str = balance_data.get('930', '').strip()
          avg_price_str = balance_data.get('931', '').strip()

          # 필수 정보 확인 (stock_code는 이미 함수 인자로 받음)
          if not account_no:
              self.add_log(f"  ⚠️ [BALANCE_UPDATE] 잔고 데이터 누락 (계좌): {balance_data}")
              return

          # --- 👇 종목 코드 정제 로직 제거 (이미 인자로 받음) ---
          # stock_code = stock_code_raw[1:] if stock_code_raw.startswith('A') else stock_code_raw
          # if stock_code.endswith(('_NX', '_AL')): stock_code = stock_code[:-3]
          # --- 👆 제거 끝 ---

          try: current_size = int(current_size_str) if current_size_str else 0
          except ValueError: current_size = 0
          try: avg_price = float(avg_price_str) if avg_price_str else 0.0
          except ValueError: avg_price = 0.0

          position_info = self.positions.get(stock_code)
          current_pos_status = position_info.get('status') if position_info else None

          if position_info and current_pos_status in ['IN_POSITION', 'PENDING_EXIT']:
              engine_size = position_info.get('size', 0)
              if engine_size != current_size:
                  self.add_log(f"  🔄 [BALANCE_UPDATE] 수량 불일치 감지 (Case 1): [{stock_code}], 엔진:{engine_size} != 잔고:{current_size}. 엔진 상태 동기화.")
                  position_info['size'] = current_size

              if current_pos_status == 'PENDING_EXIT' and current_size == 0:
                  self.add_log(f"  ℹ️ [BALANCE_UPDATE] 잔고 0 확인 ({stock_code}, 상태: PENDING_EXIT). 엔진 포지션 제거 (Case 1 변형).")
                  self.positions.pop(stock_code, None)
                  await self._unsubscribe_realtime_stock(stock_code)

          elif not position_info and current_size > 0:
              self.add_log(f"  ⚠️ [BALANCE_UPDATE] 불일치 잔고({stock_code}, {current_size}주 @ {avg_price}) 발견 (Case 2). 엔진 상태 강제 업데이트.")
              self.positions[stock_code] = {
                  'stk_cd': stock_code, 'entry_price': avg_price, 'size': current_size,
                  'status': 'IN_POSITION', 'entry_time': datetime.now(),
                  'filled_qty': current_size, 'filled_value': avg_price * current_size
              }
              if stock_code not in self.subscribed_codes:
                   await self._subscribe_realtime_stock(stock_code)

          elif position_info and current_pos_status in ['IN_POSITION', 'PENDING_EXIT', 'ERROR_LIQUIDATION'] and current_size == 0:
              self.add_log(f"  ℹ️ [BALANCE_UPDATE] 잔고 0 확인 ({stock_code}, 상태: {current_pos_status}). 엔진 포지션 제거 (Case 3).")
              self.positions.pop(stock_code, None)
              await self._unsubscribe_realtime_stock(stock_code)

      except Exception as e:
          self.add_log(f"🚨🚨🚨 [CRITICAL] 잔고 처리(_process_balance_update) 중 심각한 오류: {e} | Data: {str(balance_data)[:200]}... 🚨🚨🚨")
          self.add_log(traceback.format_exc())

  async def _unsubscribe_realtime_stock(self, stock_code: str):
      """특정 종목의 실시간 데이터(0B, 0D) 구독 해지"""
      if self.api and stock_code in self.subscribed_codes:
          self.add_log(f"  ➖ [WS_SUB] 실시간 구독 해지 시도: {stock_code}")
          await self.api.unregister_realtime(tr_ids=['0B', '0D'], tr_keys=[stock_code, stock_code])
          self.subscribed_codes.discard(stock_code)

  async def _subscribe_realtime_stock(self, stock_code: str):
      """특정 종목의 실시간 데이터(0B, 0D) 구독 추가"""
      if self.api and stock_code not in self.subscribed_codes:
          self.add_log(f"  ➕ [WS_SUB] 실시간 구독 추가 시도 (불일치 복구): {stock_code}")
          await self.api.register_realtime(tr_ids=['0B', '0D'], tr_keys=[stock_code, stock_code])
          self.subscribed_codes.add(stock_code)

  async def run_screening(self) -> List[str]:
      """거래 대상 종목 스크리닝 (기존 로직 유지, 반환값 사용됨)"""
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
              self.candidate_stock_codes = []; self.candidate_stocks_info = []
              return []

          surge_list = candidate_stocks_raw.get('trde_qty_sdnin', [])
          self.add_log(f"  <- [SCREEN] 거래량 급증 API 응답 수신 (결과 수: {len(surge_list)})")

          if not surge_list:
              self.add_log("⚠️ [SCREEN] 스크리닝 결과 없음.")
              self.candidate_stock_codes = []; self.candidate_stocks_info = []
              return []

          candidate_stocks_intermediate = []
          for s in surge_list:
              stk_cd_raw = s.get('stk_cd'); stk_nm = s.get('stk_nm')
              cur_prc_str = s.get('cur_prc'); sdnin_rt_str = s.get('sdnin_rt')
              if not stk_cd_raw or not stk_nm or not cur_prc_str or not sdnin_rt_str: continue
              stk_cd = stk_cd_raw.strip()
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

          self.candidate_stocks_info = candidate_stocks_intermediate[:max_targets]
          self.candidate_stock_codes = [s['stk_cd'] for s in self.candidate_stocks_info]

          target_stocks_display = [f"{s['stk_cd']}({s['stk_nm']})" for s in self.candidate_stocks_info]
          if target_stocks_display:
              self.add_log(f"🎯 [SCREEN] 스크리닝 완료. 후보: {target_stocks_display}")
          else:
              self.add_log("ℹ️ [SCREEN] 최종 후보 종목 없음.")

          return self.candidate_stock_codes

      except Exception as e:
          self.add_log(f"🚨🚨🚨 [CRITICAL] 스크리닝 중 심각한 오류 발생: {e} 🚨🚨🚨")
          self.add_log(traceback.format_exc())
          self.candidate_stock_codes = []; self.candidate_stocks_info = []
          return []

  async def process_all_stocks_tick(self, current_time: datetime):
      """스크리닝된 후보 종목과 보유 종목 전체에 대해 Tick 처리 실행"""
      codes_to_process = set(self.candidate_stock_codes) | set(self.positions.keys())
      if not codes_to_process: return

      tick_interval_seconds = getattr(self.config.strategy, 'tick_interval_seconds', 5)

      self.add_log(f"⚙️ [TICK_ALL] 순차 처리 시작 (대상: {list(codes_to_process)})")
      processed_count = 0
      for stock_code in list(codes_to_process): # 순회 중 변경될 수 있으므로 list 복사
          last_tick = self.last_stock_tick_time.get(stock_code)
          if last_tick and (current_time - last_tick).total_seconds() < tick_interval_seconds:
              continue

          if not self.api:
              self.add_log(f"⚠️ [TICK:{stock_code}] API 객체 없음. Tick 처리 중단.")
              break

          await self.process_single_stock_tick(stock_code)
          self.last_stock_tick_time[stock_code] = current_time
          processed_count += 1

          # --- API 호출 간격 제어 ---
          await asyncio.sleep(1.1)

      self.add_log(f"⚙️ [TICK_ALL] 순차 처리 완료 ({processed_count}/{len(codes_to_process)}개 종목 시도)")


  async def process_single_stock_tick(self, stock_code: str):
      """개별 종목 Tick 처리: 데이터 조회, 지표 계산, 신호 판단, 주문 실행"""
      current_price = None
      current_vwap = None # VWAP 값 저장 변수
      df = None # DataFrame 초기화
      realtime_available = False
      orderbook_data = None # 호가 데이터 저장 변수 추가
      obi = None # OBI 값 저장 변수 추가
      rvol = None # RVOL 값 저장 변수 추가

      # --- 1. 실시간 데이터 우선 확인 ---
      if stock_code in self.realtime_data:
          realtime_info = self.realtime_data[stock_code]
          last_update_time = realtime_info.get('timestamp')
          # 실시간 데이터가 10초 이내에 업데이트되었는지 확인
          if last_update_time and (datetime.now() - last_update_time).total_seconds() < 10:
              current_price = realtime_info.get('last_price')
              if current_price: realtime_available = True

      # --- 2. REST API 호출 ---
      # API 객체가 없으면 함수 종료
      if not self.api:
          self.add_log(f"  ⚠️ [{stock_code}] API 객체 없음. 데이터 조회 불가.")
          return

      # 2.1 분봉 데이터 조회 (지표 계산 및 현재가 없을 때)
      # 실시간 가격 정보가 없거나, DataFrame이 아직 생성되지 않은 경우 분봉 데이터 조회
      if not realtime_available or df is None:
          # self.add_log(f"   -> [{stock_code}] 분봉 데이터 API 호출...") # 필요시 주석 해제
          raw_data = await self.api.fetch_minute_chart(stock_code, timeframe=1)
          if raw_data and raw_data.get('return_code') == 0:
              # API 응답 데이터를 DataFrame으로 변환 및 전처리
              df = preprocess_chart_data(raw_data.get("stk_min_pole_chart_qry", []))
              if df is not None and not df.empty:
                  # 실시간 가격 정보가 없었을 경우 DataFrame의 마지막 종가를 현재가로 사용
                  if not realtime_available:
                      current_price = df['close'].iloc[-1]
              else:
                  df = None # 빈 DataFrame이면 None으로 처리
                  # self.add_log(f"  ⚠️ [{stock_code}] 분봉 데이터프레임 변환 실패 또는 비어있음.")
          # else: fetch_minute_chart 내부에서 오류 로그가 처리됨

      # --- 👇 2.2 호가 데이터 조회 (OBI 계산용) ---
      orderbook_raw_data = await self.api.fetch_orderbook(stock_code)
      if orderbook_raw_data and orderbook_raw_data.get('return_code') == 0:
          orderbook_data = orderbook_raw_data # 성공 시 데이터 저장
      # --- 👆 호가 데이터 조회 끝 ---

      # --- 3. 현재가 최종 확인 ---
      # 실시간 데이터나 API 조회를 통해 현재가를 얻지 못했으면 처리 중단
      if current_price is None:
          self.add_log(f"  ⚠️ [{stock_code}] 현재가 확인 불가 (실시간/API 모두). Tick 처리 중단.")
          return

      # --- 4. 지표 계산 ---
      orb_levels = pd.Series({'orh': None, 'orl': None}) # ORB 초기화
      rvol = None # RVOL 초기화

      # DataFrame이 유효할 때만 지표 계산 수행
      if df is not None:
          add_vwap(df) # VWAP 계산
          add_ema(df) # EMA 계산
          orb_levels = calculate_orb(df, timeframe=getattr(self.config.strategy, 'orb_timeframe', 15)) # ORB 계산
          # VWAP 값 추출 (NaN 이 아닐 경우)
          current_vwap = df['VWAP'].iloc[-1] if 'VWAP' in df.columns and not pd.isna(df['VWAP'].iloc[-1]) else None

          # --- 👇 RVOL 계산 호출 (DataFrame 전달) ---
          rvol_window = getattr(self.config.strategy, 'rvol_window', 20) # 설정값 사용 (없으면 20)
          rvol = calculate_rvol(df, window=rvol_window) # 수정된 함수 호출
          # --- 👆 RVOL 계산 호출 끝 ---
      else:
          self.add_log(f"  ⚠️ [{stock_code}] 지표 계산용 DataFrame 없음. ORB/VWAP/EMA/RVOL 등 계산 불가.")

      # --- 👇 OBI 계산 (호가 데이터 필요) ---
      if orderbook_data:
          try:
              # API 응답에서 총매수/매도 잔량 키('tot_buy_req', 'tot_sel_req')로 값 추출
              total_bid_vol_str = orderbook_data.get('tot_buy_req')
              total_ask_vol_str = orderbook_data.get('tot_sel_req')

              # 문자열 값을 정수로 변환 (값이 없거나 숫자가 아니면 None)
              total_bid_vol = int(total_bid_vol_str.strip()) if total_bid_vol_str and total_bid_vol_str.strip().lstrip('-').isdigit() else None
              total_ask_vol = int(total_ask_vol_str.strip()) if total_ask_vol_str and total_ask_vol_str.strip().lstrip('-').isdigit() else None

              # 두 값 모두 유효할 때 OBI 계산 함수 호출
              if total_bid_vol is not None and total_ask_vol is not None:
                 obi = calculate_obi(total_bid_vol, total_ask_vol)
              else:
                 self.add_log(f"  ⚠️ [{stock_code}] 호가 데이터에서 총 잔량 추출 실패 또는 숫자 변환 불가. OBI 계산 스킵.")
          except Exception as obi_e: # OBI 계산 중 발생할 수 있는 예외 처리
              self.add_log(f"  🚨 [{stock_code}] OBI 계산 준비 중 오류: {obi_e}")
      else:
          # 호가 데이터 조회 실패 시 로그
          self.add_log(f"  ⚠️ [{stock_code}] 호가 데이터 없음. OBI 계산 불가.")
      # --- 👆 OBI 계산 끝 ---

      # --- 5. 필수 지표 확인 (ORB) ---
      # ORB 상단값(orh)이 없으면 전략 실행 불가
      if orb_levels['orh'] is None:
           self.add_log(f"  ⚠️ [{stock_code}] 필수 지표(ORH) 계산 불가. Tick 처리 중단.")
           return

      # --- 로그 출력 업데이트 (OBI, RVOL 포함) ---
      # DataFrame 이 있을 경우 EMA 값 추출, 없으면 'N/A'
      ema9_val = df['EMA_9'].iloc[-1] if df is not None and 'EMA_9' in df.columns and not pd.isna(df['EMA_9'].iloc[-1]) else None
      ema20_val = df['EMA_20'].iloc[-1] if df is not None and 'EMA_20' in df.columns and not pd.isna(df['EMA_20'].iloc[-1]) else None
      # 값 포맷팅 (소수점 둘째자리 또는 'N/A')
      ema9_str = f"{ema9_val:.2f}" if ema9_val is not None else "N/A"
      ema20_str = f"{ema20_val:.2f}" if ema20_val is not None else "N/A"
      rvol_str = f"{rvol:.2f}%" if rvol is not None else "N/A"
      obi_str = f"{obi:.2f}%" if obi is not None else "N/A"
      vwap_str = f"{current_vwap:.2f}" if current_vwap is not None else "N/A"
      # ORB 값 포맷팅 (정수 또는 'N/A')
      orh_str = f"{orb_levels['orh']:.0f}" if orb_levels['orh'] is not None else "N/A"
      orl_str = f"{orb_levels['orl']:.0f}" if orb_levels['orl'] is not None else "N/A"

      # 최종 로그 출력
      self.add_log(f"📊 [{stock_code}] 현재가:{current_price:.0f}, ORH:{orh_str}, ORL:{orl_str}, VWAP:{vwap_str}, EMA9:{ema9_str}, EMA20:{ema20_str}, RVOL:{rvol_str}, OBI:{obi_str}")
      # --- 로그 끝 ---

      # --- 6. 전략 로직 실행 ---
      position_info = self.positions.get(stock_code) # 현재 종목의 포지션 정보 가져오기
      current_status = position_info.get('status') if position_info else 'SEARCHING' # 포지션 없으면 'SEARCHING' 상태

      # 주문이 이미 진행 중인 상태(PENDING_ENTRY, PENDING_EXIT)면 추가 작업 없이 종료
      if current_status in ['PENDING_ENTRY', 'PENDING_EXIT']:
          return

      try:
          # --- 진입 조건 (SEARCHING 상태) ---
          if current_status == 'SEARCHING':
              # 스크리닝된 후보 종목 목록에 해당 종목이 있을 때만 진입 시도
              if stock_code in self.candidate_stock_codes:
                  # 돌파 신호 확인
                  signal = check_breakout_signal(
                      current_price, orb_levels, getattr(self.config.strategy, 'breakout_buffer', 0.15)
                  )
                  # --- 👇 진입 필터 추가 (예시: RVOL > 130%, OBI > 150%) ---
                  # 설정 파일에서 필터 임계값 가져오기 (없으면 기본값 사용)
                  rvol_threshold = getattr(self.config.strategy, 'entry_min_rvol', 130.0)
                  obi_threshold = getattr(self.config.strategy, 'entry_min_obi', 150.0)
                  # 필터 조건 충족 여부 확인 (RVOL, OBI 값이 유효하고 임계값 이상인지)
                  rvol_ok = rvol is not None and rvol >= rvol_threshold
                  obi_ok = obi is not None and obi >= obi_threshold

                  # 매수 신호가 발생했고, RVOL과 OBI 필터 조건을 모두 만족하는 경우
                  if signal == "BUY" and rvol_ok and obi_ok:
                      # 주문 수량 계산
                      order_qty = await self.calculate_order_quantity(current_price)
                      if order_qty > 0: # 주문 수량이 0보다 클 때만 주문 실행
                          self.add_log(f"🔥 [{stock_code}] 매수 신호 + 필터 충족! (RVOL:{rvol_str}, OBI:{obi_str})")
                          self.add_log(f"  -> [{stock_code}] 매수 주문 API 호출 시도 ({order_qty}주)...")
                          # 매수 주문 API 호출
                          order_result = await self.api.create_buy_order(stock_code, quantity=order_qty)
                          # 주문 결과 확인
                          if order_result and order_result.get('return_code') == 0:
                              order_no = order_result.get('ord_no') # 주문 번호 저장
                              self.add_log(f"   ➡️ [{stock_code}] 매수 주문 접수 완료: {order_no}")
                              # 포지션 상태 업데이트 (PENDING_ENTRY)
                              self.positions[stock_code] = {
                                  'stk_cd': stock_code, 'status': 'PENDING_ENTRY', 'order_no': order_no,
                                  'original_order_qty': order_qty, 'filled_qty': 0, 'filled_value': 0.0
                              }
                          else: # 주문 실패 시 로그 기록
                              error_msg = order_result.get('return_msg', '주문 실패') if order_result else 'API 호출 실패'
                              self.add_log(f"   ❌ [{stock_code}] 매수 주문 실패: {error_msg}")
                  # 매수 신호는 발생했으나 필터 조건 미충족 시 로그 기록
                  elif signal == "BUY" and (not rvol_ok or not obi_ok):
                       self.add_log(f"   ⚠️ [{stock_code}] 매수 신호 발생했으나 필터 미충족 (RVOL:{rvol_str} {'✅' if rvol_ok else '❌'}, OBI:{obi_str} {'✅' if obi_ok else '❌'}). 진입 보류.")
                  # --- 👆 진입 필터 끝 ---

          # --- 청산 조건 (IN_POSITION 상태) ---
          # 현재 포지션을 보유 중('IN_POSITION')인 경우 청산 조건 확인
          elif current_status == 'IN_POSITION' and position_info:
              # 리스크 관리 함수 호출 (익절/손절/VWAP 이탈 확인)
              signal = manage_position(position_info, current_price, current_vwap) # current_vwap 전달됨
              if signal: # 청산 신호가 발생하면
                  # 신호 종류에 따라 로그 메시지 접두사 설정
                  log_prefix = "💰" if signal == "TAKE_PROFIT" else ("📉" if signal == "VWAP_STOP_LOSS" else "🛑")
                  self.add_log(f"{log_prefix} 청산 신호({signal})! [{stock_code}] 매도 주문 실행 (현재가 {current_price:.0f}).")
                  sell_qty = position_info.get('size', 0) # 보유 수량 확인
                  if sell_qty > 0: # 보유 수량이 0보다 크면
                      # 매도 주문 API 호출
                      order_result = await self.api.create_sell_order(stock_code, quantity=sell_qty)
                      # 주문 결과 확인
                      if order_result and order_result.get('return_code') == 0:
                          order_no = order_result.get('ord_no')
                          self.add_log(f"   ➡️ [{stock_code}] 매도 주문 접수 완료: {order_no}")
                          # 포지션 상태 업데이트 (PENDING_EXIT)
                          position_info.update({
                              'status': 'PENDING_EXIT', 'order_no': order_no,
                              'exit_signal': signal, 'original_size_before_exit': sell_qty,
                              'filled_qty': 0, 'filled_value': 0.0
                          })
                      else: # 주문 실패 시 로그 기록 및 에러 상태 변경
                          error_msg = order_result.get('return_msg', '주문 실패') if order_result else 'API 호출 실패'
                          self.add_log(f"   ❌ [{stock_code}] 매도 주문 실패: {error_msg}")
                          position_info['status'] = 'ERROR_LIQUIDATION'
                  else: # 청산할 수량이 없을 경우 (오류 상황)
                      self.add_log(f"   ⚠️ [{stock_code}] 청산할 수량 없음 ({sell_qty}). 포지션 정보 오류 가능성.")
                      self.positions.pop(stock_code, None) # 해당 포지션 정보 삭제
                      await self._unsubscribe_realtime_stock(stock_code) # 실시간 구독 해지

      except Exception as e: # 예상치 못한 오류 발생 시 로그 기록
          self.add_log(f"🚨🚨🚨 [CRITICAL] 개별 Tick 처리({stock_code}) 중 예상치 못한 심각한 오류 발생: {e} 🚨🚨🚨")
          self.add_log(traceback.format_exc()) # 상세 오류 스택 추적

  async def calculate_order_quantity(self, current_price: float) -> int:
      """주문 가능 현금과 설정된 투자 금액을 기반으로 주문 수량 계산"""
      # 설정된 종목당 투자 금액 가져오기
      investment_amount_per_stock = getattr(self.config.strategy, 'investment_amount_per_stock', 0)
      if investment_amount_per_stock <= 0:
          self.add_log("   ⚠️ [DEBUG_ORDER_QTY] 설정된 투자 금액이 0 이하입니다.")
          return 0
      self.add_log(f"     [DEBUG_ORDER_QTY] 설정된 투자 금액: {investment_amount_per_stock}")

      # API를 통해 주문 가능 현금 조회
      if not self.api: self.add_log("   ⚠️ [DEBUG_ORDER_QTY] API 없음. 현금 조회 불가."); return 0
      balance_info = await self.api.fetch_account_balance()
      if not balance_info or balance_info.get('return_code') != 0 or 'ord_alow_amt' not in balance_info:
          error_msg = balance_info.get('return_msg', 'API 호출 실패 또는 응답 없음') if balance_info else 'API 객체 없음'
          self.add_log(f"   ⚠️ [DEBUG_ORDER_QTY] 주문 가능 현금 조회 실패: {error_msg}")
          return 0

      available_cash_str = balance_info.get('ord_alow_amt', '0').lstrip('0')
      self.add_log(f"     [DEBUG_ORDER_QTY] API 예수금 응답 중 주문가능금액(str): '{balance_info.get('ord_alow_amt', 'N/A')}'")
      try:
          available_cash = int(available_cash_str) if available_cash_str else 0
      except ValueError:
          self.add_log(f"   ⚠️ [DEBUG_ORDER_QTY] 주문 가능 현금 변환 오류: '{available_cash_str}'")
          return 0
      self.add_log(f"     [DEBUG_ORDER_QTY] 변환된 주문 가능 현금(int): {available_cash}")

      # 주문 가능 금액과 설정 금액 비교
      self.add_log(f"     [DEBUG_ORDER_QTY] 비교: available_cash({available_cash}) >= investment_amount_per_stock({investment_amount_per_stock}) ?")
      if available_cash < investment_amount_per_stock:
          self.add_log(f"      - 주문 불가 사유: 현금 부족({available_cash} < {investment_amount_per_stock})")
          return 0
      elif current_price <= 0:
          self.add_log(f"      - 주문 불가 사유: 현재가({current_price})가 유효하지 않음.")
          return 0
      else:
          # 수량 계산 (투자 금액 / 현재가)
          self.add_log(f"     [DEBUG_ORDER_QTY] 수량 계산 시도: investment({investment_amount_per_stock}) // current_price({current_price})")
          order_qty = int(investment_amount_per_stock // current_price)
          self.add_log(f"      - 주문 가능 현금: {available_cash:,}, 투자 예정: {investment_amount_per_stock:,}, 계산된 수량: {order_qty}주")
          return order_qty

  async def execute_kill_switch(self):
      """긴급 정지: 모든 미체결 주문 취소 및 보유 포지션 시장가 청산"""
      if self.engine_status != 'RUNNING':
          self.add_log("⚠️ [KILL] 엔진이 실행 중이 아님. Kill Switch 작동 불가."); return
      if not self.api: self.add_log("⚠️ [KILL] API 객체 없음."); return

      self.add_log("🚨🚨🚨 [KILL] Kill Switch 발동! 모든 주문 취소 및 포지션 청산 시작... 🚨🚨🚨")
      self.engine_status = 'KILLED' # 킬 스위치 상태로 변경 (메인 루프 중단 유도)
      self._stop_event.set() # 메인 루프 즉시 종료 요청

      try:
          # --- 1. 미체결 주문 조회 및 취소 (가정: fetch_pending_orders API 구현됨) ---
          self.add_log("  -> [KILL] 미체결 주문 조회 및 취소 시도...")
          # pending_orders = await self.api.fetch_pending_orders() # ka10075 호출 구현 필요
          pending_orders = [] # 임시
          if pending_orders:
              for order in pending_orders:
                  try:
                      ord_no = order['ord_no']; stk_cd = order['stk_cd']; oso_qty = int(order.get('oso_qty', 0))
                      if oso_qty > 0:
                           self.add_log(f"     - 취소 시도: 주문 {ord_no}, 종목 {stk_cd}, 수량 {oso_qty}")
                           await self.api.cancel_order(ord_no, stk_cd, oso_qty)
                  except (KeyError, ValueError, TypeError) as cancel_e:
                       self.add_log(f"     ❌ [KILL] 주문 취소 중 오류 발생 (데이터:{order}): {cancel_e}")
              self.add_log("  <- [KILL] 미체결 주문 취소 요청 완료.")
          else:
              self.add_log("  - [KILL] 취소할 미체결 주문 없음.")

          # --- 2. 보유 포지션 시장가 청산 ---
          positions_to_liquidate = list(self.positions.items()) # items() 로 복사
          if positions_to_liquidate:
              self.add_log(f"  -> [KILL] {len(positions_to_liquidate)} 건의 보유 포지션 시장가 청산 시도...")
              for stock_code, pos_info in positions_to_liquidate:
                  # 실제 보유 중인 포지션만 청산 대상
                  if pos_info.get('status') == 'IN_POSITION' and pos_info.get('size', 0) > 0:
                      quantity = pos_info['size']
                      self.add_log(f"     - 청산 시도: 종목 {stock_code}, 수량 {quantity}")
                      result = await self.api.create_sell_order(stock_code, quantity)
                      if not result or result.get('return_code') != 0:
                          error_info = result.get('return_msg', '주문 실패') if result else 'API 호출 실패'
                          self.add_log(f"     ❌ [KILL] 시장가 청산 주문 실패 ({stock_code} {quantity}주): {error_info}")
                          if stock_code in self.positions: self.positions[stock_code]['status'] = 'ERROR_LIQUIDATION'
                      else:
                          order_no = result.get('ord_no', 'N/A')
                          self.add_log(f"     ✅ [KILL] 시장가 청산 주문 접수 ({stock_code} {quantity}주): {order_no}")
                          if stock_code in self.positions:
                              self.positions[stock_code].update({
                                  'status': 'PENDING_EXIT', 'order_no': order_no,
                                  'exit_signal': 'KILL_SWITCH', 'original_size_before_exit': quantity,
                                  'filled_qty': 0, 'filled_value': 0.0
                              })
                  elif pos_info.get('status') in ['PENDING_ENTRY', 'PENDING_EXIT']:
                       self.add_log(f"     ℹ️ [KILL] 주문 진행 중인 포지션({stock_code}, 상태:{pos_info.get('status')})은 미체결 취소로 처리됩니다.")
              self.add_log("  <- [KILL] 시장가 청산 주문 접수 완료.")
          else:
              self.add_log("  - [KILL] 청산할 보유 포지션 없음.")

          self.add_log("🚨 Kill Switch 처리 완료. 엔진 종료 대기...")
          # shutdown()은 finally 블록에서 호출되므로 여기서 기다림

      except Exception as e:
          self.add_log(f"🚨🚨🚨 [CRITICAL] Kill Switch 처리 중 심각한 오류 발생: {e} 🚨🚨🚨")
          self.add_log(traceback.format_exc())
          self.engine_status = 'ERROR'
          # stop()은 이미 호출되었을 수 있으나 확실히 호출
          if not self._stop_event.is_set(): await self.stop()