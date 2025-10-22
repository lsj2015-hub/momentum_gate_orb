import asyncio
from core.engine import TradingEngine
from config.loader import config # config 임포트

async def main():
  engine = TradingEngine(config) # config 전달
  await engine.initialize_session() # <<< 장 시작 시 종목 선정

  # 실제 운영 시에는 장 운영 시간 동안만 루프를 돌도록 스케줄링 필요
  while True:
      await engine.process_tick()
      await asyncio.sleep(60) # 1분 대기

if __name__ == "__main__":
  asyncio.run(main())