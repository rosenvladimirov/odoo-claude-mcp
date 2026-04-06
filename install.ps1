# ═══════════════════════════════════════════════════════════
# Odoo Claude MCP — Installer for Windows (PowerShell)
# ═══════════════════════════════════════════════════════════
# Run: powershell -ExecutionPolicy Bypass -File install.ps1
# ═══════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/rosenvladimirov/odoo-claude-mcp.git"
$InstallDir = "$env:USERPROFILE\odoo-claude-mcp"

Write-Host ""
Write-Host "=== Odoo Claude MCP - Windows Installer ===" -ForegroundColor Cyan
Write-Host ""

# ── Check prerequisites ──────────────────────────────────
function Check-Command($cmd, $hint) {
    if (!(Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "ERROR: $cmd is not installed." -ForegroundColor Red
        Write-Host "  $hint"
        exit 1
    }
}

Check-Command "docker" "Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
Check-Command "git" "Install Git: https://git-scm.com/downloads/win"

# Check Docker is running
try {
    docker info 2>$null | Out-Null
} catch {
    Write-Host "ERROR: Docker is not running. Start Docker Desktop first." -ForegroundColor Red
    exit 1
}

# ── Clone or update ──────────────────────────────────────
if (Test-Path $InstallDir) {
    Write-Host "Updating existing installation..."
    Set-Location $InstallDir
    git pull --ff-only
} else {
    Write-Host "Cloning repository..."
    git clone $RepoUrl $InstallDir
    Set-Location $InstallDir
}

# ── Create .env ──────────────────────────────────────────
if (!(Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "Created .env from template." -ForegroundColor Green
    $ApiKey = Read-Host "Enter your Anthropic API key (or press Enter to skip)"
    if ($ApiKey) {
        (Get-Content .env) -replace '^ANTHROPIC_API_KEY=.*', "ANTHROPIC_API_KEY=$ApiKey" | Set-Content .env
        Write-Host "API key saved." -ForegroundColor Green
    }
}

# ── Build & start core services ──────────────────────────
Write-Host ""
Write-Host "Building and starting services..."
docker compose up -d --build claude-terminal odoo-rpc-mcp

# ── Optional: Portainer MCP ──────────────────────────────
$EnablePortainer = Read-Host "Enable Portainer MCP? (Docker management) [y/N]"
if ($EnablePortainer -match '^[Yy]') {
    $PUrl = Read-Host "  Portainer URL (e.g. http://192.168.1.100:9000)"
    $PToken = Read-Host "  Portainer API Token"
    (Get-Content .env) -replace '^PORTAINER_URL=.*', "PORTAINER_URL=$PUrl" | Set-Content .env
    (Get-Content .env) -replace '^PORTAINER_TOKEN=.*', "PORTAINER_TOKEN=$PToken" | Set-Content .env
    docker compose build portainer-mcp
    docker compose up -d portainer-mcp
    Write-Host "Portainer MCP started on port 8085." -ForegroundColor Green
}

# ── Optional: GitHub MCP ─────────────────────────────────
$EnableGithub = Read-Host "Enable GitHub MCP? (repo management) [y/N]"
if ($EnableGithub -match '^[Yy]') {
    $GToken = Read-Host "  GitHub Personal Access Token"
    (Get-Content .env) -replace '^GITHUB_TOKEN=.*', "GITHUB_TOKEN=$GToken" | Set-Content .env
    docker compose up -d github-mcp
    Write-Host "GitHub MCP started on port 8086." -ForegroundColor Green
}

# ── Register MCP servers with Claude Code ────────────────
Write-Host ""
Write-Host "Registering MCP servers with Claude Code..."

if (Get-Command claude -ErrorAction SilentlyContinue) {
    claude mcp add -t http -s user odoo-rpc http://localhost:8084/mcp
    Write-Host "  + odoo-rpc (port 8084)" -ForegroundColor Green

    if ($EnablePortainer -match '^[Yy]') {
        claude mcp add -t sse -s user portainer http://localhost:8085/sse
        Write-Host "  + portainer (port 8085)" -ForegroundColor Green
    }
    if ($EnableGithub -match '^[Yy]') {
        claude mcp add -t http -s user github-mcp http://localhost:8086/mcp -H "Authorization: Bearer $GToken"
        Write-Host "  + github-mcp (port 8086)" -ForegroundColor Green
    }
} else {
    Write-Host "  Claude Code CLI not found. Install:" -ForegroundColor Yellow
    Write-Host "  npm install -g @anthropic-ai/claude-code"
}

# ── Done ─────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Installation complete! ===" -ForegroundColor Green
Write-Host ""
docker compose ps
Write-Host ""
Write-Host "Web terminal: http://localhost:8080"
Write-Host "Config file:  $InstallDir\.env"
