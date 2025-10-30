# **익절(Take-Profit)**과 손절(Stop-Loss) 규칙을 정의합니다.

import pandas as pd
from typing import Dict, Optional
from config.loader import config

def manage_position(
  position: Dict,
  df: pd.DataFrame
) -> Optional[str]:
  """
  보유 중인 포지션의 익절, 손절, 또는 기타 청산 조건을 확인합니다.
  Args:
    position: 보유 포지션 정보. 
              {
                'entry_price': 10000, 'size': 10, ...,
                'target_profit_pct': 2.5,  <-- 이 값을 읽도록 수정
                'stop_loss_pct': -1.0,   <-- 이 값을 읽도록 수정
                'partial_profit_pct': 1.5 <-- 이 값을 읽도록 수정
              }
    df: 'close', EMA 컬럼들, 'vwap' 컬럼 포함 DataFrame
  Returns:
    "PARTIAL_TAKE_PROFIT", "TAKE_PROFIT", "STOP_LOSS", "EMA_CROSS_SELL", "VWAP_BREAK_SELL", or None
  """
  if not position or df.empty:
    return None

  entry_price = position.get('entry_price')
  partial_profit_taken = position.get('partial_profit_taken', False) 
  if not entry_price:
    return None

  # --- 👇 [수정] 포지션 딕셔너리에서 직접 리스크 설정값을 읽어옵니다. ---
  # config 전역 변수 대신 position에 저장된 값을 사용
  # 만약 값이 없다면 config의 기본값을 안전장치(fallback)로 사용합니다.
  TAKE_PROFIT_PCT = position.get('target_profit_pct', config.strategy.take_profit_pct)
  STOP_LOSS_PCT = position.get('stop_loss_pct', config.strategy.stop_loss_pct)
  PARTIAL_TAKE_PROFIT_PCT = position.get('partial_profit_pct', config.strategy.partial_take_profit_pct)
  # --- 👆 [수정] ---

  current_price = df['close'].iloc[-1]
  # EMA 컬럼명을 config 값으로 동적 생성
  ema_short_col = f'EMA_{config.strategy.ema_short_period}'
  ema_long_col = f'EMA_{config.strategy.ema_long_period}'
  latest_ema_short = df[ema_short_col].iloc[-1] if ema_short_col in df.columns else None
  latest_ema_long = df[ema_long_col].iloc[-1] if ema_long_col in df.columns else None
  latest_vwap = df['vwap'].iloc[-1] if 'vwap' in df.columns else None

  profit_pct = ((current_price - entry_price) / entry_price) * 100

  # --- 0. 부분 익절 조건 확인 ---
  # partial_take_profit_pct가 설정되어 있고, 아직 부분 익절을 안했고, 목표 수익률 도달 시
  if (PARTIAL_TAKE_PROFIT_PCT is not None and
      not partial_profit_taken and
      profit_pct >= PARTIAL_TAKE_PROFIT_PCT): # <-- 수정: position에서 읽어온 값 사용
    print(f"Partial 💰 부분 익절 신호 발생: 현재 수익률({profit_pct:.2f}%) >= 부분 익절 목표({PARTIAL_TAKE_PROFIT_PCT}%)")
    return "PARTIAL_TAKE_PROFIT" # 부분 익절 신호 반환

  # --- 1. 익절 조건 확인 (position 값 사용) ---
  if profit_pct >= TAKE_PROFIT_PCT: # <-- 수정: position에서 읽어온 값 사용
    print(f"💰 (전체) 익절 신호 발생 (고정 비율): 현재 수익률({profit_pct:.2f}%) >= 목표 수익률({TAKE_PROFIT_PCT}%)")
    return "TAKE_PROFIT"

  # --- 2. 손절 조건 확인 (position 값 사용) ---
  if profit_pct <= STOP_LOSS_PCT: # <-- 수정: position에서 읽어온 값 사용
    print(f"🛑 손절 신호 발생 (고정 비율): 현재 수익률({profit_pct:.2f}%) <= 손절률({STOP_LOSS_PCT}%)")
    return "STOP_LOSS"

  # --- 3. EMA 데드크로스 청산 조건 확인 ---
  if (latest_ema_short is not None and latest_ema_long is not None and
      latest_ema_short < latest_ema_long):
    if len(df) > 1:
        prev_ema_short = df[ema_short_col].iloc[-2] 
        prev_ema_long = df[ema_long_col].iloc[-2]
        if prev_ema_short >= prev_ema_long:
             print(f"📉 청산 신호 발생 (EMA 데드크로스): EMA 단기({latest_ema_short:.2f}) < EMA 장기({latest_ema_long:.2f})")
             return "EMA_CROSS_SELL"
    else:
        print(f"📉 청산 신호 발생 (EMA 데드크로스): EMA 단기({latest_ema_short:.2f}) < EMA 장기({latest_ema_long:.2f})")
        return "EMA_CROSS_SELL"

  # --- 4. VWAP 하향 이탈 손절 조건 확인 ---
  if config.strategy.stop_loss_vwap_pct is not None:
      vwap_stop_trigger = latest_vwap * (1 - config.strategy.stop_loss_vwap_pct / 100) if latest_vwap else None
      if vwap_stop_trigger is not None and current_price < vwap_stop_trigger:
         if len(df) > 1:
             prev_price = df['close'].iloc[-2]
             prev_vwap_trigger = (df['vwap'].iloc[-2] * (1 - config.strategy.stop_loss_vwap_pct / 100)) if 'vwap' in df.columns and len(df)>1 else None
             if prev_vwap_trigger is not None and prev_price >= prev_vwap_trigger:
                 print(f"📉 청산 신호 발생 (VWAP {config.strategy.stop_loss_vwap_pct}% 이탈): 현재가({current_price}) < VWAP Stop Trigger({vwap_stop_trigger:.2f})")
                 return "VWAP_BREAK_SELL"
         else:
            print(f"📉 청산 신호 발생 (VWAP {config.strategy.stop_loss_vwap_pct}% 이탈): 현재가({current_price}) < VWAP Stop Trigger({vwap_stop_trigger:.2f})")
            return "VWAP_BREAK_SELL"
  else: 
      if latest_vwap is not None and current_price < latest_vwap:
         if len(df) > 1:
             prev_price = df['close'].iloc[-2]
             prev_vwap = df['vwap'].iloc[-2] if 'vwap' in df.columns else None
             if prev_vwap is not None and prev_price >= prev_vwap:
                 print(f"📉 청산 신호 발생 (VWAP 단순 이탈): 현재가({current_price}) < VWAP({latest_vwap:.2f})")
                 return "VWAP_BREAK_SELL"
         else:
            print(f"📉 청산 신호 발생 (VWAP 단순 이탈): 현재가({current_price}) < VWAP({latest_vwap:.2f})")
            return "VWAP_BREAK_SELL"

  return None