# 모멘텀 종목을 찾는 로직을 구현합니다.

import pandas as pd
from typing import Optional, List, Dict

from gateway.kiwoom_api import KiwoomAPI

# --- 사용자 설정 가능 기준 (초기 고정값) ---
MIN_VOLUME_RATE = 500.0  # 최소 전일비 거래량 급증률 (%)
MIN_PRICE_RATE = 3.0    # 최소 시가 대비 상승률 (%) (ka10027 API 필터에서 이미 처리됨)
MIN_TRADING_AMOUNT = 10_0000_0000 # 최소 거래대금 (원) (ka10027 API 필터에서 이미 처리됨)
NUM_FINAL_TARGETS = 1    # 최종 선정할 종목 수

async def find_momentum_stocks(api: KiwoomAPI) -> Optional[str]:
  """
  정의된 기준에 따라 모멘텀이 발생한 종목을 찾아 반환합니다.

  Args:
    api: KiwoomAPI 인스턴스

  Returns:
    선정된 종목 코드 (문자열) 또는 None
  """
  print("🔍 모멘텀 종목 탐색 시작...")

  # 1. 거래량 급증 종목 조회
  volume_surge_list = await api.fetch_volume_surge_stocks()
  if not volume_surge_list:
    print("❗️ 거래량 급증 종목을 가져오지 못했습니다.")
    return None
  
  # DataFrame 변환 및 필터링 (급증률 기준)
  vol_df = pd.DataFrame(volume_surge_list)
  vol_df['volume_rate'] = pd.to_numeric(vol_df['sdnin_rt'], errors='coerce') # API 응답 키 'sdnin_rt' [cite: 974]
  vol_filtered = vol_df[vol_df['volume_rate'] >= MIN_VOLUME_RATE]
  if vol_filtered.empty:
      print("ℹ️ 거래량 급증 기준을 만족하는 종목이 없습니다.")
      return None
  print(f"📊 거래량 급증 기준 만족 종목 수: {len(vol_filtered)}")

  # 2. 등락률 상위 종목 조회 (API 호출 시 이미 가격/거래대금/종목 필터 적용됨)
  price_rank_list = await api.fetch_price_rank_stocks(rank_type="1") # 1: 상승률 순위
  if not price_rank_list:
    print("❗️ 등락률 상위 종목을 가져오지 못했습니다.")
    return None

  price_df = pd.DataFrame(price_rank_list)
  # 필요한 컬럼만 선택 (stk_cd, stk_nm, 거래대금 정보가 필요함 - API 응답 확인 필요)
  # ka10027 응답에는 'trde_prica'(거래대금) 필드가 없음 -> 다른 API(예: ka10032) 추가 호출 또는 필터링 기준 변경 필요
  # 우선 종목 코드만 추출
  if price_df.empty:
    print("ℹ️ 등락률 상위 기준을 만족하는 종목이 없습니다.")
    return None
  print(f"📈 등락률 상위 기준 만족 종목 수: {len(price_df)}")

  # 3. 두 조건 교집합 찾기 (종목 코드로)
  # API 응답 종목 코드 키 이름 확인 ('stk_cd') [cite: 974, 1156]
  volume_codes = set(vol_filtered['stk_cd'])
  price_codes = set(price_df['stk_cd'])
  
  intersect_codes = list(volume_codes.intersection(price_codes))
  
  if not intersect_codes:
    print("ℹ️ 거래량 급증과 등락률 상위 조건을 모두 만족하는 종목이 없습니다.")
    return None
  print(f"🤝 교집합 종목 수: {len(intersect_codes)}")

  # 4. 교집합 종목 대상 상세 정보 조회 (ka10095 사용)
  details_list = await api.fetch_multiple_stock_details(intersect_codes)
  if not details_list:
    print("❗️ 교집합 종목의 상세 정보 조회에 실패했습니다.")
    # 상세 정보 조회가 안되면 임시로 첫번째 종목 반환 (선택적)
    raw_target_code = intersect_codes[0]
    final_target_code = raw_target_code.split('_')[0]
    print(f"⚠️ 임시 선정된 종목 (거래대금 확인 불가): {final_target_code}")
    return final_target_code
    # return None # 또는 실패로 간주하고 None 반환

  details_df = pd.DataFrame(details_list)

  # 필요한 컬럼만 추출하고 데이터 타입 변환
  details_df['stk_cd_clean'] = details_df['stk_cd'].apply(lambda x: x.split('_')[0])
  details_df['trading_amount'] = pd.to_numeric(details_df['trde_prica'], errors='coerce').fillna(0) # 거래대금 키 'trde_prica' 
  details_df['current_price'] = pd.to_numeric(details_df['cur_prc'].astype(str).str.replace(r'[+-]', '', regex=True), errors='coerce').fillna(0) # 현재가 키 'cur_prc' 
  details_df['open_price'] = pd.to_numeric(details_df['open_pric'].astype(str).str.replace(r'[+-]', '', regex=True), errors='coerce').fillna(0) # 시가 키 'open_pric' 

  # 5. 인트라데이 필터링: 현재가가 시가보다 높은 종목만 선택
  intraday_filtered_df = details_df[details_df['current_price'] > details_df['open_price']]
  if intraday_filtered_df.empty:
    print("ℹ️ 교집합 종목 중 현재가가 시가보다 높은 종목이 없습니다.")
    return None
  print(f"☀️ 인트라데이 필터링 후 종목 수: {len(intraday_filtered_df)}")

  # 6. 최종 정렬 및 선정: 거래대금 기준 내림차순 정렬 후 상위 N개 선택
  final_targets = intraday_filtered_df.sort_values(by='trading_amount', ascending=False).head(NUM_FINAL_TARGETS)

  if not final_targets.empty:
    final_target_code = final_targets.iloc[0]['stk_cd_clean']
    print(f"🎯 최종 선정된 종목 (거래대금 최상위): {final_target_code}")
    return final_target_code
  else:
    print("ℹ️ 최종 선정 기준을 만족하는 종목이 없습니다.")
    return None