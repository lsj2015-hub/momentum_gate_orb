# core/engine.py

import asyncio
from datetime import datetime
from typing import Dict, List

from config.loader import config
from gateway.kiwoom_api import KiwoomAPI
from data.manager import preprocess_chart_data
from data.indicators import add_vwap, calculate_orb
from strategy.momentum_orb import check_breakout_signal
from strategy.risk_manager import manage_position
from strategy import screener

class TradingEngine:
  """
  전체 트레이딩 로직을 관장하는 핵심 실행 엔진. (UI 연동 및 상태 관리 버전)
  """
  def __init__(self, config):
    self.config = config
    self.target_stock = "900140" # 엘브이엠씨홀딩스
    
    # --- 상태 관리 변수 ---
    # SEARCHING: 매수 기회 탐색
    # PENDING_ENTRY: 매수 주문 후 체결 대기
    # IN_POSITION: 포지션 보유 중 (매도 기회 탐색)
    # PENDING_EXIT: 매도 주문 후 체결 대기
    self.position: Dict[str, any] = {
      'status': 'SEARCHING',
      'stk_cd': None,
      'entry_price': 0.0,
      'size': 0,
      'order_no': None
    }
    self.logs: List[str] = []
    self.add_log("🤖 트레이딩 엔진 초기화 완료. 매수 기회를 탐색합니다.")

  async def initialize_session(self):
      """장 시작 시 호출되어 오늘의 대상 종목을 선정합니다."""
      self.add_log("📈 장 시작! 오늘의 모멘텀 종목을 탐색합니다...")
      # 종목 선정을 위해 임시 API 인스턴스 사용
      api = KiwoomAPI()
      try:
          # screener 함수 호출 시 API 객체 전달
          target_code = await screener.find_momentum_stocks(api)
          if target_code:
              # Kiwoom API는 종목코드 앞에 'A'를 붙이지 않으므로 제거
              self.target_stock = target_code.lstrip('A')
              self.add_log(f"🎯 오늘의 대상 종목 선정: {self.target_stock}")
              # 필요하다면 종목명 조회 로직 추가
              # stock_info = await api.fetch_stock_info(self.target_stock)
              # if stock_info:
              #     self.add_log(f"   종목명: {stock_info.get('stk_nm')}")

          else:
              self.add_log("⚠️ 조건에 맞는 모멘텀 종목을 찾지 못했습니다. 오늘은 거래하지 않습니다.")
              self.target_stock = None # 대상 종목이 없으면 None으로 설정
      except Exception as e:
          self.add_log(f"❗️ 종목 선정 중 오류 발생: {e}")
          self.target_stock = None
      finally:
          await api.close() # 임시 API 인스턴스 종료

  def add_log(self, message: str):
    """로그 메시지를 추가하고 화면에 출력합니다."""
    log_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(log_msg)
    self.logs.insert(0, log_msg)
    if len(self.logs) > 20:
      self.logs.pop()

  async def process_tick(self):
    """매 틱마다 API 인스턴스를 생성하고 상태에 따라 적절한 로직을 수행합니다."""

    if not self.target_stock:
      # 대상 종목이 없으면 로그를 남기지 않고 조용히 대기 (선택 사항)
      # self.add_log("대상 종목 없음. 대기합니다.") 
      await asyncio.sleep(1) # CPU 사용 방지를 위해 짧게 대기
      return # 함수 종료
    
    self.add_log("... 새로운 틱 처리 시작 ...")
    
    api = KiwoomAPI()
    try:
      status = self.position.get('status')
      if status == 'SEARCHING':
        await self._handle_searching_state(api)
      elif status == 'PENDING_ENTRY':
        await self._handle_pending_entry_state(api)
      elif status == 'IN_POSITION':
        await self._handle_in_position_state(api)
      elif status == 'PENDING_EXIT':
        await self._handle_pending_exit_state(api)
    finally:
      await api.close()
    
    self.add_log(f"현재 상태: {self.position['status']}, 보유 종목: {self.position['stk_cd']}")

  async def _handle_searching_state(self, api: KiwoomAPI):
    """'SEARCHING' 상태: 매수 신호를 탐색하고 주문을 실행합니다."""
    self.add_log("상태: SEARCHING - 매수 신호를 탐색합니다.")
    
    # 1. 데이터 수집 및 가공
    raw_data = await api.fetch_minute_chart(self.target_stock, timeframe=1)
    if not (raw_data and raw_data.get("stk_min_pole_chart_qry")):
      self.add_log("❗️ 데이터를 가져오지 못했습니다.")
      return

    df = preprocess_chart_data(raw_data["stk_min_pole_chart_qry"])
    if df is None or df.empty:
      self.add_log("❗️ 데이터프레임 변환에 실패했습니다.")
      return
      
    # 2. 보조지표 계산 및 신호 확인
    add_vwap(df)
    orb_levels = calculate_orb(df, timeframe=config.strategy.orb_timeframe)
    current_price = df['close'].iloc[-1]
    self.add_log(f"현재가: {current_price}, ORH: {orb_levels['orh']}, ORL: {orb_levels['orl']}")
    
    signal = check_breakout_signal(current_price, orb_levels, config.strategy.breakout_buffer)
    
    if signal == "BUY":
      self.add_log("🔥 매수 신호 포착! 시장가 매수 주문을 실행합니다.")
      order_result = await api.create_buy_order(self.target_stock, quantity=1) # 예시: 1주 매수
      
      if order_result and order_result.get('return_code') == 0:
        self.position['status'] = 'PENDING_ENTRY'
        self.position['stk_cd'] = self.target_stock
        self.position['order_no'] = order_result.get('ord_no')
        self.add_log(f"➡️ 주문 접수 완료 (주문번호: {self.position['order_no']}). 체결 대기로 전환합니다.")
      else:
        self.add_log(f"❗️ 주문 접수 실패: {order_result}")

  async def _handle_pending_entry_state(self, api: KiwoomAPI):
    """'PENDING_ENTRY' 상태: 매수 주문의 체결 여부를 확인합니다."""
    order_no = self.position.get('order_no')
    self.add_log(f"상태: PENDING_ENTRY - 매수 주문({order_no})의 체결을 확인합니다.")

    order_status = await api.fetch_order_status(order_no) #

    if order_status and order_status['status'] == 'FILLED':
      # --- 상태 변경: PENDING_ENTRY -> IN_POSITION ---
      # API 응답에서 실제 체결된 가격과 수량을 가져옵니다.
      executed_price = order_status.get('executed_price', 0.0)
      executed_qty = order_status.get('executed_qty', 0)

      # 체결 수량이 0보다 클 때만 포지션 진입 처리
      if executed_qty > 0:
        self.position['status'] = 'IN_POSITION'
        self.position['entry_price'] = executed_price
        self.position['size'] = executed_qty # ❗️API에서 받은 실제 체결 수량으로 업데이트
        self.position['entry_time'] = datetime.now()
        self.position['order_no'] = None # 완료된 주문번호 초기화
        # 로그에 체결 수량 명시
        self.add_log(f"✅ *** 매수 체결 완료! *** (체결가: {executed_price}, 수량: {executed_qty})")
        self.add_log(f"➡️ 포지션 진입 완료. 현재 포지션: {self.position}")
      else:
        # 체결 수량이 0이면 (오류 등으로) 상태를 다시 Searching으로 돌림
        self.add_log(f"⚠️ 체결 수량이 0입니다. 주문({order_no}) 확인 필요. 상태를 SEARCHING으로 복귀합니다.")
        self.position = {
          'status': 'SEARCHING', 'stk_cd': None, 'entry_price': 0.0,
          'size': 0, 'order_no': None
        }

    elif order_status and order_status['status'] == 'PENDING':
      self.add_log(f"⏳ 주문({order_no})이 아직 체결되지 않았습니다. 대기를 유지합니다.")
    else:
      # fetch_order_status가 None을 반환하거나 예상치 못한 status를 반환한 경우
      self.add_log(f"❗️ 주문({order_no}) 상태를 확인할 수 없습니다. 안전을 위해 상태를 SEARCHING으로 복귀합니다.")
      self.position = {
        'status': 'SEARCHING', 'stk_cd': None, 'entry_price': 0.0,
        'size': 0, 'order_no': None
      }

  async def _handle_in_position_state(self, api: KiwoomAPI):
    """'IN_POSITION' 상태: 청산 신호를 탐색하고 주문을 실행합니다."""
    self.add_log(f"상태: IN_POSITION - 보유 종목({self.position['stk_cd']})의 청산을 탐색합니다.")
    
    # 1. 현재가 확인
    raw_data = await api.fetch_minute_chart(self.target_stock, timeframe=1)
    if not (raw_data and raw_data.get("stk_min_pole_chart_qry")):
        self.add_log("❗️ 데이터를 가져오지 못했습니다.")
        return
    df = preprocess_chart_data(raw_data["stk_min_pole_chart_qry"])
    if df is None or df.empty:
        self.add_log("❗️ 데이터프레임 변환에 실패했습니다.")
        return
    current_price = df['close'].iloc[-1]
    
    # 2. 청산 신호 확인
    signal = manage_position(self.position, current_price)
    if signal in ["TAKE_PROFIT", "STOP_LOSS"]:
      self.add_log(f"🎉 {signal} 조건 충족! 시장가 매도 주문을 실행합니다.")
      order_result = await api.create_sell_order(self.target_stock, self.position.get('size', 0))
      
      if order_result and order_result.get('return_code') == 0:
        self.position['status'] = 'PENDING_EXIT'
        self.position['order_no'] = order_result.get('ord_no')
        self.add_log(f"⬅️ 매도 주문 접수 완료 (주문번호: {self.position['order_no']}). 체결 대기로 전환합니다.")
      else:
        self.add_log(f"❗️ 매도 주문 접수 실패: {order_result}")

  async def _handle_pending_exit_state(self, api: KiwoomAPI):
    """'PENDING_EXIT' 상태: 매도 주문의 체결 여부를 확인합니다."""
    order_no = self.position.get('order_no')
    self.add_log(f"상태: PENDING_EXIT - 매도 주문({order_no})의 체결을 확인합니다.")
    
    order_status = await api.fetch_order_status(order_no)
    
    if order_status and order_status['status'] == 'FILLED':
      self.add_log(f"✅ *** 매도 체결 완료! ***")
      self.add_log(f"⬅️ 포지션 청산 완료. 새로운 매매 기회를 탐색합니다.")
      self.position = {
        'status': 'SEARCHING', 'stk_cd': None, 'entry_price': 0.0,
        'size': 0, 'order_no': None
      }
    else:
      self.add_log(f"⏳ 주문({order_no})이 아직 체결되지 않았습니다. 대기를 유지합니다.")