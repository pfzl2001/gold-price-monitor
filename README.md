# CZB 金价监控与买入策略分析系统 (CZB Gold Price Monitor & Buy Strategy)

本项目是一个基于 Python 和 GitHub Actions 实现的黄金价格监控与量化投资买入决策分析系统。系统通过定时爬取浙商银行聚金宝（`CZB-JCJ`）的金价数据，自动保存日志，并结合多种量化技术指标进行多维度买入信号分析。当系统判定满足买入条件时，将自动通过电子邮件发送提醒通知。

---

## 📂 项目结构

```text
czb_gold_price_monitor/
├── monitor.py               # 金价获取与记录核心脚本
├── buy_strategy.py          # 多维度技术指标分析与邮件提醒策略
├── monitor.log              # 历史金价数据日志（自动更新）
├── buy_strategy.log         # 每次分析策略运行的输出日志（自动更新）
├── buy_notify_state.json    # 存储通知状态（如最后通知时间、价格，防重复骚扰）
├── strategy_config.json     # 策略配置参数文件（支持持久化策略阈值）
└── requirements.txt         # 项目依赖库（requests 等）
```

---

## 🛠️ 系统工作流程

1. **定时调度 (GitHub Actions)**:
   - 配置文件：[.github/workflows/czb_gold_price_monitor.yml](file:///d:/OneDrive/programming/anaconda/leetcode/.github/workflows/czb_gold_price_monitor.yml)
   - 运行周期：通过外部调度（如 `cron-job.org` 触发 `repository_dispatch`）或 GitHub Actions 自带 Cron 定时任务（工作日北京时间 9:00 - 23:00，每小时执行一次）。
2. **获取金价 (`monitor.py`)**:
   - 请求京东金融的金价接口获取浙商银行聚金宝实时价格。
   - 提取最新价格、涨跌值、涨跌幅、交易时间，并以结构化单行日志追加写入到 `monitor.log` 中。
3. **策略分析 (`buy_strategy.py`)**:
   - 解析最新的 `monitor.log` 历史数据，支持以下策略组合判定：
     - **硬性价格阈值**：金价低于设定阈值时直接建议买入。
     - **目标价策略**：低于用户设定的理想买入价。
     - **日跌幅策略**：当日跌幅超出设定比例（可能超卖）。
     - **分时与日均线策略**：计算多周期均线，若金价低于均线则判定为买入时机。
     - **RSI超卖策略**：计算相对强弱指标，RSI 低于设定值（如 30）判定为超卖。
     - **回调策略**：从近期最高点回调一定比例。
     - **布林带策略**：价格跌破下轨。
     - **日内高频策略 (8分钟线)**：使用 8 分钟级 Bollinger + RSI + ATR 指标进行日内交易建议。
4. **邮件通知**:
   - 当分析出 `强烈买入` (STRONG_BUY) 或 `建议买入` (BUY) 信号时，使用 Resend SMTP 发送格式化的 HTML 邮件通知。
   - 读取并写入 `buy_notify_state.json` 限制在同一天内如果价格没有更低，则不重复发送，避免邮件骚扰。
5. **状态同步**:
   - GitHub Actions 运行结束后，会自动把最新的 `monitor.log`、`buy_strategy.log` 和 `buy_notify_state.json` 提交（Git Commit & Push）并同步回仓库。

---

## 📧 邮件配置说明

当前项目的邮件发送配置**直接声明在 GitHub Actions 工作流的的环境变量中**，您可以在工作流文件里找到并修改：

- **配置文件路径**：[.github/workflows/czb_gold_price_monitor.yml](file:///d:/OneDrive/programming/anaconda/leetcode/.github/workflows/czb_gold_price_monitor.yml)
- **配置项位置 (第 47 - 50 行)**:
  ```yaml
  env:
    CZB_TZ: "Asia/Shanghai"
    # 邮件配置移到这里，只有策略建议买入时才发邮件
    EMAIL_USER: "1697669486@qq.com"       # 发件人 QQ 邮箱
    EMAIL_PASS: "wucigkvypuxycahe"       # QQ 邮箱的 SMTP 授权码/应用密码
    EMAIL_TO: "1697669486@qq.com"         # 收件人邮箱（支持逗号分隔的多个邮箱）
    EMAIL_SUBJECT: "🔔 金价买入提醒"        # 邮件主题前缀
  ```

> [!NOTE]
> `EMAIL_PASS` 使用的是 QQ 邮箱专用的 SMTP 客户端授权密码（16位字母），而非您的 QQ 登录密码。

---

## 💻 本地运行与开发

如果您想在本地运行该项目进行测试或查看当前策略报告：

### 1. 安装依赖
```bash
pip install -r czb_gold_price_monitor/requirements.txt
```

### 2. 本地执行金价拉取
```bash
python czb_gold_price_monitor/monitor.py
```

### 3. 本地执行策略分析并输出报告
```bash
python czb_gold_price_monitor/buy_strategy.py
```
*(注：如果需要测试本地邮件发送，请在执行前设置相应的 `EMAIL_USER`、`EMAIL_PASS` 和 `EMAIL_TO` 环境变量。)*

---

## 📈 成本价策略与通知开关配置 (在 `buy_strategy.py` 文件头部设置)

为了关闭不必要的虚拟止损/止盈提醒，并支持您在不改动核心逻辑代码的情况下，方便地在文件头部配置您的实际黄金持仓成本价和邮件通知开关，我们设计了如下配置方式：

### 1. 核心思路
- **关闭虚拟提醒**：由于您不一定会按照每次提醒进行买入，系统自动维护 of 虚拟 8 分钟策略持仓状态（`LONG`、`STOP_LOSS_SELL`、`TAKE_PROFIT_SELL`）与您的实际交易不符。我们已**完全关闭 8 分钟策略的虚拟止损/止盈邮件提醒**（日志中仍然保留分析，但不发邮件骚扰）。
- **文件头部配置实际成本价**：
  - 在 [buy_strategy.py](file:///d:/OneDrive/programming/anaconda/leetcode/czb_gold_price_monitor/buy_strategy.py) 头部可以定义 `CZB_GOLD_COST` 和 `ENABLE_EMAIL_NOTIFICATION`。
  - 如果将 `CZB_GOLD_COST` 设置为您的实际黄金成本价（如 `558.5`），系统会自动激活**实际成本追踪提醒策略**（如止损、止盈、补仓提醒）。
  - 如果设置为 `None` 或 `0.0`，代表您**当前没有买入**，系统将仅发送普通买入条件满足时的提醒，绝不发送任何持仓相关的止损/止盈/补仓提醒。
- **通知总开关**：
  - 如果将 `ENABLE_EMAIL_NOTIFICATION` 设置为 `False`，系统将完全不会向您发送任何提醒邮件（包括常规买入推荐、高频策略推荐和持仓成本追踪通知）。

### 2. 如何配置您的成本价与开关
打开策略文件 [buy_strategy.py](file:///d:/OneDrive/programming/anaconda/leetcode/czb_gold_price_monitor/buy_strategy.py)，您可以在文件最上方（导入模块下方）看到以下配置区域：

```python
# ================= 全局个性化设置 =================
# 您的实际黄金持仓成本价（元/克），支持浮点数（如 558.5）。如果未买入，请设置为 None 或 0.0
CZB_GOLD_COST = None

# 是否启用邮件通知。如果设为 False，系统将完全不会发送任何提醒邮件
ENABLE_EMAIL_NOTIFICATION = True
# ==================================================
```

- **修改持仓成本**：直接将 `CZB_GOLD_COST = None` 改为例如 `CZB_GOLD_COST = 560.5`，保存提交即可。
- **暂停所有邮件提醒**：将 `ENABLE_EMAIL_NOTIFICATION = True` 改为 `ENABLE_EMAIL_NOTIFICATION = False`，即可安静运行不再发送任何邮件。

### 3. 成本价追踪策略规则
当 `CZB_GOLD_COST` 设定为有效价格时，系统运行以下规则：
1. **止盈提醒 (Take Profit)**：当前价格涨超您的成本价一定比例（默认 `+5.0%`，可自定义），提示分批止盈。
2. **止损提醒 (Stop Loss)**：当前价格跌破您的成本价一定比例（默认 `-3.0%`，可自定义），提示风控止损。
3. **加仓/补仓提醒 (Average Down)**：当前价格低于您的成本价一定比例（默认 `-1.5%`，可自定义），提示可以分批补仓以摊薄成本。

*(注：上述触发提醒同样会遵循防刷屏冷却时间 `NOTIFY_COOLDOWN_HOURS` (默认4小时) 和变动跌幅门槛，避免频繁重复发送)*

