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
# from data.manager import preprocess_chart_data # 주석 처리

class KiwoomAPI:
    """키움증권 REST API 및 WebSocket API와의 비동기 통신을 담당합니다."""

    TOKEN_FILE = ".token"
    BASE_URL_PROD = "https://api.kiwoom.com"
    REALTIME_URI_PROD = "wss://api.kiwoom.com:10000/api/dostk/websocket"

    def __init__(self):
        self.base_url = self.BASE_URL_PROD
        self.realtime_uri = self.REALTIME_URI_PROD

        self.app_key = config.kiwoom.app_key
        self.app_secret = config.kiwoom.app_secret
        self.account_no = config.kiwoom.account_no
        self._access_token: Optional[str] = None # Bearer 제외 순수 토큰 저장
        self._token_expires_at: Optional[datetime] = None
        self.client = httpx.AsyncClient(timeout=None)

        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.message_handler: Optional[Callable[[Dict], None]] = None

        self._load_token_from_file()

    # --- 토큰 관리 ---
    def _load_token_from_file(self):
        if os.path.exists(self.TOKEN_FILE):
            try:
                with open(self.TOKEN_FILE, 'r') as f:
                    token_data = json.load(f)
                self._access_token = token_data.get('access_token') # 순수 토큰 로드
                expires_str = token_data.get('expires_at')
                if expires_str:
                    self._token_expires_at = datetime.fromisoformat(expires_str)
                    if self.is_token_valid():
                        print(f"ℹ️ 저장된 토큰을 불러왔습니다. (만료: {self._token_expires_at})")
                    else:
                        print(f"⚠️ 저장된 토큰 만료됨 (만료: {self._token_expires_at}).")
                        self._access_token = None; self._token_expires_at = None
                else:
                    print("⚠️ 토큰 파일에 만료 정보 없음."); self._access_token = None; self._token_expires_at = None
            except (json.JSONDecodeError, KeyError, ValueError, OSError) as e:
                print(f"⚠️ 토큰 파일 로드 오류: {e}."); self._access_token = None; self._token_expires_at = None
        else:
            print(f"ℹ️ 토큰 파일({self.TOKEN_FILE}) 없음."); self._access_token = None; self._token_expires_at = None

    def _save_token_to_file(self):
        if self._access_token and self._token_expires_at:
            token_data = {'access_token': self._access_token, 'expires_at': self._token_expires_at.isoformat()}
            try:
                with open(self.TOKEN_FILE, 'w') as f: json.dump(token_data, f)
                print(f"💾 새 토큰 저장 완료 (만료: {self._token_expires_at})")
            except IOError as e: print(f"❌ 토큰 파일 저장 실패: {e}")

    def is_token_valid(self) -> bool:
        if not self._access_token or not self._token_expires_at: return False
        return datetime.now() + timedelta(minutes=1) < self._token_expires_at

    async def get_access_token(self) -> Optional[str]:
        """httpx(비동기)로 접근 토큰 발급/갱신 (Bearer 제외 순수 토큰 반환)"""
        if self.is_token_valid(): return self._access_token
        print("ℹ️ 접근 토큰 신규 발급/갱신 시도...")
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
                self._access_token = access_token # 순수 토큰 저장
                try: self._token_expires_at = datetime.strptime(expires_dt_str, "%Y%m%d%H%M%S")
                except ValueError: print(f"❌ 만료 시간 형식 오류: {expires_dt_str}"); return None
                print(f"✅ 접근 토큰 발급/갱신 성공 (만료: {self._token_expires_at})")
                self._save_token_to_file(); return self._access_token # 순수 토큰 반환
            else:
                error_msg = data.get('error_description') or data.get('return_msg') or data.get('msg1', '알 수 없는 오류')
                print(f"❌ 토큰 발급 응답 오류: {error_msg} | 응답: {data}")
                self._access_token = None; self._token_expires_at = None; return None
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_data = e.response.json(); error_msg = error_data.get('error_description') or error_data.get('return_msg') or error_data.get('msg1', error_text)
            except: pass
            print(f"❌ 접근 토큰 발급 실패 (HTTP {e.response.status_code}): {error_msg}")
            self._access_token = None; self._token_expires_at = None; return None
        except Exception as e:
            print(f"❌ 예상치 못한 오류 (get_access_token): {e}")
            self._access_token = None; self._token_expires_at = None; return None

    async def _get_headers(self, tr_id: str, is_order: bool = False) -> Optional[Dict]:
        """REST API 요청에 필요한 헤더 생성 (Bearer 포함)"""
        pure_token = await self.get_access_token() # 순수 토큰 받기
        if not pure_token: print(f"❌ 헤더 생성 실패: 유효 토큰 없음 (tr_id: {tr_id})"); return None
        access_token_with_bearer = f"Bearer {pure_token}" # Bearer 추가

        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": access_token_with_bearer, # Bearer 포함된 토큰 사용
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }
        if is_order:
            if not self.account_no: print("❌ 주문 헤더 생성 실패: 계좌번호 없음."); return None
            headers["custtype"] = "P"
            headers["tr_cont"] = "N"
        return headers

    # --- WebSocket 연결 및 관리 ---
    async def connect_websocket(self, handler: Callable[[Dict], None]) -> bool:
        """웹소켓 연결, LOGIN 인증, 실시간 데이터 수신 시작"""
        if self.websocket and self.websocket.open:
            print("ℹ️ 이미 웹소켓에 연결됨."); return True

        pure_token = await self.get_access_token() # Bearer 제외 순수 토큰
        if not pure_token:
            print("❌ 웹소켓 연결 불가: 유효 토큰 없음."); return False

        self.message_handler = handler
        print(f"🛰️ 웹소켓 연결 시도: {self.realtime_uri}")

        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        print("⚠️ SSL 인증서 검증을 비활성화합니다. (테스트 목적, 보안 주의!)")

        try:
            # 1. 웹소켓 연결 (헤더 없이)
            self.websocket = await websockets.connect(
                self.realtime_uri,
                ping_interval=None,  # 자동 PING 비활성화
                ping_timeout=None,   # PING 타임아웃 비활성화
                open_timeout=30,
                ssl=ssl_context
            )
            print("✅ 웹소켓 연결 성공! (SSL 검증 비활성화, 자동 PING 비활성화)")

            # 2. LOGIN 메시지 전송
            try:
                login_packet = {'trnm': 'LOGIN', 'token': pure_token}
                login_request_string = json.dumps(login_packet)
                print(f"➡️ WS LOGIN 요청 전송: {json.dumps({'trnm': 'LOGIN', 'token': '...' + pure_token[-10:]})}")
                await self.websocket.send(login_request_string)
                print("✅ WS LOGIN 요청 전송 완료")

                # 3. LOGIN 응답 대기 및 확인
                print("⏳ WS LOGIN 응답 대기 중...")
                login_response_str = await asyncio.wait_for(self.websocket.recv(), timeout=10)
                print(f"📬 WS LOGIN 응답 수신: {login_response_str}")
                login_response = json.loads(login_response_str)

                if login_response.get('trnm') == 'LOGIN' and login_response.get('return_code') == 0:
                    print("✅ 웹소켓 LOGIN 성공")
                else:
                    print(f"❌ 웹소켓 LOGIN 실패: {login_response}")
                    await self.disconnect_websocket(); return False

            except asyncio.TimeoutError:
                print("❌ WS LOGIN 응답 시간 초과 (10초)")
                await self.disconnect_websocket(); return False
            except json.JSONDecodeError:
                print(f"❌ WS LOGIN 응답 파싱 실패: {login_response_str}")
                await self.disconnect_websocket(); return False
            except Exception as login_e:
                print(f"❌ WS LOGIN 처리 중 오류: {login_e}")
                print(f" traceback: {traceback.format_exc()}")
                await self.disconnect_websocket(); return False

            # 4. 메시지 수신 루프 시작
            asyncio.create_task(self._receive_messages())

            # 5. TR 등록 전 잠시 대기
            await asyncio.sleep(1)

            # 6. 실시간 TR 등록은 임시로 비활성화 (메인 기능 테스트 우선)
            # if self.account_no:
            #     print("ℹ️ 계좌 관련 실시간 데이터 등록을 시도합니다...")
            #     # 키움 API의 실제 실시간 코드 사용 (주문체결: '00', 잔고: '04')
            #     await self.register_realtime(['00', '04'], [self.account_no, self.account_no])
            # else:
            #     print("ℹ️ 계좌번호가 없어 실시간 등록을 건너뜁니다.")
            print("ℹ️ 실시간 등록은 임시로 비활성화되었습니다. (메인 기능 테스트 우선)")
            
            return True

        # --- 연결 실패 처리 ---
        except websockets.exceptions.InvalidStatusCode as e:
            print(f"❌ 웹소켓 연결 실패 (상태 코드 {e.status_code}): {e.headers}. 주소/서버 상태 확인.")
        except asyncio.TimeoutError:
            print(f'❌ 웹소켓 연결 시간 초과 (30초)')
        except OSError as e:
             print(f"❌ 웹소켓 연결 OS 오류: {e}")
        except Exception as e:
            print(f"❌ 웹소켓 연결 중 예상치 못한 오류: {e}")
            print(f" traceback: {traceback.format_exc()}")
        # --- 연결 실패 시 정리 ---
        self.websocket = None
        return False

    async def _receive_messages(self):
        """웹소켓 메시지 수신 및 처리 루프 (PING 응답 없이 처리)"""
        if not self.websocket or not self.websocket.open:
            print("⚠️ 메시지 수신 불가: 웹소켓 연결 안됨."); return
        print("👂 실시간 메시지 수신 대기 중...")
        try:
            async for message in self.websocket:
                print(f"📬 WS 수신: {message[:200]}{'...' if len(str(message)) > 200 else ''}")

                if isinstance(message, bytes): print("ℹ️ Bytes 메시지 수신 (무시)"); continue
                if not isinstance(message, str) or not message.strip(): print("ℹ️ 비어있는 문자열 메시지 수신 (무시)"); continue

                try:
                    data = json.loads(message)
                    trnm = data.get("trnm")

                    # 시스템 메시지
                    if trnm == "SYSTEM":
                        code = data.get("code"); msg = data.get("message")
                        print(f"ℹ️ WS 시스템 메시지: [{code}] {msg}")
                        continue

                    # PING 처리 - 응답하지 않고 무시
                    if trnm == 'PING':
                        print(">>> PING 수신. 응답하지 않고 무시합니다.")
                        continue # PING에 대해 응답하지 않음

                    # LOGIN 응답 (이미 connect_websocket에서 처리됨)
                    if trnm == 'LOGIN': continue

                    # REG/REMOVE 응답
                    if trnm in ['REG', 'REMOVE']:
                        rt_cd = data.get('return_code')
                        msg = data.get('return_msg', '메시지 없음')
                        if rt_cd == 0: print(f"✅ WS 응답 ({trnm}): {msg}")
                        else: print(f"❌ WS 오류 응답 ({trnm}): [{rt_cd}] {msg}")
                        continue

                    # 실시간 데이터 (header/body 구조)
                    header = data.get('header')
                    body_str = data.get('body')
                    if header and body_str:
                        tr_id = header.get('tr_id')
                        tr_type = header.get('tr_type') # 실시간은 '3'
                        if tr_type == '3' and tr_id in ['H0STASP0', 'H0STCNI0']: # 주문체결, 잔고 
                            try:
                                body_data = json.loads(body_str)
                                if self.message_handler:
                                    self.message_handler({"header": header, "body": body_data})
                            except json.JSONDecodeError: print(f"⚠️ 실시간 body 파싱 실패: {body_str}")
                        else:
                            print(f"ℹ️ 처리되지 않은 실시간 데이터: H:{header} / B:{body_str}")
                    else:
                        # PONG 관련 메시지는 무시
                        if trnm == 'PONG':
                            print(f"ℹ️ PONG 응답 수신 (무시): {data}")
                        else:
                            print(f"ℹ️ 알 수 없는 형식의 WS 메시지: {data}")

                except json.JSONDecodeError: print(f"⚠️ WS JSON 파싱 실패: {message[:100]}...")
                except Exception as e:
                    print(f"❌ WS 메시지 처리 중 오류: {e} | Msg: {message[:100]}...")
                    print(f" traceback: {traceback.format_exc()}")

        except websockets.exceptions.ConnectionClosedOK: print("ℹ️ 웹소켓 정상 종료.")
        except websockets.exceptions.ConnectionClosedError as e: print(f"❌ 웹소켓 비정상 종료: {e.code} {e.reason}")
        except asyncio.CancelledError: print("ℹ️ 메시지 수신 태스크 취소됨.")
        except Exception as e: print(f"❌ WS 수신 루프 오류: {e}")
        finally: print("🛑 메시지 수신 루프 종료."); self.websocket = None

    async def send_websocket_request_raw(self, message: str):
        """JSON 문자열을 웹소켓으로 직접 전송 (LOGIN, REG, REMOVE 용)"""
        if self.websocket and self.websocket.open:
            try:
                await self.websocket.send(message)
                # print(f"➡️ WS RAW 전송: {message}")
            except Exception as e:
                print(f"❌ 웹소켓 RAW 메시지 전송 실패: {e}")
        else:
            print("⚠️ 웹소켓 미연결, RAW 전송 불가.")

    async def register_realtime_account(self, tr_ids: list[str], tr_keys: list[str], group_no: str = "1"):
        """계좌 관련 실시간 데이터 구독 ('REG') - 키움 API 형식에 맞게 수정"""
        print(f"➡️ 계좌 실시간 등록 요청 시도: ID={tr_ids}, KEY={tr_keys}")
        if len(tr_ids) != len(tr_keys):
            print("❌ 실시간 등록 실패: tr_id와 tr_key 개수가 일치하지 않음"); return

        # 키움 API 실시간 등록 형식에 맞게 수정
        data_list = []
        for tr_id, tr_key in zip(tr_ids, tr_keys):
            data_list.append({
                "tr_id": tr_id,   # 실시간 코드
                "tr_key": tr_key  # 계좌번호
            })

        request_message = {
            'trnm': 'REG',
            'grp_no': group_no,
            'refresh': '1',
            'data': data_list
        }
        request_string = json.dumps(request_message)
        print(f"➡️ WS REG 요청 전송: {request_string}")
        await self.send_websocket_request_raw(request_string)

    async def register_realtime(self, tr_ids: list[str], tr_keys: list[str], group_no: str = "1"):
        """일반적인 실시간 데이터 구독 ('REG') 메시지 구성 및 전송"""
        print(f"➡️ 실시간 등록 요청 시도: ID(type)={tr_ids}, KEY(item)={tr_keys}")
        if len(tr_ids) != len(tr_keys):
            print("❌ 실시간 등록 실패: tr_id(type)와 tr_key(item) 개수가 일치하지 않음"); return

        # 일반적인 실시간 등록 포맷
        data_list = []
        for tr_id, tr_key in zip(tr_ids, tr_keys):
            data_list.append({
                "item": tr_key,  # 종목코드 등
                "type": tr_id    # 실시간 타입
            })

        request_message = {
            'trnm': 'REG',
            'grp_no': group_no,
            'refresh': '1', 
            'data': data_list
        }
        request_string = json.dumps(request_message)
        print(f"➡️ WS REG 요청 전송: {request_string}")
        await self.send_websocket_request_raw(request_string)

    async def unregister_realtime(self, tr_ids: List[str], tr_keys: List[str], group_no: str = "1"):
        """실시간 데이터 구독 해지 ('REMOVE') 메시지 구성 및 전송"""
        print(f"➡️ 실시간 해지 요청 시도: ID(type)={tr_ids}, KEY(item)={tr_keys}")
        if len(tr_ids) != len(tr_keys):
            print("❌ 실시간 해지 실패: tr_id(type)와 tr_key(item) 개수가 일치하지 않음"); return

        data_list_formatted = [{'item': key, 'type': tid} for tid, key in zip(tr_ids, tr_keys)]

        request_message = { 'trnm': 'REMOVE', 'grp_no': group_no, 'data': data_list_formatted }
        request_string = json.dumps(request_message)
        print(f"➡️ WS REMOVE 요청 전송: {request_string}")
        await self.send_websocket_request_raw(request_string)

    async def disconnect_websocket(self):
        if self.websocket and self.websocket.open:
            print("🔌 웹소켓 연결 종료 시도...")
            try:
                await self.websocket.close()
            except Exception as e: print(f"⚠️ 웹소켓 종료 중 오류: {e}")
            finally: self.websocket = None; print("🔌 웹소켓 연결 종료 완료.")

    async def close(self):
        await self.disconnect_websocket()
        if self.client and not self.client.is_closed:
            try: await self.client.aclose(); print("🔌 HTTP 클라이언트 세션 종료")
            except Exception as e: print(f"⚠️ HTTP 클라이언트 종료 중 오류: {e}")

    # --- REST API 메서드 ---
    async def fetch_stock_info(self, stock_code: str) -> Optional[Dict]:
        url = "/api/dostk/stkinfo"; tr_id = "ka10001"
        headers = await self._get_headers(tr_id)
        if not headers: return None
        body = {"stk_cd": stock_code}
        try:
            res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
            res.raise_for_status(); data = res.json()
            if data and data.get('output') and data.get('rt_cd') == '0': return data['output']
            else: print(f"⚠️ [{stock_code}] 종목 정보 없음: {data.get('msg1', 'API 응답 없음')}"); return None
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_data = e.response.json(); error_msg = error_data.get('msg1', error_text)
            except: pass
            print(f"❌ [{stock_code}] 종목 정보 HTTP 오류 {e.response.status_code}: {error_msg}")
        except Exception as e: print(f"❌ [{stock_code}] 종목 정보 조회 오류: {e}")
        return None

    async def fetch_minute_chart(self, stock_code: str, timeframe: int = 1) -> Optional[Dict]:
        url = "/api/dostk/chart"; tr_id = "ka10080"
        headers = await self._get_headers(tr_id)
        if not headers: return None
        body = {"stk_cd": stock_code, "tic_scope": str(timeframe), "upd_stkpc_tp": "0"}
        try:
            res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
            res.raise_for_status(); data = res.json()
            result_key = 'output2' if 'output2' in data else ('stk_min_pole_chart_qry' if 'stk_min_pole_chart_qry' in data else None)
            if data and result_key and data.get(result_key) and data.get('rt_cd') == '0':
                return {'stk_min_pole_chart_qry': data[result_key]}
            else: print(f"⚠️ [{stock_code}] {timeframe}분봉 데이터 없음: {data.get('msg1', 'API 응답 없음')}"); return None
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_data = e.response.json(); error_msg = error_data.get('msg1', error_text)
            except: pass
            print(f"❌ [{stock_code}] 분봉 데이터 HTTP 오류 {e.response.status_code}: {error_msg}")
        except Exception as e: print(f"❌ [{stock_code}] 분봉 데이터 조회 오류: {e}")
        return None

    async def fetch_volume_surge_stocks(self, market_type: str = "000") -> List[Dict]:
        """거래량 급증 종목 조회 - 헤더 및 파라미터 개선"""
        tr_id = "ka10023"  # 명시적으로 tr_id 설정
        url_path = "/api/dostk/rkinfo"
        full_url = f"{self.base_url}{url_path}"
        
        # 헤더 생성 전에 토큰 확인
        if not await self.get_access_token():
            print("❌ 거래량 급증 조회 실패: 유효한 토큰을 얻을 수 없음")
            return []
            
        headers = await self._get_headers(tr_id)
        if not headers: 
            print("❌ 거래량 급증 조회 실패: 헤더 생성 불가")
            return []
        
        # 헤더 내용 확인 로그 (디버깅용)
        print(f"🔍 요청 헤더 확인: tr_id={headers.get('tr_id')}, appkey={headers.get('appkey')[:10] if headers.get('appkey') else 'None'}...")
        
        # 키움 API 스펙에 맞는 파라미터 설정
        body = { 
            "mrkt_tp": market_type,  # 시장 구분
            "sort_tp": "1",          # 정렬 방식 (1: 급증률)
            "tm_tp": "0",            # 시간 구분 (0: 당일)
            "tm": "",                # 시간 (당일일 경우 빈 값)
            "trde_qty_tp": "01",     # 거래량 구분
            "stk_cnd": "",           # 종목 조건 (빈 값)
            "pric_tp": "",           # 가격 조건 (빈 값)
            "stex_tp": ""            # 거래소 구분 (빈 값)
        }
        
        try:
            print(f"🔍 거래량 급증({market_type}) 요청 URL: {full_url}")
            print(f"🔍 거래량 급증({market_type}) 요청 Body: {body}")
            
            res = await self.client.post(full_url, headers=headers, json=body)
            print(f"📡 HTTP 응답 상태: {res.status_code}")
            
            res.raise_for_status()
            data = res.json()
            
            print(f"📊 API 응답 상태: rt_cd={data.get('rt_cd')}, return_code={data.get('return_code')}")
            print(f"📊 API 응답 메시지: msg1={data.get('msg1')}, return_msg={data.get('return_msg')}")
            
            # 성공 조건 확인 (rt_cd 또는 return_code)
            is_success = (data.get('rt_cd') == '0') or (data.get('return_code') == 0)
            
            if is_success:
                # 다양한 응답 키 확인
                result_key = None
                possible_keys = ['output', 'output1', 'trde_qty_sdnin', 'data']
                for key in possible_keys:
                    if key in data and data[key]:
                        result_key = key
                        break
                
                if result_key:
                    result_data = data[result_key]
                    if isinstance(result_data, list) and len(result_data) > 0:
                        print(f"✅ 거래량 급증 ({market_type}) 종목 {len(result_data)}건 조회")
                        return result_data
                    else:
                        print(f"⚠️ 거래량 급증({market_type}) 결과는 있지만 빈 배열")
                        return []
                else:
                    print(f"⚠️ 거래량 급증({market_type}) 응답에서 데이터 키를 찾을 수 없음")
                    print(f"📋 사용 가능한 키들: {list(data.keys())}")
                    return []
            else: 
                error_msg = data.get('msg1') or data.get('return_msg') or 'API 응답 오류'
                print(f"⚠️ 거래량 급증({market_type}) API 오류: {error_msg}")
                print(f"📋 전체 응답: {data}")
                return []
                
        except httpx.HTTPStatusError as e:
            error_detail = e.response.text
            try: 
                error_json = e.response.json()
                error_detail = error_json.get('msg1') or error_json.get('return_msg') or error_detail
                print(f"📋 오류 응답 상세: {error_json}")
            except: pass
            print(f"❌ 거래량 급증({market_type}) 조회 오류 (HTTP {e.response.status_code}): {error_detail}")
        except httpx.RequestError as e: 
            print(f"❌ 거래량 급증({market_type}) 조회 네트워크 오류: {e}")
        except Exception as e: 
            print(f"❌ 예상치 못한 오류 (fetch_volume_surge_stocks): {e}")
            print(f" traceback: {traceback.format_exc()}")
        return []

    async def fetch_multiple_stock_details(self, stock_codes: List[str]) -> List[Dict]:
        if not stock_codes: return []
        url = "/api/dostk/stkinfo"; tr_id = "ka10095"
        headers = await self._get_headers(tr_id)
        if not headers: return []
        body = {"stk_cd": "|".join(stock_codes)}
        try:
            res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
            res.raise_for_status(); data = res.json()
            result_key = 'atn_stk_infr' if 'atn_stk_infr' in data else ('output1' if 'output1' in data else None)
            if data and result_key and data.get(result_key) and data.get('rt_cd') == '0':
                print(f"✅ 다수 종목 ({len(stock_codes)}개) 상세 정보 조회 성공")
                return data[result_key]
            else: print(f"⚠️ 다수 종목 상세 정보 데이터 없음: {data.get('msg1', 'API 응답 없음')}"); return []
        except Exception as e: print(f"❌ 다수 종목 상세 정보 조회 오류: {e}"); return []

    # --- 주문 API ---
    async def create_buy_order(self, stock_code: str, quantity: int, price: int = 0) -> Optional[Dict]:
        url = "/api/dostk/ordr"; tr_id = "kt10000"
        headers = await self._get_headers(tr_id, is_order=True)
        if not headers: return None
        account_prefix, account_suffix = (self.account_no.split('-') + [''])[:2] if self.account_no and '-' in self.account_no else (None, None)
        if not account_prefix or not account_suffix: print("❌ 매수 주문 실패: 계좌번호 형식 오류."); return None

        trade_type = "3" if price == 0 else "0"
        body = { "canp_no": account_prefix, "acnm_no": account_suffix, "ord_gno": "01",
                 "dmst_stex_tp": "KRX", "stk_cd": stock_code, "ord_qty": str(quantity),
                 "ord_uv": str(price) if price > 0 else "0", "trde_tp": trade_type }
        try:
            print(f"➡️ 매수 주문 요청: {body}")
            res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
            res.raise_for_status(); data = res.json()
            if data and data.get('output1', {}).get('rt_cd') == '0':
                ord_no = data.get('output2', {}).get('ord_no')
                msg = data.get('output1', {}).get('msg1', '성공')
                print(f"✅ [매수 주문 성공] {stock_code} {quantity}주 ({'시장가' if price==0 else f'지정가 {price}'}). 주문번호: {ord_no}")
                return {'return_code': 0, 'ord_no': ord_no, 'msg': msg}
            else:
                error_msg = data.get('output1', {}).get('msg1', 'API 실패'); print(f"❌ [매수 주문 API 오류] {stock_code}. 오류: {error_msg}")
                return {'return_code': -1, 'error': error_msg}
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_data = e.response.json(); error_msg = error_data.get('msg1', error_text)
            except: pass
            print(f"❌ [매수 주문 HTTP 오류 {e.response.status_code}] {stock_code}. 오류: {error_msg}")
            return {'return_code': e.response.status_code, 'error': error_text}
        except Exception as e:
            print(f"❌ [매수 주문 오류] {stock_code}. 오류: {e}"); return {'return_code': -99, 'error': str(e)}

    async def create_sell_order(self, stock_code: str, quantity: int, price: int = 0) -> Optional[Dict]:
        url = "/api/dostk/ordr"; tr_id = "kt10001"
        headers = await self._get_headers(tr_id, is_order=True)
        if not headers: return None
        account_prefix, account_suffix = (self.account_no.split('-') + [''])[:2] if self.account_no and '-' in self.account_no else (None, None)
        if not account_prefix or not account_suffix: print("❌ 매도 주문 실패: 계좌번호 형식 오류."); return None

        trade_type = "3" if price == 0 else "0"
        body = { "canp_no": account_prefix, "acnm_no": account_suffix, "ord_gno": "01",
                 "dmst_stex_tp": "KRX", "stk_cd": stock_code, "ord_qty": str(quantity),
                 "ord_uv": str(price) if price > 0 else "0", "trde_tp": trade_type }
        try:
            print(f"➡️ 매도 주문 요청: {body}")
            res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
            res.raise_for_status(); data = res.json()
            if data and data.get('output1', {}).get('rt_cd') == '0':
                ord_no = data.get('output2', {}).get('ord_no')
                msg = data.get('output1', {}).get('msg1', '성공')
                print(f"✅ [매도 주문 성공] {stock_code} {quantity}주 ({'시장가' if price==0 else f'지정가 {price}'}). 주문번호: {ord_no}")
                return {'return_code': 0, 'ord_no': ord_no, 'msg': msg}
            else:
                error_msg = data.get('output1', {}).get('msg1', 'API 실패'); print(f"❌ [매도 주문 API 오류] {stock_code}. 오류: {error_msg}")
                return {'return_code': -1, 'error': error_msg}
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_data = e.response.json(); error_msg = error_data.get('msg1', error_text)
            except: pass
            print(f"❌ [매도 주문 HTTP 오류 {e.response.status_code}] {stock_code}. 오류: {error_msg}")
            return {'return_code': e.response.status_code, 'error': error_text}
        except Exception as e:
            print(f"❌ [매도 주문 오류] {stock_code}. 오류: {e}"); return {'return_code': -99, 'error': str(e)}

    async def cancel_order(self, order_no: str, stock_code: str, quantity: int = 0) -> Optional[Dict]:
        url = "/api/dostk/ordr"; tr_id = "kt10003"
        headers = await self._get_headers(tr_id, is_order=True)
        if not headers: return None
        account_prefix, account_suffix = (self.account_no.split('-') + [''])[:2] if self.account_no and '-' in self.account_no else (None, None)
        if not account_prefix or not account_suffix: print("❌ 주문 취소 실패: 계좌번호 형식 오류."); return None

        cancel_qty_str = "0" if quantity == 0 else str(quantity)
        body = { "canp_no": account_prefix, "acnm_no": account_suffix, "ord_gno": "01",
                 "dmst_stex_tp": "KRX", "orig_ord_no": order_no, "stk_cd": stock_code,
                 "cncl_qty": cancel_qty_str }
        try:
            print(f"➡️ 주문 취소 요청: {body}")
            res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
            res.raise_for_status(); data = res.json()
            if data and data.get('output1', {}).get('rt_cd') == '0':
                new_ord_no = data.get('output2', {}).get('ord_no')
                msg = data.get('output1', {}).get('msg1', '성공')
                print(f"✅ [주문 취소 성공] 원주문: {order_no}, 취소 주문번호: {new_ord_no}")
                return {'return_code': 0, 'ord_no': new_ord_no, 'msg': msg}
            else:
                error_msg = data.get('output1', {}).get('msg1', 'API 실패'); print(f"❌ [주문 취소 API 오류] 원주문: {order_no}. 오류: {error_msg}")
                return {'return_code': -1, 'error': error_msg}
        except httpx.HTTPStatusError as e:
            error_text = e.response.text; error_msg = error_text
            try: error_data = e.response.json(); error_msg = error_data.get('msg1', error_text)
            except: pass
            print(f"⚠️ [주문 취소 HTTP 오류 {e.response.status_code}] 원주문: {order_no}. 오류: {error_msg}")
            return {'return_code': e.response.status_code, 'error': error_text}
        except Exception as e:
            print(f"❌ [주문 취소 오류] 원주문: {order_no}. 오류: {e}"); return {'return_code': -99, 'error': str(e)}