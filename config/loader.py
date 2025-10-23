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
    take_profit_pct: float = 2.5
    stop_loss_pct: float = -1.0
    # VWAP 손절은 선택적 필드로 정의 (yaml 파일에 없어도 오류 발생 안 함)
    stop_loss_vwap_pct: Optional[float] = None
    max_target_stocks: int = 5       # 기본값 5개
    max_concurrent_positions: int = 3 # 기본값 3개

# --- 전체 설정을 통합하는 메인 모델 ---

class Config(BaseModel):
    is_mock: bool = True # 환경 설정 플래그 추가 (기본값 True: 모의투자)
    kiwoom: KiwoomConfig
    engine: EngineConfig = Field(default_factory=EngineConfig)
    strategy: StrategyConfig

# --- 설정 로드 함수 ---

def load_config(path: str = "config/config.yaml") -> Config:
    """YAML 설정 파일을 로드하고 Pydantic 모델로 파싱합니다."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        if not config_data:
             raise ValueError("설정 파일이 비어있습니다.")
        # Pydantic 모델을 사용하여 데이터 유효성 검사 및 파싱
        return Config(**config_data)
    except FileNotFoundError:
        print(f"🚨 설정 파일({path})을 찾을 수 없습니다. 기본값으로 진행합니다.")
        # 기본값 생성 시 모의/실거래 키 모두 더미값 사용
        dummy_kiwoom = KiwoomConfig(
            app_key="dummy_real_key", app_secret="dummy_real_secret", account_no="dummy_real_acct",
            mock_app_key="dummy_mock_key", mock_app_secret="dummy_mock_secret", mock_account_no="dummy_mock_acct"
        )
        return Config(kiwoom=dummy_kiwoom, strategy=StrategyConfig())
    except yaml.YAMLError as e:
        print(f"🚨 설정 파일({path}) 파싱 오류: {e}")
        raise
    except Exception as e:
        print(f"🚨 설정 로드 중 예상치 못한 오류: {e}")
        raise

# --- 전역 설정 객체 ---
config: Config = load_config()