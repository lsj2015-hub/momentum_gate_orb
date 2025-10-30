import yaml
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
import sys
import os

# --- 개별 설정 섹션 모델 정의 ---

class KiwoomConfig(BaseModel):
    # 실거래 정보
    app_key: str = Field(..., description="실거래 API 앱 키")
    app_secret: str = Field(..., description="실거래 API 앱 시크릿")
    account_no: str = Field(..., description="실거래 계좌번호 (하이픈 제외 8자리 또는 10자리)")

    # 모의투자 정보 (Optional)
    mock_app_key: Optional[str] = Field(None, description="모의투자 API 앱 키")
    mock_app_secret: Optional[str] = Field(None, description="모의투자 API 앱 시크릿")
    mock_account_no: Optional[str] = Field(None, description="모의투자 계좌번호 (하이픈 제외 8자리 또는 10자리)")

class StrategyConfig(BaseModel):
    # --- ORB 관련 ---
    orb_timeframe: int = Field(default=15, description="ORB 계산 시간 (분, 예: 9시 N분까지)")
    breakout_buffer: float = Field(default=0.15, description="ORB 돌파 시 진입 버퍼 (%)")

    # --- 진입 필터 관련 ---
    ema_short_period: int = Field(default=9, description="단기 EMA 기간")
    ema_long_period: int = Field(default=20, description="장기 EMA 기간")
    rvol_period: int = Field(default=20, description="RVOL 계산 시 이동평균 기간 (봉 개수)")
    rvol_threshold: float = Field(default=150.0, description="상대 거래량(RVOL) 최소 진입 기준 (%)")
    obi_threshold: float = Field(default=1.5, description="주문 장부 불균형(OBI) 최소 진입 기준 (매수잔량/매도잔량 비율)")
    strength_threshold: float = Field(default=100.0, description="체결 강도 최소 진입 기준 (%)")

    # --- 청산 조건 관련 ---
    take_profit_pct: float = Field(default=2.5, description="고정 익절 기준 (%)")
    stop_loss_pct: float = Field(default=-1.0, description="고정 손절 기준 (%)")
    partial_take_profit_pct: Optional[float] = Field(default=1.5, description="부분 익절 목표 수익률 (%). None이면 사용 안 함")
    partial_take_profit_ratio: float = Field(default=0.4, description="부분 익절 시 매도 비율 (예: 0.4 = 40%)")
    time_stop_hour: int = Field(default=14, description="시간 청산 기준 (시)")
    time_stop_minute: int = Field(default=50, description="시간 청산 기준 (분)")

    # --- 자금/포지션 관리 ---
    investment_amount_per_stock: int = Field(..., description="종목당 고정 투자 금액 (원)") # 기본값 없이 필수 입력
    max_concurrent_positions: int = Field(default=5, description="동시에 보유할 최대 종목 수")

    # --- 스크리닝 관련 상세 설정 ---
    max_target_stocks: int = Field(default=5, description="스크리닝 후 최대 관심 종목 수")
    screening_interval_minutes: int = Field(default=5, description="스크리닝 주기 (분)")
    screening_surge_timeframe_minutes: int = Field(default=5, description="거래량 급증 비교 시간 (분)")
    screening_min_volume_threshold: int = Field(default=10, description="최소 거래량 기준 (만주 단위, 예: 10 -> 10,000주)")
    screening_min_price: int = Field(default=1000, description="최소 가격 기준 (원)")
    screening_min_surge_rate: float = Field(default=100.0, description="최소 거래량 급증률 기준 (%)")

    # --- Tick 처리 간격 설정 ---
    tick_interval_seconds: int = Field(default=5, description="개별 종목 Tick 데이터 처리 주기 (초)")

# --- 👇 백테스팅 설정 클래스 ---
class BacktestConfig(BaseModel):
    initial_balance: float = Field(..., description="백테스팅 초기 자본금 (원)") # 필수 입력
    commission_rate: float = Field(default=0.00015, description="매매 수수료율 (예: 0.015% -> 0.00015)")
    tax_rate: float = Field(default=0.002, description="매도 시 거래세율 (예: 0.2% -> 0.002)")
    use_fixed_amount: bool = Field(default=True, description="True: strategy.investment_amount_per_stock 사용, False: 아래 investment_ratio 사용")
    investment_ratio: Optional[float] = Field(default=0.1, description="자산 대비 투자 비율 (use_fixed_amount=False 시 사용, 예: 10% -> 0.1)")

# --- 👇 로깅 설정 클래스 추가 ---
class LoggingConfig(BaseModel):
    level: str = Field(default="INFO", description="로그 레벨 (DEBUG, INFO, WARNING, ERROR)")
    directory: str = Field(default="logs", description="로그 파일 저장 디렉토리")
    rotation: str = Field(default="10 MB", description="로그 파일 순환 크기")
    retention: str = Field(default="7 days", description="로그 파일 보관 기간")
    format: str = Field(
        default="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} - {message}",
        description="로그 포맷"
    )

# --- 메인 설정 클래스 ---
class Config(BaseModel):
    is_mock: bool = Field(default=False, description="True: 모의투자 API 사용, False: 실거래 API 사용")
    kiwoom: KiwoomConfig
    strategy: StrategyConfig
    
    # --- 👇 [핵심 수정] ---
    # backtest: BacktestConfig  <- (이전 코드)
    backtest: Optional[BacktestConfig] = None # 👈 [수정] 필수 항목에서 선택 항목으로 변경
    # --- 👆 [핵심 수정] ---
    
    logging: LoggingConfig

def load_config(path: str = "config/config.yaml") -> Config:
    """YAML 설정 파일을 로드하고 Pydantic 모델로 파싱합니다."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
            if not config_data:
                raise ValueError(f"설정 파일({path})이 비어 있거나 유효한 YAML 형식이 아닙니다.")
            
        # 'engine' 섹션 마이그레이션 (이전 config.yaml 호환용)
        if 'engine' in config_data:
            if 'strategy' not in config_data:
                config_data['strategy'] = {}
            if 'screening_interval_minutes' in config_data['engine']:
                config_data['strategy']['screening_interval_minutes'] = config_data['engine']['screening_interval_minutes']
            del config_data['engine']
            print("ℹ️ [Config] 'engine' 섹션을 'strategy'로 자동 병합했습니다.")

        return Config(**config_data)
    
    except FileNotFoundError:
        print(f"❌ 설정 파일({path})을 찾을 수 없습니다.")
        raise
    except yaml.YAMLError as e:
        print(f"❌ 설정 파일({path}) 파싱 오류: {e}")
        raise
    except ValueError as e: # 빈 파일 또는 잘못된 형식 오류 처리
        print(f"❌ 설정 파일({path}) 내용 오류: {e}")
        raise
    except Exception as e: # Pydantic 유효성 검사 오류 포함
        print(f"❌ 설정 로드 중 오류 발생: {e}")
        # Pydantic 유효성 검사 오류 시 상세 정보 출력
        if hasattr(e, 'errors'):
             for error in e.errors():
                 print(f"  - 필드: {'.'.join(map(str, error['loc']))}, 오류: {error['msg']}")
        raise

# 전역 설정 객체
try:
    # [수정] config.yaml 파일의 실제 위치를 기준으로 loader.py의 상대 경로를 계산
    # loader.py는 config/ 폴더 안에 있으므로, config.yaml은 같은 폴더에 있어야 함.
    # 만약 loader.py가 루트에 있다면 path="config/config.yaml"이 맞음.
    # 현재 제공된 파일 경로(loader.py)를 기준으로 config.yaml 경로를 잡습니다.
    
    # loader.py의 실제 경로
    loader_dir = os.path.dirname(os.path.abspath(__file__))
    # config.yaml의 실제 경로 (loader.py와 같은 폴더에 있다고 가정)
    config_file_path = os.path.join(loader_dir, "config.yaml")

    # 만약 config.yaml이 상위 폴더(프로젝트 루트)에 있다면:
    # config_file_path = os.path.join(os.path.dirname(loader_dir), "config.yaml")

    # ------------------------------------------------------------------
    # [가정] 제공된 'loader.py'와 'config.yaml'이 원래 같은 폴더에 있다고 가정하고,
    # 사용자가 제공한 원본 load_config() 함수를 그대로 사용합니다.
    # (파일 경로 수정은 주석 처리)
    # ------------------------------------------------------------------
    
    config = load_config() # 원본 코드 (loader.py와 config.yaml이 다른 폴더에 있음을 가정)
    print("✅ 설정 파일 로드 완료.")
except Exception:
    print("🔥 프로그램 실행에 필요한 설정을 로드하지 못했습니다. config/config.yaml 파일을 확인하세요.")
    raise SystemExit("설정 로드 실패로 프로그램을 종료합니다.")