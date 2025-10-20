import yaml
from pydantic import BaseModel

class KiwoomConfig(BaseModel):
    app_key: str
    app_secret: str
    account_no: str

class StrategyConfig(BaseModel):
    orb_timeframe: int = 15
    breakout_buffer: float = 0.15

class Config(BaseModel):
    kiwoom: KiwoomConfig
    strategy: StrategyConfig

def load_config(path: str = "config/config.yaml") -> Config:
    """YAML 설정 파일을 로드하고 Pydantic 모델로 파싱합니다."""
    with open(path, 'r', encoding='utf-8') as f:
        config_data = yaml.safe_load(f)
    return Config(**config_data)

# 전역 설정 객체
config = load_config()