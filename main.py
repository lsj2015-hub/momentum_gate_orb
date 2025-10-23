import asyncio
import signal # signal 모듈 임포트
from core.engine import TradingEngine
from config.loader import config # 설정 로드 (선택적: config 직접 사용 안하면 불필요)

# --- 비동기 메인 함수 ---
async def main():
    # TradingEngine 인스턴스 생성 시 config 객체를 전달하지 않음 (내부에서 import)
    engine = TradingEngine()

    # --- 종료 신호 처리 ---
    loop = asyncio.get_running_loop()

    def signal_handler():
        print("\nCtrl+C 감지. 엔진 종료 신호 전송...")
        # 엔진의 stop 메서드를 비동기적으로 호출
        asyncio.create_task(engine.stop())

    # SIGINT (Ctrl+C) 신호에 핸들러 등록
    try:
        loop.add_signal_handler(signal.SIGINT, signal_handler)
    except NotImplementedError:
        # Windows 환경 등 add_signal_handler를 지원하지 않는 경우
        print("경고: add_signal_handler를 지원하지 않는 환경입니다. Ctrl+C 종료가 불안정할 수 있습니다.")
        # signal.signal(signal.SIGINT, lambda s, f: asyncio.create_task(engine.stop())) # 대체 방식 (불안정 가능)

    # --- 엔진 시작 ---
    try:
        await engine.start() # 수정: engine.start() 호출
    except asyncio.CancelledError:
        print("메인 태스크 취소됨.") # 엔진 종료 시 발생할 수 있음
    except Exception as e:
        print(f"🚨 main 함수에서 예외 발생: {e}")
        # 오류 발생 시에도 종료 시도
        if engine.engine_status != 'STOPPED':
             await engine.shutdown()

# --- 프로그램 진입점 ---
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # signal_handler가 처리하므로 여기서는 특별히 할 일 없음
        print("프로그램 종료 중...")
    except Exception as e:
        print(f"🚨 프로그램 실행 중 심각한 오류 발생: {e}") 