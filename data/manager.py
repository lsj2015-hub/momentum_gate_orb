# Kiwoom API가 보내준 분봉 데이터를 우리가 다루기 쉬운 형태로 변환하는 역할을 합니다.

import pandas as pd
from typing import List, Dict, Optional, Any

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

def update_ohlcv_with_candle(df: Optional[pd.DataFrame], candle: Dict[str, Any]) -> pd.DataFrame:
    """
    실시간 틱 데이터로 완성된 1분봉 캔들(dict)을
    기존 OHLCV DataFrame에 추가(append)합니다.
    """
    if 'time' not in candle or 'open' not in candle or 'high' not in candle or 'low' not in candle or 'close' not in candle or 'volume' not in candle:
        # 필수 키가 없으면 데이터프레임을 변경하지 않고 반환
        return df if df is not None else pd.DataFrame()

    try:
        # 캔들 시간(datetime 객체)을 DataFrame 인덱스로 사용
        candle_time = candle['time']

        # 새 캔들 데이터를 DataFrame 형식으로 변환
        new_row = pd.DataFrame(
            [{
                'open': candle['open'],
                'high': candle['high'],
                'low': candle['low'],
                'close': candle['close'],
                'volume': candle['volume']
            }],
            index=[candle_time] # 인덱스로 캔들 시간 설정
        )
        new_row.index.name = 'datetime'

        if df is None or df.empty:
            # 기존 DataFrame이 없으면 새 DataFrame 반환
            return new_row

        # --- 중복 방지 및 업데이트 ---
        if candle_time in df.index:
            # 만약 이미 해당 시간의 데이터가 있다면, 덮어쓰기
            df.loc[candle_time] = new_row.iloc[0]
            return df
        else:
            # DataFrame에 새 캔들 추가 (concat 사용)
            df = pd.concat([df, new_row])
            return df

    except Exception as e:
        print(f"🚨 [update_ohlcv_with_candle] 캔들 추가 오류: {e}, 캔들: {candle}")
        return df if df is not None else pd.DataFrame() # 오류 발생 시 원본 반환