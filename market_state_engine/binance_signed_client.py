import hashlib
import hmac
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .env_loader import load_env_file


class BinanceSignedClient:
    def __init__(self, api_key=None, api_secret=None, base_url="https://fapi.binance.com", timeout=10, env_path=".env"):
        env = load_env_file(env_path)
        self.api_key = api_key or env.get("BINANCE_API_KEY")
        self.api_secret = api_secret or env.get("BINANCE_API_SECRET")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        if not self.api_key or not self.api_secret:
            raise ValueError("Missing BINANCE_API_KEY or BINANCE_API_SECRET")

    def get_account(self):
        return self._signed_request("GET", "/fapi/v2/account")

    def get_position_risk(self, symbol=None):
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._signed_request("GET", "/fapi/v2/positionRisk", params)

    def set_leverage(self, symbol, leverage):
        return self._signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        try:
            return self._signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
        except RuntimeError as exc:
            if "No need to change margin type" in str(exc):
                return {"ok": True, "message": "margin type already set"}
            raise

    def place_market_order(self, symbol, side, quantity, reduce_only=False):
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"

        return self._signed_request("POST", "/fapi/v1/order", params)

    def _signed_request(self, method, path, params=None):
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urlencode(params)
        signature = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        query = f"{query}&signature={signature}"
        url = f"{self.base_url}{path}?{query}"

        request = Request(url, method=method, headers={"X-MBX-APIKEY": self.api_key})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(f"Binance API error {exc.code}: {body}") from exc
