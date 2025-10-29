# ORB 전략의 핵심인 **시가 돌파 범위(ORB)**와 **거래량가중평균가(VWAP)**를 계산하는 함수를 먼저 구현합니다.

import pandas as pd
from typing import Dict, Optional

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
def calculate_rvol(df: pd.DataFrame, window: int = 20) -> Optional[float]:
    """
    현재 거래량 대비 이동평균 거래량 비율(RVOL)을 계산합니다.
    DataFrame을 직접 받아 이동평균을 계산합니다.

    Args:
      df: 시계열 데이터프레임 ('volume' 컬럼 포함)
      window: 이동평균 계산 기간 (기본값: 20)

    Returns:
      계산된 RVOL 값 (float) 또는 계산 불가 시 None
    """
    # 필요한 'volume' 컬럼 존재 여부 및 데이터 길이 확인 (window + 현재 봉 1개)
    if 'volume' not in df.columns or len(df) < window + 1:
        print(f"⚠️ RVOL({window}) 계산을 위한 데이터가 부족합니다 (필요: {window + 1}개, 현재: {len(df)}개).")
        return None

    try:
        # 최근 window 기간 동안의 거래량 이동평균 계산 (현재 봉 제외)
        # .iloc[-(window + 1):-1] : 뒤에서부터 (window + 1)번째 데이터부터 마지막 데이터 직전까지 선택
        avg_volume = df['volume'].iloc[-(window + 1):-1].mean()
        # 현재 봉(가장 마지막 데이터)의 거래량
        current_volume = df['volume'].iloc[-1]

        # 이동평균 거래량이 0이면 계산 불가 (0으로 나누기 방지)
        if avg_volume is None or pd.isna(avg_volume) or avg_volume <= 0:
            print(f"⚠️ 이동평균 거래량({avg_volume})이 유효하지 않아 RVOL({window}) 계산 불가.")
            return None

        # RVOL 계산: (현재 거래량 / 평균 거래량) * 100
        rvol = (current_volume / avg_volume) * 100
        print(f"✅ RVOL({window}) 계산 완료: {rvol:.2f}% (현재:{current_volume:.0f} / 평균:{avg_volume:.0f})")
        return rvol
    except Exception as e: # 계산 중 예외 발생 시
        print(f"❌ RVOL({window}) 계산 중 오류 발생: {e}")
        return None

# --- OBI 계산 함수 (구조만 정의) ---
def calculate_obi(total_bid_volume: Optional[int], total_ask_volume: Optional[int]) -> Optional[float]:
    """
    호가 잔량 비율 (Order Book Imbalance, OBI)을 계산합니다.
    OBI = (총 매수 잔량 / 총 매도 잔량) * 100

    Args:
      total_bid_volume: 총 매수 호가 잔량 (정수)
      total_ask_volume: 총 매도 호가 잔량 (정수)

    Returns:
      계산된 OBI 값 (float) 또는 계산 불가 시 None
    """
    # 입력값이 유효하지 않으면 None 반환
    if total_bid_volume is None or total_ask_volume is None:
        print("⚠️ OBI 계산 입력값 누락 (총매수 또는 총매도 잔량).")
        return None

    # 총 매도 잔량이 0 이하이면 계산 불가 (0으로 나누기 방지)
    if total_ask_volume <= 0:
        print("⚠️ 총 매도 잔량이 0 이하이므로 OBI 계산 불가.")
        # 이 경우, 매우 큰 값(무한대) 대신 특정 상한값(예: 10000%)을 반환하거나 None을 반환할 수 있습니다.
        # 여기서는 None을 반환합니다. 필요시 상한값으로 변경 가능합니다.
        return None

    try:
        # OBI 계산: (총 매수 잔량 / 총 매도 잔량) * 100
        obi = (total_bid_volume / total_ask_volume) * 100
        print(f"✅ OBI 계산 완료: {obi:.2f}% (매수잔량:{total_bid_volume}/매도잔량:{total_ask_volume})")
        return obi
    except Exception as e: # 기타 예상치 못한 오류 처리
        print(f"❌ OBI 계산 중 오류 발생: {e}")
        return None