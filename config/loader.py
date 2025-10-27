# config/loader.py

import yaml
from pydantic import BaseModel, Field
from typing import Optional

# --- 개별 설정 섹션 모델 정의 ---

class KiwoomConfig(BaseModel):
    # 실거래 정보
    app_key: str
    app_secret: str
    account_no: str
    # 모의투자 정보
    mock_app_key: str
    mock_app_secret: str
    mock_account_no: str

class EngineConfig(BaseModel):
    screening_interval_minutes: int = 5 # 기본값 5분

class StrategyConfig(BaseModel):
    orb_timeframe: int = 15
    breakout_buffer: float = 0.15
    # --- 👇 필드 추가 ---
    investment_amount_per_stock: int # 기본값을 설정해도 좋지만, YAML에 있으니 타입만 명시
    # --- 👆 필드 추가 끝 ---
    take_profit_pct: float = 2.5
    stop_loss_pct: float = -1.0
    stop_loss_vwap_pct: Optional[float] = None
    max_target_stocks: int = 5
    max_concurrent_positions: int = 3 # 기존 코드에는 5였으나 YAML 기준으로 3으로 변경 (필요시 조정)

    # --- 스크리닝 관련 설정 ---
    screening_interval_minutes: int = 5
    screening_surge_timeframe_minutes: int = 5 # 타입 명시 (문자열 -> 숫자)
    screening_min_volume_threshold: int = 10    # 타입 명시
    screening_min_price: int = 1000             # 타입 명시
    screening_min_surge_rate: float = 100.0     # 타입 명시

    # --- Tick 처리 간격 설정 ---
    tick_interval_seconds: int = 5

# --- 전체 설정을 통합하는 메인 모델 ---
# (Config 클래스 및 load_config 함수는 이전 수정 내용 유지 - 디버그 프린트 포함)
class Config(BaseModel):
    is_mock: bool = True
    kiwoom: KiwoomConfig
    engine: EngineConfig = Field(default_factory=EngineConfig)
    strategy: StrategyConfig

def load_config(path: str = "config/config.yaml") -> Config:
    """YAML 설정 파일을 로드하고 Pydantic 모델로 파싱합니다."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        if not config_data:
             raise ValueError("설정 파일이 비어있습니다.")
        
        parsed_config = Config(**config_data)

        # --- 디버깅 코드 유지 ---
        print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        # 이제 오류 없이 실행되어야 합니다.
        print(f"!!! DEBUG LOADER: 로드된 investment_amount_per_stock = {parsed_config.strategy.investment_amount_per_stock}")
        print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        # --- 디버깅 코드 끝 ---

        return parsed_config

    except FileNotFoundError:
        print(f"🚨 설정 파일({path})을 찾을 수 없습니다. 기본값으로 진행합니다.")
        # 기본값 생성 시에도 investment_amount_per_stock 추가 필요
        dummy_kiwoom = KiwoomConfig(
            app_key="dummy_real_key", app_secret="dummy_real_secret", account_no="dummy_real_acct",
            mock_app_key="dummy_mock_key", mock_app_secret="dummy_mock_secret", mock_account_no="dummy_mock_acct"
        )
        # --- 👇 기본값 StrategyConfig 생성 시에도 필드 추가 ---
        default_strategy = StrategyConfig(investment_amount_per_stock=100000) # 기본값 설정
        # --- 👆 수정 끝 ---
        return Config(kiwoom=dummy_kiwoom, strategy=default_strategy) # 수정됨
    except yaml.YAMLError as e:
        print(f"🚨 설정 파일({path}) 파싱 오류: {e}")
        raise
    except Exception as e:
        print(f"🚨 설정 로드 중 예상치 못한 오류: {e}")
        raise

# --- 전역 설정 객체 ---
config: Config = load_config()