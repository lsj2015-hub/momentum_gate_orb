# 언제 매수하고, 언제 매도할지를 결정하는 로직을 작성

import pandas as pd
from typing import Dict

def check_breakout_signal(current_price: float, orb_levels: pd.Series, breakout_buffer: float) -> str:
  """
  현재 가격이 ORB 상단 또는 하단을 돌파했는지 확인하여 매매 신호를 생성합니다.

  Args:
    current_price: 현재가
    orb_levels: 'orh'와 'orl'을 포함하는 Series
    breakout_buffer: 돌파 기준 버퍼 (%)

  Returns:
    "BUY", "SELL", or "HOLD"
  """
  orh = orb_levels.get('orh')
  orl = orb_levels.get('orl')

  if orh is None or orl is None:
    return "HOLD" # ORB가 아직 계산되지 않았으면 관망
  
  # --------------------------------------------------------------------
  # ❗️❗️❗️ 테스트를 위해 임시로 매수 신호를 강제 발생시키는 코드 ❗️❗️❗️
  # 테스트가 끝나면 반드시 원래 코드로 되돌려야 합니다.
  # print("⚠️ [테스트 모드] 매수 신호를 강제로 발생시킵니다.")
  # return "BUY"
  # --------------------------------------------------------------------

  # ▼▼▼ 원래 로직 ▼▼▼
  # 돌파 기준 가격 계산 (버퍼 적용)
  buy_trigger_price = orh * (1 + breakout_buffer / 100)
  sell_trigger_price = orl * (1 - breakout_buffer / 100)

  if current_price > buy_trigger_price:
    print(f"🚀 매수 신호 발생: 현재가({current_price}) > 매수 트리거({buy_trigger_price:.2f})")
    return "BUY"
  
  # 숏 포지션(공매도)은 고려하지 않으므로, 하방 돌파는 매도 신호가 아닙니다.
  # 대신, 보유 중인 포지션을 청산하는 로직은 RiskManager에서 처리합니다.
  # if current_price < sell_trigger_price:
  #   return "SELL"

  return "HOLD"