import httpx
import asyncio
import json
import os
from typing import Optional, Dict
from datetime import datetime, timedelta

from config.loader import config

from data.manager import preprocess_chart_data
from data.indicators import calculate_orb, add_vwap

class KiwoomAPI:
  """키움증권 REST API와의 비동기 통신을 담당합니다."""

  TOKEN_FILE = ".token" # 토큰을 저장할 파일 이름

  def __init__(self):
    self.base_url = "https://api.kiwoom.com"
    self.app_key = config.kiwoom.app_key
    self.app_secret = config.kiwoom.app_secret
    self.account_no = config.kiwoom.account_no
    self._access_token: Optional[str] = None
    self._token_expires_at: Optional[datetime] = None
    self.client = httpx.AsyncClient(timeout=None)
    self._load_token_from_file()

  async def close(self):
    """HTTP 클라이언트 세션을 종료합니다."""
    if not self.client.is_closed:
      await self.client.aclose()

  # 파일에서 토큰 로드하는 메서드 추가
  def _load_token_from_file(self):
    """파일에 저장된 접근 토큰 정보를 불러옵니다."""
    if os.path.exists(self.TOKEN_FILE):
      try:
        with open(self.TOKEN_FILE, 'r') as f:
          token_data = json.load(f)
          self._access_token = token_data.get('access_token')
          expires_str = token_data.get('expires_at')
          if expires_str:
            self._token_expires_at = datetime.fromisoformat(expires_str)
            print(f"ℹ️ 저장된 토큰을 불러왔습니다. (만료: {self._token_expires_at})")
      except (json.JSONDecodeError, KeyError):
        print("⚠️ 토큰 파일이 손상되었거나 형식이 올바르지 않습니다.")
        self._access_token = None
        self._token_expires_at = None

  # 파일에 토큰 저장하는 메서드 추가
  def _save_token_to_file(self):
    """발급받은 접근 토큰 정보를 파일에 저장합니다."""
    if self._access_token and self._token_expires_at:
      token_data = {
        'access_token': self._access_token,
        'expires_at': self._token_expires_at.isoformat()
      }
      with open(self.TOKEN_FILE, 'w') as f:
        json.dump(token_data, f)
      print(f"💾 새로운 토큰을 파일에 저장했습니다.")

  def is_token_valid(self) -> bool:
    """현재 보유한 접근 토큰이 유효한지 확인합니다."""
    if self._access_token is None or self._token_expires_at is None:
      return False
    return datetime.now() < (self._token_expires_at - timedelta(minutes=1))

  async def get_access_token(self) -> str:
    """OAuth 인증을 통해 접근 토큰을 발급받습니다. (API ID: au10001)"""
    url = f"{self.base_url}/oauth2/token"
    headers = {"Content-Type": "application/json;charset=UTF-8"}
    body = {
      "grant_type": "client_credentials",
      "appkey": self.app_key,
      "secretkey": self.app_secret,
    }
    
    try:
      res = await self.client.post(url, headers=headers, json=body)
      res.raise_for_status()
      data = res.json()
      
      access_token = data.get("token")
      expires_dt_str = data.get("expires_dt")

      if access_token and expires_dt_str:
        self._access_token = access_token
        self._token_expires_at = datetime.strptime(expires_dt_str, "%Y%m%d%H%M%S")
        print(f"✅ 접근 토큰 신규 발급 성공 (만료: {self._token_expires_at})")

        self._save_token_to_file()
        
        return self._access_token
      else:
        error_msg = data.get('return_msg', '알 수 없는 오류')
        print(f"❌ 토큰 발급 실패: {error_msg}")
        raise ValueError(f"응답에서 'token' 또는 'expires_dt'를 찾을 수 없습니다: {data}")

    except httpx.HTTPStatusError as e:
      error_data = e.response.json()
      print(f"❌ 접근 토큰 발급 실패 (HTTP 오류): {e.response.status_code} - {error_data.get('return_msg', e.response.text)}")
      raise
    except Exception as e:
      print(f"❌ 예상치 못한 오류 발생 (get_access_token): {e}")
      raise

  async def _get_headers(self, tr_id: str, is_order: bool = False) -> dict:
    """데이터 API 요청에 필요한 헤더를 생성합니다."""
    if not self.is_token_valid():
      print("ℹ️ 유효한 접근 토큰이 없거나 만료되어 재발급을 시도합니다.")
      await self.get_access_token()
    else:
      print("ℹ️ 기존 접근 토큰을 재사용합니다.")

    headers = {
      "Content-Type": "application/json;charset=UTF-8",
      "authorization": f"Bearer {self._access_token}",
      "api-id": tr_id,
    }

    # 주문 요청일 경우에만 계좌번호 헤더 추가
    if is_order:
      headers["acnt_no"] = self.account_no.split('-')[0]

    return headers

  async def fetch_stock_info(self, stock_code: str) -> Optional[Dict]:
    """주식 기본 정보를 요청합니다. (API ID: ka10001)"""
    url = "/api/dostk/stkinfo"
    tr_id = "ka10001"
    try:
      headers = await self._get_headers(tr_id)
      body = {"stk_cd": stock_code}
      res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
      res.raise_for_status()
      data = res.json()
      print(f"✅ [{stock_code}] 종목 정보 조회 성공")
      return data
    except Exception as e:
      print(f"❌ [{stock_code}] 종목 정보 조회 중 오류 발생: {e}")
      return None

  async def fetch_minute_chart(self, stock_code: str, timeframe: int = 1) -> Optional[Dict]:
    """주식 분봉 차트 데이터를 요청합니다. (API ID: ka10080)"""
    url = "/api/dostk/chart"
    tr_id = "ka10080"
    
    try:
      headers = await self._get_headers(tr_id)
      body = {
        "stk_cd": stock_code,
        "tic_scope": str(timeframe),
        "upd_stkpc_tp": "0"
      }
      
      res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
      res.raise_for_status()
      
      data = res.json()
      print(f"✅ [{stock_code}] {timeframe}분봉 데이터 조회 성공")
      return data

    except httpx.HTTPStatusError as e:
      print(f"❌ 분봉 데이터 조회 실패 (HTTP 오류): {e.response.status_code} - {e.response.text}")
    except Exception as e:
      print(f"❌ 예상치 못한 오류 발생 (fetch_minute_chart): {e}")
    return None

  # 주식 매수 주문
  async def create_buy_order(self, stock_code: str, quantity: int) -> Optional[Dict]:
    """주식 시장가 매수 주문을 실행합니다. (API ID: kt10000)"""
    url = "/api/dostk/ordr"
    tr_id = "kt10000"
    try:
      headers = await self._get_headers(tr_id, is_order=True)
      body = {
        "dmst_stex_tp": "KRX",
        "stk_cd": stock_code,
        "ord_qty": str(quantity),
        "trde_tp": "3" # 3: 시장가
      }
      res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
      res.raise_for_status()
      data = res.json()
      print(f"✅ [매수 주문 성공] {stock_code} {quantity}주. 주문번호: {data.get('ord_no')}")
      return data
    except Exception as e:
      print(f"❌ [매수 주문 실패] {stock_code} {quantity}주. 오류: {e}")
      return None

  # 주식 매도 주문
  async def create_sell_order(self, stock_code: str, quantity: int) -> Optional[Dict]:
    """주식 시장가 매도 주문을 실행합니다. (API ID: kt10001)"""
    url = "/api/dostk/ordr"
    tr_id = "kt10001"
    try:
      headers = await self._get_headers(tr_id, is_order=True)
      body = {
        "dmst_stex_tp": "KRX",
        "stk_cd": stock_code,
        "ord_qty": str(quantity),
        "trde_tp": "3" # 3: 시장가
      }
      res = await self.client.post(f"{self.base_url}{url}", headers=headers, json=body)
      res.raise_for_status()
      data = res.json()
      print(f"✅ [매도 주문 성공] {stock_code} {quantity}주. 주문번호: {data.get('ord_no')}")
      return data
    except Exception as e:
      print(f"❌ [매도 주문 실패] {stock_code} {quantity}주. 오류: {e}")
      return None