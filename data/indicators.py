# ORB 전략의 핵심인 **시가 돌파 범위(ORB)**와 **거래량가중평균가(VWAP)**를 계산하는 함수를 먼저 구현합니다.

# data/indicators.py

import pandas as pd
# import pandas_ta as ta # pandas_ta import 제거
from typing import Dict, Optional

# calculate_orb 함수 (기존 코드 유지)
def calculate_orb(df: pd.DataFrame, timeframe: int = 15) -> pd.Series:
  """
  개장 후 특정 시간(timeframe) 동안의 고가(ORH)와 저가(ORL)를 계산합니다.
  """
  try:
    now_kst = pd.Timestamp.now(tz='Asia/Seoul')
    start_time_str = '09:00'
    orb_end_time = (now_kst.normalize() + pd.Timedelta(hours=9, minutes=timeframe))
    orb_end_time_str = orb_end_time.strftime('%H:%M')

    if df.empty:
        print("🔍 [DEBUG_ORB] 원본 DataFrame이 비어 있습니다.")
        raise ValueError("입력 DataFrame이 비어 있습니다.")

    opening_range_df = df.between_time(start_time_str, orb_end_time_str)

    if not opening_range_df.empty:
      orh = opening_range_df['high'].max()
      orl = opening_range_df['low'].min()
      print(f"✅ ORB 계산 완료 (ORH: {orh}, ORL: {orl})")
      return pd.Series({'orh': orh, 'orl': orl})
    else:
      print(f"⚠️ ORB 계산을 위한 데이터가 부족합니다 ({start_time_str} ~ {orb_end_time.strftime('%H:%M')}).")
      return pd.Series({'orh': None, 'orl': None})

  except Exception as e:
    print(f"❌ ORB 계산 중 오류 발생: {e}")
    return pd.Series({'orh': None, 'orl': None})

# add_vwap 함수 (직접 계산 방식 유지)
def add_vwap(df: pd.DataFrame):
  """DataFrame에 VWAP(거래량가중평균가)를 직접 계산하여 추가합니다."""
  try:
    if 'close' in df.columns and 'volume' in df.columns:
        cumulative_pv = (df['close'] * df['volume']).cumsum()
        cumulative_volume = df['volume'].cumsum()
        df['VWAP'] = cumulative_pv / cumulative_volume.replace(0, float('nan'))
        print("✅ VWAP 지표 추가 완료 (직접 계산)")
    else:
        print("⚠️ VWAP 계산에 필요한 컬럼(close, volume)이 부족합니다.")
  except Exception as e:
    print(f"❌ VWAP 계산 중 오류 발생: {e}")
    if 'VWAP' not in df.columns:
        df['VWAP'] = float('nan')

# --- 👇 EMA 계산 함수 수정 (pandas ewm 사용) ---
def add_ema(df: pd.DataFrame, short_period: int = 9, long_period: int = 20):
    """DataFrame에 단기 및 장기 EMA(지수이동평균)를 직접 계산하여 추가합니다 (pandas ewm 사용)."""
    try:
        if 'close' in df.columns and len(df) >= max(short_period, long_period): # 최소 데이터 길이 확인
            # 단기 EMA 계산 (adjust=False는 재귀적 계산 방식 사용)
            ema_short_col = f'EMA_{short_period}'
            df[ema_short_col] = df['close'].ewm(span=short_period, adjust=False).mean()

            # 장기 EMA 계산
            ema_long_col = f'EMA_{long_period}'
            df[ema_long_col] = df['close'].ewm(span=long_period, adjust=False).mean()
            print(f"✅ EMA({short_period}/{long_period}) 지표 추가 완료 (직접 계산)")
        elif 'close' not in df.columns:
            print("⚠️ EMA 계산에 필요한 'close' 컬럼이 없습니다.")
        else: # 데이터 부족
            print(f"⚠️ EMA 계산 위한 데이터 부족 (필요: {max(short_period, long_period)}, 현재: {len(df)})")
            # 데이터 부족 시에도 NaN 컬럼 생성
            ema_short_col = f'EMA_{short_period}'
            ema_long_col = f'EMA_{long_period}'
            if ema_short_col not in df.columns: df[ema_short_col] = float('nan')
            if ema_long_col not in df.columns: df[ema_long_col] = float('nan')
    except Exception as e:
        print(f"❌ EMA 계산 중 오류 발생: {e}")
        # 오류 시에도 NaN 컬럼 생성
        ema_short_col = f'EMA_{short_period}'
        ema_long_col = f'EMA_{long_period}'
        if ema_short_col not in df.columns: df[ema_short_col] = float('nan')
        if ema_long_col not in df.columns: df[ema_long_col] = float('nan')
# --- 👆 EMA 계산 함수 수정 끝 ---

# --- RVOL 계산 함수 (구조만 정의) ---
def calculate_rvol(current_volume: float, historical_avg_volume: Optional[float]) -> Optional[float]:
    """
    상대거래량 (Relative Volume, RVOL)을 계산합니다.
    (주의: historical_avg_volume 데이터 준비 로직은 별도 구현 필요)
    """
    if historical_avg_volume is not None and historical_avg_volume > 0:
        rvol = (current_volume / historical_avg_volume) * 100
        return rvol
    else:
        return None

# --- OBI 계산 함수 (구조만 정의) ---
def calculate_obi(total_bid_volume: Optional[int], total_ask_volume: Optional[int]) -> Optional[float]:
    """
    호가 잔량 비율 (Order Book Imbalance, OBI)을 계산합니다.
    (주의: 실시간 호가 데이터 필요)
    """
    if total_bid_volume is not None and total_ask_volume is not None and total_ask_volume > 0:
        obi = (total_bid_volume / total_ask_volume) * 100
        return obi
    else:
        return None