# ORB 전략의 핵심인 **시가 돌파 범위(ORB)**와 **거래량가중평균가(VWAP)**를 계산하는 함수를 먼저 구현합니다.

import pandas as pd
# import pandas_ta as ta # pandas_ta import 제거

def calculate_orb(df: pd.DataFrame, timeframe: int = 15) -> pd.Series:
  """
  개장 후 특정 시간(timeframe) 동안의 고가(ORH)와 저가(ORL)를 계산합니다.
  """
  # 시간대 정보 추가 (한국 시간 기준)
  try:
      # tz_localize(None)으로 시간대 정보 제거 후 시간 비교
      df_tz_naive = df.tz_localize(None) if df.index.tz is not None else df
      start_time = pd.Timestamp.now().normalize() + pd.Timedelta(hours=9) # 장 시작 시간 (09:00)
      orb_end_time = start_time + pd.Timedelta(minutes=timeframe) # ORB 종료 시간

      # ORB 구간 데이터 필터링 (시간대 정보 없는 인덱스 사용)
      opening_range_df = df_tz_naive[(df_tz_naive.index >= start_time) & (df_tz_naive.index < orb_end_time)]

      if not opening_range_df.empty:
          orh = opening_range_df['high'].max()
          orl = opening_range_df['low'].min()
          print(f"✅ ORB 계산 완료 (ORH: {orh}, ORL: {orl})")
          return pd.Series({'orh': orh, 'orl': orl})
      else:
          print("⚠️ ORB 계산을 위한 데이터가 부족합니다 (09:00 ~ {orb_end_time.strftime('%H:%M')}).")
          return pd.Series({'orh': None, 'orl': None})
  except Exception as e:
      print(f"❌ ORB 계산 중 오류 발생: {e}")
      return pd.Series({'orh': None, 'orl': None})


def add_vwap(df: pd.DataFrame):
  """DataFrame에 VWAP(거래량가중평균가)를 직접 계산하여 추가합니다."""
  # VWAP 계산: Sum(Price * Volume) / Sum(Volume)
  # typical_price = (df['high'] + df['low'] + df['close']) / 3 # 일반적인 VWAP 계산 시 사용
  # 여기서는 종가(close)를 기준으로 계산
  try:
    # 일별 누적 계산을 위해 날짜별로 그룹화하지 않고 전체 누적 계산
    # (키움 API는 당일 데이터만 제공하므로 일반적으로 문제 없음)
    cumulative_pv = (df['close'] * df['volume']).cumsum()
    cumulative_volume = df['volume'].cumsum()

    # 0으로 나누는 것을 방지
    df['VWAP'] = cumulative_pv / cumulative_volume.replace(0, float('nan')) # 0 대신 NaN으로 대체 후 계산
    # df.fillna(method='ffill', inplace=True) # 첫 행 NaN 값 채우기 (선택 사항)
    print("✅ VWAP 지표 추가 완료 (직접 계산)")
  except Exception as e:
    print(f"❌ VWAP 계산 중 오류 발생: {e}")
    if 'VWAP' not in df.columns: # 오류 발생 시 컬럼 추가 방지
        df['VWAP'] = float('nan') # 오류 시 NaN 값으로 컬럼 추가