# 🔮 CryptoSignal Hub

多维度加密货币信号分析系统 —— 聚合技术指标、衍生品数据、期权、宏观经济、情绪指标，通过评分引擎 + AI 生成交易信号报告，邮件推送强信号告警。

## ✨ 核心功能

- **多维度数据采集** — 交易所 K 线 / 资金费率 / 持仓量 / 多空比 / 期权 / 宏观 / 恐惧贪婪指数
- **规则评分引擎** — 7 个因子加权评分，输出方向 + 信心度 + 关键价位
- **AI 分析报告** — 支持 DeepSeek / OpenAI，将结构化数据翻译为自然语言
- **智能邮件推送** — 日报 + 强信号即时告警 + 美股时段加强监控 + 防骚扰限频
- **数据大屏** — 暗色加密风格 Web 界面，一屏掌握市场全貌
- **服务健康监控** — 各 API 状态一目了然，响应延迟实时展示
- **运行日志查看** — Web 端在线查看、日志轮转不占磁盘

## 🚀 快速部署

### ⚡ 方式一：一键安装（推荐新用户）

```bash
mkdir -p ~/cryptosignal && cd ~/cryptosignal && \
curl -fsSL https://raw.githubusercontent.com/linlea666/crypto-signal-hub/main/deploy.sh -o deploy.sh && \
chmod +x deploy.sh && ./deploy.sh
```

自动完成：✅ 环境检查 → ⚙️ 交互式配置 → 🐳 Docker 构建 → 🚀 启动服务

### 📦 方式二：Docker Compose（Clone 代码）

```bash
git clone https://github.com/linlea666/crypto-signal-hub.git
cd crypto-signal-hub
cp .env.example .env
# 编辑 .env 填入你的配置
docker compose up -d
```

### 🖥️ 方式三：本地运行（开发 / 调试）

```bash
cd crypto-signal-hub
pip install -r requirements.txt
python main.py
```

首次启动自动打开浏览器，进入引导配置页。

## 📋 前置要求

| 方式 | 要求 |
|------|------|
| Docker 部署 | Docker 20.10+, Docker Compose 2.0+ |
| 本地运行 | Python 3.10+ |

## 🐳 宝塔面板 Docker 部署

如果你的服务器安装了宝塔面板，可以通过面板的 Docker 管理功能部署：

1. **SSH 到服务器**，运行上面的「一键安装」命令
2. 或在宝塔终端中执行 `docker compose` 命令
3. 部署完成后，在宝塔 **Docker → 容器** 列表中可看到 `cryptosignal-hub`
4. 点击「日志」可查看容器运行日志
5. Web 界面地址：`http://你的服务器IP:8686`

> 💡 如需修改端口，编辑 `.env` 文件中的 `CSH_PORT`，然后 `docker compose restart`

## 🔧 管理命令

```bash
# 查看日志
docker compose logs -f

# 重启服务
docker compose restart

# 停止服务
docker compose down

# 更新版本
git pull && docker compose up -d --build

# 只使用远程镜像更新
docker compose pull && docker compose up -d
```

## 📁 目录结构

```
crypto-signal-hub/
├── main.py              # 主入口
├── Dockerfile           # Docker 镜像定义
├── docker-compose.yml   # 本地/开发 compose
├── docker-compose.prod.yml  # 生产 compose（镜像模式）
├── deploy.sh            # 一键部署脚本
├── .env.example         # 环境变量模板
├── requirements.txt     # Python 依赖
├── core/                # 核心模型/常量/接口/健康检查
├── config/              # 配置 Schema + YAML 管理
├── storage/             # SQLite 持久化
├── collectors/          # 数据采集（交易所/宏观/期权）
├── engine/              # 评分引擎 + 7 个因子
├── analyzer/            # AI 分析报告生成
├── notifier/            # 邮件推送 + 限频
├── scheduler/           # 定时任务调度
├── web/                 # FastAPI Web 界面
│   ├── templates/       # HTML 模板
│   ├── static/          # CSS / JS
│   └── routes/          # 页面 + API 路由
└── data/                # 运行时数据（挂载卷）
    ├── config.yaml      # 用户配置
    ├── signals.db       # SQLite 数据库
    └── logs/            # 日志文件（自动轮转）
```

## ⚙️ 配置说明

所有配置均可通过 Web 界面修改（`http://IP:8686/config`），也可直接编辑 `data/config.yaml`。

首次访问自动进入引导页，只需配置邮箱和 AI 即可开始使用。

## 📊 数据源

| 维度 | 来源 | 说明 |
|------|------|------|
| K线/MA/RSI | OKX + Binance | CCXT 统一接口 |
| 资金费率 | OKX + Binance | 交叉验证 |
| 持仓量 | OKX + Binance | OI 变化 + 价格联动 |
| 多空比 | OKX | 账户 + Taker 维度 |
| 期权 | Deribit | Max Pain / PCR / IV |
| 美股指数 | Yahoo Finance | NASDAQ / DXY / VIX |
| 恐惧贪婪 | Alternative.me | 情绪指标 |

## 📄 License

MIT
