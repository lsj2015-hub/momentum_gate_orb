import asyncio
import pandas as pd
from loguru import logger
import numpy as np
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Callable, Any
import json
import traceback # 상세 오류 로깅을 위해 추가

from config.loader import config
from gateway.kiwoom_api import KiwoomAPI

from data.manager import preprocess_chart_data, update_ohlcv_with_candle

from data.indicators import (
    add_vwap, calculate_orb, add_ema, 
    calculate_rvol, calculate_obi, get_strength
)
from strategy.momentum_orb import check_breakout_signal
from strategy.risk_manager import manage_position

# --- 👇 실시간 트레이딩 엔진 클래스 ---
class TradingEngine:
  """웹소켓 기반 실시간 다중 종목 트레이딩 로직 관장 엔진"""
  def __init__(self):
    self.config = config 
    self.positions: Dict[str, Dict] = {} 
    self.logs: List[str] = [] 
    self.api: Optional[KiwoomAPI] = None 
    self._stop_event = asyncio.Event() 
    
    self.screening_interval = timedelta(minutes=getattr(self.config.strategy, 'screening_interval_minutes', 5))
    self.last_screening_time = datetime.min.replace(tzinfo=None) 
        
    self.engine_status: str = "INITIALIZING" 
    
    self.target_stocks: Set[str] = set() # {종목코드1, 종목코드2, ...}
    
    # --- 캔들 집계기(Aggregator)용 변수 추가 ---
    # 1. 실시간 1분봉 OHLCV 데이터 (DataFrame)
    self.ohlcv_data: Dict[str, pd.DataFrame] = {} # {'종목코드': DataFrame}
    # 2. 현재 집계 중인 1분봉 캔들 (Dict)
    self.current_candle: Dict[str, Dict[str, Any]] = {} # {'종목코드': {'time': ..., 'open': ..., 'high': ..., 'low': ..., 'close': ..., 'volume': ...}}
    # ---
    
    self.realtime_data: Dict[str, Dict] = {} 
    self.orderbook_data: Dict[str, Dict] = {} 
    self.cumulative_volumes: Dict[str, Dict] = {} 
    self.subscribed_codes: Set[str] = set() 
    self._realtime_registered = False 
    self.vi_status: Dict[str, bool] = {} 

    # --- 대시보드에서 제어할 전략 설정 변수 ---
    self.orb_timeframe = self.config.strategy.orb_timeframe
    self.breakout_buffer = self.config.strategy.breakout_buffer
    self.take_profit_pct = self.config.strategy.take_profit_pct
    self.stop_loss_pct = self.config.strategy.stop_loss_pct
    self.partial_take_profit_pct = self.config.strategy.partial_take_profit_pct
    self.partial_take_profit_ratio = self.config.strategy.partial_take_profit_ratio

  def add_log(self, message: str, level: str = "INFO"):
    log_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {message}" 
    self.logs.insert(0, log_msg)
    if len(self.logs) > 100: self.logs.pop()

    if level.upper() == "DEBUG": logger.debug(message)
    elif level.upper() == "INFO": logger.info(message)
    elif level.upper() == "WARNING": logger.warning(message)
    elif level.upper() == "ERROR": logger.error(message)
    elif level.upper() == "CRITICAL": logger.critical(message)
    else: logger.info(message)

  # --- 대시보드 연동을 위한 설정 업데이트 메서드 ---
  def update_strategy_settings(self, settings: Dict):
      """대시보드에서 변경된 전략 설정을 엔진 인스턴스에 업데이트합니다."""
      try:
          # 각 설정 값을 float 또는 int로 변환하여 저장
          self.orb_timeframe = int(settings.get('orb_timeframe', self.orb_timeframe))
          self.breakout_buffer = float(settings.get('breakout_buffer', self.breakout_buffer))
          self.take_profit_pct = float(settings.get('take_profit_pct', self.take_profit_pct))
          self.stop_loss_pct = float(settings.get('stop_loss_pct', self.stop_loss_pct))
          # (참고: 부분 익절 등 다른 값들도 추후 동일하게 추가 가능)
          
          log_msg = (
              f"⚙️ 전략 설정 업데이트 완료:\n"
              f"  - ORB Timeframe: {self.orb_timeframe} 분\n"
              f"  - Breakout Buffer: {self.breakout_buffer:.2f} %\n"
              f"  - Take Profit: {self.take_profit_pct:.2f} %\n"
              f"  - Stop Loss: {self.stop_loss_pct:.2f} %"
          )
          self.add_log(log_msg, level="INFO")
      except Exception as e:
          self.add_log(f"🚨 설정 업데이트 중 오류 발생: {e}", level="ERROR")

  async def start(self):
    """엔진 메인 실행 로직 (WebSocket 연결 및 스크리닝 루프)"""
    self.add_log("🚀 엔진 시작 (v2: 실시간 캔들 집계 모드)...", level="INFO")
    self.engine_status = "STARTING"
    self.api = KiwoomAPI() 

    ws_connected = False
    try:
        # --- 웹소켓 연결 시도 ---
        self.add_log("  -> [START] 웹소켓 연결 시도...", level="INFO")
        ws_connected = await self.api.connect_websocket(self.handle_realtime_data)

        if not ws_connected:
            self.add_log("❌ [CRITICAL] 웹소켓 연결 실패. 엔진 루프를 시작할 수 없습니다.", level="CRITICAL")
            self.engine_status = "ERROR"
            await self.shutdown()
            return 

        self.engine_status = "RUNNING"
        self.add_log("✅ 웹소켓 연결 및 기본 TR 등록 완료. 메인 루프 시작.", level="INFO")

        # --- 메인 루프 (스크리닝 전용) ---
        while not self._stop_event.is_set():
            now = datetime.now()

            # --- 스크리닝 실행 (주기적으로) ---
            if now.replace(tzinfo=None) - self.last_screening_time >= self.screening_interval:
                self.add_log("🔍 스크리닝 시작...", level="DEBUG")
                await self.run_screening()
                self.last_screening_time = now.replace(tzinfo=None) 
                self.add_log("   스크리닝 완료.", level="DEBUG")

            # ❗️ 제거: 대상 종목 Tick 처리 (process_single_stock_tick) 루프
            # 이 로직은 _handle_new_candle로 이전됨

            await asyncio.sleep(5) # 스크리닝 루프 지연 (CPU 사용량 조절)

    except asyncio.CancelledError:
        self.add_log("🔶 엔진 메인 루프 취소됨 (종료 신호 수신).", level="WARNING")
    except Exception as e:
        self.add_log(f"🔥 엔진 메인 루프에서 치명적 오류 발생: {e}", level="CRITICAL")
        logger.exception(e) 
        self.engine_status = "ERROR"
    finally:
        self.add_log("🚪 [FINALLY] 엔진 종료 처리 시작...", level="INFO")
        await self.shutdown()
        self.engine_status = "STOPPED"
        self.add_log("🛑 엔진 종료 완료.", level="INFO")

  async def stop(self):
    """엔진 종료 신호 설정 (기존과 동일)"""
    if not self._stop_event.is_set():
        self.add_log("⏹️ 엔진 종료 신호 수신...", level="WARNING") 
        self._stop_event.set()

  async def shutdown(self):
    """엔진 리소스 정리"""
    self.add_log("🛑 엔진 종료(Shutdown) 절차 시작...", level="INFO") 
    if self.api and self.subscribed_codes:
        self.add_log("  -> [Shutdown] 실시간 구독 해지 시도...", level="DEBUG") 
        codes_to_remove = list(self.subscribed_codes)
        # ✅ '1h'도 종목코드(tr_key)가 필요한 TR로 가정
        tr_ids = ['0B', '0D', '1h'] * len(codes_to_remove)
        tr_keys = [code for code in codes_to_remove for _ in range(3)]
        
        # 기본 TR('00', '04')도 해지 (키움 API 정책에 따라 필요 없을 수 있음)
        tr_ids.extend(['00', '04'])
        tr_keys.extend(["", ""]) # 기본 TR은 tr_key가 ""
        
        try:
            await self.api.unregister_realtime(tr_ids=tr_ids, tr_keys=tr_keys)
            self.subscribed_codes.clear()
            self.add_log("  ✅ [Shutdown] 실시간 구독 해지 요청 완료.", level="DEBUG") 
        except Exception as e:
            self.add_log(f"  ⚠️ [Shutdown] 실시간 구독 해지 중 오류: {e}", level="WARNING") 
            logger.exception(e) 

    if self.api:
        self.add_log("  -> [Shutdown] 웹소켓 연결 해제 시도...", level="DEBUG") 
        await self.api.disconnect_websocket()
        self.add_log("  -> [Shutdown] HTTP 클라이언트 세션 종료 시도...", level="DEBUG") 
        await self.api.close()
        self.add_log("  ✅ [Shutdown] API 리소스 정리 완료.", level="INFO") 
    else:
        self.add_log("  ⚠️ [Shutdown] API 객체가 없어 정리 스킵.", level="WARNING") 

    self.add_log("🏁 [Shutdown] 엔진 종료 절차 완료됨.", level="INFO")

  async def run_screening(self):
    """거래량 급증 종목 스크리닝 및 실시간 구독 관리"""
    self.add_log("  -> [SCREEN] 거래량 급증 스크리닝 시작", level="DEBUG") 
    if not self.api: self.add_log("  ⚠️ [SCREEN] API 객체 없음", level="WARNING"); return 

    try:
        # ka10023 API 파라미터 준비
        params = {
            'mrkt_tp': '000', # 000: 전체, 001: 코스피, 101: 코스닥
            'sort_tp': '2',   # 1: 급증량, 2: 급증률
            'tm_tp': '1',     # 1: 분, 2: 전일
            'tm': str(self.config.strategy.screening_surge_timeframe_minutes), # 비교 시간(분)
            'trde_qty_tp': str(self.config.strategy.screening_min_volume_threshold * 10000).zfill(4) if self.config.strategy.screening_min_volume_threshold > 0 else '0000', # 최소 거래량 (만주 -> 주 변환 및 4자리 맞춤)
            'stk_cnd': '14',  # 0: 전체, 1: 관리제외, 14: ETF/ETN 제외 (API 문서 확인하여 적절한 코드 사용)
            'pric_tp': '8',   # 0: 전체, 8: 1천원 이상 (config 연동)
            'stex_tp': '3'    # 1: KRX, 2: NXT, 3: 통합
        }
        if self.config.strategy.screening_min_price >= 1000: params['pric_tp'] = '8' 
        else: params['pric_tp'] = '0' 

        self.add_log(f"   [SCREEN] API 요청 파라미터: {params}", level="DEBUG") 

        rank_data = await self.api.fetch_volume_surge_rank(**params)

        if rank_data is None:
             self.add_log("  ⚠️ [SCREEN] 거래량 급증 API 호출 실패 (None 반환).", level="WARNING") 
             return

        if rank_data.get('return_code') in [0, '0'] and 'trde_qty_sdnin' in rank_data:
            potential_targets = []
            for item in rank_data.get('trde_qty_sdnin', []):
                try:
                    code = item.get('stk_cd', '').strip()
                    name = item.get('stk_nm', '').strip()
                    surge_rate_str = item.get('sdnin_rt', '0').replace('+','').replace('-','').strip()
                    current_price_str = item.get('cur_prc', '0').replace('+','').replace('-','').strip()
                    volume_str = item.get('now_trde_qty', '0').strip()

                    if not code or not name: continue 

                    surge_rate = float(surge_rate_str) if surge_rate_str else 0.0
                    current_price = int(current_price_str) if current_price_str else 0
                    volume = int(volume_str) if volume_str else 0

                    if (surge_rate >= self.config.strategy.screening_min_surge_rate and
                        current_price >= self.config.strategy.screening_min_price and
                        volume >= self.config.strategy.screening_min_volume_threshold * 10000): 
                        potential_targets.append({'code': code, 'name': name, 'surge_rate': surge_rate})
                except (ValueError, TypeError) as parse_e:
                    self.add_log(f"   ⚠️ [SCREEN] 데이터 파싱 오류: {parse_e}, Item: {item}", level="WARNING") 
                    continue

            potential_targets.sort(key=lambda x: x['surge_rate'], reverse=True)
            
            # ❗️ 수정: new_targets를 self.target_stocks에 할당
            new_targets = {item['code'] for item in potential_targets[:self.config.strategy.max_target_stocks]}
            self.target_stocks = new_targets # 감시 대상 목록 자체를 업데이트

            # --- 실시간 구독 관리 ---
            current_subs = self.subscribed_codes.copy()
            holding_codes = set(self.positions.keys())
            
            # ❗️ 수정: 감시 대상(new_targets) + 보유 종목(holding_codes)이 구독 대상
            required_subs = self.target_stocks | holding_codes 
            
            to_add = required_subs - current_subs
            to_remove = current_subs - required_subs

            await self._update_realtime_subscriptions(to_add, to_remove)

            active_targets_count = len(self.target_stocks)
            self.add_log(f"   [SCREEN] 스크리닝 결과 {len(new_targets)}개. 신규 {len(to_add)}개 추가, {len(to_remove)}개 제거. 현재 감시 대상 {active_targets_count}개 (보유 {len(holding_codes)}개 별도 관리)", level="INFO")

        else:
            error_msg = rank_data.get('return_msg', 'API 응답 없음')
            self.add_log(f"  ⚠️ [SCREEN] 거래량 급증 데이터 조회 실패 또는 데이터 없음: {error_msg} (code: {rank_data.get('return_code')})", level="WARNING") 

    except Exception as e:
        self.add_log(f"🚨 [CRITICAL] 스크리닝 중 오류 발생: {e}", level="CRITICAL") 
        logger.exception(e) 

  async def _update_realtime_subscriptions(self, codes_to_add: Set[str], codes_to_remove: Set[str]):
    """필요한 실시간 데이터 구독/해지 및 신규 종목 데이터 초기화"""
    if not self.api: return

    # 구독 추가
    if codes_to_add:
        tr_ids = ['0B', '0D', '1h'] * len(codes_to_add) # 체결, 호가, VI
        tr_keys = [code for code in codes_to_add for _ in range(3)]
        
        await self.api.register_realtime(tr_ids=tr_ids, tr_keys=tr_keys)
        self.subscribed_codes.update(codes_to_add)
        self.add_log(f"   ➕ [SUB_UPDATE] 구독 추가 요청: {codes_to_add}", level="DEBUG")

        # --- ❗️ [신규] 신규 추가된 종목의 1분봉 차트 이력 가져오기 ---
        for code in codes_to_add:
            if code not in self.ohlcv_data: # 아직 데이터가 없는 경우에만
                self.add_log(f"   🔄 [SUB_UPDATE] 신규 종목 ({code}) 1분봉 차트 이력 조회 시작...", level="DEBUG")
                asyncio.create_task(self._initialize_stock_data(code))
            # 캔들 집계기 초기화
            self.current_candle.pop(code, None) 
            self.cumulative_volumes.pop(code, None)

    # 구독 해지
    if codes_to_remove:
        tr_ids = ['0B', '0D', '1h'] * len(codes_to_remove)
        tr_keys = [code for code in codes_to_remove for _ in range(3)]
        
        await self.api.unregister_realtime(tr_ids=tr_ids, tr_keys=tr_keys)
        self.subscribed_codes.difference_update(codes_to_remove)
        self.add_log(f"   ➖ [SUB_UPDATE] 구독 해지 요청: {codes_to_remove}", level="DEBUG") 
        
        # 관련 데이터 정리
        for code in codes_to_remove:
            self.realtime_data.pop(code, None)
            self.orderbook_data.pop(code, None)
            self.cumulative_volumes.pop(code, None)
            self.vi_status.pop(code, None)
            self.ohlcv_data.pop(code, None) # ❗️ 차트 데이터도 제거
            self.current_candle.pop(code, None) # ❗️ 집계 중인 캔들도 제거
  # --- 👆 _update_realtime_subscriptions 함수 수정 끝 ---

  async def _initialize_stock_data(self, stock_code: str):
    """(1회성) 1분봉 차트 이력을 조회하여 ohlcv_data에 저장"""
    if not self.api: return
    try:
        chart_data = await self.api.fetch_minute_chart(stock_code, timeframe=1)
        
        if chart_data is None or chart_data.get('return_code') != 0 or 'stk_min_pole_chart_qry' not in chart_data:
            msg = chart_data.get('return_msg', '분봉 API 오류') if chart_data else '분봉 API 호출 실패'
            self.add_log(f"  ⚠️ [{stock_code}] (초기화) 분봉 데이터 API 오류: {msg}", level="WARNING")
            self.ohlcv_data[stock_code] = pd.DataFrame() # 오류 시 빈 DF 저장
            return

        df = preprocess_chart_data(chart_data["stk_min_pole_chart_qry"])
        if df is None: 
            self.add_log(f"  ⚠️ [{stock_code}] (초기화) 분봉 데이터프레임 변환 실패.", level="WARNING")
            self.ohlcv_data[stock_code] = pd.DataFrame()
            return
            
        self.ohlcv_data[stock_code] = df
        self.add_log(f"  ✅ [{stock_code}] (초기화) 1분봉 차트 이력 {len(df)}건 로드 완료.", level="INFO")

    except Exception as e:
        self.add_log(f"🚨 [CRITICAL] ({stock_code}) 데이터 초기화 중 오류: {e}", level="CRITICAL")
        logger.exception(e)
        self.ohlcv_data[stock_code] = pd.DataFrame() # 오류 시 빈 DF 저장

  async def _process_vi_update(self, stock_code: str, values: Dict):
    """실시간 VI 발동/해제('1h') 데이터 처리 (비동기)"""
    try:
        vi_status_flag = values.get('9068') 
        vi_type = values.get('1225')       
        vi_direction = values.get('9069')  
        vi_release_time_raw = values.get('1224') 

        vi_release_time = f"{vi_release_time_raw[:2]}:{vi_release_time_raw[2:4]}:{vi_release_time_raw[4:]}" if vi_release_time_raw and len(vi_release_time_raw) == 6 else "N/A"
        is_vi_activated = False; status_text = "해제"

        if vi_status_flag in ['1', '2']: 
            is_vi_activated = True
            direction_text = '⬆️상승' if vi_direction == '1' else ('⬇️하락' if vi_direction == '2' else '?')
            vi_type_text = vi_type if vi_type else '?'
            status_text = f"🚨발동🚨 ({vi_type_text}, {direction_text})"

        self.vi_status[stock_code] = is_vi_activated
        self.add_log(f"⚡️ [{stock_code}] VI 상태 업데이트: {status_text} (해제 예정: {vi_release_time})", level="WARNING") 

    except Exception as e:
        self.add_log(f"  🚨 [RT_VI] 실시간 VI({stock_code}) 처리 오류: {e}", level="ERROR") 
        logger.exception(e) 

  def check_vi_status(self, stock_code: str) -> bool:
    is_active = self.vi_status.get(stock_code, False)
    if is_active:
        self.add_log(f"   ⚠️ [{stock_code}] VI 발동 상태 확인됨.", level="DEBUG") 
    return is_active

  def calculate_order_quantity(self, stock_code: str, current_price: float) -> int:
    investment_amount = self.config.strategy.investment_amount_per_stock
    if current_price <= 0:
        self.add_log(f"   ⚠️ [{stock_code}] 주문 수량 계산 불가: 현재가({current_price}) <= 0", level="WARNING") 
        return 0
    quantity = int(investment_amount // current_price)
    self.add_log(f"   ℹ️ [{stock_code}] 주문 수량 계산: 금액({investment_amount}) / 현재가({current_price:.0f}) => {quantity}주", level="DEBUG") 
    return quantity

  def handle_realtime_data(self, ws_data: Dict):
    """웹소켓 콜백 함수"""
    try:
        trnm = ws_data.get('trnm')
        if trnm == 'REAL':
            realtime_data_list = ws_data.get('data')
            if isinstance(realtime_data_list, list):
                for item_data in realtime_data_list:
                    data_type = item_data.get('type')
                    item_code_raw = item_data.get('item', '')
                    values = item_data.get('values')

                    if not data_type or not values:
                        self.add_log(f"⚠️ 실시간 데이터 항목 형식 오류 (type/values 누락): {item_data}", level="WARNING") 
                        continue

                    stock_code = None
                    if item_code_raw:
                        stock_code = item_code_raw[1:] if item_code_raw.startswith('A') else item_code_raw
                        if stock_code.endswith(('_NX', '_AL')): stock_code = stock_code[:-3]

                    # 비동기 처리 예약
                    if data_type == '0B' and stock_code: # 체결
                        # ❗️ 수정: item_data['item'] 대신 정제된 stock_code 전달
                        asyncio.create_task(self._process_realtime_execution(stock_code, values))
                    elif data_type == '0D' and stock_code: # 호가
                        # ❗️ 수정: item_data['item'] 대신 정제된 stock_code 전달
                        asyncio.create_task(self._process_realtime_orderbook(stock_code, values))
                    elif data_type == '00': # 주문 체결 통보
                        # ❗️ 수정: stock_code가 None일 수 있음 (정상)
                        asyncio.create_task(self._process_execution_update(stock_code, values))
                    elif data_type == '04' and stock_code: # 잔고 통보
                        # ❗️ 수정: item_data['item'] 대신 정제된 stock_code 전달
                        asyncio.create_task(self._process_balance_update(stock_code, values))
                    elif data_type == '1h' and stock_code: # VI 발동/해제
                         # ❗️ 수정: item_data['item'] 대신 정제된 stock_code 전달
                         asyncio.create_task(self._process_vi_update(stock_code, values))

        elif trnm in ['REG', 'REMOVE']:
            return_code_raw = ws_data.get('return_code'); return_msg = ws_data.get('return_msg', '')
            try: return_code = int(str(return_code_raw).strip())
            except: return_code = -1
            log_level = "INFO" if return_code == 0 else "WARNING"
            self.add_log(f"📬 WS 응답 ({trnm}): code={return_code_raw}, msg='{return_msg}'", level=log_level) 

            if trnm == 'REG' and not self._realtime_registered:
                if return_code == 0: self._realtime_registered = True
                else: self.engine_status = 'ERROR'; self.add_log("  -> WS 등록 실패로 엔진 상태 ERROR 변경", level="ERROR") 

    except Exception as e:
        self.add_log(f"🚨 실시간 콜백 오류: {e}", level="ERROR") 
        logger.exception(e) 

  async def _process_realtime_execution(self, stock_code: str, values: Dict):
    """실시간 체결(0B) 처리: 1분봉 캔들 집계 및 체결강도 누적"""
    try:
        last_price_str = values.get('10') # 현재가
        exec_vol_signed_str = values.get('15') # 거래량 (+/- 포함)
        exec_time_str = values.get('20') # 체결시간 (HHMMSS)

        if not last_price_str or not exec_vol_signed_str or not exec_time_str: return

        last_price = float(last_price_str.replace('+','').replace('-','').strip())
        exec_vol_signed = int(exec_vol_signed_str.strip())
        exec_vol_abs = abs(exec_vol_signed) # 절대 거래량
        now = datetime.now()
        
        # KST 기준 시간 객체 생성 (체결 시간 사용)
        try:
            current_time = datetime.strptime(
                f"{now.year}-{now.month}-{now.day} {exec_time_str}", 
                "%Y-%m-%d %H%M%S"
            )
        except ValueError:
            current_time = now # 파싱 실패 시 현재 시간 사용

        # 1. 실시간 데이터 저장 (기존 로직 유지)
        if stock_code not in self.realtime_data: self.realtime_data[stock_code] = {}
        self.realtime_data[stock_code].update({ 'last_price': last_price, 'timestamp': current_time })

        # 2. 체결강도 누적 (기존 로직 유지, 시간대 정보 제거)
        if stock_code not in self.cumulative_volumes:
            self.cumulative_volumes[stock_code] = {'buy_vol': 0, 'sell_vol': 0, 'timestamp': current_time.replace(tzinfo=None)}

        last_update = self.cumulative_volumes[stock_code]['timestamp']
        if (current_time.replace(tzinfo=None) - last_update).total_seconds() > 60: # 1분 지났으면 초기화
            self.cumulative_volumes[stock_code] = {'buy_vol': 0, 'sell_vol': 0, 'timestamp': current_time.replace(tzinfo=None)}

        current_cumulative = self.cumulative_volumes[stock_code]
        if exec_vol_signed > 0: current_cumulative['buy_vol'] += exec_vol_signed
        elif exec_vol_signed < 0: current_cumulative['sell_vol'] += abs(exec_vol_signed)
        current_cumulative['timestamp'] = current_time.replace(tzinfo=None)
        
        # 3. --- [신규] 1분봉 캔들 집계 ---
        current_minute = current_time.replace(second=0, microsecond=0) # 현재 캔들의 분(minute)
        
        if stock_code not in self.current_candle or not self.current_candle[stock_code]:
            # 해당 종목의 첫 틱 (또는 새 캔들 시작)
            self.current_candle[stock_code] = {
                'time': current_minute,
                'open': last_price,
                'high': last_price,
                'low': last_price,
                'close': last_price,
                'volume': exec_vol_abs
            }
        
        else:
            candle = self.current_candle[stock_code]
            
            if candle['time'] == current_minute:
                # 1) 같은 분(minute) 캔들에 틱 추가
                candle['high'] = max(candle['high'], last_price)
                candle['low'] = min(candle['low'], last_price)
                candle['close'] = last_price
                candle['volume'] += exec_vol_abs
            
            else:
                # 2) ❗️새로운 분(minute) 시작 = 이전 캔들 완성❗️
                
                # (A) 완성된 캔들(candle)을 비동기 처리
                self.add_log(f"🕯️  [{stock_code}] 1분봉 완성: {candle['time'].strftime('%H:%M')} (O:{candle['open']} H:{candle['high']} L:{candle['low']} C:{candle['close']} V:{candle['volume']})", level="DEBUG")
                asyncio.create_task(self._handle_new_candle(stock_code, candle.copy()))
                
                # (B) 새 캔들 시작
                self.current_candle[stock_code] = {
                    'time': current_minute,
                    'open': last_price,
                    'high': last_price,
                    'low': last_price,
                    'close': last_price,
                    'volume': exec_vol_abs
                }

    except (ValueError, KeyError) as e:
        self.add_log(f"  🚨 [RT_EXEC] ({stock_code}) 데이터 처리 오류: {e}, Data: {values}", level="ERROR") 
    except Exception as e:
        self.add_log(f"  🚨 [RT_EXEC] ({stock_code}) 예상치 못한 오류: {e}", level="ERROR") 
        logger.exception(e) 

  async def _handle_new_candle(self, stock_code: str, completed_candle: Dict[str, Any]):
    """
    완성된 1분봉 캔들을 받아 DataFrame에 추가하고, 
    모든 지표 계산 및 매매 전략을 실행합니다.
    """
    
    if stock_code not in self.ohlcv_data:
        self.add_log(f"  ⚠️ [{stock_code}] 1분봉 완성 신호 수신. 차트 이력(ohlcv_data)이 준비되지 않아 처리 보류.", level="WARNING")
        return
        
    try:
        df = update_ohlcv_with_candle(self.ohlcv_data[stock_code], completed_candle)
        if df is None or df.empty:
             self.add_log(f"  ⚠️ [{stock_code}] 캔들 업데이트 후 DataFrame이 비어있음.", level="WARNING"); return
        self.ohlcv_data[stock_code] = df
    except Exception as df_e:
        self.add_log(f"🚨 [{stock_code}] 1분봉 캔들 DataFrame 업데이트 중 오류: {df_e}", level="ERROR")
        logger.exception(df_e)
        return

    try:
        current_price = completed_candle['close'] 
        total_ask_vol = 0
        total_bid_vol = 0
        orderbook_ws_data = self.orderbook_data.get(stock_code)
        if orderbook_ws_data:
            total_ask_vol = int(orderbook_ws_data.get('total_ask_vol', 0))
            total_bid_vol = int(orderbook_ws_data.get('total_bid_vol', 0))
        
        # --- [수정] 지표 계산 시 self의 동적 설정값 사용 ---
        add_vwap(df)
        add_ema(df, short_period=self.config.strategy.ema_short_period, long_period=self.config.strategy.ema_long_period)
        
        # ❗️ config.strategy.orb_timeframe 대신 self.orb_timeframe 사용
        orb_levels = calculate_orb(df, timeframe=self.orb_timeframe)
        
        rvol_period = self.config.strategy.rvol_period
        rvol = calculate_rvol(df, window=rvol_period)
        # ... (나머지 지표 계산 동일) ...
        cumulative_vols = self.cumulative_volumes.get(stock_code)
        strength_val = None
        if cumulative_vols:
            strength_val = get_strength(cumulative_vols['buy_vol'], cumulative_vols['sell_vol'])
        if 'strength' not in df.columns: df['strength'] = np.nan
        if strength_val is not None: df.iloc[-1, df.columns.get_loc('strength')] = strength_val
        else: df.iloc[-1, df.columns.get_loc('strength')] = np.nan
        obi = calculate_obi(total_bid_vol, total_ask_vol)
        # --- [수정] ---

        if orb_levels['orh'] is None: self.add_log(f"  ⚠️ [{stock_code}] ORH 계산 불가 (데이터 부족?).", level="DEBUG"); return 

        # ... (로그 출력 부분 동일) ...
        orh_str = f"{orb_levels['orh']:.0f}" if orb_levels['orh'] is not None else "N/A"
        orl_str = f"{orb_levels['orl']:.0f}" if orb_levels['orl'] is not None else "N/A"
        vwap_str = f"{df['vwap'].iloc[-1]:.0f}" if 'vwap' in df.columns and not pd.isna(df['vwap'].iloc[-1]) else "N/A"
        ema_short_col = f'EMA_{self.config.strategy.ema_short_period}'; ema_long_col = f'EMA_{self.config.strategy.ema_long_period}'
        ema9_str = f"{df[ema_short_col].iloc[-1]:.0f}" if ema_short_col in df.columns and not pd.isna(df[ema_short_col].iloc[-1]) else "N/A"
        ema20_str = f"{df[ema_long_col].iloc[-1]:.0f}" if ema_long_col in df.columns and not pd.isna(df[ema_long_col].iloc[-1]) else "N/A"
        rvol_str = f"{rvol:.1f}%" if rvol is not None else "N/A"
        obi_str = f"{obi:.2f}" if obi is not None else "N/A"
        strength_str = f"{strength_val:.1f}%" if strength_val is not None else "N/A"
        self.add_log(f"📊 [{stock_code}] 현재가:{current_price:.0f}, ORH:{orh_str}, ORL:{orl_str}, VWAP:{vwap_str}, EMA({ema9_str}/{ema20_str}), RVOL:{rvol_str}, OBI:{obi_str}, Strength:{strength_str}", level="DEBUG")

        position_info = self.positions.get(stock_code)

        # 5-1. 포지션 없을 때 (진입 시도)
        if not position_info or position_info.get('status') == 'CLOSED':
            if self.check_vi_status(stock_code):
                self.add_log(f"   ⚠️ [{stock_code}] VI 발동 중. 신규 진입 보류.", level="INFO") 
                return

            # --- 👇 [수정] check_breakout_signal 호출 시 self.breakout_buffer 전달 ---
            # ❗️ (기존) signal = check_breakout_signal(df, orb_levels) 
            # ❗️ (수정) momentum_orb.py의 원본 시그니처에 맞게 수정
            signal = check_breakout_signal(current_price, orb_levels, self.breakout_buffer) 
            # --- 👆 [수정] ---
            
            # ❗️ [임시 수정] RVOL 필터 비활성화 (기존과 동일)
            rvol_ok = True 
            obi_ok = obi is not None and obi >= self.config.strategy.obi_threshold
            strength_ok = strength_val is not None and strength_val >= self.config.strategy.strength_threshold
            ema_short_val = df[ema_short_col].iloc[-1] if ema_short_col in df.columns and not pd.isna(df[ema_short_col].iloc[-1]) else None
            ema_long_val = df[ema_long_col].iloc[-1] if ema_long_col in df.columns and not pd.isna(df[ema_long_col].iloc[-1]) else None
            momentum_ok = ema_short_val is not None and ema_long_val is not None and ema_short_val > ema_long_val

            if signal == "BUY":
                if rvol_ok and obi_ok and strength_ok and momentum_ok:
                    if len([p for p in self.positions.values() if p.get('status') == 'IN_POSITION']) >= self.config.strategy.max_concurrent_positions:
                        self.add_log(f"   ⚠️ [{stock_code}] 최대 보유 종목 수({self.config.strategy.max_concurrent_positions}) 도달. 진입 보류.", level="WARNING") 
                        return

                    order_qty = self.calculate_order_quantity(stock_code, current_price)
                    if order_qty > 0:
                        self.add_log(f"🔥 [{stock_code}] 매수 조건 충족! {order_qty}주 시장가 매수 주문 시도...", level="INFO") 
                        order_result = await self.api.create_buy_order(stock_code, order_qty)

                        if order_result and order_result.get('return_code') == 0:
                            order_no = order_result.get('ord_no')
                            
                            # --- 👇 [수정] 포지션 생성 시 현재 엔진의 설정값을 복사/저장 ---
                            self.positions[stock_code] = {
                                'stk_cd': stock_code, 'entry_price': None, 'size': order_qty, 
                                'status': 'PENDING_ENTRY', 'order_no': order_no,
                                'entry_time': None, 'partial_profit_taken': False,
                                # ❗️ 현재 엔진의 동적 설정값을 이 포지션에 '고정'시킴
                                'target_profit_pct': self.take_profit_pct, 
                                'stop_loss_pct': self.stop_loss_pct,       
                                'partial_profit_pct': self.partial_take_profit_pct,
                                'partial_profit_ratio': self.partial_take_profit_ratio 
                            }
                            # --- 👆 [수정] ---
                            
                            self.add_log(f"   ➡️ [{stock_code}] 매수 주문 접수 완료: {order_no}", level="INFO") 
                        else:
                            error_msg = order_result.get('return_msg', '주문 실패') if order_result else 'API 호출 실패'
                            self.add_log(f"   ❌ [{stock_code}] 매수 주문 실패: {error_msg}", level="ERROR") 
                else:
                    filter_log = f"RVOL:{rvol_ok}, OBI:{obi_ok}, Strength:{strength_ok}, Momentum:{momentum_ok}"
                    self.add_log(f"   ⚠️ [{stock_code}] 매수 신호 발생했으나 필터 미충족 ({filter_log}). 진입 보류.", level="DEBUG") 

        # 5-2. 포지션 있을 때 (청산 시도)
        elif position_info.get('status') == 'IN_POSITION':
            if self.check_vi_status(stock_code):
                exit_signal = "VI_STOP"
                self.add_log(f"   🚨 [{stock_code}] VI 발동 감지! 강제 청산 시도.", level="WARNING") 
            else:
                # --- 👇 [수정] manage_position 호출 시그니처 변경 없음 ---
                # (risk_manager가 position_info에서 값을 읽도록 수정했기 때문)
                exit_signal = manage_position(position_info, df) 
                # --- 👆 [수정] ---

                TIME_STOP_HOUR = self.config.strategy.time_stop_hour; TIME_STOP_MINUTE = self.config.strategy.time_stop_minute
                now_kst = datetime.now().astimezone() 
                if now_kst.hour >= TIME_STOP_HOUR and now_kst.minute >= TIME_STOP_MINUTE and exit_signal is None:
                   exit_signal = "TIME_STOP"
                   self.add_log(f"   ⏰ [{stock_code}] 시간 청산 조건 ({TIME_STOP_HOUR}:{TIME_STOP_MINUTE}) 도달.", level="INFO") 

            # 부분 익절
            if exit_signal == "PARTIAL_TAKE_PROFIT" and not position_info.get('partial_profit_taken', False):
                current_size = position_info.get('size', 0)
                # ❗️ [수정] config 대신 position_info에 저장된 ratio 사용
                partial_ratio = position_info.get('partial_profit_ratio', self.config.strategy.partial_take_profit_ratio)
                size_to_sell = math.ceil(current_size * partial_ratio) 

                if size_to_sell > 0 and size_to_sell < current_size :
                    # ... (이하 부분 익절 로직 동일) ...
                    self.add_log(f"💰 [{stock_code}] 부분 익절 실행 ({partial_ratio*100:.0f}%): {size_to_sell}주 매도 시도", level="INFO") 
                    order_result = await self.api.create_sell_order(stock_code, size_to_sell) 

                    if order_result and order_result.get('return_code') == 0:
                        order_no = order_result.get('ord_no')
                        position_info.update({
                            'status': 'PENDING_EXIT', 'exit_signal': exit_signal,
                            'order_no': order_no, 'original_size_before_exit': current_size, 
                            'size_to_sell': size_to_sell, 'filled_qty': 0, 'filled_value': 0.0 
                        })
                        self.add_log(f" PARTIAL ⬅️ [{stock_code}] 부분 익절 주문 접수 완료. 상태: {position_info}", level="INFO") 
                    else:
                        error_msg = order_result.get('return_msg', '주문 실패') if order_result else 'API 호출 실패'
                        self.add_log(f"❌ [{stock_code}] 부분 익절 주문 실패: {error_msg}", level="ERROR") 
                        position_info['status'] = 'ERROR_EXIT_ORDER' 
                elif size_to_sell >= current_size and current_size > 0:
                     exit_signal = "TAKE_PROFIT" 
                     self.add_log(f"   ℹ️ [{stock_code}] 부분 익절 수량이 현재 수량 이상 -> 전체 익절로 전환.", level="INFO") 
                else: exit_signal = None 

            # 전체 청산
            if exit_signal in ["TAKE_PROFIT", "STOP_LOSS", "EMA_CROSS_SELL", "VWAP_BREAK_SELL", "TIME_STOP", "VI_STOP"]:
                # ... (이하 전체 청산 로직 동일) ...
                if exit_signal != "PARTIAL_TAKE_PROFIT": 
                    self.add_log(f"🎉 [{stock_code}] 전체 청산 조건 ({exit_signal}) 충족! 매도 주문 실행.", level="INFO") 

                size_to_sell = position_info.get('size', 0)
                if size_to_sell > 0:
                    order_result = await self.api.create_sell_order(stock_code, size_to_sell) 

                    if order_result and order_result.get('return_code') == 0:
                        order_no = order_result.get('ord_no')
                        position_info.update({
                            'status': 'PENDING_EXIT', 'exit_signal': exit_signal,
                            'order_no': order_no, 'original_size_before_exit': size_to_sell, 
                            'size_to_sell': size_to_sell, 'filled_qty': 0, 'filled_value': 0.0
                        })
                        self.add_log(f"⬅️ [{stock_code}] (전체) 청산 주문 접수 완료. 상태: {position_info}", level="INFO") 
                    else:
                        error_msg = order_result.get('return_msg', '주문 실패') if order_result else 'API 호출 실패'
                        self.add_log(f"❌ [{stock_code}] (전체) 청산 주문 실패: {error_msg}", level="ERROR") 
                        position_info['status'] = 'ERROR_EXIT_ORDER' 

        elif position_info.get('status') == 'PENDING_ENTRY':
            self.add_log(f"  ⏳ [{stock_code}] 매수 주문({position_info.get('order_no')}) 진행 중...", level="DEBUG") 
        elif position_info.get('status') == 'PENDING_EXIT':
            self.add_log(f"  ⏳ [{stock_code}] 매도 주문({position_info.get('order_no')}) 진행 중...", level="DEBUG") 

    except Exception as e:
        self.add_log(f"🚨 [CRITICAL] 캔들 핸들러({stock_code}) 오류: {e} 🚨", level="CRITICAL") 
        logger.exception(e) 
        if stock_code in self.positions: self.positions[stock_code]['status'] = 'ERROR_TICK'

  async def _process_realtime_orderbook(self, stock_code: str, values: Dict):
    try:
        total_ask_vol_str = values.get('121') 
        total_bid_vol_str = values.get('125') 
        timestamp_str = values.get('21')     

        if total_ask_vol_str is None or total_bid_vol_str is None or timestamp_str is None:
             self.add_log(f"   ⚠️ [RT_ORDERBOOK] ({stock_code}) 호가 데이터 누락: {values}", level="DEBUG"); return 

        total_ask_vol = int(total_ask_vol_str)
        total_bid_vol = int(total_bid_vol_str)
        now = datetime.now()

        if stock_code not in self.orderbook_data: self.orderbook_data[stock_code] = {}
        self.orderbook_data[stock_code].update({
            'total_ask_vol': total_ask_vol,
            'total_bid_vol': total_bid_vol,
            'timestamp': now
        })

    except (ValueError, KeyError) as e:
        self.add_log(f"  🚨 [RT_ORDERBOOK] ({stock_code}) 데이터 처리 오류: {e}, Data: {values}", level="ERROR") 
    except Exception as e:
        self.add_log(f"  🚨 [RT_ORDERBOOK] ({stock_code}) 예상치 못한 오류: {e}", level="ERROR") 
        logger.exception(e) 

  # --- _process_execution_update 함수 (기존과 동일) ---
  async def _process_execution_update(self, stock_code: Optional[str], values: Dict):
    stock_code_from_value = None 
    try:
        order_no = values.get('9203') 
        exec_no = values.get('909') 
        stock_code_raw = values.get('9001') 
        order_status = values.get('913') 
        filled_qty_str = values.get('911') 
        unfilled_qty_str = values.get('902') 
        filled_price_str = values.get('910') 
        io_type = values.get('905') 

        if stock_code_raw and stock_code_raw.startswith('A'):
            stock_code_from_value = stock_code_raw[1:]
            if stock_code_from_value.endswith(('_NX', '_AL')): stock_code_from_value = stock_code_from_value[:-3]
        elif stock_code: 
             stock_code_from_value = stock_code
        else: 
             self.add_log(f"⚠️ [RT_EXEC_UPDATE] 종목코드 확인 불가: {values}", level="WARNING"); return 

        if not order_no or not order_status:
            self.add_log(f"⚠️ [RT_EXEC_UPDATE] 필수 정보 누락(주문번호/상태): {values}", level="WARNING"); return 

        target_pos_code = None
        target_pos_info = None
        for code, pos in self.positions.items():
            if pos.get('order_no') == order_no:
                target_pos_code = code
                target_pos_info = pos
                break

        if not target_pos_info: return 

        if order_status == '체결' and exec_no:
            try:
                filled_qty = int(filled_qty_str) if filled_qty_str else 0
                filled_price = float(filled_price_str) if filled_price_str else 0.0
                unfilled_qty = int(unfilled_qty_str) if unfilled_qty_str else 0
            except (ValueError, TypeError):
                 self.add_log(f"🚨 [RT_EXEC_UPDATE] ({target_pos_code}) 체결 값 변환 오류: qty='{filled_qty_str}', price='{filled_price_str}', unfilled='{unfilled_qty_str}'", level="ERROR"); return 

            if filled_qty <= 0 or filled_price <= 0: return 

            current_status = target_pos_info.get('status')
            io_type_text = '매수' if io_type == '+매수' else ('매도' if io_type == '-매도' else io_type)
            self.add_log(f"✅ [{target_pos_code}] {io_type_text} 체결 완료: {filled_qty}주 @ {filled_price:.0f}", level="INFO") 

            if current_status == 'PENDING_ENTRY':
                target_pos_info['entry_price'] = filled_price
                target_pos_info['entry_time'] = datetime.now()
                if unfilled_qty == 0: 
                    target_pos_info['status'] = 'IN_POSITION'
                    self.add_log(f"   ℹ️ [{target_pos_code}] 포지션 상태 변경: PENDING_ENTRY -> IN_POSITION", level="DEBUG") 
                else: 
                     self.add_log(f"   ⚠️ [{target_pos_code}] 매수 부분 체결 감지 (로직 추가 필요): 주문({target_pos_info.get('size', 0)}), 체결({filled_qty}), 미체결({unfilled_qty})", level="WARNING") 

            elif current_status == 'PENDING_EXIT':
                target_pos_info['filled_qty'] = target_pos_info.get('filled_qty', 0) + filled_qty
                target_pos_info['filled_value'] = target_pos_info.get('filled_value', 0.0) + (filled_price * filled_qty)

                if target_pos_info.get('exit_signal') == "PARTIAL_TAKE_PROFIT":
                    if target_pos_info['filled_qty'] >= target_pos_info.get('size_to_sell', 0): 
                         remaining_size = target_pos_info.get('original_size_before_exit', 0) - target_pos_info['filled_qty']
                         if remaining_size < 0: remaining_size = 0
                         target_pos_info['size'] = remaining_size
                         target_pos_info['status'] = 'IN_POSITION' if remaining_size > 0 else 'CLOSED' 
                         target_pos_info['partial_profit_taken'] = True
                         target_pos_info['order_no'] = None
                         self.add_log(f"💰 [{target_pos_code}] 부분 청산 체결 업데이트 완료. 상태: {target_pos_info}", level="INFO") 
                    else: 
                         self.add_log(f"   ⏳ [{target_pos_code}] 부분 청산 진행 중... (체결:{target_pos_info['filled_qty']}/{target_pos_info.get('size_to_sell')})", level="DEBUG") 

                else:
                    if target_pos_info['filled_qty'] >= target_pos_info.get('original_size_before_exit', 0): 
                        target_pos_info['status'] = 'CLOSED'
                        target_pos_info['order_no'] = None
                        self.add_log(f"🏁 [{target_pos_code}] 전체 청산 체결 업데이트 완료. 상태: {target_pos_info}", level="INFO") 
                    else: 
                        self.add_log(f"   ⏳ [{target_pos_code}] 전체 청산 진행 중... (체결:{target_pos_info['filled_qty']}/{target_pos_info.get('original_size_before_exit')})", level="DEBUG") 

        elif order_status == '확인':
            if io_type == '±정정':
                 self.add_log(f"   ℹ️ [{target_pos_code}] 주문 정정 확인 (주문번호: {order_no})", level="INFO") 
            elif io_type == '∓취소':
                 self.add_log(f"   ℹ️ [{target_pos_code}] 주문 취소 확인 (주문번호: {order_no})", level="INFO") 
                 if target_pos_info.get('status') == 'PENDING_ENTRY': target_pos_info['status'] = 'CANCELLED'
                 elif target_pos_info.get('status') == 'PENDING_EXIT': target_pos_info['status'] = 'IN_POSITION' 
                 target_pos_info['order_no'] = None

        elif order_status == '거부':
             self.add_log(f"   ❌ [{target_pos_code}] 주문 거부 (주문번호: {order_no})", level="ERROR") 
             if target_pos_info.get('status') == 'PENDING_ENTRY': target_pos_info['status'] = 'REJECTED'
             elif target_pos_info.get('status') == 'PENDING_EXIT': target_pos_info['status'] = 'IN_POSITION' 
             target_pos_info['order_no'] = None

        elif order_status != '접수':
            self.add_log(f"   ℹ️ [{target_pos_code}] 주문 상태 변경: {order_status} (주문번호: {order_no})", level="DEBUG") 

    except Exception as e:
        self.add_log(f"🚨 [RT_EXEC_UPDATE] ({stock_code_from_value or 'Unknown'}) 체결 처리 오류: {e}", level="ERROR") 
        logger.exception(e) 

  async def _process_balance_update(self, stock_code: str, values: Dict):
    try:
        current_qty_str = values.get('930') 
        avg_price_str = values.get('931') 
        current_price_str = values.get('10') 

        if current_qty_str is None or avg_price_str is None or current_price_str is None: return

        current_qty = int(current_qty_str)
        avg_price = float(avg_price_str)
        current_price = float(current_price_str.replace('+','').replace('-','').strip())

        self.add_log(f"💰 [RT_BALANCE] ({stock_code}) 잔고 업데이트: 보유 {current_qty}주, 평단 {avg_price:.0f}, 현재가 {current_price:.0f}", level="DEBUG") 

    except Exception as e:
        self.add_log(f"🚨 [RT_BALANCE] ({stock_code}) 잔고 처리 오류: {e}", level="ERROR") 
        logger.exception(e) 

  async def execute_kill_switch(self):
    self.add_log("🚨🚨🚨 [KILL SWITCH] 긴급 정지 발동! 모든 포지션 시장가 청산 시도! 🚨🚨🚨", level="CRITICAL") 
    self.engine_status = "KILL_SWITCH_ACTIVATED"
    await self.stop() 

    if self.api:
        positions_to_liquidate = self.positions.copy() 
        self.add_log(f"  -> [KILL] 청산 대상 포지션 {len(positions_to_liquidate)}개 확인.", level="INFO") 

        for stock_code, pos_info in positions_to_liquidate.items():
            if pos_info.get('status') == 'IN_POSITION' and pos_info.get('size', 0) > 0:
                quantity = pos_info['size']
                self.add_log(f"     -> [KILL] 시장가 청산 시도 ({stock_code} {quantity}주)...", level="WARNING") 
                result = await self.api.create_sell_order(stock_code, quantity) 
                if result and result.get('return_code') == 0:
                    if stock_code in self.positions: self.positions[stock_code].update({'status': 'PENDING_EXIT', 'exit_signal': 'KILL_SWITCH', 'order_no': result.get('ord_no'), 'original_size_before_exit': quantity, 'filled_qty': 0, 'filled_value': 0.0 })
                    self.add_log(f"     ✅ [KILL] 시장가 청산 주문 접수 ({stock_code} {quantity}주)", level="INFO") 
                else:
                    error_info = result.get('return_msg', '주문 실패') if result else 'API 호출 실패'
                    self.add_log(f"     ❌ [KILL] 시장가 청산 주문 실패 ({stock_code} {quantity}주): {error_info}", level="ERROR") 
                    if stock_code in self.positions: self.positions[stock_code]['status'] = 'ERROR_LIQUIDATION' 
            elif pos_info.get('status') in ['PENDING_ENTRY', 'PENDING_EXIT']:
                self.add_log(f"     ℹ️ [KILL] 주문 진행 중 포지션({stock_code}) 확인. 미체결 취소 필요.", level="INFO") 

        self.add_log("  <- [KILL] 시장가 청산 주문 접수 완료.", level="INFO") 
    else:
        self.add_log("  ⚠️ [KILL] API 객체가 없어 청산 불가.", level="ERROR")