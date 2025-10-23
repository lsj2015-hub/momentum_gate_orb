# **익절(Take-Profit)**과 손절(Stop-Loss) 규칙을 정의합니다.

from typing import Dict, Optional
# config loader 추가
from config.loader import config

def manage_position(
  position: Dict,
  current_price: float,
  current_vwap: Optional[float] = None # current_vwap 인자 추가
) -> Optional[str]:
  """
  보유 중인 포지션의 익절 또는 손절 조건을 확인합니다.
  VWAP 기반 손절 로직 추가.

  Args:
    position: 보유 포지션 정보.
              {'entry_price': 10000, 'size': 10, ...}
    current_price: 현재가
    current_vwap: 현재 VWAP 값 (Optional)

  Returns:
    "TAKE_PROFIT", "STOP_LOSS", "VWAP_STOP_LOSS", or None
  """
  if not position:
    return None

  entry_price = position.get('entry_price')
  if not entry_price or entry_price == 0: # entry_price가 0인 경우 방지
    print("⚠️ 손익 계산 불가: 진입 가격 정보 없음")
    return None

  # --- 리스크 관리 규칙 (config.yaml 에서 로드) ---
  TAKE_PROFIT_PCT = config.strategy.take_profit_pct
  STOP_LOSS_PCT = config.strategy.stop_loss_pct
  # VWAP 손절 설정 로드 (설정 파일에 없으면 기본값 None 사용)
  STOP_LOSS_VWAP_PCT = getattr(config.strategy, 'stop_loss_vwap_pct', None)
  # --------------------------------------------------

  profit_pct = ((current_price - entry_price) / entry_price) * 100

  # 익절 조건 확인
  if TAKE_PROFIT_PCT is not None and profit_pct >= TAKE_PROFIT_PCT:
    print(f"💰 익절 신호 발생: 현재 수익률({profit_pct:.2f}%) >= 목표 수익률({TAKE_PROFIT_PCT}%)")
    return "TAKE_PROFIT"

  # 고정 손절 조건 확인
  if STOP_LOSS_PCT is not None and profit_pct <= STOP_LOSS_PCT:
    print(f"🛑 고정 손절 신호 발생: 현재 수익률({profit_pct:.2f}%) <= 손절률({STOP_LOSS_PCT}%)")
    return "STOP_LOSS"

  # VWAP 기반 손절 조건 확인 (설정값이 있고, VWAP 값이 유효할 때)
  if STOP_LOSS_VWAP_PCT is not None and current_vwap is not None and current_vwap > 0:
      vwap_deviation_pct = ((current_price - current_vwap) / current_vwap) * 100
      # 현재가가 VWAP 아래로 설정된 비율 이상 하락했을 때
      if vwap_deviation_pct <= -abs(STOP_LOSS_VWAP_PCT): # 음수 비교 위해 abs 사용
           print(f"📉 VWAP 손절 신호 발생: 현재가({current_price})가 VWAP({current_vwap:.2f}) 대비 {vwap_deviation_pct:.2f}% <= 기준(-{abs(STOP_LOSS_VWAP_PCT)}%)")
           return "VWAP_STOP_LOSS"

  return None