# Odoo Claude MCP — Documentation Site

This folder contains the **GitHub Pages** static site for `odoo-claude-mcp`.

**Live site:** https://rosenvladimirov.github.io/odoo-claude-mcp/

## Structure

```
docs/
├── index.html              # Landing page
├── .nojekyll               # Disable Jekyll processing
├── assets/
│   └── style.css           # Main stylesheet
├── screenshots/            # Product screenshots (add as you make them)
│   ├── README.md           # Placeholder instructions
│   └── .gitkeep
└── README.md               # This file
```

## Enable GitHub Pages

1. Go to repository **Settings → Pages**
2. Under **Source**, select:
   - Branch: `main`
   - Folder: `/docs`
3. Click **Save**
4. Wait ~1 minute for first deployment
5. Site will be live at `https://rosenvladimirov.github.io/odoo-claude-mcp/`

## Custom domain (optional)

To use `mcp.bl-consulting.net` or similar:

1. Create a `CNAME` file in `docs/`:
   ```
   mcp.bl-consulting.net
   ```
2. Configure DNS at your registrar:
   ```
   CNAME  mcp  rosenvladimirov.github.io.
   ```
3. Wait for DNS propagation (~5-15 minutes)
4. In GitHub Pages settings, check **Enforce HTTPS**

## Local preview

Any static web server works. Examples:

```bash
# Python 3
cd docs && python3 -m http.server 8000

# Node.js
cd docs && npx serve

# PHP
cd docs && php -S localhost:8000
```

Then open http://localhost:8000

## Updating content

The site is pure HTML/CSS — no build step required. Just edit `index.html` and `assets/style.css`, commit, and push. GitHub Pages rebuilds automatically within 30-60 seconds.

## Adding screenshots

1. Drop PNG/JPG files into `docs/screenshots/`
2. Reference them in `index.html` with relative paths:
   ```html
   <img src="screenshots/terminal.png" alt="Claude Terminal in action">
   ```

Recommended screenshots to add (see `screenshots/README.md`):

- `terminal-dark.png` — Claude Terminal with dark theme
- `connection-manager.png` — Qt connection manager GUI
- `odoo-mcp-in-claude.png` — Claude conversation using odoo-mcp tools
- `k3s-deployment.png` — Kubernetes deployment overview
- `claude-ai-connector.png` — Custom Connector configuration in Claude.ai

## Design system

- **Font pairing:** Fraunces (display, italic emphasis) + Inter Tight (body) + JetBrains Mono (code)
- **Primary color:** Odoo purple `#714B67`
- **Accent:** Warm orange `#D97757`
- **Background:** Warm off-white `#FAFAF5`
- **Philosophy:** Editorial typography, generous whitespace, subtle motion, mono-heavy technical areas

## License

Same as the main project — AGPL-3.0.
