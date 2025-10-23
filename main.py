import asyncio
from core.engine import TradingEngine
from config.loader import config # config 로더 import

if __name__ == "__main__":
  # TradingEngine 생성 시 config 객체 전달
  engine = TradingEngine(config)
  try:
      # 엔진의 start 메서드를 비동기로 실행
      asyncio.run(engine.start())
  except KeyboardInterrupt:
      # Ctrl+C 입력 시 엔진의 stop 메서드를 호출하여 종료
      print("KeyboardInterrupt received, stopping engine...")
      asyncio.run(engine.stop())