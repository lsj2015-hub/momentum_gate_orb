import pandas as pd
from typing import Dict
from config.loader import config # 설정 로더 import

# ❗️ 임계값을 config 객체에서 직접 사용 (전역 변수 불필요)

def check_breakout_signal(
    df: pd.DataFrame,
    orb_levels: pd.Series
    # breakout_buffer 인자 제거 (config에서 직접 사용)
) -> str:
  """
  ORB 상단 돌파 및 추가 필터 조건(RVOL, OBI, EMA, 체결강도)을 확인하여 매수 신호를 생성합니다.
  Args:
    df: 'close', 'rvol', 'obi', 'EMA_short', 'EMA_long', 'strength' 컬럼 포함 DataFrame
        (EMA 컬럼명은 config 설정에 따라 동적으로 생성됨. 예: 'EMA_9')
    orb_levels: 'orh'와 'orl'을 포함하는 Series
  Returns:
    "BUY" or "HOLD"
  """
  if df.empty or orb_levels.empty:
    return "HOLD"

  current_price = df['close'].iloc[-1]
  orh = orb_levels.get('orh')
  orl = orb_levels.get('orl')

  if orh is None or orl is None:
    return "HOLD" # ORB가 아직 계산되지 않았으면 관망

  # --- 지표 값 가져오기 (가장 최근 값) ---
  latest_rvol = df['rvol'].iloc[-1] if 'rvol' in df.columns and not pd.isna(df['rvol'].iloc[-1]) else None # NaN도 None 처리
  latest_obi = df['obi'].iloc[-1] if 'obi' in df.columns and not pd.isna(df['obi'].iloc[-1]) else None       # NaN도 None 처리

  # EMA 컬럼명을 config 값으로 동적 생성
  ema_short_col = f'EMA_{config.strategy.ema_short_period}'
  ema_long_col = f'EMA_{config.strategy.ema_long_period}'
  latest_ema_short = df[ema_short_col].iloc[-1] if ema_short_col in df.columns else None
  latest_ema_long = df[ema_long_col].iloc[-1] if ema_long_col in df.columns else None
  latest_strength = df['strength'].iloc[-1] if 'strength' in df.columns and not pd.isna(df['strength'].iloc[-1]) else None
  
  # --- 돌파 기준 가격 계산 (config 값 사용) ---
  buy_trigger_price = orh * (1 + config.strategy.breakout_buffer / 100)

  # --- 1. ORB 상단 돌파 확인 ---
  is_breakout = current_price > buy_trigger_price
  if not is_breakout:
    return "HOLD"

  print(f"🚀 ORB 상단 돌파: 현재가({current_price}) > 매수 트리거({buy_trigger_price:.2f})")

  # --- 2. 진입 필터 조건 확인 (config 값 사용) ---
  # RVOL 조건
  rvol_ok = latest_rvol is not None and latest_rvol >= config.strategy.rvol_threshold
  # ✅ None 체크 후 포맷팅 또는 'N/A' 출력
  rvol_str = f"{latest_rvol:.2f}" if latest_rvol is not None else "N/A"
  print(f"   - RVOL Check: {rvol_str} >= {config.strategy.rvol_threshold} -> {'OK' if rvol_ok else 'NG'}")

  # OBI 조건
  obi_ok = latest_obi is not None and latest_obi >= config.strategy.obi_threshold
  # ✅ None 체크 후 포맷팅 또는 'N/A' 출력
  obi_str = f"{latest_obi:.2f}" if latest_obi is not None else "N/A"
  print(f"   - OBI Check: {obi_str} >= {config.strategy.obi_threshold} -> {'OK' if obi_ok else 'NG'}")

  # 상승 모멘텀 조건 (EMA)
  momentum_ok = (latest_ema_short is not None and latest_ema_long is not None and
                   latest_ema_short > latest_ema_long)
  # ✅ None 체크 후 포맷팅 또는 'N/A' 출력
  ema_short_str = f"{latest_ema_short:.2f}" if latest_ema_short is not None else "N/A"
  ema_long_str = f"{latest_ema_long:.2f}" if latest_ema_long is not None else "N/A"
  print(f"   - Momentum (EMA) Check: {ema_short_str} > {ema_long_str} -> {'OK' if momentum_ok else 'NG'}")

  # 체결강도 조건
  strength_ok = latest_strength is not None and latest_strength >= config.strategy.strength_threshold
  # ✅ None 체크 후 포맷팅 또는 'N/A' 출력
  strength_str = f"{latest_strength:.2f}" if latest_strength is not None else "N/A"
  print(f"   - Strength Check: {strength_str} >= {config.strategy.strength_threshold} -> {'OK' if strength_ok else 'NG'}")


  # --- 최종 진입 결정 ---
  if rvol_ok and obi_ok and momentum_ok and strength_ok:
    print(f"🔥 모든 진입 조건 충족! 매수 신호 발생!")
    return "BUY"
  else:
    print("⚠️ ORB는 돌파했으나 필터 조건 미충족. 진입 보류.")
    return "HOLD"