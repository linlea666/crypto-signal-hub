#!/usr/bin/env bash
# ============================================
# CryptoSignal Hub - 一键交互式部署脚本
# 适用于 Linux / macOS 服务器
# ============================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

APP_NAME="CryptoSignal Hub"
REPO_URL="https://github.com/YOUR_USERNAME/crypto-signal-hub"
INSTALL_DIR="$(pwd)"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   🔮 ${APP_NAME} - 一键部署脚本     ║${NC}"
echo -e "${CYAN}║   多维度加密货币信号分析系统           ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── 工具函数 ──

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

prompt_with_default() {
    local prompt="$1"
    local default="$2"
    local result
    read -rp "$(echo -e "${CYAN}${prompt} [${default}]: ${NC}")" result
    echo "${result:-$default}"
}

# ── 1. 环境检查 ──

echo -e "${YELLOW}━━━ Step 1/4: 环境检查 ━━━${NC}"
echo ""

# Docker
if ! command -v docker &>/dev/null; then
    err "未检测到 Docker，请先安装 Docker 20.10+"
    echo "  安装指南: https://docs.docker.com/engine/install/"
    exit 1
fi
DOCKER_VER=$(docker --version | grep -oP '[\d]+\.[\d]+' | head -1 2>/dev/null || docker --version)
ok "Docker 已安装 (${DOCKER_VER})"

# Docker Compose
if docker compose version &>/dev/null; then
    COMPOSE_CMD="docker compose"
    COMPOSE_VER=$(docker compose version --short 2>/dev/null || echo "v2+")
elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
    COMPOSE_VER=$(docker-compose --version | grep -oP '[\d]+\.[\d]+' | head -1 2>/dev/null || echo "v1")
else
    err "未检测到 Docker Compose，请先安装"
    exit 1
fi
ok "Docker Compose 已安装 (${COMPOSE_VER})"

# 磁盘空间
AVAIL=$(df -BG . 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G' || echo "?")
if [ "$AVAIL" != "?" ] && [ "$AVAIL" -lt 2 ]; then
    warn "磁盘剩余空间不足 2GB (${AVAIL}GB)，建议清理"
fi

echo ""

# ── 2. 交互式配置 ──

echo -e "${YELLOW}━━━ Step 2/4: 参数配置（回车使用默认值）━━━${NC}"
echo ""

PORT=$(prompt_with_default "📡 Web 端口" "8686")
echo ""

echo -e "${CYAN}📧 邮件推送配置（可跳过，后续在 Web 界面配置）${NC}"
SMTP_HOST=$(prompt_with_default "  SMTP 服务器" "smtp.163.com")
SMTP_PORT=$(prompt_with_default "  SMTP 端口" "465")
SMTP_USER=$(prompt_with_default "  发件邮箱" "")
SMTP_PASS=$(prompt_with_default "  邮箱授权码" "")
MAIL_TO=$(prompt_with_default "  收件邮箱" "")
echo ""

echo -e "${CYAN}🤖 AI 分析配置（可跳过，后续在 Web 界面配置）${NC}"
AI_PROVIDER=$(prompt_with_default "  AI 服务商 (deepseek/openai)" "deepseek")
AI_API_KEY=$(prompt_with_default "  API Key" "")
if [ "$AI_PROVIDER" = "openai" ]; then
    AI_MODEL=$(prompt_with_default "  模型名" "gpt-4o-mini")
    AI_BASE_URL=$(prompt_with_default "  API 地址" "https://api.openai.com/v1")
else
    AI_MODEL=$(prompt_with_default "  模型名" "deepseek-chat")
    AI_BASE_URL=$(prompt_with_default "  API 地址" "https://api.deepseek.com/v1")
fi
echo ""

# ── 3. 生成配置文件 ──

echo -e "${YELLOW}━━━ Step 3/4: 生成配置 ━━━${NC}"
echo ""

cat > .env <<ENVEOF
# CryptoSignal Hub 配置（由部署脚本自动生成）
CSH_PORT=${PORT}

# 邮件
SMTP_HOST=${SMTP_HOST}
SMTP_PORT=${SMTP_PORT}
SMTP_USER=${SMTP_USER}
SMTP_PASS=${SMTP_PASS}
MAIL_TO=${MAIL_TO}

# AI
AI_PROVIDER=${AI_PROVIDER}
AI_API_KEY=${AI_API_KEY}
AI_MODEL=${AI_MODEL}
AI_BASE_URL=${AI_BASE_URL}
ENVEOF

ok "配置文件已生成 (.env)"

# 确保 data 目录存在
mkdir -p data/logs
ok "数据目录已创建 (data/)"

# 如果没有 docker-compose.yml，下载它
if [ ! -f "docker-compose.yml" ] && [ ! -f "docker-compose.prod.yml" ]; then
    info "下载 docker-compose 配置..."
    if command -v curl &>/dev/null; then
        curl -fsSL "${REPO_URL}/raw/main/docker-compose.prod.yml" -o docker-compose.yml 2>/dev/null || true
    elif command -v wget &>/dev/null; then
        wget -q -O docker-compose.yml "${REPO_URL}/raw/main/docker-compose.prod.yml" 2>/dev/null || true
    fi
fi

# 如果本地有 Dockerfile，优先本地构建
if [ -f "Dockerfile" ]; then
    DEPLOY_MODE="build"
    info "检测到 Dockerfile，将使用本地构建模式"
    COMPOSE_FILE="docker-compose.yml"
else
    DEPLOY_MODE="pull"
    info "使用远程镜像模式"
    COMPOSE_FILE="docker-compose.prod.yml"
    [ ! -f "$COMPOSE_FILE" ] && COMPOSE_FILE="docker-compose.yml"
fi

echo ""

# ── 4. 构建并启动 ──

echo -e "${YELLOW}━━━ Step 4/4: 构建并启动 ━━━${NC}"
echo ""

if [ "$DEPLOY_MODE" = "build" ]; then
    info "正在构建 Docker 镜像（首次约需 2-5 分钟）..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" build --no-cache
    ok "镜像构建完成"
fi

info "正在启动服务..."
$COMPOSE_CMD -f "$COMPOSE_FILE" up -d

echo ""

# 等待容器启动
info "等待服务就绪..."
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if curl -sf "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  ✅ ${APP_NAME} 部署成功！                    ║${NC}"
    echo -e "${GREEN}╠══════════════════════════════════════════════════╣${NC}"
    echo -e "${GREEN}║                                                  ║${NC}"
    echo -e "${GREEN}║  🌐 访问地址: http://YOUR_IP:${PORT}              ║${NC}"
    echo -e "${GREEN}║  📋 查看日志: ${COMPOSE_CMD} logs -f              ║${NC}"
    echo -e "${GREEN}║  🔄 更新版本: ./deploy.sh                        ║${NC}"
    echo -e "${GREEN}║  ⏹️  停止服务: ${COMPOSE_CMD} down                ║${NC}"
    echo -e "${GREEN}║                                                  ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
else
    warn "服务可能仍在启动中，请稍后手动检查："
    echo "  $COMPOSE_CMD -f $COMPOSE_FILE logs -f"
fi

echo ""
echo -e "${CYAN}💡 提示：首次访问会进入引导配置页面，按提示完成设置即可。${NC}"
echo -e "${CYAN}📖 文档：${REPO_URL}${NC}"
echo ""
