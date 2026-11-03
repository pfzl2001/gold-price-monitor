# 金价监控与买入策略分析系统

[![GitHub Actions](https://github.com/pfzl2001/gold-price-monitor/actions/workflows/gold_price_monitor.yml/badge.svg)](https://github.com/pfzl2001/gold-price-monitor/actions)

基于 Python + GitHub Actions 的黄金价格自动监控与量化买入分析系统。金价数据来源于**京东金融**黄金价格接口，系统定时获取实时金价并结合多种量化技术指标自动分析买入时机，满足条件时通过邮件发送提醒通知。

此外，项目还包含**基金净值周五播报**：每周五自动抓取徽银理财产品（PNHY230387A）的单位净值，计算相对上一净值日的涨跌幅，并通过同一套 Resend 邮件通道推送。

> [!IMPORTANT]
> 金价监控当前已按需求**停止**：开关位于 `monitor.py` → `ENABLE_GOLD_MONITOR = False`。详见「🔛 金价监控开关」章节；基金净值播报不受该开关影响，独立运行。

## ✨ 功能特性

- 📊 **定时金价采集** — 通过京东金融接口自动获取实时金价并记录历史数据
- 🤖 **多维度策略分析** — 均线、RSI、布林带、回调、连续下跌等 7 种策略
- 📈 **8分钟高频策略** — 日内级别 Bollinger + RSI + ATR 量化交易信号
- 💰 **持仓成本追踪** — 止盈/止损/补仓智能提醒
- 📧 **邮件通知** — 通过 [Resend](https://resend.com) SMTP 发送提醒邮件
- 📈 **基金净值周五播报** — 每周五推送理财净值相对上一净值日的涨跌幅，含本周明细与近 8 日走势
- 🔛 **一键启停** — 金价监控由 `monitor.py` 中单个布尔开关控制，随时可恢复
- 🔁 **GitHub Actions 自动运行** — 零成本云端定时执行

---

## 📂 项目结构

```text
gold-price-monitor/
├── monitor.py               # 金价获取与记录核心脚本（含总开关 ENABLE_GOLD_MONITOR）
├── buy_strategy.py          # 多维度技术指标分析与邮件提醒策略
├── fund_nav_notify.py       # 基金净值周五播报（抓取 / 计算 / HTML 邮件）
├── requirements.txt         # 项目依赖库
├── .env.example             # 环境变量配置模板
├── .github/
│   └── workflows/
│       ├── gold_price_monitor.yml   # 金价监控工作流（读取 py 开关决定是否执行）
│       └── fund_nav_notify.yml      # 基金净值每周五播报工作流
├── monitor.log              # 历史金价数据日志（运行时自动生成）
├── buy_strategy.log         # 策略分析输出日志（运行时自动生成）
├── buy_notify_state.json    # 金价通知状态文件（运行时自动生成）
└── fund_nav_state.json      # 基金播报防重复状态（运行时自动生成，由 Actions 提交）
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

4. **完成！** 基金净值播报会在每周五（含补发窗口）自动发送；金价工作流仍按原调度触发，但当前开关为关闭状态，会立即退出、不产生任何请求与邮件（如需恢复见下方开关章节）。

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

## 🔛 金价监控开关（当前：关闭）

金价监控由 **`monitor.py` 文件头部的单个开关**控制，这是唯一控制点：

```python
# ================= 金价监控总开关（唯一控制点） =================
ENABLE_GOLD_MONITOR = False   # 当前：停止监控
# ==============================================================
```

| 取值 | 行为 |
|------|------|
| `False` | **停止监控**：不请求金价接口、不写 `monitor.log`、不执行策略分析、不发送任何金价邮件 |
| `True` | 恢复监控：采集 / 分析 / 提醒全部按原有逻辑运行 |

联动机制：

- `buy_strategy.py` 直接复用该开关，关闭时立即退出；
- `.github/workflows/gold_price_monitor.yml` 会读取该开关，关闭时跳过全部金价步骤；
- 因此**只需改这一处**即可启停整套金价监控，无需改动工作流或其他文件。

> [!NOTE]
> 金价工作流原有的定时触发仍保留（脚本会立刻退出，不产生任何请求、日志或邮件），这样把开关改回 `True` 后可立即恢复监控。若希望连定时触发也一并停用，注释掉 `gold_price_monitor.yml` 里的 `schedule` 段即可。
> 基金净值播报由独立工作流驱动（见「📈 基金净值周五播报」章节），与该开关完全无关。

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

## 📈 基金净值周五播报

`fund_nav_notify.py` 每周五把「徽银理财“安盈”稳健最低持有30天理财产品」（代码 `PNHY230387A`）的净值变动发到 `EMAIL_TO`。

**关于「前一天」**：银行理财净值仅在净值公布日更新（工作日，遇节假日顺延），周末与节假日无净值。因此对比基准取**上一个净值公布日**（通常是周四），而非自然日前一天；邮件中会明确写出基准日期与基准净值。

**数据来源**：徽银理财官网 `lccs_income.php?fundCode=PNHY230387A&dateType=all` 全量净值 JSON（与 `lccs_display.php?id=17222` 页面同源）；接口异常时自动降级解析 `lccs_income_list.php` HTML 表格。接口返回的重复日期记录会自动去重。若本地环境缺少根证书导致 SSL 校验失败，脚本会自动降级重试一次，也可设置 `NAV_SKIP_SSL_VERIFY=1` 显式跳过校验。

**推送时机**（`.github/workflows/fund_nav_notify.yml`，cron 使用 UTC）：

| 北京时间 | 用途 |
|---------|------|
| 周五 20:00 / 22:00 | 净值当日已披露则直接推送 |
| 周六 09:00 / 12:00 | 净值 T+1 披露时补发 |
| 周一 09:00 | 节假日顺延等极端延迟的兜底 |

脚本每次触发都会自动判断：若目标周五净值尚未公布则跳过、等待下次补发；同一周五只发送一次（`fund_nav_state.json` 防重复）。若该周五本身是非净值日（节假日），则改用当周最后一个净值日并在邮件中注明。

**本地调试**：

```bash
python fund_nav_notify.py --dry-run                        # 生成报告与 HTML 预览，不发送邮件
python fund_nav_notify.py --check                          # 只检查净值是否已公布
python fund_nav_notify.py --date 2026-10-30 --dry-run     # 指定周五补发 / 测试
python fund_nav_notify.py --force                          # 忽略防重复记录强制发送
```

> 邮件复用项目已有的 Resend SMTP 配置（`SMTP_PASS` / `SENDER_EMAIL` / `EMAIL_TO`），无需新增 Secrets。涨跌配色遵循国内习惯：**红涨绿跌**。

---

## 🛠️ 系统工作流程

> 当前金价链路已由 `ENABLE_GOLD_MONITOR = False` 关闭（下图整条链路跳过）；基金净值播报由独立工作流每周五触发，不受影响。

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

基金净值播报（独立工作流 `fund_nav_notify.yml`）：

```
周五 20:00/22:00 · 周六补发 · 周一兜底 (Cron)
        │
        ▼
  fund_nav_notify.py ──▶ 官网全量净值 JSON（失败降级 HTML）
        │
        ├─ 目标周五净值未公布 ──▶ 跳过，等待下次触发补发
        │
        └─ 已公布 ──▶ 计算周五 vs 上一净值日涨跌 ──▶ HTML 报告
                                                        │
                                                        ▼
                                  📧 Resend SMTP 邮件 + fund_nav_state.json 防重复
```

---

## 📡 数据来源

本项目的金价数据来源于 **京东金融** 黄金价格公开接口。
基金净值数据来源于 **徽银理财官网** 的产品净值披露接口（`lccs_income.php` / `lccs_income_list.php`）。

> [!NOTE]
> 本项目仅供学习和个人使用，金价数据的准确性和实时性取决于上游接口。投资有风险，策略分析结果仅供参考，不构成任何投资建议。

---

## 📜 License

MIT
