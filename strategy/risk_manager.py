# **익절(Take-Profit)**과 손절(Stop-Loss) 규칙을 정의합니다.

import pandas as pd
from typing import Dict, Optional
from config.loader import config

# ❗️ 익절/손절 비율 (추후 설정 파일로 이동 필요)
TAKE_PROFIT_PCT = config.strategy.take_profit_pct
STOP_LOSS_PCT = config.strategy.stop_loss_pct

def manage_position(
  position: Dict,
  df: pd.DataFrame
) -> Optional[str]:
  """
  보유 중인 포지션의 익절, 손절, 또는 기타 청산 조건을 확인합니다.
  Args:
    position: 보유 포지션 정보. {'entry_price': 10000, 'size': 10, ...}
    df: 'close', EMA 컬럼들('EMA_short', 'EMA_long'), 'vwap' 컬럼 포함 DataFrame
        (EMA 컬럼명은 config 설정에 따라 동적으로 생성됨. 예: 'EMA_9')
  Returns:
    "TAKE_PROFIT", "STOP_LOSS", "EMA_CROSS_SELL", "VWAP_BREAK_SELL", or None
  """
  if not position or df.empty:
    return None

  entry_price = position.get('entry_price')
  partial_profit_taken = position.get('partial_profit_taken', False) # 부분 익절 실행 여부 확인
  if not entry_price:
    return None

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
  if (config.strategy.partial_take_profit_pct is not None and
      not partial_profit_taken and
      profit_pct >= config.strategy.partial_take_profit_pct):
    print(f"Partial 💰 부분 익절 신호 발생: 현재 수익률({profit_pct:.2f}%) >= 부분 익절 목표({config.strategy.partial_take_profit_pct}%)")
    return "PARTIAL_TAKE_PROFIT" # 부분 익절 신호 반환

  # --- 1. 익절 조건 확인 (config 값 사용) ---
  if profit_pct >= config.strategy.take_profit_pct:
    print(f"💰 (전체) 익절 신호 발생 (고정 비율): 현재 수익률({profit_pct:.2f}%) >= 목표 수익률({config.strategy.take_profit_pct}%)")
    return "TAKE_PROFIT"

  # --- 2. 손절 조건 확인 (config 값 사용) ---
  if profit_pct <= config.strategy.stop_loss_pct:
    print(f"🛑 손절 신호 발생 (고정 비율): 현재 수익률({profit_pct:.2f}%) <= 손절률({config.strategy.stop_loss_pct}%)")
    return "STOP_LOSS"

  # --- 3. EMA 데드크로스 청산 조건 확인 ---
  if (latest_ema_short is not None and latest_ema_long is not None and
      latest_ema_short < latest_ema_long):
    if len(df) > 1:
        prev_ema_short = df[ema_short_col].iloc[-2] # 이전 값 접근 시에도 동적 컬럼명 사용
        prev_ema_long = df[ema_long_col].iloc[-2]
        if prev_ema_short >= prev_ema_long:
             print(f"📉 청산 신호 발생 (EMA 데드크로스): EMA 단기({latest_ema_short:.2f}) < EMA 장기({latest_ema_long:.2f})")
             return "EMA_CROSS_SELL"
    else:
        print(f"📉 청산 신호 발생 (EMA 데드크로스): EMA 단기({latest_ema_short:.2f}) < EMA 장기({latest_ema_long:.2f})")
        return "EMA_CROSS_SELL"

  # --- 4. VWAP 하향 이탈 손절 조건 확인 ---
  # ❗️ config.strategy.stop_loss_vwap_pct 값이 설정되었는지 확인 후 로직 실행
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
  else: # --- 기존 VWAP 이탈 로직 (단순 하향 이탈) ---
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