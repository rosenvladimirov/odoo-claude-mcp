/**
 * Claude Terminal Gateway — Phase 2.2 per-session container dispatcher.
 *
 * Each WebSocket connection results in a fresh Docker container running
 * the claude-terminal-session image. Container env carries the user's
 * Odoo + MCP credentials; bind mount maps the persistent slice from
 * /srv/claude-terminal-shared/users/${MCP_PROFILE} into /home/claude.
 * On WebSocket close the container is killed and AutoRemove cleans up.
 *
 * Trust model:
 *   • Browser → gateway via WebSocket (auth params in URL).
 *   • Gateway → MCP /api/user/register-connection to validate.
 *   • Gateway spawns container; container has no docker.sock, no caps,
 *     read-only rootfs, tmpfs /tmp + /run, memory + CPU + pids limits.
 *   • Session containers cannot reach each other or peer at /proc.
 */

"use strict";

const http = require("http");
const fs = require("fs");
const net = require("net");
const path = require("path");
const crypto = require("crypto");
const Docker = require("dockerode");

// ── Config from env ────────────────────────────────────────────
const LISTEN_PORT = parseInt(process.env.GATEWAY_PORT || "8080", 10);
const MCP_HOST = process.env.MCP_HOST || "odoo-rpc-mcp";
const MCP_PORT = parseInt(process.env.MCP_PORT || "8084", 10);
const MCP_URL_INTERNAL = `http://${MCP_HOST}:${MCP_PORT}`;

// Session image + bind path on host.
const SESSION_IMAGE = process.env.SESSION_IMAGE || "vladimirovrosen/claude-terminal-session:3.0.0";
const SESSION_NETWORK = process.env.SESSION_NETWORK || "claude-session-bridge";
const SHARED_HOST_PATH = process.env.SHARED_HOST_PATH || "/srv/claude-terminal-shared/users";

// Capacity safeguards.
const MAX_CONCURRENT = parseInt(process.env.MCP_SESSION_MAX_CONCURRENT || "30", 10);
const PER_USER_LIMIT = parseInt(process.env.MCP_SESSION_PER_USER_LIMIT || "2", 10);
const IDLE_TIMEOUT_MS = parseInt(process.env.MCP_SESSION_IDLE_TIMEOUT || "1800", 10) * 1000;

// Resource limits per session container.
const SESSION_MEMORY_MB = parseInt(process.env.MCP_SESSION_MEMORY_MB || "512", 10);
const SESSION_CPU_QUOTA = parseInt(process.env.MCP_SESSION_CPU_QUOTA || "50000", 10);   // 0.5 CPU @ 100ms period
const SESSION_PIDS_LIMIT = parseInt(process.env.MCP_SESSION_PIDS_LIMIT || "100", 10);

// ── Static assets (loaded once at boot) ─────────────────────────
const LANDING_HTML = fs.readFileSync(path.join(__dirname, "landing.html"), "utf-8");
const TOOLS_HTML = (() => {
    try { return fs.readFileSync(path.join(__dirname, "tools.html"), "utf-8"); }
    catch { return null; }
})();

// ── Active session registry ─────────────────────────────────────
// id → { containerId, profile, startedAt, idleTimer, ws }
const SESSIONS = new Map();
function activeCount() { return SESSIONS.size; }
function activeForProfile(profile) {
    let n = 0;
    for (const s of SESSIONS.values()) if (s.profile === profile) n++;
    return n;
}

// ── Docker client (talks to /var/run/docker.sock mounted in image) ──
const docker = new Docker({ socketPath: "/var/run/docker.sock" });

// ── HTTP helpers ────────────────────────────────────────────────
function isLandingRequest(req) {
    const url = new URL(req.url, `http://${req.headers.host}`);
    return req.method === "GET" && url.pathname === "/" && !url.searchParams.has("arg");
}

const MCP_API_PATHS = ["/api/user/register-connection", "/api/identify", "/health"];
function isMcpApiRequest(req) {
    const path = (req.url.split("?")[0] || "").replace(/\/+$/, "");
    return MCP_API_PATHS.some((p) => path === p || path.startsWith(p + "/"));
}

function proxyToMcp(req, res) {
    const headers = { ...req.headers, host: `${MCP_HOST}:${MCP_PORT}` };
    const fwd = req.headers["x-forwarded-for"];
    headers["x-forwarded-for"] = fwd
        ? `${fwd}, ${req.socket.remoteAddress}`
        : req.socket.remoteAddress;
    const mcpReq = http.request({
        hostname: MCP_HOST, port: MCP_PORT, path: req.url, method: req.method, headers,
    }, (mcpRes) => {
        res.writeHead(mcpRes.statusCode, mcpRes.headers);
        mcpRes.pipe(res, { end: true });
    });
    mcpReq.on("error", (err) => {
        res.writeHead(502);
        res.end(`MCP server unavailable: ${err.message}`);
    });
    req.pipe(mcpReq, { end: true });
}

// ── HTTP server ─────────────────────────────────────────────────
const server = http.createServer((req, res) => {
    if (isLandingRequest(req)) {
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache" });
        return res.end(LANDING_HTML);
    }
    const docPath = (req.url.split("?")[0] || "").replace(/\/+$/, "");
    if (req.method === "GET" && TOOLS_HTML && (docPath === "/tools.html" || docPath === "/tools")) {
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "public, max-age=300" });
        return res.end(TOOLS_HTML);
    }
    if (req.method === "GET" && (docPath === "/index.html" || docPath === "/index")) {
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        return res.end(LANDING_HTML);
    }
    if (req.method === "GET" && docPath === "/health") {
        const body = JSON.stringify({
            status: "ok",
            active_sessions: activeCount(),
            max_concurrent: MAX_CONCURRENT,
            session_image: SESSION_IMAGE,
        });
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(body);
    }
    if (isMcpApiRequest(req)) return proxyToMcp(req, res);
    res.writeHead(404);
    res.end("not found");
});

// ── URL param parser ────────────────────────────────────────────
// ttyd uses ?arg=KEY=VALUE&arg=KEY2=VALUE2 — preserved for compat.
function parseUrlArgs(url) {
    const u = new URL(url, "http://x");
    const args = {};
    for (const v of u.searchParams.getAll("arg")) {
        const eq = v.indexOf("=");
        if (eq > 0) args[v.slice(0, eq)] = v.slice(eq + 1);
    }
    return args;
}

// ── Validate API key + resolve MCP profile ─────────────────────
function mcpRegister(args) {
    return new Promise((resolve, reject) => {
        const payload = JSON.stringify({
            name:    args.USER_NAME || "User",
            alias:   "default",
            url:     args.ODOO_URL || "",
            db:      args.ODOO_DB || "",
            login:   args.USER_LOGIN || args.ODOO_USER || "admin",
            api_key: args.API_KEY || "",
            active:  true,
        });
        const req = http.request({
            hostname: MCP_HOST,
            port: MCP_PORT,
            path: "/api/user/register-connection",
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Content-Length": Buffer.byteLength(payload),
                "User-Agent": "ClaudeTerminalGateway/3.0",
            },
            timeout: 10000,
        }, (r) => {
            const chunks = [];
            r.on("data", (c) => chunks.push(c));
            r.on("end", () => {
                const body = Buffer.concat(chunks).toString();
                if (r.statusCode >= 400) return reject(new Error(`MCP ${r.statusCode}: ${body}`));
                try {
                    const j = JSON.parse(body);
                    const profile = j.profile || j.owner;
                    if (!profile) return reject(new Error("MCP returned no profile"));
                    resolve({ profile, raw: j });
                } catch (e) { reject(new Error(`MCP non-JSON: ${e.message}`)); }
            });
        });
        req.on("error", reject);
        req.on("timeout", () => req.destroy(new Error("MCP timeout")));
        req.write(payload);
        req.end();
    });
}

// ── Spawn one session container ─────────────────────────────────
async function spawnSession(args, profile, sessionId) {
    // Ensure persistent host dir exists (created lazily on first session).
    // chown to uid 1000 (claude) so the container can write into the bind
    // mount; gateway runs as root and would otherwise leave the dir
    // root-owned, denying the in-container user.
    const hostDir = path.join(SHARED_HOST_PATH, profile);
    try {
        fs.mkdirSync(hostDir, { recursive: true, mode: 0o700 });
        fs.chownSync(hostDir, 1000, 1000);
    } catch (e) {
        console.error(`[session ${sessionId}] hostDir provisioning fail: ${e.message}`);
    }

    const containerName = `session-${sessionId}`;
    const env = [
        `API_KEY=${args.API_KEY}`,
        `ODOO_URL=${args.ODOO_URL}`,
        `ODOO_DB=${args.ODOO_DB}`,
        `USER_LOGIN=${args.USER_LOGIN || args.ODOO_USER || "admin"}`,
        `USER_NAME=${args.USER_NAME || ""}`,
        `USER_EMAIL=${args.USER_EMAIL || ""}`,
        `ODOO_USER=${args.ODOO_USER || "admin"}`,
        `ODOO_UID=${args.ODOO_UID || "0"}`,
        `ODOO_MODEL=${args.ODOO_MODEL || ""}`,
        `ODOO_RES_ID=${args.ODOO_RES_ID || "0"}`,
        `ODOO_VIEW_TYPE=${args.ODOO_VIEW_TYPE || "form"}`,
        `TERMINAL_URL=${args.TERMINAL_URL || ""}`,
        `SESSION_ID=${sessionId}`,
        `MCP_PROFILE=${profile}`,
        `MCP_URL=${MCP_URL_INTERNAL}`,
        `CLAUDE_THEME=${args.CLAUDE_THEME || process.env.CLAUDE_THEME || "github"}`,
    ];

    const create = await docker.createContainer({
        Image: SESSION_IMAGE,
        name: containerName,
        Env: env,
        Tty: true,                  // for terminal experience
        OpenStdin: false,           // ttyd handles stdio inside
        AttachStdin: false,
        AttachStdout: false,
        AttachStderr: false,
        ExposedPorts: { "8081/tcp": {} },
        HostConfig: {
            // AutoRemove disabled in dev so failed containers can be inspected;
            // re-enable in prod (or rely on the orphan reaper for cleanup).
            AutoRemove: process.env.MCP_SESSION_AUTOREMOVE !== "0",
            Binds: [`${hostDir}:/home/claude:rw`],
            ReadonlyRootfs: true,
            Tmpfs: {
                "/tmp": "rw,noexec,nosuid,size=64m",
                "/run": "rw,noexec,nosuid,size=16m",
            },
            Memory: SESSION_MEMORY_MB * 1024 * 1024,
            MemorySwap: SESSION_MEMORY_MB * 1024 * 1024,   // disable swap
            CpuQuota: SESSION_CPU_QUOTA,
            CpuPeriod: 100000,
            PidsLimit: SESSION_PIDS_LIMIT,
            CapDrop: ["ALL"],
            SecurityOpt: ["no-new-privileges"],
            NetworkMode: SESSION_NETWORK,
            RestartPolicy: { Name: "no" },
        },
        Labels: {
            "com.blconsulting.service": "claude-session",
            "com.blconsulting.profile": profile,
            "com.blconsulting.session-id": sessionId,
        },
    });

    await create.start();

    // Wait for ttyd to come up — poll its / endpoint.
    const containerHostInternal = containerName;        // resolved by Docker DNS
    const TTYD_READY_TIMEOUT_MS = 6000;
    const TTYD_POLL_INTERVAL_MS = 100;
    const start = Date.now();
    while (Date.now() - start < TTYD_READY_TIMEOUT_MS) {
        try {
            await new Promise((resolve, reject) => {
                const r = http.get(`http://${containerHostInternal}:8081/`, (res) => {
                    res.resume();
                    if (res.statusCode === 200) resolve();
                    else reject(new Error(`ttyd HTTP ${res.statusCode}`));
                });
                r.on("error", reject);
                r.setTimeout(500, () => r.destroy(new Error("ttyd probe timeout")));
            });
            return { container: create, hostInternal: containerHostInternal };
        } catch (_) {
            await new Promise((r) => setTimeout(r, TTYD_POLL_INTERVAL_MS));
        }
    }
    // Failed to come up — kill and surface.
    try { await create.stop({ t: 1 }); } catch {}
    throw new Error(`session ${sessionId} ttyd did not become ready in ${TTYD_READY_TIMEOUT_MS}ms`);
}

// ── WebSocket upgrade — auth, spawn, proxy ─────────────────────
server.on("upgrade", async (req, clientSocket, head) => {
    const args = parseUrlArgs(req.url);

    // Required params
    if (!args.API_KEY || !args.ODOO_URL || !args.ODOO_DB) {
        clientSocket.write(
            "HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n\r\n" +
            "Missing API_KEY, ODOO_URL or ODOO_DB in ?arg= params"
        );
        return clientSocket.destroy();
    }

    // Capacity gate
    if (activeCount() >= MAX_CONCURRENT) {
        clientSocket.write(
            "HTTP/1.1 503 Service Unavailable\r\nRetry-After: 30\r\n\r\n" +
            "Capacity reached — try again in 30 seconds"
        );
        return clientSocket.destroy();
    }

    let profile;
    try {
        const auth = await mcpRegister(args);
        profile = auth.profile;
    } catch (e) {
        console.error(`[auth] failed: ${e.message}`);
        clientSocket.write(
            "HTTP/1.1 401 Unauthorized\r\nContent-Type: text/plain\r\n\r\n" +
            `Authentication failed: ${e.message}`
        );
        return clientSocket.destroy();
    }

    if (activeForProfile(profile) >= PER_USER_LIMIT) {
        clientSocket.write(
            "HTTP/1.1 429 Too Many Requests\r\nContent-Type: text/plain\r\n\r\n" +
            `Per-user session limit (${PER_USER_LIMIT}) reached. Close another tab first.`
        );
        return clientSocket.destroy();
    }

    const sessionId = crypto.randomBytes(8).toString("hex");
    const t0 = Date.now();
    let spawned;
    try {
        spawned = await spawnSession(args, profile, sessionId);
    } catch (e) {
        console.error(`[session ${sessionId}] spawn failed: ${e.message}`);
        clientSocket.write(
            "HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n\r\n" +
            `Spawn failed: ${e.message}`
        );
        return clientSocket.destroy();
    }
    const spawnMs = Date.now() - t0;

    // Connect to container's ttyd, forward original WS upgrade.
    const upstream = net.connect(8081, spawned.hostInternal, () => {
        const reqLine = `${req.method} ${req.url} HTTP/1.1\r\n`;
        const headers = Object.entries(req.headers)
            .map(([k, v]) => `${k}: ${v}`).join("\r\n");
        upstream.write(reqLine + headers + "\r\n\r\n");
        if (head && head.length) upstream.write(head);
        clientSocket.pipe(upstream);
        upstream.pipe(clientSocket);
    });

    // Idle timeout — kill container if no activity.
    let idleTimer = setTimeout(() => {
        console.warn(`[session ${sessionId}] idle timeout, killing container`);
        cleanup("idle-timeout");
    }, IDLE_TIMEOUT_MS);
    function bumpIdle() {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => cleanup("idle-timeout"), IDLE_TIMEOUT_MS);
    }
    clientSocket.on("data", bumpIdle);
    upstream.on("data", bumpIdle);

    // Cleanup once on whatever closes first.
    let cleaned = false;
    function cleanup(reason) {
        if (cleaned) return;
        cleaned = true;
        clearTimeout(idleTimer);
        SESSIONS.delete(sessionId);
        try { upstream.destroy(); } catch {}
        try { clientSocket.destroy(); } catch {}
        spawned.container.stop({ t: 2 }).catch(() => {});
        const elapsedSec = ((Date.now() - t0) / 1000).toFixed(1);
        console.log(`[session ${sessionId}] closed (${reason}) profile=${profile} duration=${elapsedSec}s`);
    }
    clientSocket.on("close", () => cleanup("client-close"));
    clientSocket.on("error", () => cleanup("client-error"));
    upstream.on("close", () => cleanup("upstream-close"));
    upstream.on("error", () => cleanup("upstream-error"));

    SESSIONS.set(sessionId, {
        containerId: spawned.container.id,
        profile,
        startedAt: t0,
        idleTimer,
        ws: clientSocket,
    });

    console.log(`[session ${sessionId}] spawned profile=${profile} spawn_ms=${spawnMs} active=${activeCount()}/${MAX_CONCURRENT}`);
});

// ── Periodic orphan-container reaper ────────────────────────────
// Catches containers whose gateway tracking was lost (e.g. gateway
// restarted while sessions were live).
async function reapOrphans() {
    try {
        const containers = await docker.listContainers({
            all: true,
            filters: JSON.stringify({ label: ["com.blconsulting.service=claude-session"] }),
        });
        for (const c of containers) {
            const sid = c.Labels["com.blconsulting.session-id"];
            if (!SESSIONS.has(sid)) {
                console.warn(`[reap] orphan ${c.Names[0]} → killing`);
                try { await docker.getContainer(c.Id).stop({ t: 2 }); } catch {}
            }
        }
    } catch (e) {
        console.error(`[reap] error: ${e.message}`);
    }
}
setInterval(reapOrphans, 60_000);

// ── Graceful shutdown ───────────────────────────────────────────
async function shutdown(signal) {
    console.log(`[gateway] ${signal} — terminating ${activeCount()} active sessions`);
    server.close();
    const tasks = [];
    for (const [sid, s] of SESSIONS) {
        tasks.push(docker.getContainer(s.containerId).stop({ t: 2 }).catch(() => {}));
        try { s.ws.destroy(); } catch {}
    }
    await Promise.all(tasks);
    process.exit(0);
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));

// ── Start ──────────────────────────────────────────────────────
server.listen(LISTEN_PORT, "0.0.0.0", () => {
    console.log(`[gateway] listening :${LISTEN_PORT} → spawning ${SESSION_IMAGE}`);
    console.log(`[gateway] capacity=${MAX_CONCURRENT} per_user=${PER_USER_LIMIT} idle=${IDLE_TIMEOUT_MS / 1000}s`);
    console.log(`[gateway] mcp=${MCP_URL_INTERNAL} shared_host=${SHARED_HOST_PATH}`);
});
