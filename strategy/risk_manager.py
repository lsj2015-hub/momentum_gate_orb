# **익절(Take-Profit)**과 손절(Stop-Loss) 규칙을 정의합니다.

from typing import Dict, Optional

def manage_position(
  position: Dict, 
  current_price: float
) -> Optional[str]:
  """
  보유 중인 포지션의 익절 또는 손절 조건을 확인합니다.

  Args:
    position: 보유 포지션 정보. 
              {'entry_price': 10000, 'size': 10, ...}
    current_price: 현재가

  Returns:
    "TAKE_PROFIT", "STOP_LOSS", or None
  """
  if not position:
    return None

  entry_price = position.get('entry_price')
  if not entry_price:
    return None

  # --- 리스크 관리 규칙 정의 ---
  # 예시: 익절 +2.5%, 손절 -1.0%
  TAKE_PROFIT_PCT = 2.5
  STOP_LOSS_PCT = -1.0
  # ----------------------------

  profit_pct = ((current_price - entry_price) / entry_price) * 100

  # 익절 조건 확인
  if profit_pct >= TAKE_PROFIT_PCT:
    print(f"💰 익절 신호 발생: 현재 수익률({profit_pct:.2f}%) >= 목표 수익률({TAKE_PROFIT_PCT}%)")
    return "TAKE_PROFIT"
  
  # 손절 조건 확인
  if profit_pct <= STOP_LOSS_PCT:
    print(f"🛑 손절 신호 발생: 현재 수익률({profit_pct:.2f}%) <= 손절률({STOP_LOSS_PCT}%)")
    return "STOP_LOSS"

  return None