import os
import time
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


URL = "https://ahhmb2.kxbxjj.com/hmb_m_anhui_2025/services/insure/findInsurePersonDetail"

PAYLOAD: Dict[str, Any] = {
    "userName": os.getenv("HMB_USER_NAME", "朱守仓"),
    "userCardNo": os.getenv("HMB_USER_CARD_NO", "340122197201243951"),
    "applicationStrategy": os.getenv("HMB_APPLICATION_STRATEGY", "anhui2025001"),
    "productCode": os.getenv("HMB_PRODUCT_CODE", "anhui2025001"),
}

LOG_PATH = Path(os.getenv("HMB_LOG_PATH", "monitor.log"))
DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes
DEFAULT_MAX_ITERATIONS = None  # None means keep running


def build_headers() -> Dict[str, str]:
    """Build request headers from environment values."""

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://ahhmb2.kxbxjj.com",
        "Referer": "https://ahhmb2.kxbxjj.com/anhui_mobile_2025/?t=1766561622044",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/143.0.0.0 Mobile Safari/537.36 Edg/143.0.0.0"
        ),
    }

    # Sensitive headers come from env vars to avoid hard-coding tokens.
    cookie = os.getenv("HMB_COOKIE")
    if cookie:
        headers["Cookie"] = cookie

    wx_token = os.getenv("HMB_WX_TOKEN")
    if wx_token:
        headers["wxtoken"] = wx_token

    return headers


def build_proxies() -> Optional[Dict[str, str]]:
    proxy_url = os.getenv("HMB_PROXY_URL", "http://127.0.0.1:7897")
    use_proxy = os.getenv("HMB_USE_PROXY", "0") == "1"
    if not use_proxy:
        return None
    return {"http": proxy_url, "https": proxy_url}


def write_log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def get_email_config() -> Optional[Dict[str, Any]]:
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASS")
    recipients_raw = os.getenv("EMAIL_TO")
    if not user or not password or not recipients_raw:
        return None
    recipients: List[str] = [r.strip()
                             for r in recipients_raw.split(",") if r.strip()]
    if not recipients:
        return None
    return {
        "user": user,
        "password": password,
        "recipients": recipients,
        "subject": os.getenv("EMAIL_SUBJECT", "监控状态变化"),
    }


def send_email_change(prev_state: Dict[str, Any], new_state: Dict[str, Any]) -> None:
    config = get_email_config()
    if not config:
        return
    try:
        import yagmail
    except ImportError as exc:
        write_log(f"{datetime.now()} email skipped: yagmail missing ({exc})")
        return

    body_lines = [
        f"上次状态: {prev_state}",
        f"本次状态: {new_state}",
        f"时间: {datetime.now()}",
    ]
    try:
        yag = yagmail.SMTP(
            user=config["user"], password=config["password"], host="smtp.qq.com")
        yag.send(config["recipients"], config["subject"],
                 "\n".join(body_lines))
        yag.close()
        write_log(f"{datetime.now()} email sent to {config['recipients']}")
    except Exception as exc:  # noqa: BLE001 - best-effort notify
        write_log(f"{datetime.now()} email failed: {exc}")


def resolve_interval_seconds() -> int:
    """Resolve polling interval from env or fallback to default."""
    env_val = os.getenv("HMB_INTERVAL_SECONDS")
    if env_val and env_val.isdigit():
        return max(1, int(env_val))
    return DEFAULT_INTERVAL_SECONDS


def resolve_max_iterations() -> Optional[int]:
    env_val = os.getenv("HMB_MAX_ITERATIONS")
    if env_val and env_val.isdigit():
        return max(1, int(env_val))
    return DEFAULT_MAX_ITERATIONS


def monitor(interval_seconds: Optional[int] = None, max_iterations: Optional[int] = None) -> None:
    headers = build_headers()
    proxies = build_proxies()
    interval = interval_seconds or resolve_interval_seconds()
    iterations_limit = max_iterations if max_iterations is not None else resolve_max_iterations()
    last_state: Optional[Dict[str, Any]] = None
    iterations = 0

    with requests.Session() as session:
        while True:
            try:
                resp = session.post(
                    URL,
                    json=PAYLOAD,
                    headers=headers,
                    proxies=proxies,
                    timeout=15,
                )
                resp.raise_for_status()
                payload = resp.json()
            except requests.RequestException as exc:
                print(f"[{datetime.now()}] network error: {exc}")
                write_log(f"{datetime.now()} network error: {exc}")
            except ValueError:
                print(f"[{datetime.now()}] response is not valid JSON: {resp.text}")
                write_log(f"{datetime.now()} invalid json: {resp.text}")
            else:
                code = payload.get("code")
                message = payload.get("message")
                data = payload.get("data", {}) if isinstance(
                    payload, dict) else {}

                try:
                    raw_payload = json.dumps(payload, ensure_ascii=False)
                except Exception:
                    raw_payload = str(payload)

                state = {
                    "code": code,
                    "message": message,
                    "policy": data.get("selfPolicySn"),
                    "policyStatus": data.get("policyStatus"),
                    "payStatus": data.get("payStatus"),
                    "applicantName": data.get("applicantName"),
                }

                print(
                    f"[{datetime.now()}] code={state['code']}, message={state['message']}, "
                    f"policy={state['policy']}, status={state['policyStatus']}"
                )
                print(raw_payload)
                write_log(
                    f"{datetime.now()} code={state['code']} message={state['message']} "
                    f"policy={state['policy']} status={state['policyStatus']} "
                    f"applicantName={state['applicantName']} payStatus={state['payStatus']}"
                )
                write_log(f"{datetime.now()} raw_payload={raw_payload}")

                if state.get("code") != "200":
                    write_log(f"{datetime.now()} code not 200: {state}")
                    send_email_change(last_state or {"code": "initial"}, state)

                if last_state is None:
                    print("[info] initial fetch recorded")
                elif state != last_state:
                    print("[alert] state changed")
                    write_log(
                        f"{datetime.now()} state changed: {last_state} -> {state}")
                    send_email_change(last_state, state)

                last_state = state
            finally:
                iterations += 1
                if iterations_limit is not None and iterations >= iterations_limit:
                    break

                time.sleep(interval)


if __name__ == "__main__":
    monitor()
