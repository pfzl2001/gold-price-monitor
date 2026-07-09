"""
金价买入策略分析工具
基于多种技术指标判断当前是否适合买入

策略说明:
1. 价格阈值策略 - 低于阈值（如1000元）就买入（硬性条件）
2. 目标价策略 - 低于设定目标价就买入（理想价位）
3. 日跌幅策略 - 当日跌幅超过阈值可能超卖
4. 均线策略 - 价格低于N日均线时买入
5. RSI策略 - RSI低于30认为超卖
6. 回调策略 - 从近期高点回调一定比例时买入
"""
from __future__ import annotations

import json
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any
from enum import Enum
from zoneinfo import ZoneInfo

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

# 复用 monitor.py 的金价获取逻辑
from monitor import fetch_gold_price, GoldPriceResult, _now_in_timezone, DEFAULT_TZ

# ================= 全局个性化设置 =================
# 您的实际黄金持仓成本价（元/克），支持浮点数（如 558.5）。如果未买入，请设置为 None 或 0.0
CZB_GOLD_COST = None

# 是否启用邮件通知。如果设为 False，系统将完全不会发送任何提醒邮件
ENABLE_EMAIL_NOTIFICATION = True
# ==================================================


def send_resend_email(subject: str, html_content: str, receiver_email: str = "1697669486@qq.com") -> bool:
    """通用邮件发送函数（Resend SMTP）"""
    # 1. 邮件配置，优先使用环境变量，其次使用硬编码默认值
    smtp_host = os.getenv("SMTP_HOST", "smtp.resend.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465").strip())
    smtp_user = os.getenv("SMTP_USER", "resend").strip()
    smtp_pass = os.getenv("SMTP_PASS", "re_eGN8AvLy_CrG71aGKzfZfChNenTZwiprF").strip()
    sender_email = os.getenv("SENDER_EMAIL", "noreply@mail.602020.xyz").strip()
    
    # 2. 构建邮件内容
    msg = MIMEMultipart('alternative')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = f"金价监控助手 <{sender_email}>"
    msg['To'] = receiver_email
    
    # 根据内容特征自动决定使用 html 还是 plain
    subtype = 'html' if '<html>' in html_content or '<body' in html_content or '<br' in html_content else 'plain'
    msg.attach(MIMEText(html_content, subtype, 'utf-8'))
    
    # 支持逗号分隔的多个收件人
    recipients = [addr.strip() for addr in receiver_email.split(",") if addr.strip()]
    
    # 3. 发送邮件
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(sender_email, recipients, msg.as_string())
        print(f"[alert] 📧 Resend 邮件已发送到: {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"[error] Resend 邮件发送失败: {e}")
        return False

LOG_FILE_PATH = Path(__file__).with_name("monitor.log")
STRATEGY_CONFIG_PATH = Path(__file__).with_name("strategy_config.json")
NOTIFY_STATE_PATH = Path(__file__).with_name("buy_notify_state.json")

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _as_percent(value: float) -> float:
    """Normalize raisePercent into percentage units.

    JD's API returns raisePercent as a ratio (e.g. -0.005 = -0.5%).
    Historical logs may also contain already-percent values depending on data sources.
    """
    v = float(value)
    return v * 100.0 if abs(v) <= 1.0 else v


class Signal(Enum):
    """买入信号强度"""
    STRONG_BUY = "🟢 强烈买入"
    BUY = "🟡 建议买入"
    HOLD = "⚪ 观望等待"
    AVOID = "🔴 暂不建议"


@dataclass
class StrategyResult:
    """单个策略的分析结果"""
    name: str
    signal: Signal
    reason: str
    weight: float = 1.0  # 策略权重


@dataclass
class PriceRecord:
    """价格记录"""
    timestamp: datetime
    price: float
    raise_percent: float


@dataclass
class BuyStrategyConfig:
    """买入策略配置"""
    # 价格阈值策略（硬性条件）
    price_threshold: float = 1000.0  # 低于此价格就提醒买入（元/克）

    # 目标价策略（理想买入价）
    target_price: float = 1015.0  # 目标买入价格（元/克）

    # 日跌幅策略
    daily_drop_threshold: float = -0.3  # 日跌幅阈值（%）

    # 均线策略
    intraday_ma_periods: List[int] = field(default_factory=lambda: [5, 10, 20])
    daily_ma_periods: List[int] = field(default_factory=lambda: [5, 10, 20])

    # RSI策略
    rsi_period: int = 14  # RSI计算周期
    rsi_oversold: float = 30.0  # 超卖阈值
    rsi_overbought: float = 70.0  # 超买阈值

    # 回调策略
    pullback_threshold: float = 2.0  # 从高点回调比例（%）
    lookback_days: int = 7  # 查看最近N天的高点
    
    # 布林带策略
    bollinger_period: int = 20  # 布林带周期
    bollinger_std: float = 2.0  # 标准差倍数
    
    # 连续下跌策略
    consecutive_drop_days: int = 3  # 连续下跌天数阈值

    # 实际持仓成本（从环境变量/仓库变量读取，如果设置了的话）
    actual_cost: Optional[float] = None
    actual_cost_take_profit_pct: float = 5.0    # 实际持仓止盈涨幅门槛 (%)
    actual_cost_stop_loss_pct: float = 3.0      # 实际持仓止损跌幅门槛 (%)
    actual_cost_average_down_pct: float = 1.5    # 实际持仓补仓跌幅门槛 (%)

    @classmethod
    def load(cls, path: Path = STRATEGY_CONFIG_PATH) -> "BuyStrategyConfig":
        """从配置文件加载"""
        config = cls()
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                # 过滤掉不存在的字段以防报错
                valid_data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                config = cls(**valid_data)
            except Exception as e:
                print(f"[warn] 加载配置失败: {e}, 使用默认配置")
        
        # 始终从文件头部的全局配置变量读取实际持仓成本
        global CZB_GOLD_COST
        if CZB_GOLD_COST is not None and CZB_GOLD_COST > 0:
            config.actual_cost = float(CZB_GOLD_COST)
            print(f"[info] 已成功从文件头部全局变量加载实际持仓成本: {config.actual_cost} 元/克")
        else:
            config.actual_cost = None
        return config

    def save(self, path: Path = STRATEGY_CONFIG_PATH) -> None:
        """保存配置到文件"""
        data = {
            "price_threshold": self.price_threshold,
            "target_price": self.target_price,
            "daily_drop_threshold": self.daily_drop_threshold,
            "intraday_ma_periods": self.intraday_ma_periods,
            "daily_ma_periods": self.daily_ma_periods,
            "rsi_period": self.rsi_period,
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "pullback_threshold": self.pullback_threshold,
            "lookback_days": self.lookback_days,
            "bollinger_period": self.bollinger_period,
            "bollinger_std": self.bollinger_std,
            "consecutive_drop_days": self.consecutive_drop_days,
            "actual_cost_take_profit_pct": self.actual_cost_take_profit_pct,
            "actual_cost_stop_loss_pct": self.actual_cost_stop_loss_pct,
            "actual_cost_average_down_pct": self.actual_cost_average_down_pct,
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def load_price_history(log_path: Path = LOG_FILE_PATH) -> List[PriceRecord]:
    """从日志文件加载历史价格数据"""
    records = []
    if not log_path.exists():
        return records

    max_lines = int(os.getenv("CZB_HISTORY_MAX_LINES", "5000").strip() or "5000")
    with log_path.open("r", encoding="utf-8") as f:
        lines = deque(f, maxlen=max_lines)

    for line in lines:
            line = line.strip()
            if not line or line.startswith("["):  # 跳过空行和错误日志
                continue

            parts = [line]
            if "}{" in line:
                parts = line.replace("}{", "}\n{").splitlines()

            for part in parts:
                part = part.strip()
                if not part:
                    continue
                try:
                    data = json.loads(part)
                    timestamp_str = data.get("timestamp", "")
                    price = data.get("lastPrice")
                    raise_pct = data.get("raisePercent", 0.0)

                    if timestamp_str and price is not None:
                        timestamp = datetime.fromisoformat(timestamp_str)
                        price_value = float(price)
                        if price_value <= 0:
                            continue
                        records.append(
                            PriceRecord(
                                timestamp=timestamp,
                                price=price_value,
                                raise_percent=_as_percent(float(raise_pct)),
                            )
                        )
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue

    # 按时间排序
    records.sort(key=lambda x: x.timestamp)
    return records


def calculate_ma(prices: List[float], period: int) -> Optional[float]:
    """计算移动平均线"""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """
    计算RSI指标（使用标准 Wilder 平滑方法）
    RSI = 100 - 100/(1 + RS)
    RS = 平均上涨幅度 / 平均下跌幅度
    使用 EMA（指数移动平均）而非简单平均，更符合标准 RSI 定义
    """
    if len(prices) < period + 1:
        return None

    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    
    # 初始平均值（前 period 个变化的简单平均）
    initial_gains = [max(c, 0) for c in changes[:period]]
    initial_losses = [max(-c, 0) for c in changes[:period]]
    
    avg_gain = sum(initial_gains) / period
    avg_loss = sum(initial_losses) / period
    
    # 使用 Wilder 平滑（EMA with alpha = 1/period）
    for c in changes[period:]:
        gain = max(c, 0)
        loss = max(-c, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_bollinger_bands(
    prices: List[float], period: int = 20, num_std: float = 2.0
) -> Optional[Dict[str, float]]:
    """
    计算布林带
    中轨 = N日移动平均线
    上轨 = 中轨 + K * N日标准差
    下轨 = 中轨 - K * N日标准差
    """
    if len(prices) < period:
        return None
    
    recent = prices[-period:]
    middle = sum(recent) / period
    
    # 计算标准差
    variance = sum((p - middle) ** 2 for p in recent) / period
    std_dev = variance ** 0.5
    
    upper = middle + num_std * std_dev
    lower = middle - num_std * std_dev
    
    return {
        "upper": upper,
        "middle": middle,
        "lower": lower,
        "std_dev": std_dev,
        "bandwidth": (upper - lower) / middle * 100  # 带宽百分比
    }


def calculate_sample_std(values: List[float]) -> float:
    n = len(values)
    if n <= 1:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return variance ** 0.5


def calculate_bollinger_bands_sample(
    prices: List[float], period: int = 20, num_std: float = 2.0
) -> Optional[Dict[str, float]]:
    if len(prices) < period:
        return None
    recent = prices[-period:]
    middle = sum(recent) / period
    std_dev = calculate_sample_std(recent)
    upper = middle + num_std * std_dev
    lower = middle - num_std * std_dev
    return {
        "upper": upper,
        "middle": middle,
        "lower": lower,
        "std_dev": std_dev,
    }


def calculate_simple_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    recent_changes = changes[-period:]
    gains = [max(c, 0) for c in recent_changes]
    losses = [max(-c, 0) for c in recent_changes]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / (avg_loss + 1e-9)
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_atr(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    tr = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    return sum(tr[-period:]) / period


def _to_tz_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=ZoneInfo(DEFAULT_TZ))
    return ts


def _floor_time_by_minutes(ts: datetime, freq_minutes: int) -> datetime:
    if freq_minutes <= 0:
        raise ValueError("freq_minutes must be positive")

    if ts.tzinfo is None:
        base = ts.replace(second=0, microsecond=0)
        total_minutes = base.hour * 60 + base.minute
        delta_minutes = total_minutes % freq_minutes
        return base - timedelta(minutes=delta_minutes)

    ts_utc = ts.astimezone(timezone.utc)
    bucket_seconds = (int(ts_utc.timestamp()) // (freq_minutes * 60)) * (freq_minutes * 60)
    return datetime.fromtimestamp(bucket_seconds, tz=timezone.utc).astimezone(ts.tzinfo)


def resample_prices_ffill(
    records: List[PriceRecord],
    *,
    freq_minutes: int = 8,
    max_steps: int = 5000,
) -> List[float]:
    if not records:
        return []

    normalized = [
        PriceRecord(timestamp=_to_tz_aware(r.timestamp), price=r.price, raise_percent=r.raise_percent)
        for r in records
        if r.price > 0
    ]
    if not normalized:
        return []

    normalized.sort(key=lambda r: r.timestamp)

    bucket_to_price: Dict[datetime, float] = {}
    for r in normalized:
        bucket = _floor_time_by_minutes(r.timestamp, freq_minutes)
        bucket_to_price[bucket] = r.price

    start_bucket = _floor_time_by_minutes(normalized[0].timestamp, freq_minutes)
    end_bucket = _floor_time_by_minutes(normalized[-1].timestamp, freq_minutes)

    if max_steps <= 0:
        max_steps = 1

    capped_start = end_bucket - timedelta(minutes=freq_minutes * (max_steps - 1))
    if capped_start > start_bucket:
        start_bucket = capped_start

    out: List[float] = []
    cursor = start_bucket
    last_price: Optional[float] = None
    step = timedelta(minutes=freq_minutes)
    while cursor <= end_bucket:
        if cursor in bucket_to_price:
            last_price = bucket_to_price[cursor]
        if last_price is not None:
            out.append(last_price)
        cursor = cursor + step

    return out


class BuyStrategyAnalyzer:
    """买入策略分析器"""

    def __init__(self, config: Optional[BuyStrategyConfig] = None):
        self.config = config or BuyStrategyConfig.load()
        self.history: List[PriceRecord] = []
        self.current: Optional[GoldPriceResult] = None

    def load_data(self) -> None:
        """加载历史数据和当前价格"""
        self.history = load_price_history()
        try:
            self.current = fetch_gold_price()
        except Exception as e:
            print(f"[error] 获取当前价格失败: {e}")
            self.current = None

    def get_current_raise_percent(self) -> Optional[float]:
        if not self.current:
            return None
        return _as_percent(self.current.raise_percent)

    def get_prices_list(self) -> List[float]:
        """获取价格列表（包含当前价格）"""
        records = list(self.history)
        if self.current and self.current.last_price > 0:
            now = _now_in_timezone(DEFAULT_TZ)
            records.append(
                PriceRecord(
                    timestamp=now,
                    price=float(self.current.last_price),
                    raise_percent=float(self.get_current_raise_percent() or 0.0),
                )
            )

        freq_minutes = int(os.getenv("CZB_RESAMPLE_MINUTES", "8").strip() or "8")
        max_steps = int(os.getenv("CZB_RESAMPLE_MAX_STEPS", "5000").strip() or "5000")
        return resample_prices_ffill(records, freq_minutes=freq_minutes, max_steps=max_steps)

    def strategy_price_threshold(self) -> StrategyResult:
        """价格阈值策略 - 低于阈值就买入（硬性条件）"""
        if not self.current:
            return StrategyResult(
                name="价格阈值策略",
                signal=Signal.HOLD,
                reason="无法获取当前价格",
                weight=2.0  # 权重较高，这是硬性条件
            )

        price = self.current.last_price
        threshold = self.config.price_threshold

        if price < threshold:
            return StrategyResult(
                name="价格阈值策略",
                signal=Signal.STRONG_BUY,
                reason=f"🔥 当前价 {price:.2f} < 阈值 {threshold:.2f}，满足买入条件！",
                weight=2.0
            )
        else:
            diff = price - threshold
            return StrategyResult(
                name="价格阈值策略",
                signal=Signal.HOLD,
                reason=f"当前价 {price:.2f} 高于阈值 {threshold:.2f}（差 {diff:.2f} 元）",
                weight=2.0
            )

    def strategy_target_price(self) -> StrategyResult:
        """目标价策略 - 理想买入价"""
        if not self.current:
            return StrategyResult(
                name="目标价策略",
                signal=Signal.HOLD,
                reason="无法获取当前价格"
            )

        price = self.current.last_price
        target = self.config.target_price
        diff_pct = (price - target) / target * 100

        if price <= target:
            return StrategyResult(
                name="目标价策略",
                signal=Signal.STRONG_BUY,
                reason=f"当前价 {price:.2f} ≤ 目标价 {target:.2f}，达到理想买入价位！"
            )
        elif diff_pct <= 1.0:
            return StrategyResult(
                name="目标价策略",
                signal=Signal.BUY,
                reason=f"当前价 {price:.2f} 接近目标价 {target:.2f}（高{diff_pct:.1f}%）"
            )
        elif diff_pct <= 3.0:
            return StrategyResult(
                name="目标价策略",
                signal=Signal.HOLD,
                reason=f"当前价 {price:.2f} 距目标价 {target:.2f} 还差 {diff_pct:.1f}%"
            )
        else:
            return StrategyResult(
                name="目标价策略",
                signal=Signal.AVOID,
                reason=f"当前价 {price:.2f} 远高于目标价 {target:.2f}（高{diff_pct:.1f}%）"
            )

    def strategy_daily_drop(self) -> StrategyResult:
        """日跌幅策略"""
        if not self.current:
            return StrategyResult(
                name="日跌幅策略",
                signal=Signal.HOLD,
                reason="无法获取当前价格"
            )

        raise_pct = _as_percent(self.current.raise_percent)
        threshold = self.config.daily_drop_threshold

        if raise_pct <= threshold * 2:
            return StrategyResult(
                name="日跌幅策略",
                signal=Signal.STRONG_BUY,
                reason=f"今日跌幅 {raise_pct:.2f}% 大幅下跌，可能超卖！"
            )
        elif raise_pct <= threshold:
            return StrategyResult(
                name="日跌幅策略",
                signal=Signal.BUY,
                reason=f"今日跌幅 {raise_pct:.2f}% 超过阈值 {threshold:.2f}%"
            )
        elif raise_pct < 0:
            return StrategyResult(
                name="日跌幅策略",
                signal=Signal.HOLD,
                reason=f"今日小跌 {raise_pct:.2f}%，继续观察"
            )
        else:
            return StrategyResult(
                name="日跌幅策略",
                signal=Signal.AVOID,
                reason=f"今日上涨 {raise_pct:.2f}%，不建议追高"
            )

    def get_daily_prices_list(self) -> List[float]:
        """获取每日收盘价列表（按日期排序）"""
        daily_closes = {}
        for r in self.history:
            d = r.timestamp.date()
            daily_closes[d] = r.price
            
        if self.current:
            now = _now_in_timezone(DEFAULT_TZ)
            daily_closes[now.date()] = self.current.last_price
            
        sorted_dates = sorted(daily_closes.keys())
        return [daily_closes[d] for d in sorted_dates]

    def strategy_intraday_ma(self) -> StrategyResult:
        """分时均线策略（基于固定分钟重采样数据）"""
        prices = self.get_prices_list()
        periods = self.config.intraday_ma_periods
        freq_minutes = int(os.getenv("CZB_RESAMPLE_MINUTES", "15").strip() or "15")

        if not self.current or len(prices) < min(periods):
            return StrategyResult(
                name="分时均线策略",
                signal=Signal.HOLD,
                reason=f"历史数据不足（需要至少{min(periods)}条分时记录，每{freq_minutes}分钟）"
            )

        current_price = self.current.last_price
        ma_results = {}
        below_count = 0

        for period in periods:
            ma = calculate_ma(prices, period)
            if ma is not None:
                hours = (period * freq_minutes) / 60
                label = f"MA{period}({hours:.1f}h)"
                ma_results[label] = ma
                if current_price < ma:
                    below_count += 1

        if not ma_results:
            return StrategyResult(
                name="分时均线策略",
                signal=Signal.HOLD,
                reason="无法计算均线"
            )

        ma_info = ", ".join([f"{k}={v:.2f}" for k, v in ma_results.items()])
        total = len(ma_results)

        if below_count == total:
            return StrategyResult(
                name="分时均线策略",
                signal=Signal.STRONG_BUY,
                reason=f"价格 {current_price:.2f} 低于所有分时均线！({ma_info})"
            )
        elif below_count >= total / 2:
            return StrategyResult(
                name="分时均线策略",
                signal=Signal.BUY,
                reason=f"价格 {current_price:.2f} 低于部分分时均线 ({ma_info})"
            )
        elif below_count > 0:
            return StrategyResult(
                name="分时均线策略",
                signal=Signal.HOLD,
                reason=f"价格 {current_price:.2f} 在分时均线附近 ({ma_info})"
            )
        else:
            return StrategyResult(
                name="分时均线策略",
                signal=Signal.AVOID,
                reason=f"价格 {current_price:.2f} 高于所有分时均线 ({ma_info})"
            )

    def strategy_daily_ma(self) -> StrategyResult:
        """日均线策略（基于每日收盘价）"""
        prices = self.get_daily_prices_list()
        periods = self.config.daily_ma_periods

        if not self.current or len(prices) < min(periods):
            return StrategyResult(
                name="日均线策略",
                signal=Signal.HOLD,
                reason=f"历史天数不足（需要至少{min(periods)}天记录，当前{len(prices)}天）"
            )

        current_price = self.current.last_price
        ma_results = {}
        below_count = 0

        for period in periods:
            ma = calculate_ma(prices, period)
            if ma is not None:
                ma_results[f"MA{period}d"] = ma
                if current_price < ma:
                    below_count += 1

        if not ma_results:
            return StrategyResult(
                name="日均线策略",
                signal=Signal.HOLD,
                reason="无法计算日均线"
            )

        ma_info = ", ".join([f"{k}={v:.2f}" for k, v in ma_results.items()])
        total = len(ma_results)

        if below_count == total:
            return StrategyResult(
                name="日均线策略",
                signal=Signal.STRONG_BUY,
                reason=f"价格 {current_price:.2f} 低于所有日均线！({ma_info})"
            )
        elif below_count >= total / 2:
            return StrategyResult(
                name="日均线策略",
                signal=Signal.BUY,
                reason=f"价格 {current_price:.2f} 低于部分日均线 ({ma_info})"
            )
        elif below_count > 0:
            return StrategyResult(
                name="日均线策略",
                signal=Signal.HOLD,
                reason=f"价格 {current_price:.2f} 在日均线附近 ({ma_info})"
            )
        else:
            return StrategyResult(
                name="日均线策略",
                signal=Signal.AVOID,
                reason=f"价格 {current_price:.2f} 高于所有日均线 ({ma_info})"
            )

    def strategy_rsi(self) -> StrategyResult:
        """RSI策略"""
        prices = self.get_prices_list()
        rsi = calculate_rsi(prices, self.config.rsi_period)

        if rsi is None:
            return StrategyResult(
                name="RSI策略",
                signal=Signal.HOLD,
                reason=f"历史数据不足（需要至少{self.config.rsi_period + 1}条记录）"
            )

        if rsi <= self.config.rsi_oversold:
            return StrategyResult(
                name="RSI策略",
                signal=Signal.STRONG_BUY,
                reason=f"RSI={rsi:.1f} 处于超卖区域（<{self.config.rsi_oversold}），强烈买入信号！"
            )
        elif rsi <= 40:
            return StrategyResult(
                name="RSI策略",
                signal=Signal.BUY,
                reason=f"RSI={rsi:.1f} 偏低，接近超卖区域"
            )
        elif rsi >= self.config.rsi_overbought:
            return StrategyResult(
                name="RSI策略",
                signal=Signal.AVOID,
                reason=f"RSI={rsi:.1f} 处于超买区域（>{self.config.rsi_overbought}），不建议买入"
            )
        else:
            return StrategyResult(
                name="RSI策略",
                signal=Signal.HOLD,
                reason=f"RSI={rsi:.1f} 处于中性区域"
            )

    def strategy_pullback(self) -> StrategyResult:
        """回调策略 - 从近期高点回调"""
        if not self.current:
            return StrategyResult(
                name="回调策略",
                signal=Signal.HOLD,
                reason="无法获取当前价格"
            )

        # 获取最近N天的数据
        # 使用带时区的当前时间，确保与历史记录比较时类型一致
        now = _now_in_timezone(DEFAULT_TZ)
        cutoff = now - timedelta(days=self.config.lookback_days)

        # 统一时区处理：如果历史记录是 naive，转换为 aware（假设是本地时区）
        def normalize_timestamp(ts: datetime) -> datetime:
            if ts.tzinfo is None:
                # naive datetime，假设是本地时区
                from zoneinfo import ZoneInfo
                return ts.replace(tzinfo=ZoneInfo(DEFAULT_TZ))
            return ts

        recent_records = [
            r for r in self.history
            if normalize_timestamp(r.timestamp) >= cutoff
        ]

        if len(recent_records) < 3:
            return StrategyResult(
                name="回调策略",
                signal=Signal.HOLD,
                reason=f"近{self.config.lookback_days}天数据不足"
            )

        recent_prices = [r.price for r in recent_records]
        recent_high = max(recent_prices)
        current_price = self.current.last_price
        pullback_pct = (recent_high - current_price) / recent_high * 100

        threshold = self.config.pullback_threshold

        if pullback_pct >= threshold * 2:
            return StrategyResult(
                name="回调策略",
                signal=Signal.STRONG_BUY,
                reason=f"从近{self.config.lookback_days}天高点 {recent_high:.2f} 回调 {pullback_pct:.1f}%，大幅回调！"
            )
        elif pullback_pct >= threshold:
            return StrategyResult(
                name="回调策略",
                signal=Signal.BUY,
                reason=f"从近{self.config.lookback_days}天高点 {recent_high:.2f} 回调 {pullback_pct:.1f}%"
            )
        elif pullback_pct > 0:
            return StrategyResult(
                name="回调策略",
                signal=Signal.HOLD,
                reason=f"从高点 {recent_high:.2f} 小幅回调 {pullback_pct:.1f}%"
            )
        else:
            return StrategyResult(
                name="回调策略",
                signal=Signal.AVOID,
                reason=f"当前价格 {current_price:.2f} 处于近期高位"
            )

    def strategy_bollinger_bands(self) -> StrategyResult:
        """布林带策略 - 价格触及下轨时可能超卖"""
        prices = self.get_prices_list()
        
        if not self.current:
            return StrategyResult(
                name="布林带策略",
                signal=Signal.HOLD,
                reason="无法获取当前价格"
            )
        
        bb = calculate_bollinger_bands(
            prices, 
            self.config.bollinger_period, 
            self.config.bollinger_std
        )
        
        if bb is None:
            return StrategyResult(
                name="布林带策略",
                signal=Signal.HOLD,
                reason=f"历史数据不足（需要至少{self.config.bollinger_period}条记录）"
            )
        
        current_price = self.current.last_price
        lower = bb["lower"]
        middle = bb["middle"]
        upper = bb["upper"]
        
        # 计算当前价格在布林带中的位置 (0=下轨, 0.5=中轨, 1=上轨)
        if upper != lower:
            position = (current_price - lower) / (upper - lower)
        else:
            position = 0.5
        
        if current_price <= lower:
            return StrategyResult(
                name="布林带策略",
                signal=Signal.STRONG_BUY,
                reason=f"价格 {current_price:.2f} 触及下轨 {lower:.2f}，可能超卖！（中轨={middle:.2f}）",
                weight=1.2
            )
        elif position <= 0.2:
            return StrategyResult(
                name="布林带策略",
                signal=Signal.BUY,
                reason=f"价格 {current_price:.2f} 接近下轨 {lower:.2f}（位置={position:.1%}）"
            )
        elif position >= 0.8:
            return StrategyResult(
                name="布林带策略",
                signal=Signal.AVOID,
                reason=f"价格 {current_price:.2f} 接近上轨 {upper:.2f}，可能超买（位置={position:.1%}）"
            )
        else:
            return StrategyResult(
                name="布林带策略",
                signal=Signal.HOLD,
                reason=f"价格 {current_price:.2f} 在布林带中部（下={lower:.2f}, 中={middle:.2f}, 上={upper:.2f}）"
            )

    def strategy_consecutive_drops(self) -> StrategyResult:
        """连续下跌策略 - 检测连续多日下跌的趋势"""
        daily_prices = self.get_daily_prices_list()
        
        if len(daily_prices) < 2:
            return StrategyResult(
                name="连续下跌策略",
                signal=Signal.HOLD,
                reason="历史数据不足"
            )
        
        # 计算连续下跌天数
        consecutive_drops = 0
        for i in range(len(daily_prices) - 1, 0, -1):
            if daily_prices[i] < daily_prices[i - 1]:
                consecutive_drops += 1
            else:
                break
        
        # 计算累计跌幅
        if consecutive_drops > 0 and len(daily_prices) > consecutive_drops:
            start_price = daily_prices[-(consecutive_drops + 1)]
            end_price = daily_prices[-1]
            total_drop_pct = (start_price - end_price) / start_price * 100
        else:
            total_drop_pct = 0
        
        threshold = self.config.consecutive_drop_days
        
        if consecutive_drops >= threshold * 2:
            return StrategyResult(
                name="连续下跌策略",
                signal=Signal.STRONG_BUY,
                reason=f"已连续下跌 {consecutive_drops} 天（累计跌幅 {total_drop_pct:.1f}%），可能超卖！",
                weight=1.5  # 连续大跌权重更高
            )
        elif consecutive_drops >= threshold:
            return StrategyResult(
                name="连续下跌策略",
                signal=Signal.BUY,
                reason=f"连续下跌 {consecutive_drops} 天（累计跌幅 {total_drop_pct:.1f}%），接近超卖"
            )
        elif consecutive_drops > 0:
            return StrategyResult(
                name="连续下跌策略",
                signal=Signal.HOLD,
                reason=f"小幅连跌 {consecutive_drops} 天，继续观察"
            )
        else:
            # 检查是否连续上涨
            consecutive_rises = 0
            for i in range(len(daily_prices) - 1, 0, -1):
                if daily_prices[i] > daily_prices[i - 1]:
                    consecutive_rises += 1
                else:
                    break
            
            if consecutive_rises >= threshold:
                return StrategyResult(
                    name="连续下跌策略",
                    signal=Signal.AVOID,
                    reason=f"已连续上涨 {consecutive_rises} 天，谨慎追高"
                )
            else:
                return StrategyResult(
                    name="连续下跌策略",
                    signal=Signal.HOLD,
                    reason="价格无明显连续趋势"
                )

    def analyze_actual_cost(self) -> Optional[Dict[str, Any]]:
        """分析实际持仓成本，生成止损、止盈或补仓提醒"""
        cost = self.config.actual_cost
        if cost is None or cost <= 0:
            return None

        if not self.current:
            return None

        current_price = self.current.last_price
        diff_pct = (current_price - cost) / cost * 100

        tp_threshold = self.config.actual_cost_take_profit_pct
        sl_threshold = -self.config.actual_cost_stop_loss_pct
        ad_threshold = -self.config.actual_cost_average_down_pct

        signal = "HOLD"
        reason = ""

        if diff_pct >= tp_threshold:
            signal = "TAKE_PROFIT"
            reason = f"🎉 当前金价 {current_price:.2f} 已涨超您的成本价 {cost:.2f} 达 {diff_pct:+.2f}%（触发止盈阈值 {tp_threshold:.1f}%），建议考虑分批止盈！"
        elif diff_pct <= sl_threshold:
            signal = "STOP_LOSS"
            reason = f"🚨 当前金价 {current_price:.2f} 已跌破您的成本价 {cost:.2f} 达 {diff_pct:+.2f}%（触发止损阈值 {sl_threshold:.1f}%），请注意风控！"
        elif diff_pct <= ad_threshold:
            signal = "AVERAGE_DOWN"
            reason = f"📉 当前金价 {current_price:.2f} 低于您的成本价 {cost:.2f} 达 {diff_pct:+.2f}%（达到补仓阈值 {ad_threshold:.1f}%），可考虑补仓以摊薄成本。"
        else:
            reason = f"当前金价 {current_price:.2f} 较您的成本价 {cost:.2f} 变动为 {diff_pct:+.2f}%（未触发提醒阈值：止盈+{tp_threshold:.1f}%, 止损{sl_threshold:.1f}%, 补仓{ad_threshold:.1f}%）"

        return {
            "cost": cost,
            "current_price": current_price,
            "diff_pct": diff_pct,
            "signal": signal,
            "reason": reason,
        }

    def _should_notify_actual_cost(self, analysis: Dict[str, Any]) -> bool:
        """实际成本提醒的防刷屏过滤"""
        signal = analysis["signal"]
        if signal == "HOLD":
            return False

        current_price = analysis["current_price"]
        cooldown_hours = float(os.getenv("NOTIFY_COOLDOWN_HOURS", "4").strip() or "4")
        daily_max = int(os.getenv("NOTIFY_DAILY_MAX", "3").strip() or "3")

        state = self._load_notify_state()
        cost_state = state.get("actual_cost_notify")
        if not isinstance(cost_state, dict):
            cost_state = {}

        last_at = cost_state.get("last_notified_at")
        last_price = cost_state.get("last_notified_price")
        last_sig = cost_state.get("last_notified_signal")
        notify_date = cost_state.get("notify_date")
        notify_count = int(cost_state.get("notify_count_today", 0))

        now = _now_in_timezone(DEFAULT_TZ)
        today_str = now.date().isoformat()

        # 1. 每日上限
        if notify_date == today_str and notify_count >= daily_max:
            print(f"[info] [成本策略] 今日已发送 {notify_count} 次成本提醒，达到上限 {daily_max}，跳过")
            return False

        # 2. 冷却时间
        if last_at:
            try:
                last_dt = datetime.fromisoformat(last_at.strip())
                if last_dt.tzinfo is None:
                    last_local = last_dt.replace(tzinfo=ZoneInfo(DEFAULT_TZ))
                else:
                    last_local = last_dt.astimezone(ZoneInfo(DEFAULT_TZ))
                
                time_since_last = now - last_local
                cooldown_delta = timedelta(hours=cooldown_hours)
                if time_since_last < cooldown_delta:
                    # 如果信号变化了，允许发送；如果信号相同，则触发冷却
                    if last_sig == signal:
                        remaining_min = (cooldown_delta - time_since_last).total_seconds() / 60
                        print(f"[info] [成本策略] 距上次相同成本提醒仅 {time_since_last.total_seconds()/60:.0f} 分钟，冷却中（剩余 {remaining_min:.0f} 分钟），跳过")
                        return False
            except Exception as e:
                print(f"[warn] [成本策略] 解析上次通知时间失败: {e}")

        # 3. 价格变动过滤（如果信号相同，且价格变动不明显，则不重复提醒）
        if last_sig == signal and last_price is not None:
            try:
                last_price_f = float(last_price)
                price_diff_pct = abs(current_price - last_price_f) / last_price_f * 100
                drop_pct_threshold = float(os.getenv("NOTIFY_DROP_PCT", "0.5").strip() or "0.5")
                if price_diff_pct < drop_pct_threshold:
                    print(f"[info] [成本策略] 当前价 {current_price:.2f} 较上次提醒价 {last_price_f:.2f} 变动仅 {price_diff_pct:.2f}%，低于门槛 {drop_pct_threshold}%，跳过")
                    return False
            except Exception:
                pass

        return True

    def send_actual_cost_notification(self, analysis: Dict[str, Any]) -> bool:
        """发送实际成本策略邮件提醒"""
        global ENABLE_EMAIL_NOTIFICATION
        if not ENABLE_EMAIL_NOTIFICATION:
            print("[info] [成本策略] 邮件通知已关闭，跳过发送成本策略提醒。")
            return False

        if not analysis or analysis.get("signal") == "HOLD":
            return False

        if not self._should_notify_actual_cost(analysis):
            return False

        email_to = os.getenv("EMAIL_TO", "1697669486@qq.com").strip()

        if not email_to:
            print("[warn] EMAIL_TO 为空，跳过发送")
            return False

        signal = analysis["signal"]
        current_price = analysis["current_price"]
        cost = analysis["cost"]
        diff_pct = analysis["diff_pct"]

        subject_map = {
            "TAKE_PROFIT": "💰 黄金实际持仓【建议分批止盈】",
            "STOP_LOSS": "🚨 黄金实际持仓【风控止损警报】",
            "AVERAGE_DOWN": "📉 黄金实际持仓【建议分批补仓】",
        }
        subject = f"{subject_map.get(signal, '🔔 黄金持仓动向')} - 当前 {current_price:.2f} 元/克"

        now = _now_in_timezone(DEFAULT_TZ)
        body = (
            f"🔔 您设置的实际黄金持仓成本策略已触发提醒！\n\n"
            f"📊 持仓与市价状态:\n"
            f"  - 您的成本价: {cost:.2f} 元/克\n"
            f"  - 当前最新价: {current_price:.2f} 元/克\n"
            f"  - 当前收益率: {diff_pct:+.2f}%\n\n"
            f"📢 策略分析意见:\n"
            f"  - {analysis['reason']}\n\n"
            f"⏰ 报价时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

        success = send_resend_email(subject, body, email_to)
        if success:
            # 更新状态
            today_str = now.date().isoformat()
            state = self._load_notify_state()
            cost_state = state.get("actual_cost_notify")
            if not isinstance(cost_state, dict):
                cost_state = {}

            cost_state["last_notified_price"] = float(current_price)
            cost_state["last_notified_at"] = now.isoformat(sep=" ", timespec="seconds")
            cost_state["last_notified_signal"] = signal
            if cost_state.get("notify_date") == today_str:
                cost_state["notify_count_today"] = int(cost_state.get("notify_count_today", 0)) + 1
            else:
                cost_state["notify_date"] = today_str
                cost_state["notify_count_today"] = 1

            state["actual_cost_notify"] = cost_state
            self._save_notify_state(state)
            return True
        return False

    def analyze(self) -> Dict[str, Any]:
        """执行所有策略分析"""
        self.load_data()

        results = [
            self.strategy_daily_drop(),
            self.strategy_intraday_ma(),
            self.strategy_daily_ma(),
            self.strategy_rsi(),
            self.strategy_pullback(),
            self.strategy_bollinger_bands(),
            self.strategy_consecutive_drops(),
        ]

        # 计算综合评分
        signal_scores = {
            Signal.STRONG_BUY: 2,
            Signal.BUY: 1,
            Signal.HOLD: 0,
            Signal.AVOID: -1,
        }

        total_score = sum(signal_scores[r.signal] * r.weight for r in results)
        max_score = sum(
            signal_scores[Signal.STRONG_BUY] * r.weight for r in results)

        # 综合建议
        if total_score >= max_score * 0.7:
            overall = Signal.STRONG_BUY
            advice = "多个指标显示买入信号，建议买入！"
        elif total_score >= max_score * 0.4:
            overall = Signal.BUY
            advice = "部分指标支持买入，可以考虑建仓"
        elif total_score >= 0:
            overall = Signal.HOLD
            advice = "信号不明确，建议继续观望"
        else:
            overall = Signal.AVOID
            advice = "当前不建议买入，等待更好时机"

        # 运行 8分钟高频策略
        intraday_result = self.run_intraday_bot()

        # 运行实际持仓成本策略分析
        actual_cost_result = self.analyze_actual_cost()

        raise_percent = self.get_current_raise_percent()
        return {
            "current_price": self.current.last_price if self.current else None,
            "raise_percent": raise_percent,
            "trade_time": self.current.trade_datetime if self.current else None,
            "history_count": len(self.history),
            "strategies": results,
            "total_score": total_score,
            "max_score": max_score,
            "overall_signal": overall,
            "advice": advice,
            "intraday_bot": intraday_result,
            "actual_cost_analysis": actual_cost_result,
        }

    def print_report(self) -> Dict[str, Any]:
        """打印分析报告并返回结果"""
        result = self.analyze()

        print("\n" + "=" * 60)
        print("📊 金价买入策略分析报告")
        print("=" * 60)

        now = _now_in_timezone(DEFAULT_TZ)
        print(f"⏰ 分析时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        if result["current_price"]:
            print(f"💰 当前金价: {result['current_price']:.2f} 元/克")
            print(f"📈 今日涨跌: {result['raise_percent']:+.2f}%")
            print(f"🕐 报价时间: {result['trade_time']}")
        else:
            print("❌ 无法获取当前价格")

        print(f"📚 历史数据: {result['history_count']} 条记录")
        print("-" * 60)

        print("\n📋 各策略分析结果:\n")
        for strategy in result["strategies"]:
            print(f"  {strategy.signal.value} {strategy.name}")
            print(f"     └─ {strategy.reason}")
            print()

        # 8分钟高频日内量化策略状态打印
        print("-" * 60)
        print("🤖 8分钟高频日内量化策略状态:")
        bot_res = result.get("intraday_bot", {})
        bot_sig = bot_res.get("signal", "HOLD")
        bot_info = bot_res.get("info", {})
        if bot_sig == "DATA_WARMING_UP":
            print(f"  状态: ⏳ 数据积累中 ({bot_info.get('reason')})")
        else:
            pos = bot_info.get("position_after")
            pos_label = "📈 持多仓 (LONG)" if pos == "LONG" else "⚪ 空仓 (无持仓)"
            print(f"  当前仓位: {pos_label}")
            if pos == "LONG":
                print(f"  ┌─ 买入价格: {bot_info.get('entry_price_after'):.2f} 元/克")
                print(f"  ├─ 动态止损价: {bot_info.get('stop_loss_after'):.2f} 元/克")
                gain_val = bot_info.get("price") - bot_info.get("entry_price_after")
                gain_pct = (gain_val / bot_info.get("entry_price_after") * 100) if bot_info.get("entry_price_after") > 0 else 0.0
                print(f"  └─ 当前损益: {gain_val:+.2f} 元/克 ({gain_pct:+.2f}%)")
            print(f"  当前指标: 价格={bot_info.get('price'):.2f}, RSI={bot_info.get('rsi'):.1f}, 布林下轨={bot_info.get('lower_band'):.2f}, 布林上轨={bot_info.get('upper_band'):.2f}, ATR={bot_info.get('atr'):.2f}")
            if bot_sig != "HOLD":
                print(f"  🚨 策略触发信号: {bot_sig}")

        # 实际持仓成本追踪状态打印
        print("-" * 60)
        print("📈 实际持仓成本追踪状态:")
        ac_res = result.get("actual_cost_analysis")
        if ac_res:
            print(f"  您的持仓成本: {ac_res['cost']:.2f} 元/克")
            print(f"  当前最新价格: {ac_res['current_price']:.2f} 元/克")
            print(f"  持仓浮动盈亏: {ac_res['diff_pct']:+.2f}%")
            print(f"  策略状态信号: {ac_res['signal']} ({ac_res['reason']})")
        else:
            print("  状态: ⚪ 未配置实际成本价 (可设置代码头部全局变量 CZB_GOLD_COST 启用)")

        print("\n" + "=" * 60)

        return result

    def should_notify(self, result: Dict[str, Any]) -> bool:
        """判断是否应该发送邮件通知"""
        signal = result.get("overall_signal")
        return signal in (Signal.STRONG_BUY, Signal.BUY)

    def _load_notify_state(self) -> Dict[str, Any]:
        if not NOTIFY_STATE_PATH.exists():
            return {}
        try:
            with NOTIFY_STATE_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_notify_state(self, state: Dict[str, Any]) -> None:
        NOTIFY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = NOTIFY_STATE_PATH.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp_path.replace(NOTIFY_STATE_PATH)

    def _should_notify_in_day(self, result: Dict[str, Any]) -> bool:
        """智能防刷屏通知策略

        三层过滤机制，避免一天内收到过多通知：
        1. 冷却期：距上次通知至少 NOTIFY_COOLDOWN_HOURS 小时（默认4小时）
        2. 价格跌幅门槛：当前价格比上次通知价格低至少 NOTIFY_DROP_PCT%（默认0.5%）才再次通知
        3. 每日上限：每天最多发送 NOTIFY_DAILY_MAX 次通知（默认3次）
        """
        current_price = result.get("current_price")
        if current_price is None:
            return False

        # 从环境变量读取可配置参数
        cooldown_hours = float(os.getenv("NOTIFY_COOLDOWN_HOURS", "4").strip() or "4")
        drop_pct_threshold = float(os.getenv("NOTIFY_DROP_PCT", "0.5").strip() or "0.5")
        daily_max = int(os.getenv("NOTIFY_DAILY_MAX", "3").strip() or "3")

        state = self._load_notify_state()
        last_at = state.get("last_notified_at")
        last_price = state.get("last_notified_price")
        notify_date = state.get("notify_date")  # 格式: "2026-07-01"
        notify_count = int(state.get("notify_count_today", 0))

        now = _now_in_timezone(DEFAULT_TZ)
        today_str = now.date().isoformat()

        # --- 检查1：是否有上次通知记录 ---
        if not isinstance(last_at, str) or not last_at.strip():
            return True

        try:
            last_dt = datetime.fromisoformat(last_at.strip())
        except Exception:
            return True

        try:
            tz = ZoneInfo(DEFAULT_TZ)
            if last_dt.tzinfo is None:
                last_local = last_dt.replace(tzinfo=tz)
            else:
                last_local = last_dt.astimezone(tz)
        except Exception:
            last_local = last_dt

        # --- 检查2：每日通知次数上限 ---
        if notify_date == today_str and notify_count >= daily_max:
            print(
                f"[info] 今日已发送 {notify_count} 次通知，达到上限 {daily_max}，跳过"
            )
            return False

        # --- 检查3：冷却期（距上次通知不足 cooldown_hours 小时） ---
        time_since_last = now - last_local
        cooldown_delta = timedelta(hours=cooldown_hours)
        if time_since_last < cooldown_delta:
            remaining = cooldown_delta - time_since_last
            remaining_min = remaining.total_seconds() / 60
            print(
                f"[info] 距上次通知仅 {time_since_last.total_seconds()/60:.0f} 分钟，"
                f"冷却期 {cooldown_hours}h 未到（剩余 {remaining_min:.0f} 分钟），跳过"
            )
            return False

        # --- 检查4：价格跌幅是否足够大 ---
        try:
            last_price_f = float(last_price) if last_price is not None else None
        except Exception:
            last_price_f = None

        if last_price_f is not None and last_price_f > 0:
            price_f = float(current_price)
            drop_pct = (last_price_f - price_f) / last_price_f * 100
            if drop_pct < drop_pct_threshold:
                # 价格没有显著下跌（甚至可能上涨了），跳过通知
                print(
                    f"[info] 当前价 {price_f:.2f} 较上次通知价 {last_price_f:.2f} "
                    f"变动 {-drop_pct:+.2f}%，未达到 -{drop_pct_threshold}% 门槛，跳过"
                )
                return False

        return True

    def format_email_body(self, result: Dict[str, Any]) -> str:
        """格式化邮件内容"""
        now = _now_in_timezone(DEFAULT_TZ)
        lines = [
            "=" * 50,
            "📊 金价买入策略分析 - 建议买入！",
            "=" * 50,
            "",
            f"⏰ 分析时间: {now.strftime('%Y-%m-%d %H:%M:%S')}",
            f"💰 当前金价: {result['current_price']:.2f} 元/克",
            f"📈 今日涨跌: {result['raise_percent']:+.2f}%",
            f"🕐 报价时间: {result['trade_time']}",
            "",
            "-" * 50,
            "📋 各策略分析结果:",
            "-" * 50,
        ]

        for strategy in result["strategies"]:
            lines.append(f"  {strategy.signal.value} {strategy.name}")
            lines.append(f"     └─ {strategy.reason}")
            lines.append("")

        lines.extend([
            "-" * 50,
            f"📊 综合评分: {result['total_score']:.1f} / {result['max_score']:.1f}",
            "",
            f"🎯 {result['overall_signal'].value}",
            f"   {result['advice']}",
            "=" * 50,
        ])

        return "\n".join(lines)

    def send_buy_notification(self, result: Dict[str, Any]) -> bool:
        """当建议买入时发送邮件通知"""
        global ENABLE_EMAIL_NOTIFICATION
        if not ENABLE_EMAIL_NOTIFICATION:
            print("[info] 邮件通知已关闭，跳过发送常规买入提醒。")
            return False

        if not self.should_notify(result):
            return False

        if not self._should_notify_in_day(result):
            # 具体跳过原因已在 _should_notify_in_day 中打印
            return False

        email_to = os.getenv("EMAIL_TO", "1697669486@qq.com").strip()
        email_subject = os.getenv("EMAIL_SUBJECT", "🔔 金价买入提醒").strip()

        if not email_to:
            print("[warn] EMAIL_TO 为空，跳过发送")
            return False

        # 邮件标题加上当前价格
        price = result.get("current_price", 0)
        subject = f"{email_subject} - 当前 {price:.2f} 元/克"

        body = self.format_email_body(result)

        # 使用统一的 Resend 邮件发送逻辑
        success = send_resend_email(subject, body, email_to)
        if success:
            now = _now_in_timezone(DEFAULT_TZ)
            today_str = now.date().isoformat()
            state = self._load_notify_state()
            state["last_notified_price"] = float(price)
            state["last_notified_at"] = now.isoformat(sep=" ", timespec="seconds")
            # 更新每日计数器
            if state.get("notify_date") == today_str:
                state["notify_count_today"] = int(state.get("notify_count_today", 0)) + 1
            else:
                state["notify_date"] = today_str
                state["notify_count_today"] = 1
            self._save_notify_state(state)
            return True
        return False

    def run_intraday_bot(self) -> Dict[str, Any]:
        """
        运行 8分钟日内高频布林带+RSI+ATR 量化算法
        """
        # 1. 获取价格历史并转换为 8分钟 resampling 价格
        prices = self.get_prices_list()
        
        # 确保数据量足够
        bb_window = self.config.bollinger_period  # 默认 20
        rsi_window = self.config.rsi_period        # 默认 14
        min_required = max(bb_window, rsi_window) + 5
        
        if len(prices) < min_required:
            return {
                "signal": "DATA_WARMING_UP",
                "info": {
                    "reason": f"历史数据不足，当前只有 {len(prices)} 个8分钟点位，需要至少 {min_required} 个点位。"
                }
            }

        # 2. 计算指标
        # 2.1 当前周期的指标
        latest_bb = calculate_bollinger_bands_sample(prices, period=bb_window, num_std=self.config.bollinger_std)
        latest_rsi = calculate_simple_rsi(prices, period=rsi_window)
        latest_atr = calculate_atr(prices, period=14)
        
        # 2.2 上一周期的指标 (用于买入判断)
        prev_bb = calculate_bollinger_bands_sample(prices[:-1], period=bb_window, num_std=self.config.bollinger_std)
        
        if not (latest_bb and prev_bb and latest_rsi is not None and latest_atr is not None):
            return {
                "signal": "DATA_WARMING_UP",
                "info": {
                    "reason": "指标计算失败，数据可能不完整"
                }
            }
            
        current_price = prices[-1]
        prev_price = prices[-2]
        
        # 3. 加载交易状态
        notify_state = self._load_notify_state()
        bot_state = notify_state.get("intraday_bot")
        if not isinstance(bot_state, dict):
            bot_state = {
                "position": None,
                "entry_price": 0.0,
                "stop_loss": 0.0
            }
            
        position = bot_state.get("position")
        entry_price = float(bot_state.get("entry_price", 0.0))
        stop_loss = float(bot_state.get("stop_loss", 0.0))
        
        signal = "HOLD"
        trade_time = self.current.trade_datetime if self.current else _now_in_timezone(DEFAULT_TZ).strftime("%Y-%m-%d %H:%M:%S")
        
        info = {
            "price": current_price,
            "rsi": latest_rsi,
            "lower_band": latest_bb['lower'],
            "upper_band": latest_bb['upper'],
            "middle_band": latest_bb['middle'],
            "atr": latest_atr,
            "trade_time": trade_time,
            "position_before": position,
            "entry_price_before": entry_price,
            "stop_loss_before": stop_loss
        }
        
        # 4. 交易决策逻辑
        if position is None:
            # 买入触发：前一根K线收盘在下轨下方，当前K线重新收回下轨上方，且RSI超卖（低于35）
            prev_lower = prev_bb['lower']
            latest_lower = latest_bb['lower']
            
            if prev_price < prev_lower and current_price > latest_lower and latest_rsi < 35:
                # 触发买入
                position = 'LONG'
                entry_price = current_price
                stop_loss = current_price - (1.5 * latest_atr)
                signal = 'BUY'
                
                # 更新状态
                bot_state['position'] = position
                bot_state['entry_price'] = entry_price
                bot_state['stop_loss'] = stop_loss
                notify_state['intraday_bot'] = bot_state
                self._save_notify_state(notify_state)
                
        elif position == 'LONG':
            # 止损触发：价格跌破止损线
            if current_price <= stop_loss:
                signal = 'STOP_LOSS_SELL'
                
                # 清空持仓状态
                bot_state['position'] = None
                bot_state['entry_price'] = 0.0
                bot_state['stop_loss'] = 0.0
                notify_state['intraday_bot'] = bot_state
                self._save_notify_state(notify_state)
                
            # 止盈触发：价格触及上轨，或者RSI超买（大于70）
            elif current_price >= latest_bb['upper'] or latest_rsi > 70:
                signal = 'TAKE_PROFIT_SELL'
                
                # 清空持仓状态
                bot_state['position'] = None
                bot_state['entry_price'] = 0.0
                bot_state['stop_loss'] = 0.0
                notify_state['intraday_bot'] = bot_state
                self._save_notify_state(notify_state)
                
        # 更新返回字典中的最新状态
        info["position_after"] = position
        info["entry_price_after"] = entry_price
        info["stop_loss_after"] = stop_loss
        
        return {
            "signal": signal,
            "info": info
        }

    def send_intraday_notification(self, run_result: Dict[str, Any]) -> bool:
        """
        发送高频量化策略对应的邮件通知
        """
        global ENABLE_EMAIL_NOTIFICATION
        if not ENABLE_EMAIL_NOTIFICATION:
            print("[info] 邮件通知已关闭，跳过发送高频策略提醒。")
            return False

        signal = run_result.get("signal")
        if signal in ("HOLD", "DATA_WARMING_UP"):
            return False

        # 屏蔽虚拟止损/止盈的邮件提醒，避免对于模拟交易的骚扰
        if signal in ("STOP_LOSS_SELL", "TAKE_PROFIT_SELL"):
            print(f"[info] 过滤高频量化策略虚拟平仓信号 {signal} 邮件，不发送")
            return False
            
        info = run_result.get("info", {})
        current_price = info.get("price", 0.0)
        
        email_to = os.getenv("EMAIL_TO", "1697669486@qq.com").strip()
        
        if not email_to:
            print("[warn] EMAIL_TO 为空，跳过发送")
            return False
            
        # 根据信号构建不同的邮件标题和内容
        if signal == "BUY":
            subject = f"🔔 8分钟策略【买入建议】 - 当前 {current_price:.2f} 元/克"
            body = (
                f"🔔 8分钟高频量化策略触发【买入建议】！\n\n"
                f"📊 策略指标状态:\n"
                f"  - 当前价格: {current_price:.2f} 元/克\n"
                f"  - 布林带下轨: {info['lower_band']:.2f} 元/克\n"
                f"  - RSI值: {info['rsi']:.1f}\n"
                f"  - ATR(真实波幅): {info['atr']:.2f} 元/克\n\n"
                f"🛡️ 风控设置:\n"
                f"  - 建议买入价: {current_price:.2f} 元/克\n"
                f"  - 动态止损价 (1.5倍 ATR): {info['stop_loss_after']:.2f} 元/克 (当前价 - 1.5 * ATR)\n"
                f"  - 止盈条件: 价格高于布林带上轨({info['upper_band']:.2f} 元/克) 或 RSI 超过 70\n\n"
                f"⏰ 报价时间: {info['trade_time']}\n"
            )
        elif signal == "STOP_LOSS_SELL":
            entry_p = info.get("entry_price_before", 0.0)
            stop_l = info.get("stop_loss_before", 0.0)
            loss_val = current_price - entry_p
            loss_pct = (loss_val / entry_p * 100) if entry_p > 0 else 0.0
            subject = f"🚨 8分钟策略【止损平仓】 - 当前 {current_price:.2f} 元/克"
            body = (
                f"🚨 8分钟高频量化策略触发【止损平仓】！\n\n"
                f"📊 交易状态:\n"
                f"  - 批次买入价: {entry_p:.2f} 元/克\n"
                f"  - 触发止损价: {stop_l:.2f} 元/克\n"
                f"  - 当前价格: {current_price:.2f} 元/克\n"
                f"  - 累计损益: {loss_val:+.2f} 元/克 ({loss_pct:+.2f}%)\n\n"
                f"⏰ 报价时间: {info['trade_time']}\n"
            )
        elif signal == "TAKE_PROFIT_SELL":
            entry_p = info.get("entry_price_before", 0.0)
            profit_val = current_price - entry_p
            profit_pct = (profit_val / entry_p * 100) if entry_p > 0 else 0.0
            reason_str = ""
            if current_price >= info.get("upper_band", 0.0):
                reason_str += f"当前价格 {current_price:.2f} 突破布林带上轨 {info.get('upper_band', 0.0):.2f}; "
            if info.get("rsi", 0.0) > 70:
                reason_str += f"RSI值 {info.get('rsi', 0.0):.1f} 超过 70 超买阈值; "
            subject = f"💰 8分钟策略【止盈平仓】 - 当前 {current_price:.2f} 元/克"
            body = (
                f"💰 8分钟高频量化策略触发【止盈平仓】！\n\n"
                f"📊 交易状态:\n"
                f"  - 批次买入价: {entry_p:.2f} 元/克\n"
                f"  - 当前价格: {current_price:.2f} 元/克\n"
                f"  - 累计收益: {profit_val:+.2f} 元/克 ({profit_pct:+.2f}%)\n\n"
                f"📊 触发原因:\n"
                f"  - {reason_str}\n\n"
                f"⏰ 报价时间: {info['trade_time']}\n"
            )
        else:
            return False

        # 使用统一的 Resend 邮件发送逻辑
        return send_resend_email(subject, body, email_to)


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="金价买入策略分析")
    parser.add_argument("--target", type=float, help="设置目标买入价格")
    parser.add_argument("--save-config", action="store_true", help="保存配置")
    parser.add_argument("--show-config", action="store_true", help="显示当前配置")

    args = parser.parse_args()

    config = BuyStrategyConfig.load()

    if args.target:
        config.target_price = args.target
        print(f"[info] 目标价格已设置为: {config.target_price}")

    if args.save_config:
        config.save()
        print(f"[info] 配置已保存到: {STRATEGY_CONFIG_PATH}")

    if args.show_config:
        print("\n当前策略配置:")
        print(f"  价格阈值: {config.price_threshold} (低于此价格触发买入)")
        print(f"  目标买入价: {config.target_price} (理想买入价)")
        print(f"  日跌幅阈值: {config.daily_drop_threshold}%")
        print(f"  分时均线周期: {config.intraday_ma_periods}")
        print(f"  日均线周期: {config.daily_ma_periods}")
        print(f"  RSI周期: {config.rsi_period}")
        print(f"  RSI超卖: {config.rsi_oversold}")
        print(f"  RSI超买: {config.rsi_overbought}")
        print(f"  回调阈值: {config.pullback_threshold}%")
        print(f"  回调查看天数: {config.lookback_days}")
        return

    analyzer = BuyStrategyAnalyzer(config)
    result = analyzer.print_report()

    # 如果建议买入，发送邮件通知（原有策略）
    analyzer.send_buy_notification(result)

    # 运行并发送 8分钟高频策略通知
    if "intraday_bot" in result:
        analyzer.send_intraday_notification(result["intraday_bot"])

    # 运行并发送实际持仓成本提醒通知
    if "actual_cost_analysis" in result and result["actual_cost_analysis"]:
        analyzer.send_actual_cost_notification(result["actual_cost_analysis"])


if __name__ == "__main__":
    main()
