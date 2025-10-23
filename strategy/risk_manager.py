# **익절(Take-Profit)**과 손절(Stop-Loss) 규칙을 정의합니다.

import pandas as pd
from typing import Dict, Optional

# 설정 파일 로더
from config.loader import config

def manage_position(
  position: Dict,
  current_price: float,
  vwap: Optional[float] = None
) -> Optional[str]:
  """
  보유 중인 포지션의 익절 또는 손절 조건을 확인합니다.
  VWAP 기반 손절 로직 추가.

  Args:
    position: 보유 포지션 정보.
              {'entry_price': 10000, 'size': 10, ...}
    current_price: 현재가
    vwap: 현재 VWAP 값 (Optional)

  Returns:
    "TAKE_PROFIT", "STOP_LOSS", "STOP_LOSS_VWAP", or None
  """
  if not position:
    return None

  entry_price = position.get('entry_price')
  if not entry_price:
    print("⚠️ 포지션에 진입 가격 정보가 없습니다.")
    return None

  # --- 리스크 관리 규칙 정의 (config 객체에서 로드) ---
  take_profit_pct = config.strategy.take_profit_pct if hasattr(config.strategy, 'take_profit_pct') else 2.5
  stop_loss_pct = config.strategy.stop_loss_pct if hasattr(config.strategy, 'stop_loss_pct') else -1.0
  stop_loss_vwap_pct = config.strategy.stop_loss_vwap_pct if hasattr(config.strategy, 'stop_loss_vwap_pct') else 0.5
  # ---------------------------------------------------
  
  profit_pct = ((current_price - entry_price) / entry_price) * 100

  # 1. 익절 조건 확인
  if profit_pct >= take_profit_pct:
    print(f"💰 익절 신호 발생 (고정 수익률): 현재 수익률({profit_pct:.2f}%) >= 목표 수익률({take_profit_pct}%)")
    return "TAKE_PROFIT"

  # 2. VWAP 기반 손절 조건 확인 (VWAP 값이 유효하고, VWAP 손절 설정값이 있을 경우)
  if vwap is not None and stop_loss_vwap_pct is not None and current_price < vwap * (1 - stop_loss_vwap_pct / 100):
      print(f"🛑 손절 신호 발생 (VWAP 이탈): 현재가({current_price}) < VWAP 손절 기준({vwap * (1 - stop_loss_vwap_pct / 100):.2f})")
      return "STOP_LOSS_VWAP" # VWAP 손절 신호 구분

  # 3. 고정 비율 손절 조건 확인
  if profit_pct <= stop_loss_pct:
    print(f"🛑 손절 신호 발생 (고정 손실률): 현재 수익률({profit_pct:.2f}%) <= 손절률({stop_loss_pct}%)")
    return "STOP_LOSS"

  return None