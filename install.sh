#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# Odoo Claude MCP — Installer for Linux & macOS
# ═══════════════════════════════════════════════════════════
set -e

REPO_URL="https://github.com/rosenvladimirov/odoo-claude-mcp.git"
INSTALL_DIR="${HOME}/odoo-claude-mcp"

echo "╔══════════════════════════════════════════╗"
echo "║  Odoo Claude MCP — Installer             ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Check prerequisites ──────────────────────────────────
check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        echo "ERROR: $1 is not installed."
        echo "  $2"
        exit 1
    fi
}

check_cmd docker "Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
check_cmd git "Install git: https://git-scm.com/downloads"

if ! docker info &>/dev/null; then
    echo "ERROR: Docker is not running. Start Docker Desktop first."
    exit 1
fi

# ── Clone or update ──────────────────────────────────────
if [ -d "$INSTALL_DIR" ]; then
    echo "Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull --ff-only
else
    echo "Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── Create .env ──────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "Created .env from template."
    echo "Please edit ${INSTALL_DIR}/.env and set at minimum:"
    echo "  ANTHROPIC_API_KEY=sk-ant-..."
    echo ""
    read -p "Enter your Anthropic API key (or press Enter to skip): " API_KEY
    if [ -n "$API_KEY" ]; then
        sed -i.bak "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${API_KEY}|" .env
        rm -f .env.bak
        echo "API key saved."
    fi
fi

# ── Build & start core services ──────────────────────────
echo ""
echo "Building and starting services..."
docker compose up -d --build claude-terminal odoo-rpc-mcp
echo ""

# ── Optional: Portainer MCP ──────────────────────────────
read -p "Enable Portainer MCP? (Docker management) [y/N]: " ENABLE_PORTAINER
if [[ "$ENABLE_PORTAINER" =~ ^[Yy] ]]; then
    read -p "  Portainer URL (e.g. http://192.168.1.100:9000): " P_URL
    read -p "  Portainer API Token: " P_TOKEN
    sed -i.bak "s|^PORTAINER_URL=.*|PORTAINER_URL=${P_URL}|" .env
    sed -i.bak "s|^PORTAINER_TOKEN=.*|PORTAINER_TOKEN=${P_TOKEN}|" .env
    rm -f .env.bak
    docker compose build portainer-mcp
    docker compose up -d portainer-mcp
    echo "Portainer MCP started on port 8085."
fi

# ── Optional: GitHub MCP ─────────────────────────────────
read -p "Enable GitHub MCP? (repo management) [y/N]: " ENABLE_GITHUB
if [[ "$ENABLE_GITHUB" =~ ^[Yy] ]]; then
    read -p "  GitHub Personal Access Token: " G_TOKEN
    sed -i.bak "s|^GITHUB_TOKEN=.*|GITHUB_TOKEN=${G_TOKEN}|" .env
    rm -f .env.bak
    docker compose up -d github-mcp
    echo "GitHub MCP started on port 8086."
fi

# ── Register MCP servers with Claude Code ────────────────
echo ""
echo "Registering MCP servers with Claude Code..."

if command -v claude &>/dev/null; then
    claude mcp add -t http -s user odoo-rpc http://localhost:8084/mcp 2>/dev/null && \
        echo "  + odoo-rpc (port 8084)" || echo "  ~ odoo-rpc already configured"

    if [[ "$ENABLE_PORTAINER" =~ ^[Yy] ]]; then
        claude mcp add -t sse -s user portainer http://localhost:8085/sse 2>/dev/null && \
            echo "  + portainer (port 8085)" || echo "  ~ portainer already configured"
    fi

    if [[ "$ENABLE_GITHUB" =~ ^[Yy] ]]; then
        claude mcp add -t http -s user github-mcp http://localhost:8086/mcp \
            -H "Authorization: Bearer ${G_TOKEN}" 2>/dev/null && \
            echo "  + github-mcp (port 8086)" || echo "  ~ github-mcp already configured"
    fi
else
    echo "  Claude Code CLI not found. Install it first:"
    echo "  npm install -g @anthropic-ai/claude-code"
    echo ""
    echo "  Then register manually:"
    echo "  claude mcp add -t http -s user odoo-rpc http://localhost:8084/mcp"
fi

# ── Done ─────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo "Installation complete!"
echo ""
echo "Services:"
docker compose ps --format "  {{.Name}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo "Web terminal: http://localhost:8080"
echo "Config file:  ${INSTALL_DIR}/.env"
echo "══════════════════════════════════════════"
