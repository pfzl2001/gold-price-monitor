"""
基金净值周五播报
================

每周五把「徽银理财“安盈”稳健最低持有30天理财产品」的单位净值，
以及相对**上一个净值公布日**（通常是周四）的涨跌额 / 涨跌幅，
通过项目内已有的 Resend SMTP 邮件路径发送给 EMAIL_TO 指定的收件人。

关于「前一天」的说明
--------------------
银行理财净值只在净值公布日更新（工作日，遇节假日顺延），周末与节假日没有净值。
因此本脚本的对比基准是「上一个净值公布日」而非自然日的前一天；
邮件中会明确写出基准日期与基准净值，避免歧义。

数据源：徽银理财官网公开净值接口（与 lccs_display.php?id=17222 页面同源）
主接口：lccs_income.php?fundCode=...&dateType=all   （JSON，全量历史）
备接口：lccs_income_list.php?id=...&page=N          （HTML 表格，主接口异常时降级）

用法
----
    python fund_nav_notify.py                        # 正常运行（每周五由 Actions 触发）
    python fund_nav_notify.py --dry-run              # 只生成报告与预览，不发送邮件
    python fund_nav_notify.py --date 2026-10-30     # 指定目标周五（补发 / 测试）
    python fund_nav_notify.py --force               # 忽略防重复记录，强制发送
    python fund_nav_notify.py --check               # 只检查净值是否已公布，不发邮件
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# 复用项目内同一套邮件发送路径：相同的 Resend SMTP 配置、相同的环境变量 / Secrets
# （send_resend_email 定义于 buy_strategy.py，__main__ 守卫保证导入时不会执行金价分析）
from buy_strategy import send_resend_email

# ================= 配置区 =================
PRODUCT_ID = "17222"
FUND_CODE = "PNHY230387A"
PRODUCT_NAME = '徽银理财“安盈”稳健最低持有30天理财产品'
DISPLAY_URL = f"https://www.hsbankwm.com/lccs_display.php?id={PRODUCT_ID}"
HISTORY_URL = f"https://www.hsbankwm.com/lccs_income_list.php?id={PRODUCT_ID}"
JSON_API = "https://www.hsbankwm.com/lccs_income.php"

DEFAULT_TZ = "Asia/Shanghai"
# 使用固定 UTC+8 时区：中国自 1991 年起不再实行夏令时，与 Asia/Shanghai 等价。
# 固定偏移不依赖 Windows 上的系统 tzdata，保证本地与 GitHub Actions 均可直接运行。
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
TIMEOUT_SECONDS = 20

# 基金净值播报独立开关，与金价监控开关（monitor.py 的 ENABLE_GOLD_MONITOR）互不干扰
ENABLE_FUND_NAV_NOTIFY = True

# 颜色遵循 A 股 / 国内理财习惯：红涨绿跌
COLOR_UP = "#d92b2b"
COLOR_DOWN = "#0f9d58"
COLOR_FLAT = "#6b7280"

STATE_PATH = Path(__file__).with_name("fund_nav_state.json")
PREVIEW_PATH = Path(__file__).with_name("fund_nav_preview.html")
# =========================================

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


@dataclass(frozen=True)
class NavPoint:
    """单个净值公布日"""
    day: date
    value: float


@dataclass(frozen=True)
class NavRow:
    """带日涨跌幅的净值行（用于邮件表格）"""
    day: date
    value: float
    change_pct: Optional[float]


@dataclass
class FridayReport:
    """一次周五播报的完整结果"""
    target_friday: date
    status: str                     # OK / SUBSTITUTE / PENDING / NO_DATA
    nav_date: Optional[date] = None
    nav_value: Optional[float] = None
    base_date: Optional[date] = None
    base_value: Optional[float] = None
    change_value: Optional[float] = None
    change_pct: Optional[float] = None
    week_base_date: Optional[date] = None
    week_base_value: Optional[float] = None
    week_change_pct: Optional[float] = None
    note: str = ""
    recent: List[NavRow] = None     # type: ignore[assignment]
    week_rows: List[NavRow] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.recent is None:
            self.recent = []
        if self.week_rows is None:
            self.week_rows = []


# ---------------------------------------------------------------------------
# 数据获取与清洗
# ---------------------------------------------------------------------------

def _build_session() -> requests.Session:
    session = requests.Session()
    # 与 monitor.py 保持一致：默认忽略环境代理，避免本机 / CI 代理干扰
    session.trust_env = os.getenv("TRUST_ENV", "").strip().lower() in {"1", "true", "yes"}
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": DISPLAY_URL,
        "Accept": "application/json, text/plain, */*",
    })
    return session


def _get(session: requests.Session, url: str, *, params=None, timeout: int):
    """带 SSL 降级的 GET：默认校验证书；证书链异常时自动重试一次。

    部分本地环境缺少根证书会导致校验证书失败（该站点证书链不完整）。
    读取的是公开净值数据，降级仅影响证书校验、不影响数据内容，
    可通过 NAV_SKIP_SSL_VERIFY=1 显式关闭校验。
    """
    verify = os.getenv("NAV_SKIP_SSL_VERIFY", "0").strip().lower() not in {"1", "true", "yes"}
    try:
        return session.get(url, params=params, timeout=timeout, verify=verify)
    except requests.exceptions.SSLError:
        if not verify:
            raise
        print("[warn] SSL 证书校验失败，降级为不校验并重试一次（仅用于读取公开净值数据）")
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        return session.get(url, params=params, timeout=timeout, verify=False)


def _fetch_from_json(session: requests.Session, timeout: int) -> Dict[date, float]:
    """主数据源：官网图表使用的全量净值 JSON 接口"""
    resp = _get(
        session,
        JSON_API,
        params={"fundCode": FUND_CODE, "dateType": "all"},
        timeout=timeout,
    )
    resp.raise_for_status()
    # 接口响应带 UTF-8 BOM，必须用 utf-8-sig 解码
    payload = json.loads(resp.content.decode("utf-8-sig"))
    if str(payload.get("code")) != "1":
        raise ValueError(f"接口返回异常 code={payload.get('code')}")
    rows = payload.get("data") or []
    out: Dict[date, float] = {}
    for item in rows:
        day_raw, val_raw = item.get("gzrq"), item.get("jjsy")
        if not day_raw or val_raw in (None, "", "--"):
            continue
        try:
            day = date.fromisoformat(str(day_raw).strip()[:10])
            value = float(val_raw)
        except (ValueError, TypeError):
            continue
        if value <= 0:
            continue
        # 接口数据倒序且存在重复日期（如 2026-10-30 出现两次），保留首次出现的值
        out.setdefault(day, value)
    if not out:
        raise ValueError("JSON 接口未返回有效净值")
    return out


def _fetch_from_html(session: requests.Session, timeout: int, pages: int = 3) -> Dict[date, float]:
    """备用数据源：解析「更多净值」列表页 HTML 表格"""
    row_re = re.compile(
        r"<tr>\s*<td>\s*(\d{4}-\d{2}-\d{2})\s*</td>\s*"
        r"<td>\s*([\d.]+)\s*</td>\s*<td>\s*([\d.]+)\s*</td>",
        re.S,
    )
    out: Dict[date, float] = {}
    for page in range(1, pages + 1):
        resp = _get(
            session,
            HISTORY_URL,
            params={"page": page},
            timeout=timeout,
        )
        resp.raise_for_status()
        resp.encoding = "utf-8"
        for day_raw, unit_raw, _accum_raw in row_re.findall(resp.text):
            try:
                day = date.fromisoformat(day_raw)
                value = float(unit_raw)
            except ValueError:
                continue
            if value <= 0:
                continue
            out.setdefault(day, value)
    if not out:
        raise ValueError("HTML 列表页未解析到净值")
    return out


def fetch_nav_map(timeout: int = TIMEOUT_SECONDS) -> Tuple[Dict[date, float], str]:
    """获取净值映射，主接口失败时自动降级到 HTML 列表页"""
    session = _build_session()
    errors: List[str] = []
    for label, fetcher in (
        ("JSON 全量接口", _fetch_from_json),
        ("历史净值列表页", _fetch_from_html),
    ):
        try:
            return fetcher(session, timeout), label
        except Exception as exc:  # noqa: BLE001 - 需降级到备用源
            errors.append(f"{label}: {exc}")
    raise RuntimeError("净值数据获取失败 → " + " | ".join(errors))


def to_points(nav_map: Dict[date, float]) -> List[NavPoint]:
    return [NavPoint(d, v) for d, v in sorted(nav_map.items()) if v > 0]


# ---------------------------------------------------------------------------
# 周五逻辑
# ---------------------------------------------------------------------------

def resolve_target_friday(today: date, override: Optional[str] = None) -> date:
    """确定本次播报归属的周五：最近一个已到来的周五（今天若是周五则取今天）"""
    if override:
        return date.fromisoformat(override)
    return today - timedelta(days=(today.weekday() - 4) % 7)


def _pct(cur: float, prev: float) -> float:
    return (cur - prev) / prev * 100.0 if prev > 0 else 0.0


def build_report(points: List[NavPoint], target_friday: date) -> FridayReport:
    """计算目标周五净值相对上一净值日的涨跌幅"""
    nav_map = {p.day: p.value for p in points}
    dates = [p.day for p in points]
    latest = dates[-1]
    week_start = target_friday - timedelta(days=4)  # 同一周的周一
    week_points = [p for p in points if week_start <= p.day <= target_friday]

    report = FridayReport(target_friday=target_friday, status="NO_DATA")

    # 1) 目标周五净值已公布 → 正常播报
    # 2) 数据已越过该周五（说明该周五确无净值，节假日）→ 采用当周最后一个净值日
    # 3) 数据尚未更新到该周五（T+1 披露）→ 等待下一次触发补发，不发送
    if target_friday in nav_map:
        nav_date = target_friday
        report.status = "OK"
    elif latest > target_friday:
        if not week_points:
            report.status = "NO_DATA"
            report.note = f"{target_friday} 所在周无任何净值记录（节假日），本次跳过。"
            return report
        nav_date = week_points[-1].day
        report.status = "SUBSTITUTE"
        report.note = (
            f"{target_friday.isoformat()} 为非净值公布日（节假日或暂停披露），"
            f"已改用当周最后一个净值日 {nav_date.isoformat()} 作为周五代表值。"
        )
    else:
        report.status = "PENDING"
        report.note = (
            f"截至最新净值日 {latest.isoformat()}，{target_friday.isoformat()} 的净值尚未公布"
            f"（银行理财通常为 T+1 披露），本次跳过，等待下次触发补发。"
        )
        return report

    idx = dates.index(nav_date)
    if idx == 0:
        report.status = "NO_DATA"
        report.note = f"{nav_date.isoformat()} 之前没有可用的对比净值，无法计算涨跌幅。"
        return report

    nav_value = nav_map[nav_date]
    base_date = dates[idx - 1]
    base_value = nav_map[base_date]

    report.nav_date = nav_date
    report.nav_value = nav_value
    report.base_date = base_date
    report.base_value = base_value
    report.change_value = nav_value - base_value
    report.change_pct = _pct(nav_value, base_value)

    # 本周累计涨跌：以「周一之前的最后一个净值日」为基准
    if week_points:
        first_week_day = week_points[0].day
        w_idx = dates.index(first_week_day)
        if w_idx > 0:
            report.week_base_date = dates[w_idx - 1]
            report.week_base_value = nav_map[dates[w_idx - 1]]
            report.week_change_pct = _pct(nav_value, report.week_base_value)

    def to_rows(src: List[NavPoint]) -> List[NavRow]:
        rows: List[NavRow] = []
        for p in src:
            i = dates.index(p.day)
            chg = _pct(p.value, nav_map[dates[i - 1]]) if i > 0 else None
            rows.append(NavRow(day=p.day, value=p.value, change_pct=chg))
        return rows

    report.recent = to_rows(points[max(0, idx - 7): idx + 1])
    report.week_rows = to_rows(week_points)
    return report


# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------

def _color_and_arrow(pct: Optional[float]) -> Tuple[str, str]:
    if pct is None:
        return COLOR_FLAT, "—"
    if pct > 0:
        return COLOR_UP, "▲"
    if pct < 0:
        return COLOR_DOWN, "▼"
    return COLOR_FLAT, "—"


def _fmt_pct(pct: Optional[float]) -> str:
    return "—" if pct is None else f"{pct:+.4f}%"


def _fmt_value(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.6f}"


def build_email_html(report: FridayReport, source_name: str) -> str:
    """生成与项目现有邮件风格一致的 HTML 报告"""
    pct = report.change_pct or 0.0
    color, arrow = _color_and_arrow(report.change_pct)
    safe_name = html_lib.escape(PRODUCT_NAME)
    safe_note = html_lib.escape(report.note)
    weekday_cn = "一二三四五六日"[report.target_friday.weekday()]

    change_color, _ = _color_and_arrow(report.change_value)
    change_text = (
        f"{report.change_value:+.6f}" if report.change_value is not None else "—"
    )

    # 本周 / 近 8 个净值日表格
    def table_rows(rows: List[NavRow]) -> str:
        out = ""
        for r in rows:
            c, a = _color_and_arrow(r.change_pct)
            out += f"""
            <tr>
              <td style="padding:9px 14px;border-bottom:1px solid #f3f4f6;color:#374151;font-size:13px;">{r.day.isoformat()}</td>
              <td style="padding:9px 14px;border-bottom:1px solid #f3f4f6;text-align:right;color:#1e293b;font-size:13px;font-weight:600;">{r.value:.6f}</td>
              <td style="padding:9px 14px;border-bottom:1px solid #f3f4f6;text-align:right;color:{c};font-size:13px;font-weight:600;">{a} {_fmt_pct(r.change_pct)}</td>
            </tr>"""
        return out

    week_chg_html = ""
    if report.week_change_pct is not None and report.week_base_date:
        wc, wa = _color_and_arrow(report.week_change_pct)
        week_chg_html = f"""
        <tr><td style="padding:0 40px 8px;">
          <p style="margin:0;font-size:13px;color:#6b7280;">
            本周累计（对比 {report.week_base_date.isoformat()} 净值 {_fmt_value(report.week_base_value)}）：
            <b style="color:{wc};">{wa} {_fmt_pct(report.week_change_pct)}</b>
          </p>
        </td></tr>"""

    note_html = ""
    if safe_note:
        note_html = f"""
        <tr><td style="padding:0 40px 20px;">
          <div style="padding:12px 16px;border-radius:10px;background:#fffbeb;border-left:4px solid #f59e0b;color:#92400e;font-size:13px;line-height:1.6;">
            ⚠️ {safe_note}
          </div>
        </td></tr>"""

    now_text = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

    return f"""
<html>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:24px 0;">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

  <tr><td style="background:linear-gradient(135deg,#1e293b,#334155);padding:30px 40px;text-align:center;">
    <p style="margin:0;font-size:13px;color:#94a3b8;letter-spacing:1px;">WEEKLY NAV REPORT</p>
    <h1 style="margin:8px 0 0;color:#ffffff;font-size:22px;font-weight:700;">📈 每周五基金净值播报</h1>
    <p style="margin:10px 0 0;color:#cbd5e1;font-size:14px;">{safe_name}</p>
    <p style="margin:4px 0 0;color:#94a3b8;font-size:12px;">产品代码 {FUND_CODE} · 归属周 {report.target_friday.isoformat()}（周{weekday_cn}）</p>
  </td></tr>

  <tr><td style="padding:28px 40px 8px;text-align:center;">
    <p style="margin:0;font-size:13px;color:#6b7280;letter-spacing:0.5px;">单位净值（{report.nav_date.isoformat() if report.nav_date else '—'}）</p>
    <p style="margin:6px 0 0;font-size:44px;font-weight:800;color:#1e293b;line-height:1.1;">{_fmt_value(report.nav_value)}</p>
    <p style="margin:12px 0 0;">
      <span style="display:inline-block;padding:8px 20px;border-radius:10px;background:{color};color:#ffffff;font-size:22px;font-weight:800;letter-spacing:0.5px;">
        {arrow} {_fmt_pct(report.change_pct)}
      </span>
    </p>
    <p style="margin:8px 0 0;font-size:13px;color:{change_color};font-weight:600;">较上一净值日 {change_text}</p>
  </td></tr>

  <tr><td style="padding:24px 40px 20px;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border-radius:12px;">
      <tr>
        <td width="50%" style="padding:16px 20px;text-align:center;border-right:1px solid #e5e7eb;">
          <p style="margin:0;font-size:12px;color:#6b7280;">对比基准日（上一净值公布日）</p>
          <p style="margin:6px 0 0;font-size:16px;font-weight:700;color:#1e293b;">{report.base_date.isoformat() if report.base_date else '—'}</p>
          <p style="margin:4px 0 0;font-size:13px;color:#6b7280;">净值 {_fmt_value(report.base_value)}</p>
        </td>
        <td width="50%" style="padding:16px 20px;text-align:center;">
          <p style="margin:0;font-size:12px;color:#6b7280;">本次净值日</p>
          <p style="margin:6px 0 0;font-size:16px;font-weight:700;color:#1e293b;">{report.nav_date.isoformat() if report.nav_date else '—'}</p>
          <p style="margin:4px 0 0;font-size:13px;color:#6b7280;">净值 {_fmt_value(report.nav_value)}</p>
        </td>
      </tr>
    </table>
    <p style="margin:10px 0 0;font-size:12px;color:#9ca3af;line-height:1.6;">
      说明：银行理财净值仅在净值公布日更新（工作日，遇节假日顺延），因此「前一天」按<strong>上一个净值公布日</strong>计算，而非自然日前一天。
    </p>
  </td></tr>

  {note_html}
  {week_chg_html}

  <tr><td style="padding:8px 40px 24px;">
    <h2 style="margin:0 0 12px;font-size:15px;color:#1e293b;border-bottom:2px solid #e5e7eb;padding-bottom:8px;">📅 本周净值明细</h2>
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#ffffff;">
      <tr style="background:#f8fafc;">
        <th style="padding:8px 14px;text-align:left;font-size:12px;color:#6b7280;border-bottom:2px solid #e5e7eb;">净值日期</th>
        <th style="padding:8px 14px;text-align:right;font-size:12px;color:#6b7280;border-bottom:2px solid #e5e7eb;">单位净值</th>
        <th style="padding:8px 14px;text-align:right;font-size:12px;color:#6b7280;border-bottom:2px solid #e5e7eb;">日涨跌</th>
      </tr>
      {table_rows(report.week_rows)}
    </table>
  </td></tr>

  <tr><td style="padding:0 40px 28px;">
    <h2 style="margin:0 0 12px;font-size:15px;color:#1e293b;border-bottom:2px solid #e5e7eb;padding-bottom:8px;">📊 近 8 个净值日走势</h2>
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#ffffff;">
      <tr style="background:#f8fafc;">
        <th style="padding:8px 14px;text-align:left;font-size:12px;color:#6b7280;border-bottom:2px solid #e5e7eb;">净值日期</th>
        <th style="padding:8px 14px;text-align:right;font-size:12px;color:#6b7280;border-bottom:2px solid #e5e7eb;">单位净值</th>
        <th style="padding:8px 14px;text-align:right;font-size:12px;color:#6b7280;border-bottom:2px solid #e5e7eb;">日涨跌</th>
      </tr>
      {table_rows(report.recent)}
    </table>
  </td></tr>

  <tr><td style="padding:18px 40px;background:#f8fafc;text-align:center;border-top:1px solid #e5e7eb;">
    <p style="margin:0;font-size:12px;color:#6b7280;">
      数据来源：<a href="{DISPLAY_URL}" style="color:#2563eb;text-decoration:none;">徽银理财官网产品净值页</a>（{html_lib.escape(source_name)}）
    </p>
    <p style="margin:6px 0 0;font-size:12px;color:#9ca3af;">⚠️ 本邮件由自动化脚本生成，净值以管理人官方披露为准，仅供参考，不构成投资建议。</p>
    <p style="margin:4px 0 0;font-size:11px;color:#cbd5e1;">生成时间 {now_text} · Gold Price Monitor / Fund NAV Report</p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>""".strip()


def build_subject(report: FridayReport) -> str:
    arrow = "▲" if (report.change_pct or 0) > 0 else ("▼" if (report.change_pct or 0) < 0 else "—")
    return (
        f"📈 周五净值播报 {report.target_friday.isoformat()} | "
        f"{FUND_CODE} 净值 {_fmt_value(report.nav_value)} {arrow} {_fmt_pct(report.change_pct)}"
    )


# ---------------------------------------------------------------------------
# 状态（防止同一周五重复发送）
# ---------------------------------------------------------------------------

def load_state() -> Dict:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: Dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def record_sent(state: Dict, report: FridayReport) -> None:
    now = datetime.now(BEIJING_TZ).isoformat(sep=" ", timespec="seconds")
    state["last_sent_friday"] = report.target_friday.isoformat()
    state["last_sent_nav_date"] = report.nav_date.isoformat() if report.nav_date else None
    state["last_sent_nav_value"] = report.nav_value
    state["last_sent_change_pct"] = report.change_pct
    state["last_sent_at"] = now
    history = state.get("history") or []
    history.append({
        "friday": report.target_friday.isoformat(),
        "nav_date": report.nav_date.isoformat() if report.nav_date else None,
        "nav_value": report.nav_value,
        "change_pct": report.change_pct,
        "sent_at": now,
    })
    state["history"] = history[-20:]
    save_state(state)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="基金净值周五播报")
    parser.add_argument("--dry-run", action="store_true", help="只生成报告与 HTML 预览，不发送邮件、不更新状态")
    parser.add_argument("--force", action="store_true", help="忽略防重复记录，强制发送")
    parser.add_argument("--check", action="store_true", help="只检查净值是否已公布，不发送邮件")
    parser.add_argument("--date", help="指定目标周五（YYYY-MM-DD），用于补发或测试")
    args = parser.parse_args()

    if not ENABLE_FUND_NAV_NOTIFY:
        print("[info] 基金净值播报已关闭（fund_nav_notify.py 中 ENABLE_FUND_NAV_NOTIFY = False）。")
        return 0

    today = datetime.now(BEIJING_TZ).date()
    target = resolve_target_friday(today, args.date)
    print(f"[info] 运行日 {today.isoformat()}，目标周五 {target.isoformat()}")

    try:
        nav_map, source_name = fetch_nav_map()
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 净值数据获取失败：{exc}")
        return 1

    points = to_points(nav_map)
    if not points:
        print("[error] 清洗后无有效净值数据")
        return 1
    print(f"[info] 数据源：{source_name}；有效净值 {len(points)} 条；"
          f"最新 {points[-1].day.isoformat()} = {points[-1].value:.6f}")

    report = build_report(points, target)

    if report.status in ("PENDING", "NO_DATA"):
        print(f"[info] 状态 {report.status}：{report.note}")
        return 0

    print(f"[info] 状态 {report.status}；净值日 {report.nav_date} = {report.nav_value:.6f}；"
          f"基准日 {report.base_date} = {report.base_value:.6f}；"
          f"涨跌 {report.change_value:+.6f}（{_fmt_pct(report.change_pct)}）")
    if args.check:
        print("[info] --check 模式，不发送邮件。")
        return 0

    state = load_state()
    already = state.get("last_sent_friday") == target.isoformat()
    if already and not args.force and not args.dry_run:
        print(f"[info] 周五 {target.isoformat()} 已推送过（{state.get('last_sent_at')}），跳过，避免重复打扰。")
        return 0

    body = build_email_html(report, source_name)
    subject = build_subject(report)

    if args.dry_run:
        PREVIEW_PATH.write_text(body, encoding="utf-8")
        print(f"[dry-run] 已生成邮件预览：{PREVIEW_PATH}")
        print(f"[dry-run] 邮件主题：{subject}")
        print(f"[dry-run] 本周净值行数 {len(report.week_rows)}，近 8 日行数 {len(report.recent)}")
        print("[dry-run] 未发送邮件、未写入状态文件。")
        return 0

    email_to = os.getenv("EMAIL_TO", "").strip()
    if not email_to:
        print("[warn] EMAIL_TO 为空，跳过发送")
        return 1

    ok = send_resend_email(subject, body, email_to, sender_name="基金净值播报")
    if not ok:
        print("[error] 邮件发送失败，状态文件不更新，下次触发将自动重试。")
        return 1

    record_sent(state, report)
    print(f"[ok] 周五净值播报已发送，状态已记录：{STATE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
