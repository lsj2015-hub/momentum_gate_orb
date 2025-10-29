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
    # --- 파트너님 YAML 기준 기존 파라미터 ---
    orb_timeframe: int = 15
    breakout_buffer: float = 0.15
    investment_amount_per_stock: int # YAML에 값이 있으므로 기본값 불필요
    take_profit_pct: float = 2.5 # (전체) 익절 기준 (%)
    stop_loss_pct: float = -1.0 # 손절 기준 (%)
    partial_take_profit_pct: Optional[float] = 1.5 # 부분 익절 목표 수익률 (%) (None이면 사용 안 함)
    partial_take_profit_ratio: float = 0.4         # 부분 익절 시 매도 비율 (예: 0.4 = 40%)
    stop_loss_vwap_pct: Optional[float] = None # YAML에 주석처리 되어 있으므로 Optional 유지
    max_target_stocks: int = 5
    max_concurrent_positions: int = 5 # YAML 기준 5로 수정 (기존 loader는 3)

    # --- 스크리닝 관련 설정 ---
    screening_interval_minutes: int = 5
    screening_surge_timeframe_minutes: int = 5
    screening_min_volume_threshold: int = 10
    screening_min_price: int = 1000
    screening_min_surge_rate: float = 100.0

    # --- Tick 처리 간격 설정 ---
    tick_interval_seconds: int = 5

    # --- 👇 신규 파라미터 추가 (이전 논의 내용 반영) ---
    rvol_threshold: float = 130.0      # 상대 거래량 임계값 (%) - 기본값 설정
    obi_threshold: float = 1.5         # OBI 임계값 - 기본값 설정
    strength_threshold: float = 100.0  # 체결강도 임계값 - 기본값 설정
    time_stop_hour: int = 14           # 시간 청산 기준 (시) - 기본값 설정
    time_stop_minute: int = 50         # 시간 청산 기준 (분) - 기본값 설정
    ema_short_period: int = 9          # 단기 EMA 기간 - 기본값 설정
    ema_long_period: int = 20          # 장기 EMA 기간 - 기본값 설정
    rvol_period: int = 20              # RVOL 기간 (필요시) - 기본값 설정
    # --- 👆 신규 파라미터 추가 끝 ---


# --- 전체 설정을 통합하는 메인 모델 ---
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

        # --- 디버깅 코드 ---
        print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f"!!! DEBUG LOADER: 로드된 investment_amount_per_stock = {parsed_config.strategy.investment_amount_per_stock}")
        print(f"!!! DEBUG LOADER: 로드된 rvol_threshold = {parsed_config.strategy.rvol_threshold}") # 새로 추가된 값 예시 확인
        print(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        # --- 디버깅 코드 끝 ---

        return parsed_config

    except FileNotFoundError:
        print(f"🚨 설정 파일({path})을 찾을 수 없습니다. 기본값으로 진행합니다.")
        dummy_kiwoom = KiwoomConfig(
            app_key="dummy_real_key", app_secret="dummy_real_secret", account_no="dummy_real_acct",
            mock_app_key="dummy_mock_key", mock_app_secret="dummy_mock_secret", mock_account_no="dummy_mock_acct"
        )
        # --- 👇 기본 StrategyConfig 생성 시 모든 필드에 기본값 할당 (필수 필드 포함) ---
        default_strategy = StrategyConfig(
            # --- 필수 필드 ---
            investment_amount_per_stock=100000, # 예시 기본값
            partial_take_profit_pct=1.5, # 예시 기본값
            partial_take_profit_ratio=0.4, # 예시 기본값
            # --- Optional 제외 모든 필드에 기본값 할당 (모델 정의에 기본값 있으면 생략 가능) ---
            orb_timeframe=15,
            breakout_buffer=0.15,
            take_profit_pct=2.5,
            stop_loss_pct=-1.0,
            max_target_stocks=5,
            max_concurrent_positions=5, # YAML 기준 5
            screening_interval_minutes=5,
            screening_surge_timeframe_minutes=5,
            screening_min_volume_threshold=10,
            screening_min_price=1000,
            screening_min_surge_rate=100.0,
            tick_interval_seconds=5,
            rvol_threshold=130.0,
            obi_threshold=1.5,
            strength_threshold=100.0,
            time_stop_hour=14,
            time_stop_minute=50,
            ema_short_period=9,
            ema_long_period=20,
            rvol_period=20
        )
        # --- 👆 수정 끝 ---
        return Config(kiwoom=dummy_kiwoom, strategy=default_strategy, is_mock=True)
    except yaml.YAMLError as e:
        print(f"🚨 설정 파일({path}) 파싱 오류: {e}")
        raise
    except Exception as e:
        print(f"🚨 설정 로드 중 예상치 못한 오류: {e}")
        raise

# --- 전역 설정 객체 ---
config: Config = load_config()