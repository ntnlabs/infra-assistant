#!/bin/bash
#
# Infra Assistant - Single Machine Installer
# Target: Ubuntu 24.04 LTS with NVIDIA GPU
#
# This script will:
# 1. Install Docker (if needed)
# 2. Install NVIDIA Container Toolkit
# 3. Install Ollama (locked to GPU1)
# 4. Deploy Dify
# 5. Setup SSH Proxy and RC Bot
#
# Usage:
#   chmod +x install.sh
#   sudo ./install.sh
#

set -e  # Exit on any error

# =============================================================================
# Configuration
# =============================================================================

INSTALL_DIR="/opt/infra-assistant"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# =============================================================================
# Helper Functions
# =============================================================================

log_step() {
    echo -e "\n${GREEN}[STEP]${NC} $1"
}

log_info() {
    echo -e "${CYAN}  →${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}  ⚠${NC} $1"
}

log_error() {
    echo -e "${RED}  ✗${NC} $1"
}

log_ok() {
    echo -e "${GREEN}  ✓${NC} $1"
}

# =============================================================================
# Pre-flight Checks
# =============================================================================

echo -e "${GREEN}════════════════════════════════════════${NC}"
echo -e "${GREEN}  Infra Assistant Installer${NC}"
echo -e "${GREEN}  Target: Ubuntu 24.04 LTS${NC}"
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo ""

# Check root
if [ "$EUID" -ne 0 ]; then
    log_error "Please run as root: sudo ./install.sh"
    exit 1
fi

# Get actual user
ACTUAL_USER=${SUDO_USER:-$USER}
ACTUAL_HOME=$(getent passwd "$ACTUAL_USER" | cut -d: -f6)
log_info "Running as root, ownership will be set to: ${ACTUAL_USER}"

# Check Ubuntu version
if [ -f /etc/os-release ]; then
    . /etc/os-release
    log_info "Detected OS: ${PRETTY_NAME}"
fi

# =============================================================================
# Step 1: System Prerequisites
# =============================================================================

log_step "Installing system prerequisites..."

apt-get update -qq

apt-get install -y -qq \
    curl \
    wget \
    git \
    python3 \
    python3-pip \
    python3-venv \
    jq \
    openssl

log_ok "System packages installed"

# =============================================================================
# Step 2: Docker Installation
# =============================================================================

log_step "Setting up Docker..."

if command -v docker &> /dev/null; then
    log_info "Docker already installed: $(docker --version)"
else
    log_info "Installing Docker..."

    # Remove old versions
    apt-get remove -y -qq docker docker-engine docker.io containerd runc 2>/dev/null || true

    # Prerequisites
    apt-get install -y -qq \
        ca-certificates \
        gnupg \
        lsb-release

    # Docker GPG key
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    # Docker repository
    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
        tee /etc/apt/sources.list.d/docker.list > /dev/null

    # Install
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    log_ok "Docker installed"
fi

# Add user to docker group
if ! groups "$ACTUAL_USER" | grep -q docker; then
    usermod -aG docker "$ACTUAL_USER"
    log_warn "Added ${ACTUAL_USER} to docker group (re-login required)"
fi

# Start Docker
systemctl enable docker
systemctl start docker
log_ok "Docker is running"

# =============================================================================
# Step 3: NVIDIA Container Toolkit
# =============================================================================

log_step "Setting up NVIDIA Container Toolkit..."

if dpkg -l 2>/dev/null | grep -q nvidia-container-toolkit; then
    log_info "NVIDIA Container Toolkit already installed"
else
    log_info "Installing NVIDIA Container Toolkit..."

    # NVIDIA repository
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
        gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
        tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

    apt-get update -qq
    apt-get install -y -qq nvidia-container-toolkit

    # Configure Docker
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker

    log_ok "NVIDIA Container Toolkit installed"
fi

# Test GPU access
log_info "Testing Docker GPU access..."
if docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi &>/dev/null; then
    log_ok "Docker can access GPUs"
else
    log_warn "Docker cannot access GPUs - check NVIDIA drivers"
fi

# =============================================================================
# Step 4: Create Directory Structure
# =============================================================================

log_step "Creating directory structure..."

mkdir -p "${INSTALL_DIR}"/{ssh-proxy,rc-bot,tools,keys,logs}

# Copy files from repo
if [ -d "${SCRIPT_DIR}/ssh-proxy" ]; then
    cp -r "${SCRIPT_DIR}/ssh-proxy/"* "${INSTALL_DIR}/ssh-proxy/" 2>/dev/null || true
    log_ok "SSH Proxy files copied"
fi

if [ -d "${SCRIPT_DIR}/rc-bot" ]; then
    cp -r "${SCRIPT_DIR}/rc-bot/"* "${INSTALL_DIR}/rc-bot/" 2>/dev/null || true
    log_ok "RC Bot files copied"
fi

if [ -d "${SCRIPT_DIR}/tools" ]; then
    cp -r "${SCRIPT_DIR}/tools/"* "${INSTALL_DIR}/tools/" 2>/dev/null || true
fi

# Copy .env.example
if [ -f "${SCRIPT_DIR}/.env.example" ]; then
    cp "${SCRIPT_DIR}/.env.example" "${INSTALL_DIR}/.env.example"
    log_ok ".env.example copied"
fi

# Create .env from example if doesn't exist
if [ ! -f "${INSTALL_DIR}/.env" ]; then
    if [ -f "${INSTALL_DIR}/.env.example" ]; then
        cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"

        # Generate random SSH proxy token
        NEW_TOKEN=$(openssl rand -hex 32)
        sed -i "s/CHANGE_THIS_GENERATE_WITH_openssl_rand_hex_32/${NEW_TOKEN}/" "${INSTALL_DIR}/.env"

        log_ok "Created .env with generated SSH_PROXY_TOKEN"
        log_warn "Edit ${INSTALL_DIR}/.env with your settings!"
    fi
fi

# Copy hosts.yaml.example
if [ -f "${SCRIPT_DIR}/ssh-proxy/hosts.yaml.example" ]; then
    cp "${SCRIPT_DIR}/ssh-proxy/hosts.yaml.example" "${INSTALL_DIR}/ssh-proxy/"
    if [ ! -f "${INSTALL_DIR}/ssh-proxy/hosts.yaml" ]; then
        cp "${INSTALL_DIR}/ssh-proxy/hosts.yaml.example" "${INSTALL_DIR}/ssh-proxy/hosts.yaml"
        log_ok "Created hosts.yaml from example"
    fi
fi

# Set ownership
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "${INSTALL_DIR}"

log_ok "Directory structure created at ${INSTALL_DIR}"

# =============================================================================
# Step 5: Install Ollama (GPU1 only)
# =============================================================================

log_step "Installing Ollama (locked to GPU1)..."

# Read GPU setting from .env if exists
if [ -f "${INSTALL_DIR}/.env" ]; then
    OLLAMA_GPU=$(grep "^OLLAMA_GPU=" "${INSTALL_DIR}/.env" | cut -d= -f2)
    OLLAMA_MODEL=$(grep "^OLLAMA_MODEL=" "${INSTALL_DIR}/.env" | cut -d= -f2)
fi
OLLAMA_GPU=${OLLAMA_GPU:-1}
OLLAMA_MODEL=${OLLAMA_MODEL:-llama3.1:8b}

if command -v ollama &> /dev/null; then
    log_info "Ollama already installed"
else
    log_info "Downloading and installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
    log_ok "Ollama installed"
fi

# Create systemd service locked to specific GPU
log_info "Configuring Ollama service for GPU${OLLAMA_GPU}..."

cat > /etc/systemd/system/ollama.service << EOF
[Unit]
Description=Ollama LLM Server (GPU${OLLAMA_GPU} only)
After=network-online.target

[Service]
Type=simple
User=ollama
Group=ollama
Environment="CUDA_VISIBLE_DEVICES=${OLLAMA_GPU}"
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_MODELS=/usr/share/ollama/.ollama/models"
ExecStart=/usr/local/bin/ollama serve
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

# Create ollama user
if ! id "ollama" &>/dev/null; then
    useradd -r -s /bin/false -m -d /usr/share/ollama ollama
fi

systemctl daemon-reload
systemctl enable ollama
systemctl restart ollama

log_info "Waiting for Ollama to start..."
sleep 5

# Pull model
log_info "Pulling model: ${OLLAMA_MODEL} (this may take a while)..."
sudo -u ollama ollama pull "${OLLAMA_MODEL}" || {
    log_warn "Model pull failed, retrying..."
    sleep 10
    sudo -u ollama ollama pull "${OLLAMA_MODEL}"
}

log_ok "Ollama ready with ${OLLAMA_MODEL} on GPU${OLLAMA_GPU}"

# =============================================================================
# Step 6: Install Dify
# =============================================================================

log_step "Installing Dify..."

cd "${INSTALL_DIR}"

if [ -d "dify/.git" ]; then
    log_info "Updating existing Dify..."
    cd dify && git pull
else
    log_info "Cloning Dify repository..."
    rm -rf dify
    git clone --depth 1 https://github.com/langgenius/dify.git
    cd dify
fi

cd docker

# Create .env if needed
if [ ! -f .env ]; then
    cp .env.example .env

    # Privacy settings
    cat >> .env << 'ENVEOF'

# === Privacy Settings (added by installer) ===
SENTRY_DSN=
CHECK_UPDATE_URL=
MARKETPLACE_ENABLED=false
ENVEOF

    log_ok "Dify .env configured with privacy settings"
fi

# Start Dify
log_info "Starting Dify containers (first run takes a few minutes)..."
docker compose up -d

log_info "Waiting for Dify to initialize..."
sleep 30

# Show status
log_ok "Dify containers started"
docker compose ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null | head -15

cd "${INSTALL_DIR}"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" dify

# =============================================================================
# Step 7: Setup SSH Proxy and RC Bot
# =============================================================================

log_step "Setting up SSH Proxy and RC Bot..."

# SSH Proxy - build Docker image
cd "${INSTALL_DIR}/ssh-proxy"
if [ -f Dockerfile ]; then
    log_info "Building SSH Proxy container..."
    docker build -t infra-ssh-proxy . -q
    log_ok "SSH Proxy image built"
fi

# RC Bot - setup Python venv
cd "${INSTALL_DIR}/rc-bot"
if [ -f requirements.txt ]; then
    log_info "Setting up RC Bot Python environment..."
    python3 -m venv venv
    ./venv/bin/pip install -q --upgrade pip
    ./venv/bin/pip install -q -r requirements.txt
    chown -R "${ACTUAL_USER}:${ACTUAL_USER}" venv
    log_ok "RC Bot dependencies installed"
fi

# =============================================================================
# Done!
# =============================================================================

echo ""
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation Complete!${NC}"
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo ""
echo -e "${CYAN}Next Steps:${NC}"
echo ""
echo "1. ${YELLOW}Complete Dify setup:${NC}"
echo "   Open browser: http://YOUR_SERVER_IP/install"
echo "   Create admin account"
echo ""
echo "2. ${YELLOW}Configure Ollama in Dify:${NC}"
echo "   Settings → Model Providers → Ollama"
echo "   Base URL: http://host.docker.internal:11434"
echo "   Model: ${OLLAMA_MODEL}"
echo ""
echo "3. ${YELLOW}Edit configuration:${NC}"
echo "   nano ${INSTALL_DIR}/.env"
echo "   nano ${INSTALL_DIR}/ssh-proxy/hosts.yaml"
echo ""
echo "4. ${YELLOW}Start services:${NC}"
echo ""
echo "   # SSH Proxy"
echo "   docker run -d --name ssh-proxy --restart unless-stopped \\"
echo "     -p 127.0.0.1:5001:5001 \\"
echo "     -v ${INSTALL_DIR}/.env:/app/.env:ro \\"
echo "     -v ${INSTALL_DIR}/ssh-proxy/hosts.yaml:/app/hosts.yaml:ro \\"
echo "     -v ${INSTALL_DIR}/ssh-proxy/commands.yaml:/app/commands.yaml:ro \\"
echo "     -v ${INSTALL_DIR}/keys:/keys:ro \\"
echo "     infra-ssh-proxy"
echo ""
echo "   # RC Bot"
echo "   cd ${INSTALL_DIR}/rc-bot"
echo "   ./venv/bin/python bot.py"
echo ""
echo -e "${CYAN}Logs:${NC}"
echo "   Dify:      cd ${INSTALL_DIR}/dify/docker && docker compose logs -f"
echo "   Ollama:    journalctl -u ollama -f"
echo "   SSH Proxy: docker logs -f ssh-proxy"
echo ""
echo -e "${YELLOW}Remember: Edit .env before starting services!${NC}"
echo ""
