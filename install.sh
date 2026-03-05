#!/bin/bash
#
# Infra Assistant - Installation Script
# Target: Ubuntu 24.04 LTS (or similar)
#
# This script will:
# 1. Install Python dependencies
# 2. Install Ollama (optional)
# 3. Set up directory structure
# 4. Generate configuration files
# 5. Set up systemd services
#
# Usage:
#   chmod +x install.sh
#   sudo ./install.sh
#

set -e  # Exit on any error

# =============================================================================
# Configuration
# =============================================================================

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
log_info "Install directory: ${INSTALL_DIR}"

# Check OS
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
    openssl \
    openssh-client

log_ok "System packages installed"

# =============================================================================
# Step 2: Optional - Install Ollama
# =============================================================================

log_step "Checking for Ollama..."

if command -v ollama &> /dev/null; then
    log_ok "Ollama already installed: $(ollama --version 2>/dev/null || echo 'unknown version')"
else
    echo ""
    read -p "Install Ollama locally? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_info "Installing Ollama..."
        curl -fsSL https://ollama.com/install.sh | sh
        log_ok "Ollama installed"

        # Start service
        systemctl enable ollama
        systemctl start ollama

        log_info "Waiting for Ollama to start..."
        sleep 5

        # Pull a model
        read -p "Pull llama3.1:8b model? (Y/n): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            log_info "Pulling llama3.1:8b (this may take a while)..."
            sudo -u ollama ollama pull llama3.1:8b || log_warn "Model pull failed - can retry later"
        fi
    else
        log_info "Skipping Ollama installation"
        log_warn "Make sure OLLAMA_URL in .env points to your Ollama instance"
    fi
fi

# =============================================================================
# Step 3: Directory Structure and Configuration
# =============================================================================

log_step "Setting up configuration..."

# Create directories
mkdir -p "${INSTALL_DIR}"/{keys,logs}

# Create .env from example if doesn't exist
if [ ! -f "${INSTALL_DIR}/.env" ]; then
    if [ -f "${INSTALL_DIR}/.env.example" ]; then
        cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"

        # Generate random tokens
        SSH_TOKEN=$(openssl rand -hex 32)
        ZABBIX_TOKEN=$(openssl rand -hex 32)

        sed -i "s/CHANGE_THIS_GENERATE_WITH_openssl_rand_hex_32/${SSH_TOKEN}/" "${INSTALL_DIR}/.env"
        sed -i "0,/CHANGE_THIS_GENERATE_WITH_openssl_rand_hex_32/s/CHANGE_THIS_GENERATE_WITH_openssl_rand_hex_32/${ZABBIX_TOKEN}/" "${INSTALL_DIR}/.env"

        log_ok "Created .env with generated tokens"
        log_warn "IMPORTANT: Edit ${INSTALL_DIR}/.env with your Rocket.Chat and Zabbix credentials!"
    else
        log_error ".env.example not found"
        exit 1
    fi
else
    log_info ".env already exists, skipping"
fi

# Create hosts.yaml from example if doesn't exist
if [ ! -f "${INSTALL_DIR}/ssh-proxy/hosts.yaml" ]; then
    if [ -f "${INSTALL_DIR}/ssh-proxy/hosts.yaml.example" ]; then
        cp "${INSTALL_DIR}/ssh-proxy/hosts.yaml.example" "${INSTALL_DIR}/ssh-proxy/hosts.yaml"
        log_ok "Created ssh-proxy/hosts.yaml from example"
        log_warn "IMPORTANT: Edit ${INSTALL_DIR}/ssh-proxy/hosts.yaml with your actual hosts!"
    fi
else
    log_info "ssh-proxy/hosts.yaml already exists"
fi

# Set ownership
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "${INSTALL_DIR}"

log_ok "Configuration files created"

# =============================================================================
# Step 4: Python Dependencies
# =============================================================================

log_step "Installing Python dependencies..."

# RC Bot
if [ -d "${INSTALL_DIR}/rc-bot" ] && [ -f "${INSTALL_DIR}/rc-bot/requirements.txt" ]; then
    log_info "Setting up RC Bot Python environment..."
    cd "${INSTALL_DIR}/rc-bot"

    if [ ! -d "venv" ]; then
        python3 -m venv venv
    fi

    ./venv/bin/pip install -q --upgrade pip
    ./venv/bin/pip install -q -r requirements.txt
    chown -R "${ACTUAL_USER}:${ACTUAL_USER}" venv
    log_ok "RC Bot dependencies installed"
fi

# Zabbix Proxy
if [ -d "${INSTALL_DIR}/zabbix-proxy" ] && [ -f "${INSTALL_DIR}/zabbix-proxy/requirements.txt" ]; then
    log_info "Setting up Zabbix Proxy Python environment..."
    cd "${INSTALL_DIR}/zabbix-proxy"

    if [ ! -d "venv" ]; then
        python3 -m venv venv
    fi

    ./venv/bin/pip install -q --upgrade pip
    ./venv/bin/pip install -q -r requirements.txt
    chown -R "${ACTUAL_USER}:${ACTUAL_USER}" venv
    log_ok "Zabbix Proxy dependencies installed"
fi

# Zabbix Poller
if [ -d "${INSTALL_DIR}/zabbix-poller" ] && [ -f "${INSTALL_DIR}/zabbix-poller/requirements.txt" ]; then
    log_info "Setting up Zabbix Poller Python environment..."
    cd "${INSTALL_DIR}/zabbix-poller"

    if [ ! -d "venv" ]; then
        python3 -m venv venv
    fi

    ./venv/bin/pip install -q --upgrade pip
    ./venv/bin/pip install -q -r requirements.txt
    chown -R "${ACTUAL_USER}:${ACTUAL_USER}" venv
    log_ok "Zabbix Poller dependencies installed"
fi

# SSH Proxy
if [ -d "${INSTALL_DIR}/ssh-proxy" ] && [ -f "${INSTALL_DIR}/ssh-proxy/requirements.txt" ]; then
    log_info "Setting up SSH Proxy Python environment..."
    cd "${INSTALL_DIR}/ssh-proxy"

    if [ ! -d "venv" ]; then
        python3 -m venv venv
    fi

    ./venv/bin/pip install -q --upgrade pip
    ./venv/bin/pip install -q -r requirements.txt
    chown -R "${ACTUAL_USER}:${ACTUAL_USER}" venv
    log_ok "SSH Proxy dependencies installed"
fi

cd "${INSTALL_DIR}"

# =============================================================================
# Step 5: Systemd Services
# =============================================================================

log_step "Setting up systemd services..."

if [ -d "${INSTALL_DIR}/systemd" ]; then
    # Replace YOUR_USER placeholder if it exists
    for service_file in "${INSTALL_DIR}/systemd"/*.service "${INSTALL_DIR}/systemd"/*.timer; do
        if [ -f "$service_file" ]; then
            sed -i "s|YOUR_USER|${ACTUAL_USER}|g" "$service_file"
            sed -i "s|/opt/infra-assistant|${INSTALL_DIR}|g" "$service_file"
        fi
    done

    # Create symlinks
    for service_file in "${INSTALL_DIR}/systemd"/*.{service,timer}; do
        if [ -f "$service_file" ]; then
            service_name=$(basename "$service_file")
            if [ ! -L "/etc/systemd/system/${service_name}" ]; then
                ln -sf "$service_file" "/etc/systemd/system/${service_name}"
                log_info "Linked ${service_name}"
            fi
        fi
    done

    systemctl daemon-reload
    log_ok "Systemd services configured"
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
echo "1. ${YELLOW}Edit configuration files:${NC}"
echo "   nano ${INSTALL_DIR}/.env"
echo "   - Set RC_URL, RC_USERNAME, RC_PASSWORD, RC_CHANNELS"
echo "   - Set ZABBIX_URL, ZABBIX_USER, ZABBIX_PASSWORD"
echo "   - Set OLLAMA_URL (if using remote Ollama)"
echo ""
echo "   nano ${INSTALL_DIR}/ssh-proxy/hosts.yaml"
echo "   - Add your actual hosts with SSH credentials"
echo ""
echo "2. ${YELLOW}Start services:${NC}"
echo "   sudo systemctl enable --now ssh-proxy"
echo "   sudo systemctl enable --now zabbix-proxy"
echo "   sudo systemctl enable --now rc-bot"
echo "   sudo systemctl enable --now zabbix-poller.timer"
echo ""
echo "3. ${YELLOW}Check status:${NC}"
echo "   sudo systemctl status ssh-proxy zabbix-proxy rc-bot"
echo ""
echo "4. ${YELLOW}View logs:${NC}"
echo "   sudo journalctl -u rc-bot -f"
echo "   sudo journalctl -u ssh-proxy -f"
echo "   sudo journalctl -u zabbix-proxy -f"
echo ""
echo -e "${CYAN}Documentation:${NC}"
echo "   ${INSTALL_DIR}/README.md         - Overview"
echo "   ${INSTALL_DIR}/OPERATIONS.md     - Day-to-day operations"
echo "   ${INSTALL_DIR}/systemd/README.md - Service management"
echo ""
echo -e "${YELLOW}Remember: Edit configuration files before starting services!${NC}"
echo ""
