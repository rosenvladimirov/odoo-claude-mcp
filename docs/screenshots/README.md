# Screenshots

Add product screenshots here for use in `index.html` and the main `README.md`.

## Recommended shots

### 1. `terminal-dark.png`
**What:** Claude Terminal (`claude-terminal/`) with a dark theme (Catppuccin or Tokyo Night)
**Why:** Shows the browser-based Claude Code experience — the most visually impressive feature
**How:** Launch `claude-terminal`, pick a dark theme from the header, run a real Claude Code session
**Size:** 1600×1000 (retina-ready, cropped to terminal + surrounding chrome)

### 2. `connection-manager.png`
**What:** `tools/odoo_connect_qt.py` GUI with 5-10 connections listed
**Why:** Shows the multi-tenant management UX
**How:** Run the Qt connection manager, populate with example Odoo instances
**Size:** 1200×800

### 3. `odoo-mcp-in-claude.png`
**What:** Claude.ai or Claude Code conversation where it's using odoo-mcp tools
**Why:** Demonstrates the end-user experience — this is why it matters
**How:** Real conversation; redact any sensitive customer data
**Size:** 1400×900

### 4. `k3s-deployment.png`
**What:** `kubectl get pods` output or Lens/k9s showing the full stack running
**Why:** Proof that it really runs on K8s in production
**How:** Screenshot a real K3s cluster running the `prod` overlay
**Size:** 1400×700

### 5. `claude-ai-connector.png`
**What:** Custom Connector configuration screen in Claude.ai Settings
**Why:** Shows the Claude.ai integration path is real
**How:** Screenshot the Claude.ai settings (redact your actual token)
**Size:** 1200×800

### 6. `architecture-render.png` (optional)
**What:** Higher-fidelity rendered version of the ASCII architecture diagram
**Why:** Better for social sharing / documentation export
**How:** Excalidraw, draw.io, or Figma; export as PNG with white or purple background
**Size:** 1600×1000

## Screenshot guidelines

- **Format:** PNG with transparency where appropriate, otherwise JPG with quality 85
- **DPI:** 144 DPI (retina) preferred
- **Dimensions:** Width 1200-1600px for product shots, square/portrait for social
- **Annotations:** Keep minimal — no ugly red arrows. Use subtle highlighting or crop instead
- **Redaction:** Blur anything customer-specific; use black rectangles consistently
- **Compression:** Run through `imagemin` or `squoosh.app` before committing

## Delete this file

Once real screenshots are in place, delete this README.md — the screenshots themselves are self-explanatory.
