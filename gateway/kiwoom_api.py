# kiwoom_api.py
import httpx
import asyncio
import json
import os
import ssl
import websockets
import traceback
from typing import Optional, Dict, List, Callable
from datetime import datetime, timedelta

from config.loader import config

class KiwoomAPI:
    """키움증권 REST API 및 WebSocket API와의 비동기 통신을 담당합니다."""

    TOKEN_FILE = ".token"
    BASE_URL_PROD = "https://api.kiwoom.com"
    REALTIME_URI_PROD = "wss://api.kiwoom.com:10000/api/dostk/websocket"
    BASE_URL_MOCK = "https://mockapi.kiwoom.com"
    REALTIME_URI_MOCK = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"

    def __init__(self):
        self.is_mock = config.is_mock
        if self.is_mock:
            print("🚀 모의투자 환경으로 설정합니다.")
            self.base_url = self.BASE_URL_MOCK
            self.realtime_uri = self.REALTIME_URI_MOCK
            self.app_key = config.kiwoom.mock_app_key
            self.app_secret = config.kiwoom.mock_app_secret
            self.account_no = config.kiwoom.mock_account_no
        else:
            print("💰 실전투자 환경으로 설정합니다.")
            self.base_url = self.BASE_URL_PROD
            self.realtime_uri = self.REALTIME_URI_PROD
            self.app_key = config.kiwoom.app_key
            self.app_secret = config.kiwoom.app_secret
            self.account_no = config.kiwoom.account_no

        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self.client = httpx.AsyncClient(timeout=None)
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.message_handler: Optional[Callable[[Dict], None]] = None
        self._load_token_from_file()

    def add_log(self, message: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}][API] {message}")

    # --- 토큰 관리 (기존 코드 유지) ---
    def _load_token_from_file(self):
        if os.path.exists(self.TOKEN_FILE):
            try:
                with open(self.TOKEN_FILE, 'r') as f:
                    token_data = json.load(f)
                self._access_token = token_data.get('access_token')
                expires_str = token_data.get('expires_at')
                if expires_str:
                    self._token_expires_at = datetime.fromisoformat(expires_str)
                    if self.is_token_valid():
                        self.add_log(f"ℹ️ 저장된 토큰을 불러왔습니다. (만료: {self._token_expires_at})")
                    else:
                        self.add_log(f"⚠️ 저장된 토큰 만료됨 (만료: {self._token_expires_at}).")
                        self._access_token = None; self._token_expires_at = None
                else:
                    self.add_log("⚠️ 토큰 파일에 만료 정보 없음."); self._access_token = None; self._token_expires_at = None
            except (json.JSONDecodeError, KeyError, ValueError, OSError) as e:
                self.add_log(f"⚠️ 토큰 파일 로드 오류: {e}."); self._access_token = None; self._token_expires_at = None
        else:
            self.add_log(f"ℹ️ 토큰 파일({self.TOKEN_FILE}) 없음."); self._access_token = None; self._token_expires_at = None

    def _save_token_to_file(self):
        if self._access_token and self._token_expires_at:
            token_data = {'access_token': self._access_token, 'expires_at': self._token_expires_at.isoformat()}
            try:
                with open(self.TOKEN_FILE, 'w') as f: json.dump(token_data, f)
                self.add_log(f"💾 새 토큰 저장 완료 (만료: {self._token_expires_at})")
            except IOError as e: self.add_log(f"❌ 토큰 파일 저장 실패: {e}")

    def is_token_valid(self) -> bool:
        if not self._access_token or not self._token_expires_at: return False
        return datetime.now() + timedelta(minutes=1) < self._token_expires_at

    async def get_access_token(self) -> Optional[str]:
        if self.is_token_valid(): return self._access_token
        self.add_log("ℹ️ 접근 토큰 신규 발급/갱신 시도...")
        url = f"{self.base_url}/oauth2/token"
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        body = {"grant_type": "client_credentials", "appkey": self.app_key, "secretkey": self.app_secret}
        try:
            if self.client.is_closed: self.client = httpx.AsyncClient(timeout=None)
            res = await self.client.post(url, headers=headers, json=body)
            res.raise_for_status(); data = res.json()
            access_token = data.get("access_token") or data.get("token")
            expires_dt_str = data.get("expires_dt")
            if access_token and expires_dt_str:
                self._access_token = access_token
                try: self._token_expires_at = datetime.strptime(expires_dt_str, "%Y%m%d%H%M%S")
                except ValueError: self.add_log(f"❌ 만료 시간 형식 오류: {expires_dt_str}"); return None
                self.add_log(f"✅ 접근 토큰 발급/갱신 성공 (만료: {self._token_expires_at})")
                self._save_token_to_file(); return self._access_token
            else:
                error_msg = data.get('error_description') or data.get('return_msg') or data.get('msg1', '알 수 없는 오류')
                self.add_log(f"❌ 토큰 발급 응답 오류: {error_msg} | 응답: {data}")
                self._access_token = None; self._token_expires_at = None; return None
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_data = e.response.json(); error_msg = error_data.get('error_description') or error_data.get('return_msg') or error_data.get('msg1', error_text)
            except: pass
            self.add_log(f"❌ 접근 토큰 발급 실패 (HTTP {e.response.status_code}): {error_msg}")
            self._access_token = None; self._token_expires_at = None; return None
        except Exception as e:
            self.add_log(f"❌ 예상치 못한 오류 (get_access_token): {e}")
            self._access_token = None; self._token_expires_at = None; return None

    async def _get_headers(self, tr_id: str, is_order: bool = False) -> Optional[Dict]:
        pure_token = await self.get_access_token()
        if not pure_token: self.add_log(f"❌ 헤더 생성 실패: 유효 토큰 없음 (tr_id: {tr_id})"); return None
        access_token_with_bearer = f"Bearer {pure_token}"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": access_token_with_bearer,
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "api-id": tr_id,
        }
        if is_order:
            if not self.account_no: self.add_log("❌ 주문 헤더 생성 실패: 계좌번호 없음."); return None
            headers["custtype"] = "P"
            # 주문 TR ID를 기반으로 정확한 헤더를 추가해야 할 수 있습니다.
            # 예: 주문 API가 'tr_cont' 헤더를 요구한다면 여기서 추가
            # if tr_id in ["kt10000", "kt10001", "kt10002", "kt10003"]: # 주문 관련 TR ID 목록
            #    headers["tr_cont"] = "N" # 또는 필요한 값
        return headers

    # --- WebSocket 연결 및 관리 ---
    async def connect_websocket(self, handler: Callable[[Dict], None]) -> bool:
        """웹소켓 연결, LOGIN 인증, 실시간 데이터 수신 시작"""
        if self.websocket and self.websocket.open:
            self.add_log("ℹ️ 이미 웹소켓에 연결됨."); return True

        pure_token = await self.get_access_token()
        if not pure_token:
            self.add_log("❌ 웹소켓 연결 불가: 유효 토큰 없음."); return False

        self.message_handler = handler
        self.add_log(f"🛰️ 웹소켓 연결 시도: {self.realtime_uri}")

        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        self.add_log("⚠️ SSL 인증서 검증을 비활성화합니다. (테스트 목적, 보안 주의!)")

        connection_timeout = 60

        try:
            # --- 수정: extra_headers 제거, ping_interval=None ---
            self.websocket = await websockets.connect(
                self.realtime_uri,
                ping_interval=None, # 서버 PING에 라이브러리가 자동 PONG 응답하도록 시도
                ping_timeout=20,    # PONG 응답 대기 시간은 유지
                open_timeout=connection_timeout,
                ssl=ssl_context
            )
            # --- 수정 끝 ---
            self.add_log("✅ 웹소켓 연결 성공! (SSL 검증 비활성화)")

            # LOGIN 처리 (기존과 동일)
            try:
                login_packet = {'trnm': 'LOGIN', 'token': pure_token}
                login_request_string = json.dumps(login_packet)
                self.add_log(f"➡️ WS LOGIN 요청 전송: {json.dumps({'trnm': 'LOGIN', 'token': '...' + pure_token[-10:]})}")
                await self.websocket.send(login_request_string)
                self.add_log("✅ WS LOGIN 요청 전송 완료")

                self.add_log("⏳ WS LOGIN 응답 대기 중...")
                login_response_str = await asyncio.wait_for(self.websocket.recv(), timeout=10)
                self.add_log(f"📬 WS LOGIN 응답 수신: {login_response_str}")
                login_response = json.loads(login_response_str)

                if login_response.get('trnm') == 'LOGIN' and login_response.get('return_code') == 0:
                    self.add_log("✅ 웹소켓 LOGIN 성공")
                else:
                    self.add_log(f"❌ 웹소켓 LOGIN 실패: {login_response}")
                    await self.disconnect_websocket(); return False

            except asyncio.TimeoutError:
                self.add_log("❌ WS LOGIN 응답 시간 초과 (10초)")
                await self.disconnect_websocket(); return False
            except json.JSONDecodeError:
                self.add_log(f"❌ WS LOGIN 응답 파싱 실패: {login_response_str}")
                await self.disconnect_websocket(); return False
            except Exception as login_e:
                self.add_log(f"❌ WS LOGIN 처리 중 오류: {login_e}")
                self.add_log(f" traceback: {traceback.format_exc()}")
                await self.disconnect_websocket(); return False

            asyncio.create_task(self._receive_messages())
            await asyncio.sleep(1)

            # --- 수정: TR 등록 시 account_no를 key로 사용 ---
            # '00', '04' TR은 계좌번호를 item(key)으로 사용해야 할 수 있음 (가이드 확인 필요)
            # 일단 가이드 예시처럼 ""를 사용, 문제가 되면 계좌번호로 변경
            if not self.account_no:
                 self.add_log("❌ 실시간 TR 등록 실패: 계좌번호 설정 필요"); await self.disconnect_websocket(); return False
            await self.register_realtime(tr_ids=['00', '04'], tr_keys=["", ""]) # 키움 가이드대로 item을 "" 로 설정
            # --- 수정 끝 ---
            return True

        except websockets.exceptions.InvalidStatusCode as e:
            self.add_log(f"❌ 웹소켓 연결 실패 (상태 코드 {e.status_code}): {e.headers}. 주소/서버 상태 확인.")
        except asyncio.TimeoutError:
            self.add_log(f'❌ 웹소켓 연결 시간 초과 ({connection_timeout}초)')
        except OSError as e:
             self.add_log(f"❌ 웹소켓 연결 OS 오류: {e}")
        except Exception as e:
            self.add_log(f"❌ 웹소켓 연결 중 예상치 못한 오류: {e}")
            self.add_log(f" traceback: {traceback.format_exc()}")

        self.websocket = None
        return False

    async def _receive_messages(self):
        """웹소켓 메시지 수신 및 처리 루프 (데이터 처리, PONG 자동 처리 기대)"""
        if not self.websocket or not self.websocket.open:
            self.add_log("⚠️ 메시지 수신 불가: 웹소켓 연결 안됨."); return
        self.add_log("👂 실시간 메시지 수신 대기 중...")
        try:
            async for message in self.websocket:

                if isinstance(message, bytes): continue
                if not isinstance(message, str) or not message.strip(): continue

                try:
                    data = json.loads(message)
                    trnm = data.get("trnm")

                    if trnm == "SYSTEM":
                        code = data.get("code"); msg = data.get("message")
                        self.add_log(f"ℹ️ WS 시스템 메시지: [{code}] {msg}")
                        continue
                    
                    # --- 👇 PING 처리 로직 복구 ---
                    elif trnm == 'PING':
                        self.add_log(">>> PING 수신. PING을 그대로 응답합니다.")
                        # 수신한 PING 메시지 문자열을 그대로 다시 보냄
                        asyncio.create_task(self.send_websocket_request_raw(message))
                        continue # PING 처리는 여기서 종료
                    # --- 👆 PING 처리 복구 끝 ---

                    if trnm == 'LOGIN': continue

                    # --- 👇 REG/REMOVE 응답도 message_handler로 전달 ---
                    if trnm in ['REG', 'REMOVE']:
                        rt_cd_raw = data.get('return_code')
                        msg = data.get('return_msg', '메시지 없음')
                        self.add_log(f"📬 WS 응답 ({trnm}): code={rt_cd_raw}, msg='{msg}'") # 로그 위치 이동 및 내용 확인

                        # 핸들러가 설정되어 있으면 응답 데이터 전달
                        if self.message_handler:
                            # engine.py의 handle_realtime_data가 처리할 수 있도록 데이터 전달
                            self.message_handler(data) # data 딕셔너리 전체 전달
                        # continue 제거: 아래 REAL 처리 로직과 분리

                    elif trnm == 'REAL':
                        realtime_data_list = data.get('data')
                        if isinstance(realtime_data_list, list):
                            for item_data in realtime_data_list:
                                data_type = item_data.get('type')
                                item_code = item_data.get('item')
                                values = item_data.get('values')

                                if data_type and values and self.message_handler:
                                    # 핸들러에 필요한 정보만 전달 (가이드 구조에 맞춰)
                                    self.message_handler({
                                        "trnm": "REAL", # trnm 명시
                                        "type": data_type,
                                        "item": item_code,
                                        "values": values
                                    })
                                else:
                                    self.add_log(f"⚠️ 실시간 데이터 항목 형식 오류: {item_data}")
                        else:
                            self.add_log(f"⚠️ 'REAL' 메시지 data 필드 오류: {data}")

                    else: # PONG 또는 알 수 없는 trnm
                        if trnm != 'PONG':
                             self.add_log(f"ℹ️ 알 수 없는 형식의 WS 메시지 (trnm: {trnm}): {data}")

                except json.JSONDecodeError: self.add_log(f"⚠️ WS JSON 파싱 실패: {message[:100]}...")
                except Exception as e:
                    self.add_log(f"❌ WS 메시지 처리 중 오류: {e} | Msg: {message[:100]}...")
                    self.add_log(f" traceback: {traceback.format_exc()}")

        except websockets.exceptions.ConnectionClosedOK: self.add_log("ℹ️ 웹소켓 정상 종료.")
        except websockets.exceptions.ConnectionClosedError as e: self.add_log(f"❌ 웹소켓 비정상 종료: {e.code} {e.reason}")
        except asyncio.CancelledError: self.add_log("ℹ️ 메시지 수신 태스크 취소됨.")
        except Exception as e: self.add_log(f"❌ WS 수신 루프 오류: {e}")
        finally: self.add_log("🛑 메시지 수신 루프 종료."); self.websocket = None

    async def send_websocket_request_raw(self, message: str):
        """JSON 문자열을 웹소켓으로 직접 전송 (LOGIN, REG, REMOVE, PONG 용도)"""
        if self.websocket and self.websocket.open:
            try:
                await self.websocket.send(message)
            except Exception as e:
                self.add_log(f"❌ 웹소켓 RAW 메시지 전송 실패: {e}")
        else:
            self.add_log("⚠️ 웹소켓 미연결, RAW 전송 불가.")

    async def register_realtime(self, tr_ids: list[str], tr_keys: list[str], group_no: str = "1"):
        """실시간 데이터 구독 ('REG') 메시지 구성 및 전송 (가이드 형식 준수)"""
        self.add_log(f"➡️ 실시간 등록 요청 시도: ID(type)={tr_ids}, KEY(item)={tr_keys}")
        if len(tr_ids) != len(tr_keys):
            self.add_log("❌ 실시간 등록 실패: ID(type)와 KEY(item) 개수가 일치하지 않음"); return

        # --- 수정: data 리스트 구성 방식 (가이드 기반) ---
        # 가이드 예시: data: [{"item": ["005930"], "type": ["0B"]}, {"item": [""], "type": ["00"]}]
        data_payload = []
        grouped_items = {} # type별로 item들을 그룹화

        for tr_id, tr_key in zip(tr_ids, tr_keys):
            # item이 필요없는 TR ('00', '04') 처리
            if tr_id in ['00', '04']:
                # 빈 문자열 item을 가진 항목 추가
                data_payload.append({"item": [""], "type": [tr_id]})
            # item이 필요한 TR 처리
            else:
                if tr_id not in grouped_items:
                    grouped_items[tr_id] = []
                if tr_key: # 유효한 key만 추가
                    grouped_items[tr_id].append(tr_key)

        # 그룹화된 item들을 data_payload에 추가
        for tr_id, items in grouped_items.items():
            if items: # item이 하나라도 있을 경우에만 추가
                data_payload.append({"item": items, "type": [tr_id]})
        # --- 수정 끝 ---

        if not data_payload:
            self.add_log("⚠️ 실시간 등록 요청할 유효한 데이터 없음.")
            return

        request_message = { 'trnm': 'REG', 'grp_no': group_no, 'refresh': '1', 'data': data_payload }
        request_string = json.dumps(request_message)
        self.add_log(f"➡️ WS REG 요청 전송: {request_string}")
        await self.send_websocket_request_raw(request_string)

    async def unregister_realtime(self, tr_ids: List[str], tr_keys: List[str], group_no: str = "1"):
        """실시간 데이터 구독 해지 ('REMOVE') 메시지 구성 및 전송 (register_realtime과 동일한 로직 적용)"""
        self.add_log(f"➡️ 실시간 해지 요청 시도: ID(type)={tr_ids}, KEY(item)={tr_keys}")
        if len(tr_ids) != len(tr_keys):
            self.add_log("❌ 실시간 해지 실패: ID(type)와 KEY(item) 개수가 일치하지 않음"); return

        # register_realtime과 동일한 data payload 구성 로직 사용
        data_payload = []
        grouped_items = {}
        for tr_id, tr_key in zip(tr_ids, tr_keys):
            if tr_id in ['00', '04']: data_payload.append({"item": [""], "type": [tr_id]})
            else:
                if tr_id not in grouped_items: grouped_items[tr_id] = []
                if tr_key: grouped_items[tr_id].append(tr_key)
        for tr_id, items in grouped_items.items():
            if items: data_payload.append({"item": items, "type": [tr_id]})

        if not data_payload: self.add_log("⚠️ 실시간 해지 요청할 유효한 데이터 없음."); return

        request_message = { 'trnm': 'REMOVE', 'grp_no': group_no, 'data': data_payload }
        request_string = json.dumps(request_message)
        self.add_log(f"➡️ WS REMOVE 요청 전송: {request_string}")
        await self.send_websocket_request_raw(request_string)

    async def disconnect_websocket(self):
        if self.websocket and self.websocket.open:
            self.add_log("🔌 웹소켓 연결 종료 시도...")
            try: await self.websocket.close()
            except Exception as e: self.add_log(f"⚠️ 웹소켓 종료 중 오류: {e}")
            finally: self.websocket = None; self.add_log("🔌 웹소켓 연결 종료 완료.")

    async def close(self):
        await self.disconnect_websocket()
        if self.client and not self.client.is_closed:
            try: await self.client.aclose(); self.add_log("🔌 HTTP 클라이언트 세션 종료")
            except Exception as e: self.add_log(f"⚠️ HTTP 클라이언트 종료 중 오류: {e}")

    # --- REST API 메서드들 (이전 코드 유지) ---
    async def fetch_stock_info(self, stock_code: str) -> Optional[Dict]:
        url = "/api/dostk/stkinfo"; tr_id = "ka10001"
        headers = await self._get_headers(tr_id)
        if not headers: return None
        body = {"stk_cd": stock_code}
        try:
            res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
            res.raise_for_status(); data = res.json()
            if data and data.get('output') and data.get('rt_cd') == '0': return data['output']
            else: self.add_log(f"⚠️ [{stock_code}] 종목 정보 없음: {data.get('msg1', 'API 응답 없음')}"); return None
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_data = e.response.json(); error_msg = error_data.get('msg1', error_text)
            except: pass
            self.add_log(f"❌ [{stock_code}] 종목 정보 HTTP 오류 {e.response.status_code}: {error_msg}")
        except Exception as e: self.add_log(f"❌ [{stock_code}] 종목 정보 조회 오류: {e}")
        return None

    async def fetch_minute_chart(self, stock_code: str, timeframe: int = 1) -> Optional[Dict]:
        url = "/api/dostk/chart"; tr_id = "ka10080"
        headers = await self._get_headers(tr_id)
        if not headers: self.add_log(f"❌ [{stock_code}] 분봉 헤더 생성 실패."); return None
        body = {"stk_cd": stock_code, "tic_scope": str(timeframe), "upd_stkpc_tp": "0"}
        try:
            res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
            res.raise_for_status(); data = res.json()
            return_code = data.get('return_code')
            return_msg = data.get('return_msg', '')
            result_key = 'output2' if 'output2' in data else ('stk_min_pole_chart_qry' if 'stk_min_pole_chart_qry' in data else None)
            result_data = data.get(result_key) if result_key else None
            # self.add_log(f"  - [API_MIN_CHART] 응답 코드(return_code) ({stock_code}): {return_code}, 메시지: {return_msg}") # 로그 간소화
            if (return_code == 0 or return_code == '0') and isinstance(result_data, list):
                if result_data:
                     # self.add_log(f"  ✅ [API_MIN_CHART] ({stock_code}) {timeframe}분봉 데이터 {len(result_data)}건 정상 반환") # 로그 간소화
                     return {'stk_min_pole_chart_qry': result_data, 'return_code': 0} # 성공 시 return_code 포함
                else:
                    self.add_log(f"  ⚠️ [API_MIN_CHART] ({stock_code}) {timeframe}분봉 데이터 없음 (API 성공, 빈 리스트)."); return {'return_code': 0, 'stk_min_pole_chart_qry': []} # 성공이지만 데이터 없을 때
            else:
                error_msg = return_msg if return_msg else 'API 응답 데이터 없음 또는 형식 오류'
                self.add_log(f"  ❌ [API_MIN_CHART] ({stock_code}) 처리 실패: {error_msg} (return_code: {return_code})")
                self.add_log(f"  📄 [API_MIN_CHART] ({stock_code}) 실패 시 응답 일부: {str(data)[:200]}..."); return {'return_code': return_code, 'return_msg': error_msg} # 실패 시 return_code/msg 포함
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_json = e.response.json(); error_msg = error_json.get('return_msg', error_text)
            except: pass
            self.add_log(f"❌ [{stock_code}] 분봉 데이터 HTTP 오류 {e.response.status_code}: {error_msg}"); return {'return_code': e.response.status_code, 'return_msg': error_msg}
        except httpx.RequestError as e: self.add_log(f"❌ [{stock_code}] 분봉 데이터 네트워크 오류: {e}"); return {'return_code': -1, 'return_msg': str(e)}
        except Exception as e: self.add_log(f"❌ [{stock_code}] 분봉 데이터 조회 중 예상치 못한 오류: {e}"); self.add_log(traceback.format_exc()); return {'return_code': -99, 'return_msg': str(e)}

    async def fetch_volume_surge_rank(self, **kwargs) -> Optional[Dict]:
        """거래량 급증 종목 랭킹 조회 (API ID: ka10023). 인자는 kwargs로 받음."""
        url_path = "/api/dostk/rkinfo"; tr_id = "ka10023"
        full_url = f"{self.base_url}{url_path}"
        headers = await self._get_headers(tr_id)
        if not headers: return {'return_code': -1, 'return_msg': '헤더 생성 실패'}

        # 기본 파라미터 설정 및 kwargs로 오버라이드
        default_params = {
            'mrkt_tp': '000', 'sort_tp': '2', 'tm_tp': '1', 'tm': '5',
            'trde_qty_tp': '10', 'stk_cnd': '14', 'pric_tp': '8', 'stex_tp': '3'
        }
        raw_body = {**default_params, **kwargs} # kwargs로 기본값 덮어쓰기

        # --- 👇 타입 변환 로직 추가 ---
        body = {}
        for key, value in raw_body.items():
            if key in ['tm', 'trde_qty_tp'] and isinstance(value, (int, float)):
                # tm과 trde_qty_tp 파라미터를 문자열로 변환
                body[key] = str(value)
            elif key == 'trde_qty_tp' and isinstance(value, str):
                # trde_qty_tp가 문자열일 경우, 앞에 0을 붙여 4자리로 만드는 로직 (API 문서 길이 오류 가능성 대비)
                # 예: '10' -> '0010', '100' -> '0100' (필요 없을 경우 이 elif 블록 제거)
                # body[key] = value.zfill(4)
                body[key] = value # 우선 원본 문자열 사용
            else:
                body[key] = value
        # --- 👆 타입 변환 로직 추가 끝 ---

        try:
            # 수정된 body 사용
            self.add_log(f"🔍 [API {tr_id}] 거래량 급증 요청 Body (수정됨): {body}")
            res = await self.client.post(full_url, headers=headers, json=body)
            res.raise_for_status(); data = res.json()

            # ... (이하 try 구문 동일) ...

            return_code = data.get('return_code')
            return_msg = data.get('return_msg', '')

            if return_code == 0 or return_code == '0':
                return data
            else:
                self.add_log(f"⚠️ [API {tr_id}] 거래량 급증 데이터 없음: {return_msg} (return_code: {return_code})")
                self.add_log(f"📄 API Raw Response: {data}")
                return {'return_code': return_code, 'return_msg': return_msg}
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_json = e.response.json(); error_msg = error_json.get('return_msg', error_text)
            except: pass
            self.add_log(f"❌ [API {tr_id}] 거래량 급증 오류 (HTTP {e.response.status_code}): {error_msg}")
            return {'return_code': e.response.status_code, 'return_msg': error_msg}
        except httpx.RequestError as e:
            self.add_log(f"❌ [API {tr_id}] 거래량 급증 네트워크 오류: {e}")
            return {'return_code': -1, 'return_msg': str(e)}
        except Exception as e:
            self.add_log(f"❌ [API {tr_id}] 예상치 못한 오류 (fetch_volume_surge_rank): {e}")
            self.add_log(traceback.format_exc())
            return {'return_code': -99, 'return_msg': str(e)}

    async def create_buy_order(self, stock_code: str, quantity: int, price: Optional[int] = None) -> Optional[Dict]:
        url_path = "/api/dostk/ordr"; tr_id = "kt10000"
        full_url = f"{self.base_url}{url_path}"
        self.add_log(f"  -> [CREATE_BUY_{tr_id}] 시작: 종목({stock_code}), 수량({quantity}), 가격({price})")
        headers = await self._get_headers(tr_id, is_order=True)
        if not headers: self.add_log(f"❌ [CREATE_BUY_{tr_id}] 실패: 헤더 생성 실패 ({stock_code})."); return None
        order_price_str = str(price) if price is not None and price > 0 else ""
        trade_type = "0" if price is not None and price > 0 else "3"
        body = { "dmst_stex_tp": "KRX", "stk_cd": stock_code, "ord_qty": str(quantity), "ord_uv": order_price_str, "trde_tp": trade_type, "cond_uv": "" }
        try:
            self.add_log(f"  -> [CREATE_BUY_{tr_id}] API 요청 시도 ({stock_code})... Body: {body}")
            res = await self.client.post(full_url, headers=headers, json=body)
            self.add_log(f"  <- [CREATE_BUY_{tr_id}] API 응답 수신 ({stock_code}). Status: {res.status_code}")
            res.raise_for_status(); data = res.json()
            self.add_log(f"  <- [CREATE_BUY_{tr_id}] API 응답 JSON 파싱 완료 ({stock_code}).")
            return_code = data.get('return_code')
            return_msg = data.get('return_msg', '')
            order_no = data.get('ord_no')
            self.add_log(f"  - [CREATE_BUY_{tr_id}] 응답 처리 시작 ({stock_code}): code={return_code}, msg={return_msg}, ord_no={order_no}")
            if (return_code == 0 or return_code == '0') and order_no:
                self.add_log(f"  ✅ [CREATE_BUY_{tr_id}] 성공 ({stock_code}): 주문번호={order_no}"); return data # 성공 시 전체 응답 반환
            else:
                error_msg = return_msg if return_msg else 'API 응답 없음 또는 형식 오류'
                self.add_log(f"  ❌ [CREATE_BUY_{tr_id}] 실패 ({stock_code}): {error_msg} (return_code: {return_code})")
                self.add_log(f"📄 API Raw Response: {data}"); return data # 실패 시에도 전체 응답 반환 (오류 코드 포함)
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_json = e.response.json(); error_msg = error_json.get('return_msg', error_text)
            except: pass
            self.add_log(f"  ❌ [CREATE_BUY_{tr_id}] HTTP 오류 ({stock_code}, Status:{e.response.status_code}): {error_msg}"); print(traceback.format_exc()); return {'return_code': e.response.status_code, 'return_msg': error_msg}
        except httpx.RequestError as e: self.add_log(f"  ❌ [CREATE_BUY_{tr_id}] 네트워크 오류 ({stock_code}): {e}"); print(traceback.format_exc()); return {'return_code': -1, 'return_msg': str(e)}
        except Exception as e: self.add_log(f"  ❌ [CREATE_BUY_{tr_id}] 예상치 못한 오류 ({stock_code}): {e}"); print(traceback.format_exc()); return {'return_code': -99, 'return_msg': str(e)}
        # self.add_log(f"  -> [CREATE_BUY_{tr_id}] 함수 종료 (None 반환 예정) ({stock_code})."); return None # 로깅 변경

    async def create_sell_order(self, stock_code: str, quantity: int, price: Optional[int] = None) -> Optional[Dict]:
        url_path = "/api/dostk/ordr"; tr_id = "kt10001" # 매도 API ID 확인 (kt10001 사용)
        full_url = f"{self.base_url}{url_path}"
        self.add_log(f"  -> [CREATE_SELL_{tr_id}] 시작: 종목({stock_code}), 수량({quantity}), 가격({price})")
        headers = await self._get_headers(tr_id, is_order=True)
        if not headers: self.add_log(f"❌ [CREATE_SELL_{tr_id}] 실패: 헤더 생성 실패 ({stock_code})."); return None
        order_price_str = str(price) if price is not None and price > 0 else ""
        trade_type = "0" if price is not None and price > 0 else "3"
        body = { "dmst_stex_tp": "KRX", "stk_cd": stock_code, "ord_qty": str(quantity), "ord_uv": order_price_str, "trde_tp": trade_type, "cond_uv": "" }
        try:
            self.add_log(f"  -> [CREATE_SELL_{tr_id}] API 요청 시도 ({stock_code})... Body: {body}")
            res = await self.client.post(full_url, headers=headers, json=body)
            self.add_log(f"  <- [CREATE_SELL_{tr_id}] API 응답 수신 ({stock_code}). Status: {res.status_code}")
            res.raise_for_status(); data = res.json()
            self.add_log(f"  <- [CREATE_SELL_{tr_id}] API 응답 JSON 파싱 완료 ({stock_code}).")
            return_code = data.get('return_code')
            return_msg = data.get('return_msg', '')
            order_no = data.get('ord_no')
            self.add_log(f"  - [CREATE_SELL_{tr_id}] 응답 처리 시작 ({stock_code}): code={return_code}, msg={return_msg}, ord_no={order_no}")
            if (return_code == 0 or return_code == '0') and order_no:
                self.add_log(f"  ✅ [CREATE_SELL_{tr_id}] 성공 ({stock_code}): 주문번호={order_no}"); return data
            else:
                error_msg = return_msg if return_msg else 'API 응답 없음 또는 형식 오류'
                self.add_log(f"  ❌ [CREATE_SELL_{tr_id}] 실패 ({stock_code}): {error_msg} (return_code: {return_code})")
                self.add_log(f"📄 API Raw Response: {data}"); return data
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_json = e.response.json(); error_msg = error_json.get('return_msg', error_text)
            except: pass
            self.add_log(f"  ❌ [CREATE_SELL_{tr_id}] HTTP 오류 ({stock_code}, Status:{e.response.status_code}): {error_msg}"); print(traceback.format_exc()); return {'return_code': e.response.status_code, 'return_msg': error_msg}
        except httpx.RequestError as e: self.add_log(f"  ❌ [CREATE_SELL_{tr_id}] 네트워크 오류 ({stock_code}): {e}"); print(traceback.format_exc()); return {'return_code': -1, 'return_msg': str(e)}
        except Exception as e: self.add_log(f"  ❌ [CREATE_SELL_{tr_id}] 예상치 못한 오류 ({stock_code}): {e}"); print(traceback.format_exc()); return {'return_code': -99, 'return_msg': str(e)}
        # self.add_log(f"  -> [CREATE_SELL_{tr_id}] 함수 종료 (None 반환 예정) ({stock_code})."); return None

    async def cancel_order(self, order_no: str, stock_code: str, quantity: int = 0) -> Optional[Dict]:
        url_path = "/api/dostk/ordr"; tr_id = "kt10003" # 취소 API ID 확인 (kt10003 사용)
        full_url = f"{self.base_url}{url_path}"
        self.add_log(f"  -> [CANCEL_ORDER_{tr_id}] 시작: 원주문({order_no}), 종목({stock_code}), 수량({quantity})")
        headers = await self._get_headers(tr_id, is_order=True)
        if not headers: self.add_log(f"❌ [CANCEL_ORDER_{tr_id}] 실패: 헤더 생성 실패 ({stock_code})."); return None
        cancel_qty_str = "0" if quantity == 0 else str(quantity)
        body = { "dmst_stex_tp": "KRX", "orig_ord_no": order_no, "stk_cd": stock_code, "cncl_qty": cancel_qty_str }
        try:
            self.add_log(f"  -> [CANCEL_ORDER_{tr_id}] API 요청 시도 ({stock_code})... Body: {body}")
            res = await self.client.post(full_url, headers=headers, json=body)
            self.add_log(f"  <- [CANCEL_ORDER_{tr_id}] API 응답 수신 ({stock_code}). Status: {res.status_code}")
            res.raise_for_status(); data = res.json()
            self.add_log(f"  <- [CANCEL_ORDER_{tr_id}] API 응답 JSON 파싱 완료 ({stock_code}).")
            return_code = data.get('return_code')
            return_msg = data.get('return_msg', '')
            new_ord_no = data.get('ord_no')
            self.add_log(f"  - [CANCEL_ORDER_{tr_id}] 응답 처리 시작 ({stock_code}): code={return_code}, msg={return_msg}, new_ord_no={new_ord_no}")
            if (return_code == 0 or return_code == '0') and new_ord_no:
                self.add_log(f"  ✅ [CANCEL_ORDER_{tr_id}] 성공 ({stock_code}): 원주문={order_no}, 취소주문={new_ord_no}"); return data
            else:
                error_msg = return_msg if return_msg else 'API 응답 없음 또는 형식 오류'
                self.add_log(f"  ❌ [CANCEL_ORDER_{tr_id}] 실패 ({stock_code}): {error_msg} (return_code: {return_code})")
                self.add_log(f"📄 API Raw Response: {data}"); return data
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_json = e.response.json(); error_msg = error_json.get('return_msg', error_text)
            except: pass
            self.add_log(f"  ❌ [CANCEL_ORDER_{tr_id}] HTTP 오류 ({stock_code}, Status:{e.response.status_code}): {error_msg}"); print(traceback.format_exc()); return {'return_code': e.response.status_code, 'return_msg': error_msg}
        except httpx.RequestError as e: self.add_log(f"  ❌ [CANCEL_ORDER_{tr_id}] 네트워크 오류 ({stock_code}): {e}"); print(traceback.format_exc()); return {'return_code': -1, 'return_msg': str(e)}
        except Exception as e: self.add_log(f"  ❌ [CANCEL_ORDER_{tr_id}] 예상치 못한 오류 ({stock_code}): {e}"); print(traceback.format_exc()); return {'return_code': -99, 'return_msg': str(e)}
        # self.add_log(f"  -> [CANCEL_ORDER_{tr_id}] 함수 종료 (None 반환 예정) ({stock_code})."); return None

    async def fetch_account_balance(self) -> Optional[Dict]:
        url_path = "/api/dostk/acnt"; tr_id = "kt00001"
        full_url = f"{self.base_url}{url_path}"
        headers = await self._get_headers(tr_id, is_order=True) # is_order=True 추가
        if not headers: self.add_log("❌ 예수금 조회 실패: 헤더 생성 실패."); return None
        body = {"qry_tp": "2"}
        try:
            res = await self.client.post(full_url, headers=headers, json=body)
            res.raise_for_status(); data = res.json()
            return_code = data.get('return_code')
            return_msg = data.get('return_msg', '')
            balance_info = data
            if (return_code == 0 or return_code == '0'):
                if 'ord_alow_amt' in balance_info:
                    return balance_info
                else:
                    self.add_log(f"⚠️ [API {tr_id}] 예수금 조회 성공했으나 'ord_alow_amt' 필드 없음."); self.add_log(f"📄 API Raw Response: {data}"); return {'return_code': 0, 'return_msg': "'ord_alow_amt' missing"}
            else:
                error_msg = return_msg if return_msg else 'API 응답 없음 또는 형식 오류'
                self.add_log(f"⚠️ [API {tr_id}] 예수금 데이터 없음: {error_msg} (return_code: {return_code})"); self.add_log(f"📄 API Raw Response: {data}"); return {'return_code': return_code, 'return_msg': error_msg}
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_json = e.response.json(); error_msg = error_json.get('return_msg', error_text)
            except: pass
            self.add_log(f"❌ [API {tr_id}] 예수금 조회 오류 (HTTP {e.response.status_code}): {error_msg}"); return {'return_code': e.response.status_code, 'return_msg': error_msg}
        except httpx.RequestError as e: self.add_log(f"❌ [API {tr_id}] 예수금 조회 네트워크 오류: {e}"); return {'return_code': -1, 'return_msg': str(e)}
        except Exception as e: self.add_log(f"❌ [API {tr_id}] 예수금 조회 중 예상치 못한 오류: {e}"); print(traceback.format_exc()); return {'return_code': -99, 'return_msg': str(e)}

    # --- 👇 주식 호가 데이터 요청 함수 추가 ---
    async def fetch_orderbook(self, stock_code: str) -> Optional[Dict]:
      """주식 호가 잔량 데이터를 요청합니다. (API ID: ka10004)"""
      url = "/api/dostk/mrkcond" # API 문서상 URL 확인 필요 (ka10004)
      tr_id = "ka10004" # API ID

      try:
        headers = await self._get_headers(tr_id)
        body = {"stk_cd": stock_code}

        res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
        res.raise_for_status() # HTTP 오류 발생 시 예외 발생

        data = res.json()
        # API 응답 구조 확인 (예: return_code가 있는지)
        if data.get('return_code') == 0:
            self.add_log(f"✅ [{stock_code}] 호가 데이터 조회 성공") # add_log 대신 print 사용
            # print(f"✅ [{stock_code}] 호가 데이터 조회 성공")
            return data # 성공 시 전체 응답 데이터 반환
        else:
            error_msg = data.get('return_msg', '알 수 없는 오류')
            # print(f"❌ [{stock_code}] 호가 데이터 조회 실패: {error_msg}")
            self.add_log(f"❌ [{stock_code}] 호가 데이터 조회 실패: {error_msg}") # add_log 대신 print 사용
            return None

      except httpx.HTTPStatusError as e:
        try:
            error_data = e.response.json()
            error_msg = error_data.get('return_msg', e.response.text)
        except json.JSONDecodeError:
            error_msg = e.response.text
        # print(f"❌ [{stock_code}] 호가 데이터 조회 실패 (HTTP 오류): {e.response.status_code} - {error_msg}")
        self.add_log(f"❌ [{stock_code}] 호가 데이터 조회 실패 (HTTP 오류): {e.response.status_code} - {error_msg}") # add_log 대신 print 사용
      except Exception as e:
        # print(f"❌ [{stock_code}] 예상치 못한 오류 발생 (fetch_orderbook): {e}")
        self.add_log(f"❌ [{stock_code}] 예상치 못한 오류 발생 (fetch_orderbook): {e}") # add_log 대신 print 사용
      return None

    def _split_account_no(self) -> Optional[tuple[str, str]]:
        clean_account_no = self.account_no.replace('-', '') if self.account_no else ""
        account_prefix = ""; account_suffix = ""
        if len(clean_account_no) == 8:
            account_prefix = clean_account_no; account_suffix = "01"
            return account_prefix, account_suffix
        elif len(clean_account_no) >= 10:
            account_prefix = clean_account_no[:8]; account_suffix = clean_account_no[8:10]
            return account_prefix, account_suffix
        else:
            self.add_log(f"❌ [계좌 분리] 실패: 지원하지 않는 계좌번호 길이({len(clean_account_no)})")
            return None