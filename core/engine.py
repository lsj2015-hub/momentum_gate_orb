# 이 엔진(TradingEngine)은 우리가 지금까지 만든 모든 모듈(API 통신, 데이터 정제, 지표 계산, 신호 생성, 리스크 관리)을 조립하여 실제 트레이딩 흐름을 제어합니다.

import asyncio
from datetime import datetime
from typing import Dict, List

from config.loader import config
from gateway.kiwoom_api import KiwoomAPI
from data.manager import preprocess_chart_data
from data.indicators import add_vwap, calculate_orb
from strategy.momentum_orb import check_breakout_signal
from strategy.risk_manager import manage_position

class TradingEngine:
  """
  전체 트레이딩 로직을 관장하는 핵심 실행 엔진. (UI 연동 버전)
  """
  def __init__(self):
    self.target_stock = "027740"
    self.position: Dict[str, any] = {}
    self.logs: List[str] = []

  def add_log(self, message: str):
    """로그 메시지를 추가하고 화면에 출력합니다."""
    log_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(log_msg)
    self.logs.insert(0, log_msg)
    if len(self.logs) > 20:
      self.logs.pop()

  async def process_tick(self):
    """매 틱마다 API 인스턴스를 생성하고 작업을 수행한 후 정리합니다."""
    self.add_log("새로운 틱 처리 시작")
    
    # ⭐️ 매 틱마다 새로운 API 클라이언트 생성
    api = KiwoomAPI()
    try:
      # 1. 데이터 수집 및 가공
      raw_data = await api.fetch_minute_chart(self.target_stock, timeframe=1)
      if not (raw_data and raw_data.get("stk_min_pole_chart_qry")):
        self.add_log("❗️ 데이터를 가져오지 못했습니다.")
        return

      df = preprocess_chart_data(raw_data["stk_min_pole_chart_qry"])
      if df is None or df.empty:
        self.add_log("❗️ 데이터프레임 변환에 실패했습니다.")
        return
        
      # 2. 보조지표 계산
      add_vwap(df)
      orb_levels = calculate_orb(df, timeframe=config.strategy.orb_timeframe)
      current_price = df['close'].iloc[-1]
      
      self.add_log(f"현재가: {current_price}, ORH: {orb_levels['orh']}, ORL: {orb_levels['orl']}")

      # 3. 포지션 상태에 따른 의사결정
      if not self.position:
        signal = check_breakout_signal(
          current_price, orb_levels, config.strategy.breakout_buffer
        )
        if signal == "BUY":
          self.add_log("🔥 매수 신호 포착! 주문을 실행합니다.")
          order_result = await api.create_buy_order(self.target_stock, quantity=1)
          if order_result and order_result.get('return_code') == 0:
            self.position = {
              'stk_cd': self.target_stock, 'entry_price': current_price,
              'size': 1, 'order_no': order_result.get('ord_no')
            }
            self.add_log(f"➡️ 포지션 진입 완료: {self.position}")

      else:
        signal = manage_position(self.position, current_price)
        if signal in ["TAKE_PROFIT", "STOP_LOSS"]:
          self.add_log(f"🎉 {signal} 조건 충족! 매도 주문을 실행합니다.")
          order_result = await api.create_sell_order(self.target_stock, self.position.get('size', 0))
          if order_result and order_result.get('return_code') == 0:
            self.add_log(f"⬅️ 포지션 청산 완료: {self.position}")
            self.position = {}
    
    finally:
      # ⭐️ 작업이 끝나면 항상 API 클라이언트를 닫아줍니다.
      await api.close()