"""Bounded HTTPS requests and Feishu custom bot protocol."""

import base64
import hashlib
import hmac
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RequestError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RequestError("Unexpected HTTP redirect refused")


def request(url, payload=None, timeout=20, attempts=3):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": "codex-reset-radar/0.1 (+https://github.com/pjzck/codex-reset-radar)",
               "Accept": "application/json,text/html"}
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    # Retry GETs only. Ambiguous webhook failures are retried on the next poll.
    for attempt in range(attempts if data is None else 1):
        try:
            with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers), timeout=timeout) as response:
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise RequestError("Response exceeds size limit")
                return body.decode("utf-8-sig")
        except HTTPError as exc:
            # Never include the request URL: it may contain the webhook credential.
            if data is not None or exc.code not in {408, 429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise RequestError("HTTP request failed (status {})".format(exc.code)) from None
        except (URLError, TimeoutError, OSError) as exc:
            if data is not None or attempt == attempts - 1:
                raise RequestError("Network request failed ({})".format(type(exc).__name__)) from None
        time.sleep(min(2 ** attempt, 8))


def validate_webhook(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"open.feishu.cn", "open.larksuite.com"}
            or parsed.username or parsed.password or parsed.port not in {None, 443}
            or not parsed.path.startswith("/open-apis/bot/v2/hook/") or not parsed.path.rsplit("/", 1)[-1]
            or parsed.query or parsed.fragment):
        raise ValueError("FEISHU_WEBHOOK_URL must be a Feishu/Lark HTTPS custom-bot webhook")


def send_feishu(url, text, secret="", timeout=20):
    validate_webhook(url)
    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = str(int(time.time()))
        signing_key = (ts + "\n" + secret).encode("utf-8")
        payload.update(timestamp=ts, sign=base64.b64encode(hmac.new(signing_key, b"", hashlib.sha256).digest()).decode())
    try:
        result = json.loads(request(url, payload, timeout=timeout))
    except (ValueError, TypeError):
        raise RequestError("Feishu returned invalid JSON") from None
    if not isinstance(result, dict):
        raise RequestError("Feishu returned an invalid response")
    code = result.get("code", result.get("StatusCode"))
    if code != 0:
        # Do not echo arbitrary response text containing possible credentials.
        safe_code = str(code) if isinstance(code, int) else "unknown"
        raise RequestError("Feishu rejected message (code {}); check bot keyword/signature/IP settings".format(safe_code))
