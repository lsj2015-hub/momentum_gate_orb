# Kiwoom API가 보내준 분봉 데이터를 우리가 다루기 쉬운 형태로 변환하는 역할을 합니다.

import pandas as pd
from typing import List, Dict, Optional

def preprocess_chart_data(chart_data: List[Dict]) -> Optional[pd.DataFrame]:
  """
  키움 API의 차트 데이터(리스트)를 Pandas DataFrame으로 변환하고 전처리합니다.
  """
  if not chart_data:
    print("⚠️ 전처리할 차트 데이터가 없습니다.")
    return None

  df = pd.DataFrame(chart_data)

  # 데이터 타입 변환 및 컬럼 이름 변경
  numeric_cols = {
    'cur_prc': 'close',
    'trde_qty': 'volume',
    'open_pric': 'open',
    'high_pric': 'high',
    'low_pric': 'low'
  }
  
  for col_from, col_to in numeric_cols.items():
    df[col_to] = pd.to_numeric(df[col_from].astype(str).str.replace(r'[+-]', '', regex=True), errors='coerce')
  # 시간 데이터를 datetime 형식으로 변환하고 인덱스로 설정
  df['datetime'] = pd.to_datetime(df['cntr_tm'], format='%Y%m%d%H%M%S')
  df = df.set_index('datetime')
  
  # 필요한 컬럼만 선택하고 순서 정렬
  df = df[['open', 'high', 'low', 'close', 'volume']]
  
  # API가 최신 데이터를 가장 먼저 주므로, 시간 순으로 정렬
  df = df.sort_index()

  print("✅ API 데이터를 DataFrame으로 변환 및 정제 완료")
  return df