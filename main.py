import asyncio
import signal
import sys
from loguru import logger
from core.engine import TradingEngine
from config.loader import config

# --- 👇 Loguru 초기 설정 ---
def setup_logging():
    """Loguru 로거 설정"""
    logger.remove() # 기본 핸들러 제거

    log_config = config.logging # loader.py 에서 정의된 Config 객체 사용 가정
    log_directory = log_config.directory
    log_file_path = f"{log_directory}/bot.log" # 메인 로그 파일

    # 1. 콘솔(stdout) 핸들러 추가
    logger.add(
        sys.stdout,
        level=log_config.level.upper(), # config에서 로그 레벨 설정
        format=log_config.format,
        colorize=True
    )

    # 2. 파일 핸들러 추가 (회전 및 보관 설정 적용)
    logger.add(
        log_file_path,
        level=log_config.level.upper(),
        format=log_config.format,
        rotation=log_config.rotation,  # 예: "10 MB"
        retention=log_config.retention, # 예: "7 days"
        encoding="utf-8",
        # 비동기 환경에서 안전하게 파일 쓰기 보장
        enqueue=True, # 중요: 비동기 로깅 활성화
        backtrace=True, # 오류 발생 시 스택 트레이스 포함
        diagnose=True   # 오류 발생 시 변수 값 등 상세 정보 포함
    )

    logger.info("--- 🚀 로깅 시스템 초기화 완료 ---")
    logger.info(f"로그 레벨: {log_config.level}, 로그 파일: {log_file_path}")

# --- 비동기 메인 함수 ---
async def main():
    # TradingEngine 인스턴스 생성 시 config 객체를 전달하지 않음 (엔진 내부에서 import)
    # logger 사용 시작
    logger.info("🤖 트레이딩 엔진 인스턴스 생성 시도...")
    engine = TradingEngine()
    logger.info("✅ 트레이딩 엔진 인스턴스 생성 완료.")

    # --- 종료 신호 처리 (기존 로직 유지) ---
    loop = asyncio.get_running_loop()

    def signal_handler():
        # logger 사용
        logger.warning("⌨️ Ctrl+C 감지. 엔진 종료 신호 전송...")
        asyncio.create_task(engine.stop()) # engine.stop() 호출 유지

    try:
        loop.add_signal_handler(signal.SIGINT, signal_handler)
    except NotImplementedError:
        logger.warning("⚠️ add_signal_handler 미지원 환경. Ctrl+C 종료 불안정 가능.")

    # --- 엔진 시작 ---
    try:
        # logger 사용
        logger.info("▶️ 트레이딩 엔진 비동기 시작 (engine.start)")
        await engine.start() # 수정된 부분 확인
        logger.info("⏹️ 트레이딩 엔진 정상 종료 (engine.start 완료)")
    except asyncio.CancelledError:
        logger.warning("🔶 메인 태스크 취소됨 (엔진 종료 과정일 수 있음).")
    except Exception as e:
        # logger 사용 (Critical 레벨 및 스택 트레이스 포함)
        logger.critical(f"🔥 main 함수에서 치명적 예외 발생: {e}")
        logger.exception(e) # 스택 트레이스 자동 로깅
        if hasattr(engine, 'engine_status') and engine.engine_status != 'STOPPED':
             logger.warning("⚠️ 오류 발생으로 엔진 강제 종료 시도...")
             await engine.shutdown() # engine.shutdown() 호출 유지

# --- 프로그램 진입점 ---
if __name__ == "__main__":
    setup_logging() # --- 👈 로깅 설정 함수 호출 ---

    # --- 👇 메인 실행 로직을 try-except-finally로 감쌈 ---
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("⌨️ 사용자에 의해 프로그램 강제 종료 (KeyboardInterrupt in main)")
    except Exception as e:
        # setup_logging 전에 오류가 날 수도 있으므로 print도 사용
        print(f"CRITICAL ERROR in main execution: {e}", file=sys.stderr)
        logger.critical(f"🔥 프로그램 최상위 레벨에서 치명적인 오류 발생: {e}")
        logger.exception(e) # 스택 트레이스 포함 로깅
    finally:
        logger.info("--- 🛑 프로그램 최종 종료 ---")
        # Loguru가 파일 버퍼를 비우도록 잠시 대기 (필요시)
        # asyncio.run(asyncio.sleep(0.1)) # 필요에 따라 추가
