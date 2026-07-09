# 金价监控与买入策略分析系统

[![GitHub Actions](https://github.com/pfzl2001/gold-price-monitor/actions/workflows/gold_price_monitor.yml/badge.svg)](https://github.com/pfzl2001/gold-price-monitor/actions)

基于 Python + GitHub Actions 的黄金价格自动监控与量化买入分析系统。金价数据来源于**京东金融**黄金价格接口，系统定时获取实时金价并结合多种量化技术指标自动分析买入时机，满足条件时通过邮件发送提醒通知。

## ✨ 功能特性

- 📊 **定时金价采集** — 通过京东金融接口自动获取实时金价并记录历史数据
- 🤖 **多维度策略分析** — 均线、RSI、布林带、回调、连续下跌等 7 种策略
- 📈 **8分钟高频策略** — 日内级别 Bollinger + RSI + ATR 量化交易信号
- 💰 **持仓成本追踪** — 止盈/止损/补仓智能提醒
- 📧 **邮件通知** — 通过 [Resend](https://resend.com) SMTP 发送提醒邮件
- 🔁 **GitHub Actions 自动运行** — 零成本云端定时执行

---

## 📂 项目结构

```text
gold-price-monitor/
├── monitor.py               # 金价获取与记录核心脚本
├── buy_strategy.py          # 多维度技术指标分析与邮件提醒策略
├── requirements.txt         # 项目依赖库
├── .env.example             # 环境变量配置模板
├── .github/
│   └── workflows/
│       └── gold_price_monitor.yml  # GitHub Actions 工作流
├── monitor.log              # 历史金价数据日志（运行时自动生成）
├── buy_strategy.log         # 策略分析输出日志（运行时自动生成）
└── buy_notify_state.json    # 通知状态文件（运行时自动生成）
```

---

## 🚀 快速开始

### 方式一：GitHub Actions 自动运行（推荐）

1. **Fork 本仓库**

2. **配置 Repository Secrets**

   进入你 Fork 的仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，添加以下 Secrets：

   | Secret 名称 | 必填 | 说明 |
   |-------------|------|------|
   | `SMTP_PASS` | ✅ | Resend API Key（在 [resend.com](https://resend.com) 注册获取） |
   | `SENDER_EMAIL` | ✅ | 发件人邮箱（需在 Resend 中验证域名，如 `noreply@mail.yourdomain.com`） |
   | `EMAIL_TO` | ✅ | 收件人邮箱（支持逗号分隔多个邮箱） |

3. **启用 GitHub Actions**

   Fork 后的仓库默认禁用 Actions，进入 **Actions** 标签页点击 **I understand my workflows, go ahead and enable them**。

4. **完成！** 系统会在工作日北京时间 9:00-23:00 每小时自动运行。

> [!TIP]
> 如果不需要邮件通知，可以不配置任何 Secrets，系统仍会正常采集金价并生成策略分析报告。

---

### 方式二：本地运行

1. **克隆仓库**
   ```bash
   git clone https://github.com/pfzl2001/gold-price-monitor.git
   cd gold-price-monitor
   ```

2. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

3. **配置环境变量**（可选，仅邮件通知需要）
   ```bash
   cp .env.example .env
   # 编辑 .env 文件，填入你的配置
   ```

4. **运行金价采集**
   ```bash
   python monitor.py
   ```

5. **运行策略分析**
   ```bash
   python buy_strategy.py
   ```

---

## 📧 邮件通知配置

本项目使用 [Resend](https://resend.com) 作为邮件发送服务。

### 获取 Resend API Key

1. 注册 [Resend](https://resend.com) 账号（免费额度：每月 3000 封）
2. 在 Resend 控制台中添加并验证你的域名
3. 生成 API Key

### 环境变量说明

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `SMTP_HOST` | `smtp.resend.com` | SMTP 服务器地址 |
| `SMTP_PORT` | `465` | SMTP 端口 |
| `SMTP_USER` | `resend` | SMTP 用户名 |
| `SMTP_PASS` | — | **Resend API Key**（必填） |
| `SENDER_EMAIL` | — | **发件人邮箱**（必填，需在 Resend 中验证域名） |
| `EMAIL_TO` | — | **收件人邮箱**（必填，支持逗号分隔多个邮箱） |
| `EMAIL_SUBJECT` | `🔔 金价买入提醒` | 邮件主题前缀 |

### 通知防刷屏参数

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `NOTIFY_COOLDOWN_HOURS` | `4` | 两次通知之间最少间隔（小时） |
| `NOTIFY_DROP_PCT` | `0.5` | 价格需比上次通知价低多少 % 才再次通知 |
| `NOTIFY_DAILY_MAX` | `3` | 每天最多通知次数 |

---

## ⚙️ 策略配置

### 方式一：修改 `buy_strategy.py` 文件头部

```python
# ================= 全局个性化设置 =================
# 您的实际黄金持仓成本价（元/克），支持浮点数（如 558.5）。如果未买入，请设置为 None 或 0.0
GOLD_COST = None

# 是否启用邮件通知。如果设为 False，系统将完全不会发送任何提醒邮件
ENABLE_EMAIL_NOTIFICATION = True
# ==================================================
```

- **修改持仓成本**：将 `GOLD_COST = None` 改为如 `GOLD_COST = 560.5`
- **关闭邮件通知**：将 `ENABLE_EMAIL_NOTIFICATION` 设为 `False`

### 方式二：通过 `strategy_config.json`

运行 `python buy_strategy.py --save-config` 可持久化保存策略参数配置。

### 成本价追踪策略规则

当设定了有效的 `GOLD_COST` 时，系统自动启用以下提醒规则：

| 提醒类型 | 默认阈值 | 说明 |
|---------|---------|------|
| 🎉 止盈提醒 | +5.0% | 当前价涨超成本价一定比例，建议分批止盈 |
| 🚨 止损提醒 | -3.0% | 当前价跌破成本价一定比例，注意风控 |
| 📉 补仓提醒 | -1.5% | 当前价低于成本价一定比例，可考虑补仓摊薄成本 |

---

## 🛠️ 系统工作流程

```
GitHub Actions Cron / 手动触发
        │
        ▼
  ┌─────────────┐      ┌──────────────┐
  │ monitor.py  │ ───▶ │ monitor.log  │   金价采集 → 记录日志
  └─────────────┘      └──────────────┘
        │
        ▼
  ┌──────────────────┐  ┌──────────────────────┐
  │ buy_strategy.py  │──│ 7 种策略 + 高频策略   │
  └──────────────────┘  └──────────────────────┘
        │
        ▼ (满足买入条件)
  ┌──────────────┐
  │ Resend SMTP  │ ──▶ 📧 邮件通知
  └──────────────┘
        │
        ▼
  ┌──────────────────────────┐
  │ git commit & push 日志   │   状态同步回仓库
  └──────────────────────────┘
```

---

## 📡 数据来源

本项目的金价数据来源于 **京东金融** 黄金价格公开接口。

> [!NOTE]
> 本项目仅供学习和个人使用，金价数据的准确性和实时性取决于上游接口。投资有风险，策略分析结果仅供参考，不构成任何投资建议。

---

## 📜 License

MIT
