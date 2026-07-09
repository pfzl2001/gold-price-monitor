from __future__ import annotations

TARGET_PRICE_NOTIFY: float | None = 1135

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


GOLD_PRICE_URL = "https://api.jdjygold.com/gw2/generic/produTools/h5/m/getGoldPrice"
DEFAULT_GOLD_CODE = "CZB-JCJ"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_START_HOUR = 9
DEFAULT_END_HOUR = 24

DEFAULT_HEADERS: Dict[str, str] = {
    "accept": "application/json, text/plain, */*",
    "origin": "https://gold-price-pro.pf.jd.com",
    "referer": "https://gold-price-pro.pf.jd.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
}

LOG_FILE_PATH = Path(__file__).with_name("monitor.log")


def send_resend_email(subject: str, html_content: str, receiver_email: str) -> bool:
    """通用邮件发送函数（Resend SMTP）"""
    smtp_host = os.getenv("SMTP_HOST", "smtp.resend.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465").strip())
    smtp_user = os.getenv("SMTP_USER", "resend").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    sender_email = os.getenv("SENDER_EMAIL", "").strip()

    if not smtp_pass or not sender_email or not receiver_email:
        print("[warn] 邮件配置不完整（SMTP_PASS/SENDER_EMAIL/receiver 缺失），跳过发送")
        return False
    
    msg = MIMEMultipart('alternative')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = f"金价监控助手 <{sender_email}>"
    msg['To'] = receiver_email
    
    subtype = 'html' if '<html>' in html_content or '<body' in html_content or '<br' in html_content else 'plain'
    msg.attach(MIMEText(html_content, subtype, 'utf-8'))
    
    recipients = [addr.strip() for addr in receiver_email.split(",") if addr.strip()]
    
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(sender_email, recipients, msg.as_string())
        print(f"[alert] 📧 Resend 邮件已发送到: {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"[error] Resend 邮件发送失败: {e}")
        return False


@dataclass(frozen=True)
class GoldPriceResult:
    gold_code: str
    name: str
    last_price: float
    raise_value: float
    raise_percent: float
    trade_datetime: str
    raw: Dict[str, Any]

    def summary_line(self) -> str:
        return (
            f"goldCode={self.gold_code} name={self.name} lastPrice={self.last_price} "
            f"raise={self.raise_value} raisePercent={self.raise_percent} tradeDateTime={self.trade_datetime}"
        )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _now_in_timezone(tz_name: str) -> datetime:
    # Python 3.11 has zoneinfo in stdlib.
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz_name))


def should_run_now(
    *,
    tz_name: str = DEFAULT_TZ,
    weekdays_only: bool = True,
    start_hour: int = DEFAULT_START_HOUR,
    end_hour: int = DEFAULT_END_HOUR,
    now: Optional[datetime] = None,
) -> bool:
    if now is None:
        now = _now_in_timezone(tz_name)

    if weekdays_only and now.weekday() >= 5:
        return False

    if end_hour <= start_hour:
        return False

    # [start_hour, end_hour) in local time. With end_hour=24, this means 9..23.
    return start_hour <= now.hour < end_hour


def _build_session() -> requests.Session:
    session = requests.Session()
    # On some machines, HTTPS_PROXY/HTTP_PROXY is set (e.g. to 127.0.0.1:7890).
    # That breaks both local runs (if the proxy isn't running) and GitHub Actions.
    # Default to ignoring env proxy settings unless explicitly enabled.
    session.trust_env = _env_bool("TRUST_ENV", False)
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update(DEFAULT_HEADERS)
    return session


def fetch_gold_price(
    *,
    gold_code: str = DEFAULT_GOLD_CODE,
    session: Optional[requests.Session] = None,
    timeout_seconds: int = 15,
) -> GoldPriceResult:
    sess = session or _build_session()

    response = sess.get(
        GOLD_PRICE_URL,
        params={"goldCode": gold_code},
        proxies={"http": None, "https": None},
        timeout=timeout_seconds,
    )
    response.raise_for_status()

    payload = response.json()

    result_data = payload.get("resultData")
    if not isinstance(result_data, dict):
        raise ValueError(f"Unexpected payload (missing resultData): {payload}")

    data = result_data.get("data")
    if not isinstance(data, dict):
        raise ValueError(
            f"Unexpected payload (missing resultData.data): {payload}")

    name = str(data.get("name") or "")
    last_price = float(data.get("lastPrice"))
    raise_value = float(data.get("raise"))
    raise_percent = float(data.get("raisePercent"))

    trade_dt = data.get("tradeDateTime")
    if isinstance(trade_dt, dict):
        # Keep the human timestamp stable for logs/emails.
        trade_datetime = (
            f"{trade_dt.get('year')}-{trade_dt.get('monthValue'):02d}-{trade_dt.get('dayOfMonth'):02d} "
            f"{trade_dt.get('hour'):02d}:{trade_dt.get('minute'):02d}:{trade_dt.get('second'):02d}"
        )
    else:
        trade_datetime = str(trade_dt or "")

    unique_code = str(data.get("uniqueCode") or gold_code)

    return GoldPriceResult(
        gold_code=unique_code,
        name=name,
        last_price=last_price,
        raise_value=raise_value,
        raise_percent=raise_percent,
        trade_datetime=trade_datetime,
        raw=payload,
    )


def _append_log_line(text: str) -> None:
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _format_email_body(result: GoldPriceResult) -> str:
    lines = [
        "京东金价监控提醒：raisePercent < 0",
        f"时间(北京时间): {_now_in_timezone(DEFAULT_TZ).isoformat(sep=' ', timespec='seconds')}",
        result.summary_line(),
        "",
        "原始返回(JSON 截断):",
        json.dumps(result.raw, ensure_ascii=False)[:3000],
    ]
    return "\n".join(lines)


def _format_target_price_email_body(result: GoldPriceResult, target_price: float) -> str:
    lines = [
        "京东金价监控提醒：lastPrice > TARGET_PRICE_NOTIFY",
        f"时间(北京时间): {_now_in_timezone(DEFAULT_TZ).isoformat(sep=' ', timespec='seconds')}",
        f"targetPrice={target_price} lastPrice={result.last_price} tradeDateTime={result.trade_datetime}",
        "",
        result.summary_line(),
        "",
        "原始返回(JSON 截断):",
        json.dumps(result.raw, ensure_ascii=False)[:3000],
    ]
    return "\n".join(lines)


def _send_target_price_email_if_needed(result: GoldPriceResult) -> bool:
    target = TARGET_PRICE_NOTIFY
    if target is None:
        return False

    try:
        target_f = float(target)
    except Exception:
        return False

    if not (result.last_price > target_f):
        return False

    email_to = os.getenv("EMAIL_TO", "").strip()
    email_subject = os.getenv(
        "EMAIL_SUBJECT", "金价超过目标价提醒"
    ).strip() or "金价超过目标价提醒"

    if not email_to:
        print("[warn] 超过目标价但邮件配置缺失，跳过发送")
        return False

    subject = f"{email_subject} - 当前 {result.last_price:.2f} > 目标 {target_f:.2f}"
    body = _format_target_price_email_body(result, target_f)

    return send_resend_email(subject, body, email_to)


def _send_email_if_needed(result: GoldPriceResult) -> bool:
    if result.raise_percent >= 0:
        return False

    email_to = os.getenv("EMAIL_TO", "").strip()
    email_subject = os.getenv(
        "EMAIL_SUBJECT", "金价下跌提醒").strip() or "金价下跌提醒"

    if not email_to:
        print("[warn] raisePercent<0 但邮件配置缺失，跳过发送")
        return False

    body = _format_email_body(result)

    return send_resend_email(email_subject, body, email_to)


def main() -> int:
    tz_name = os.getenv("GOLD_TZ", DEFAULT_TZ)
    # Schedule check removed - GitHub Actions cron handles timing precisely
    # to save Actions quota. Runs only on Beijing workdays 9:00-23:00.

    gold_code = os.getenv(
        "GOLD_CODE", DEFAULT_GOLD_CODE).strip() or DEFAULT_GOLD_CODE
    timeout_seconds = _env_int("TIMEOUT_SECONDS", 15)

    try:
        result = fetch_gold_price(
            gold_code=gold_code, timeout_seconds=timeout_seconds)
    except Exception as exc:
        now = _now_in_timezone(tz_name)
        msg = f"[{now.isoformat(sep=' ', timespec='seconds')}] error fetching gold price: {exc}\n"
        print(msg.strip())
        _append_log_line(msg)

        # Optional: notify failures (off by default)
        if _env_bool("EMAIL_ON_ERROR", False):
            try:
                dummy = GoldPriceResult(
                    gold_code=gold_code,
                    name="",
                    last_price=float("nan"),
                    raise_value=float("nan"),
                    raise_percent=float("nan"),
                    trade_datetime="",
                    raw={"error": str(exc)},
                )
                _send_email_if_needed(dummy)
            except Exception:
                pass
        return 1

    now = _now_in_timezone(tz_name)
    log_payload = {
        "timestamp": now.isoformat(sep=" ", timespec="seconds"),
        "goldCode": result.gold_code,
        "name": result.name,
        "lastPrice": result.last_price,
        "raise": result.raise_value,
        "raisePercent": result.raise_percent,
        "tradeDateTime": result.trade_datetime,
    }
    log_line = json.dumps(log_payload, ensure_ascii=False) + "\n"
    print(log_line.strip())
    _append_log_line(log_line)

    target_emailed = False
    try:
        target_emailed = _send_target_price_email_if_needed(result)
    except Exception as exc:
        warn = f"[{now.isoformat(sep=' ', timespec='seconds')}] target email send failed: {exc}\n"
        print(warn.strip())
        _append_log_line(warn)

    if target_emailed:
        print("[alert] lastPrice > TARGET_PRICE_NOTIFY, email sent")

    emailed = False
    try:
        emailed = _send_email_if_needed(result)
    except Exception as exc:
        warn = f"[{now.isoformat(sep=' ', timespec='seconds')}] email send failed: {exc}\n"
        print(warn.strip())
        _append_log_line(warn)

    if emailed:
        print("[alert] raisePercent < 0, email sent")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
