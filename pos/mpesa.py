"""Safaricom Daraja (M-Pesa Express / STK Push) client.

Modes
  simulate   - no network; the payment "succeeds" a few seconds after the request. For demos/training.
  sandbox    - https://sandbox.safaricom.co.ke with your Daraja test app keys.
  production - https://api.safaricom.co.ke with your Go-Live keys, shortcode and passkey.

Payments are confirmed two ways, whichever comes first:
  1. Safaricom POSTs the result to  <mpesa_callback_base>/mpesa/callback/<token>  (needs a public HTTPS URL).
  2. The till polls; after a short wait the server calls the STK Query API itself, so it also works
     on a local network without a public URL.
"""
import base64
import re
import secrets
import time
from datetime import datetime

import requests

URLS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}


class MpesaError(Exception):
    pass


def normalize_phone(phone):
    """0712345678 / +254712345678 / 254712345678 / 712345678 -> 254712345678 (Safaricom 07xx & 01xx)."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "71":
        digits = "254" + digits
    if not re.fullmatch(r"254(7\d{8}|1\d{8})", digits):
        raise MpesaError("Enter a valid Kenyan phone number, e.g. 0712 345 678")
    return digits


class MpesaClient:
    _token_cache = {}

    def __init__(self, settings, timeout=30):
        self.mode = settings.get("mpesa_mode", "simulate")
        self.key = settings.get("mpesa_consumer_key", "")
        self.secret = settings.get("mpesa_consumer_secret", "")
        self.shortcode = settings.get("mpesa_shortcode", "")
        self.passkey = settings.get("mpesa_passkey", "")
        self.tx_type = settings.get("mpesa_transaction_type", "CustomerPayBillOnline")
        self.party_b = settings.get("mpesa_party_b") or self.shortcode
        base = (settings.get("mpesa_callback_base") or "").rstrip("/")
        token = settings.get("mpesa_callback_token", "")
        self.callback_url = f"{base}/mpesa/callback/{token}" if base else ""
        self.timeout = timeout

    @property
    def simulated(self):
        return self.mode == "simulate"

    def check_config(self):
        if self.simulated:
            return
        missing = [n for n, v in (("consumer key", self.key), ("consumer secret", self.secret),
                                  ("shortcode", self.shortcode), ("passkey", self.passkey)) if not v]
        if missing:
            raise MpesaError("M-Pesa is not configured: missing " + ", ".join(missing))
        if self.callback_url and not self.callback_url.startswith("https://"):
            raise MpesaError("The M-Pesa callback base URL must start with https://")

    def _callback(self):
        # Daraja insists on a well-formed https CallBackURL. Without a public URL we give a placeholder
        # and confirm the payment through the STK Query API instead (slower, and no receipt code).
        return self.callback_url or "https://example.com/mpesa/callback"

    # -- Daraja calls --
    def _base(self):
        return URLS[self.mode]

    def _token(self):
        cache_key = (self.mode, self.key)
        tok, exp = self._token_cache.get(cache_key, (None, 0))
        if tok and exp > time.time() + 60:
            return tok
        try:
            r = requests.get(f"{self._base()}/oauth/v1/generate?grant_type=client_credentials",
                             auth=(self.key, self.secret), timeout=self.timeout)
        except requests.RequestException as e:
            raise MpesaError(f"Could not reach Safaricom: {e}")
        if r.status_code != 200:
            raise MpesaError(f"M-Pesa login failed ({r.status_code}). Check consumer key/secret.")
        data = r.json()
        tok = data["access_token"]
        self._token_cache[cache_key] = (tok, time.time() + int(data.get("expires_in", 3599)))
        return tok

    def _password(self):
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        pw = base64.b64encode(f"{self.shortcode}{self.passkey}{ts}".encode()).decode()
        return pw, ts

    def _post(self, path, body):
        try:
            r = requests.post(f"{self._base()}{path}", json=body, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self._token()}"})
        except requests.RequestException as e:
            raise MpesaError(f"Could not reach Safaricom: {e}")
        try:
            data = r.json()
        except ValueError:
            raise MpesaError(f"Unexpected reply from Safaricom ({r.status_code})")
        return r.status_code, data

    def stk_push(self, phone, amount_kes, account_ref, description):
        phone = normalize_phone(phone)
        amount_kes = int(amount_kes)
        if amount_kes < 1:
            raise MpesaError("Amount must be at least KES 1")
        if self.simulated:
            return {"CheckoutRequestID": f"ws_SIM_{secrets.token_hex(8)}", "MerchantRequestID": "SIM",
                    "ResponseCode": "0", "CustomerMessage": "Simulated request sent", "phone": phone,
                    "simulated_at": time.time()}
        self.check_config()
        pw, ts = self._password()
        status, data = self._post("/mpesa/stkpush/v1/processrequest", {
            "BusinessShortCode": self.shortcode, "Password": pw, "Timestamp": ts,
            "TransactionType": self.tx_type, "Amount": amount_kes, "PartyA": phone, "PartyB": self.party_b,
            "PhoneNumber": phone, "CallBackURL": self._callback(),
            "AccountReference": re.sub(r"[^A-Za-z0-9]", "", account_ref)[:12] or "POS",
            "TransactionDesc": description[:13],
        })
        if status != 200 or str(data.get("ResponseCode")) != "0":
            raise MpesaError(data.get("errorMessage") or data.get("ResponseDescription") or f"STK push failed ({status})")
        data["phone"] = phone
        return data

    def stk_query(self, checkout_request_id):
        """Returns ('completed'|'failed'|'cancelled'|'pending', description)."""
        if self.simulated:
            return "pending", "Simulated"
        pw, ts = self._password()
        status, data = self._post("/mpesa/stkpushquery/v1/query", {
            "BusinessShortCode": self.shortcode, "Password": pw, "Timestamp": ts,
            "CheckoutRequestID": checkout_request_id})
        if "ResultCode" not in data:
            return "pending", data.get("errorMessage", "Still processing")  # e.g. 500.001.1001 'being processed'
        code = str(data["ResultCode"])
        return RESULT_STATUS.get(code, "failed"), data.get("ResultDesc", "")


# Daraja result codes -> our payment status
RESULT_STATUS = {"0": "completed", "1032": "cancelled", "1037": "failed", "1": "failed", "2001": "failed",
                 "1019": "failed", "1001": "failed", "17": "failed", "1025": "failed", "9999": "failed"}


def parse_callback(body):
    """Extract the useful fields from Daraja's STK callback JSON."""
    cb = (body or {}).get("Body", {}).get("stkCallback")
    if not cb or "CheckoutRequestID" not in cb:
        raise MpesaError("Not an STK callback")
    meta = {i.get("Name"): i.get("Value") for i in cb.get("CallbackMetadata", {}).get("Item", [])}
    return {
        "checkout_request_id": cb["CheckoutRequestID"],
        "result_code": str(cb.get("ResultCode")),
        "result_desc": cb.get("ResultDesc", ""),
        "amount": meta.get("Amount"),
        "receipt": meta.get("MpesaReceiptNumber"),
        "phone": str(meta["PhoneNumber"]) if meta.get("PhoneNumber") else None,
        "date": meta.get("TransactionDate"),
    }
