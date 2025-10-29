# data/indicators.py
import pandas as pd
import numpy as np
from typing import Dict, Optional
from config.loader import config # 설정 로더 추가

def calculate_orb(df: pd.DataFrame, timeframe: int = 15) -> pd.Series:
  """
  개장 후 특정 시간(timeframe) 동안의 고가(ORH)와 저가(ORL)를 계산합니다.
  """
  try:
    now_kst = pd.Timestamp.now(tz='Asia/Seoul')
    start_time_obj = now_kst.normalize() + pd.Timedelta(hours=9)
    orb_end_time_obj = start_time_obj + pd.Timedelta(minutes=timeframe)
    start_time_str = start_time_obj.strftime('%H:%M:%S') # 초 포함
    orb_end_time_str = orb_end_time_obj.strftime('%H:%M:%S') # 초 포함

    if df.empty:
        print("🔍 [DEBUG_ORB] 원본 DataFrame 비어 있음.")
        return pd.Series({'orh': None, 'orl': None}) # 빈 Series 반환

    # DataFrame 인덱스가 시간대 정보를 가지고 있는지 확인하고 통일
    if df.index.tz is None:
        df_tz_aware = df.tz_localize('Asia/Seoul', ambiguous='infer')
    elif str(df.index.tz) != 'Asia/Seoul': # 문자열 비교로 변경
        df_tz_aware = df.tz_convert('Asia/Seoul')
    else:
        df_tz_aware = df

    # 시작 시간 이후, ORB 종료 시간 *이전* 데이터 선택 (종료 시간 미포함)
    opening_range_df = df_tz_aware[(df_tz_aware.index >= start_time_obj) & (df_tz_aware.index < orb_end_time_obj)]

    if not opening_range_df.empty:
      orh = opening_range_df['high'].max()
      orl = opening_range_df['low'].min()
      print(f"✅ ORB 계산 완료 ({start_time_obj.strftime('%H:%M')}~{orb_end_time_obj.strftime('%H:%M')}): ORH={orh}, ORL={orl}")
      return pd.Series({'orh': orh, 'orl': orl})
    else:
      print(f"⚠️ ORB 계산 데이터 부족 ({start_time_obj.strftime('%H:%M')} ~ {orb_end_time_obj.strftime('%H:%M')}).")
      return pd.Series({'orh': None, 'orl': None})

  except Exception as e:
    print(f"❌ ORB 계산 중 오류: {e}")
    import traceback
    print(traceback.format_exc()) # 상세 오류 출력
    return pd.Series({'orh': None, 'orl': None})


def add_vwap(df: pd.DataFrame):
  """DataFrame에 VWAP(거래량가중평균가)를 직접 계산하여 추가합니다."""
  try:
    # high, low 컬럼도 확인
    required_cols = ['high', 'low', 'close', 'volume']
    if all(col in df.columns for col in required_cols):
        # typical_price 계산 (고가+저가+종가)/3
        typical_price = (df['high'] + df['low'] + df['close']) / 3
        cumulative_pv = (typical_price * df['volume']).cumsum()
        cumulative_volume = df['volume'].cumsum()
        # 0으로 나누는 경우 방지: cumulative_volume이 0이면 NaN 반환
        df['vwap'] = cumulative_pv / cumulative_volume.replace(0, np.nan) # np.nan 사용
        print("✅ VWAP 지표 추가 완료 (직접 계산)")
    else:
        missing_cols = [col for col in required_cols if col not in df.columns]
        print(f"⚠️ VWAP 계산 필요 컬럼 부족: {missing_cols}")
        if 'vwap' not in df.columns: df['vwap'] = np.nan # NaN 컬럼 생성

  except Exception as e:
    print(f"❌ VWAP 계산 중 오류: {e}")
    if 'vwap' not in df.columns: df['vwap'] = np.nan


def add_ema(df: pd.DataFrame, short_period: int = 9, long_period: int = 20):
    """DataFrame에 단기 및 장기 EMA를 계산하여 추가합니다 (pandas ewm 사용)."""
    try:
        ema_short_col = f'EMA_{short_period}'
        ema_long_col = f'EMA_{long_period}'

        if 'close' in df.columns:
             # 데이터가 충분한지 확인 (최소 기간 이상)
             if len(df) >= short_period:
                 df[ema_short_col] = df['close'].ewm(span=short_period, adjust=False).mean()
             else:
                 print(f"⚠️ 단기 EMA({short_period}) 계산 위한 데이터 부족 (필요: {short_period}, 현재: {len(df)})")
                 if ema_short_col not in df.columns: df[ema_short_col] = np.nan

             if len(df) >= long_period:
                 df[ema_long_col] = df['close'].ewm(span=long_period, adjust=False).mean()
             else:
                  print(f"⚠️ 장기 EMA({long_period}) 계산 위한 데이터 부족 (필요: {long_period}, 현재: {len(df)})")
                  if ema_long_col not in df.columns: df[ema_long_col] = np.nan

             if len(df) >= max(short_period, long_period):
                  print(f"✅ EMA({short_period}/{long_period}) 지표 추가 완료")
        else:
            print("⚠️ EMA 계산 필요 컬럼('close') 없음.")
            if ema_short_col not in df.columns: df[ema_short_col] = np.nan
            if ema_long_col not in df.columns: df[ema_long_col] = np.nan
    except Exception as e:
        print(f"❌ EMA 계산 중 오류: {e}")
        ema_short_col = f'EMA_{short_period}'
        ema_long_col = f'EMA_{long_period}'
        if ema_short_col not in df.columns: df[ema_short_col] = np.nan
        if ema_long_col not in df.columns: df[ema_long_col] = np.nan


def calculate_rvol(df: pd.DataFrame, window: int = 20) -> Optional[float]:
    """
    현재 봉의 거래량을 이전 N개 봉의 평균 거래량과 비교하여 RVOL(상대 거래량 비율)을 계산합니다.
    Args:
        df (pd.DataFrame): 'volume' 컬럼 포함 (시간 오름차순 가정)
        window (int): 평균 거래량 계산 기간
    Returns:
        Optional[float]: 계산된 RVOL 값 (%), 계산 불가 시 None
    """
    if df is None or df.empty or 'volume' not in df.columns or len(df) < window + 1:
        print(f"⚠️ RVOL({window}) 계산 불가: 데이터 부족 (필요: {window + 1}개, 현재: {len(df)}개)")
        return None
    try:
        current_volume = df['volume'].iloc[-1]
        # 현재 봉 제외하고 이전 window 개수만큼 선택
        previous_volumes = df['volume'].iloc[-(window + 1):-1]
        avg_previous_volume = previous_volumes.mean()

        if pd.isna(avg_previous_volume) or avg_previous_volume <= 0:
            print(f"⚠️ RVOL({window}) 계산 불가: 이전 평균 거래량({avg_previous_volume})이 유효하지 않음.")
            return None

        rvol = (current_volume / avg_previous_volume) * 100
        print(f"✅ RVOL({window}) 계산 완료: {rvol:.2f}% (현재:{current_volume:.0f}/평균:{avg_previous_volume:.0f})")
        return rvol
    except Exception as e:
        print(f"🚨 RVOL({window}) 계산 중 오류: {e}")
        return None


def calculate_obi(total_bid_volume: Optional[int], total_ask_volume: Optional[int]) -> Optional[float]:
    """
    호가 잔량 비율 (Order Book Imbalance, OBI)을 계산합니다.
    OBI = 총 매수 잔량 / 총 매도 잔량 (비율)
    Args:
      total_bid_volume: 총 매수 호가 잔량 (정수)
      total_ask_volume: 총 매도 호가 잔량 (정수)
    Returns:
      계산된 OBI 값 (float, 비율) 또는 계산 불가 시 None
    """
    if total_bid_volume is None or total_ask_volume is None:
        print("⚠️ OBI 계산 입력값 누락.")
        return None
    if total_ask_volume <= 0:
        # 매도 잔량이 0이면 매우 강한 매수세 또는 호가 공백. 매우 큰 값 또는 None 반환.
        print("⚠️ OBI 계산: 매도 잔량 0. (매우 강한 매수세 또는 호가 공백)")
        return 1000.0 # 예시: 매우 큰 값 (설정 가능하게 변경 고려)
    try:
        obi = total_bid_volume / total_ask_volume # 비율 계산
        print(f"✅ OBI 계산 완료: {obi:.2f} (매수:{total_bid_volume}/매도:{total_ask_volume})")
        return obi
    except Exception as e:
        print(f"❌ OBI 계산 중 오류: {e}")
        return None


def get_strength(cumulative_buy_volume: int, cumulative_sell_volume: int) -> Optional[float]:
    """
    누적된 매수/매도 체결량을 기반으로 체결강도를 계산합니다.
    체결강도 = (누적 매수 체결량 / 누적 매도 체결량) * 100

    Args:
        cumulative_buy_volume (int): 특정 기간 동안 누적된 매수 체결량
        cumulative_sell_volume (int): 특정 기간 동안 누적된 매도 체결량

    Returns:
        Optional[float]: 계산된 체결강도 값 (%), 계산 불가 시 None
    """
    if cumulative_sell_volume <= 0:
        # 매도 체결량이 0이면 (매수만 있었거나 거래가 없었음)
        if cumulative_buy_volume > 0:
            print("⚠️ Strength 계산: 매도 체결량 0 (매수 우위 극단값)")
            return 1000.0 # 극단적인 매수 우위 상태 (매우 큰 값 또는 다른 값으로 정의 가능)
        else:
            # print("⚠️ Strength 계산 불가: 매수/매도 누적 체결량 모두 0") # 로그 너무 많을 수 있어 주석 처리
            return None # 거래 자체가 없는 경우

    try:
        strength = (cumulative_buy_volume / cumulative_sell_volume) * 100
        # print(f"✅ Strength 계산 완료: {strength:.2f}% (매수:{cumulative_buy_volume}/매도:{cumulative_sell_volume})") # 로그 너무 많을 수 있어 주석 처리
        return strength
    except Exception as e:
        print(f"❌ Strength 계산 중 오류: {e}")
        return None