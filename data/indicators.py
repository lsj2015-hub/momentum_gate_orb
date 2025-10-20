# ORB 전략의 핵심인 **시가 돌파 범위(ORB)**와 **거래량가중평균가(VWAP)**를 계산하는 함수를 먼저 구현합니다.

import pandas as pd
import pandas_ta as ta

def calculate_orb(df: pd.DataFrame, timeframe: int = 15) -> pd.Series:
  """
  개장 후 특정 시간(timeframe) 동안의 고가(ORH)와 저가(ORL)를 계산합니다.
  """
  orb_time_str = (pd.Timestamp.now(tz='Asia/Seoul').normalize() + pd.Timedelta(hours=9, minutes=timeframe)).strftime('%H:%M')
  
  # 개장 후 timeframe까지의 데이터만 필터링
  opening_range_df = df.between_time('09:00', orb_time_str)
  
  if not opening_range_df.empty:
    orh = opening_range_df['high'].max()
    orl = opening_range_df['low'].min()
    print(f"✅ ORB 계산 완료 (ORH: {orh}, ORL: {orl})")
    return pd.Series({'orh': orh, 'orl': orl})
  else:
    print("⚠️ ORB 계산을 위한 데이터가 부족합니다.")
    return pd.Series({'orh': None, 'orl': None})


def add_vwap(df: pd.DataFrame):
  """DataFrame에 VWAP(거래량가중평균가)를 계산하여 추가합니다."""
  df.ta.vwap(append=True)
  print("✅ VWAP 지표 추가 완료")