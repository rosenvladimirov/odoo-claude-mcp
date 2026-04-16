/**
 * Claude Terminal Gateway
 *
 * Serves an HTML landing page at "/" when no ?arg= params are present.
 * Proxies all other HTTP requests and WebSocket upgrades to ttyd on
 * an internal port. This lets us show a nice info page to browsers
 * while keeping ttyd fully functional for terminal sessions.
 */

const http = require("http");
const fs = require("fs");
const net = require("net");

const LISTEN_PORT = parseInt(process.env.GATEWAY_PORT || "8080", 10);
const TTYD_PORT = parseInt(process.env.TTYD_PORT || "8081", 10);
const TTYD_HOST = "127.0.0.1";
const MCP_HOST = process.env.MCP_HOST || "odoo-rpc-mcp";
const MCP_PORT = parseInt(process.env.MCP_PORT || "8084", 10);

// Load landing page HTML
const LANDING_HTML = fs.readFileSync("/home/claude/landing.html", "utf-8");

// Paths proxied to MCP server (landing-page web login uses this for
// self-register + validation). Keep the list tight — we only want to
// expose the unauthenticated, self-authenticating endpoints.
const MCP_API_PATHS = ["/api/user/register-connection", "/health"];

function isMcpApiRequest(req) {
    const path = (req.url.split("?")[0] || "").replace(/\/+$/, "");
    return MCP_API_PATHS.some((p) => path === p || path.startsWith(p + "/"));
}

function isLandingRequest(req) {
    const url = new URL(req.url, `http://${req.headers.host}`);
    // Show landing page only for GET / without ?arg= params
    return (
        req.method === "GET" &&
        url.pathname === "/" &&
        !url.searchParams.has("arg")
    );
}

// ── HTTP server ────────────────────────────────────────────────
const server = http.createServer((req, res) => {
    if (isLandingRequest(req)) {
        res.writeHead(200, {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-cache",
        });
        res.end(LANDING_HTML);
        return;
    }

    // Proxy whitelisted /api/* paths to MCP server (web-login register
    // flow). Rewrites Host header so XMLRPC logging on the MCP side
    // reports the real client IP via X-Forwarded-For.
    if (isMcpApiRequest(req)) {
        const headers = { ...req.headers, host: `${MCP_HOST}:${MCP_PORT}` };
        const fwd = req.headers["x-forwarded-for"];
        headers["x-forwarded-for"] = fwd
            ? `${fwd}, ${req.socket.remoteAddress}`
            : req.socket.remoteAddress;
        const mcpReq = http.request(
            {
                hostname: MCP_HOST,
                port: MCP_PORT,
                path: req.url,
                method: req.method,
                headers,
            },
            (mcpRes) => {
                res.writeHead(mcpRes.statusCode, mcpRes.headers);
                mcpRes.pipe(res, { end: true });
            }
        );
        mcpReq.on("error", (err) => {
            res.writeHead(502);
            res.end(`MCP server unavailable: ${err.message}`);
        });
        req.pipe(mcpReq, { end: true });
        return;
    }

    // Proxy everything else to ttyd
    const proxyReq = http.request(
        {
            hostname: TTYD_HOST,
            port: TTYD_PORT,
            path: req.url,
            method: req.method,
            headers: req.headers,
        },
        (proxyRes) => {
            res.writeHead(proxyRes.statusCode, proxyRes.headers);
            proxyRes.pipe(res, { end: true });
        }
    );
    proxyReq.on("error", () => {
        res.writeHead(502);
        res.end("Terminal server unavailable");
    });
    req.pipe(proxyReq, { end: true });
});

// ── WebSocket upgrade (ttyd uses this) ─────────────────────────
server.on("upgrade", (req, socket, head) => {
    const proxySocket = net.connect(TTYD_PORT, TTYD_HOST, () => {
        // Forward the original HTTP upgrade request
        const reqLine = `${req.method} ${req.url} HTTP/1.1\r\n`;
        const headers = Object.entries(req.headers)
            .map(([k, v]) => `${k}: ${v}`)
            .join("\r\n");
        proxySocket.write(reqLine + headers + "\r\n\r\n");
        if (head && head.length) proxySocket.write(head);
        // Bi-directional pipe
        socket.pipe(proxySocket);
        proxySocket.pipe(socket);
    });
    proxySocket.on("error", () => socket.destroy());
    socket.on("error", () => proxySocket.destroy());
});

server.listen(LISTEN_PORT, "0.0.0.0", () => {
    console.log(`Gateway listening on :${LISTEN_PORT}, proxying to ttyd :${TTYD_PORT}`);
});
