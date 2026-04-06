#!/usr/bin/env python3
"""
Odoo Connection Manager — Qt6 cross-platform version (Windows, macOS, Linux).

Same functionality as the GTK4 version (tools/odoo_connect.py in claude.ai),
uses PySide6 for cross-platform support.
"""
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import urllib.request
import xmlrpc.client

from PySide6.QtCore import Qt, Signal, QObject, QThread
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QStackedWidget,
    QFormLayout, QLineEdit, QPushButton, QLabel, QComboBox,
    QCheckBox, QGroupBox, QMessageBox, QScrollArea, QFrame,
    QSizePolicy, QTextEdit,
)

# ── Config paths ─────────────────────────────────────────────────────
# Try project-local first, then claude.ai project, then home fallback
_candidates = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".odoo_connections"),
    os.path.expanduser("~/Проекти/odoo/odoo-18.0/claude.ai/.odoo_connections"),
    os.path.join(os.path.expanduser("~"), ".odoo_connections"),
]
CONFIG_DIR = next((d for d in _candidates if os.path.isdir(d)),
                  _candidates[0])

CONFIG_FILE = os.path.join(CONFIG_DIR, "connections.json")
LOCAL_PROFILE_FILE = os.path.join(CONFIG_DIR, "local_profile.json")
SSH_CONFIG = os.path.expanduser("~/.ssh/config")
SSH_DIR = os.path.expanduser("~/.ssh")
SESSIONS_FILE = os.path.join(CONFIG_DIR, "sessions.json")


# ── Session tracking ────────────────────────────────────────────────

def _load_sessions():
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def _is_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def get_sessions():
    import socket
    sessions = _load_sessions()
    hostname = socket.gethostname()
    cleaned = {}
    changed = False
    for name, info in sessions.items():
        if info.get("host") == hostname:
            pid = info.get("pid", 0)
            if pid and not _is_pid_alive(pid):
                changed = True
                continue
        cleaned[name] = info
    if changed:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2, ensure_ascii=False)
    return cleaned


# ── Data helpers ─────────────────────────────────────────────────────

def load_connections():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_connections(connections):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(connections, f, indent=2, ensure_ascii=False)

def load_local_profile():
    if os.path.exists(LOCAL_PROFILE_FILE):
        with open(LOCAL_PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_local_profile(profile):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(LOCAL_PROFILE_FILE, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)

def test_odoo_connection(url, db, user, api_key):
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, user, api_key, {})
    if not uid:
        raise Exception("Authentication failed.")
    version = common.version()
    return uid, version

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def save_ssh_alias(alias, hostname, ssh_user, port=22, auth="agent", identity_file=None):
    os.makedirs(os.path.dirname(SSH_CONFIG), exist_ok=True)
    block = f"\nHost {alias}\n    HostName {hostname}\n    User {ssh_user}\n"
    if port != 22:
        block += f"    Port {port}\n"
    if auth == "agent":
        block += "    ForwardAgent yes\n"
    elif auth == "key" and identity_file:
        block += f"    IdentityFile {identity_file}\n    IdentitiesOnly yes\n"
    elif auth == "password":
        block += "    PasswordAuthentication yes\n    PubkeyAuthentication no\n"
    with open(SSH_CONFIG, "a", encoding="utf-8") as f:
        f.write(block)
    try:
        os.chmod(SSH_CONFIG, 0o600)
    except OSError:
        pass

def remove_ssh_alias(alias):
    if not os.path.exists(SSH_CONFIG):
        return False
    with open(SSH_CONFIG, "r", encoding="utf-8") as f:
        lines = f.readlines()
    new, skip, removed = [], False, False
    for line in lines:
        if re.match(r"^\s*Host\s+" + re.escape(alias) + r"\s*$", line, re.IGNORECASE):
            skip, removed = True, True
            continue
        if skip and re.match(r"^\s*Host\s+", line, re.IGNORECASE):
            skip = False
        if skip and not line.strip():
            skip = False
            continue
        if not skip:
            new.append(line)
    if removed:
        with open(SSH_CONFIG, "w", encoding="utf-8") as f:
            f.writelines(new)
        try:
            os.chmod(SSH_CONFIG, 0o600)
        except OSError:
            pass
    return removed


# ── Async worker ─────────────────────────────────────────────────────

class Worker(QObject):
    finished = Signal(bool, str)

    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            result = self.func()
            self.finished.emit(True, result)
        except Exception as e:
            self.finished.emit(False, str(e))


# ── Main Window ──────────────────────────────────────────────────────

class OdooConnectWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Odoo Connection Manager")
        self.resize(900, 620)
        self.connections = load_connections()
        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        # Splitter: sidebar | content
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        # ── Sidebar ──────────────────────────────────────────
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(4, 4, 4, 4)

        # Profile button
        self.btn_profile = QPushButton("  Personal Profile")
        self.btn_profile.setObjectName("profileBtn")
        self.btn_profile.clicked.connect(self._show_profile)
        sidebar_layout.addWidget(self.btn_profile)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        sidebar_layout.addWidget(line)

        # Connection list
        self.conn_list = QListWidget()
        self.conn_list.currentItemChanged.connect(self._on_conn_selected)
        sidebar_layout.addWidget(self.conn_list)

        # Add / Delete buttons
        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("+ New")
        self.btn_add.clicked.connect(self._on_new)
        btn_row.addWidget(self.btn_add)
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.clicked.connect(self._on_delete)
        btn_row.addWidget(self.btn_delete)
        sidebar_layout.addLayout(btn_row)

        sidebar.setMaximumWidth(250)
        splitter.addWidget(sidebar)

        # ── Content stack ────────────────────────────────────
        self.stack = QStackedWidget()
        splitter.addWidget(self.stack)

        # Page 0: Connection form
        self._build_connection_page()
        # Page 1: Profile form
        self._build_profile_page()

        splitter.setSizes([220, 680])

        # Status bar
        self.statusBar().showMessage("Ready")

        # Populate sidebar
        self._refresh_sidebar()

    # ── Connection Page ──────────────────────────────────────────

    def _build_connection_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(16, 16, 16, 16)

        # Session banner
        self.session_label = QLabel("")
        self.session_label.setWordWrap(True)
        self.session_label.setVisible(False)
        self.session_label.setStyleSheet(
            "background-color: #2a6e3f; color: white; padding: 10px 14px; "
            "border-radius: 8px; font-weight: bold; font-size: 13px;")
        form.addWidget(self.session_label)

        # Server group
        grp_server = QGroupBox("Odoo Server")
        gl = QFormLayout()
        self.entry_name = QLineEdit()
        gl.addRow("Name:", self.entry_name)
        self.entry_url = QLineEdit()
        self.entry_url.setPlaceholderText("https://your-odoo.com")
        gl.addRow("URL:", self.entry_url)
        self.entry_db = QLineEdit()
        gl.addRow("Database:", self.entry_db)
        self.entry_user = QLineEdit()
        gl.addRow("User:", self.entry_user)
        self.entry_api_key = QLineEdit()
        self.entry_api_key.setEchoMode(QLineEdit.Password)
        gl.addRow("API Key:", self.entry_api_key)
        grp_server.setLayout(gl)
        form.addWidget(grp_server)

        # SSH group
        grp_ssh = QGroupBox("SSH Tunnel")
        grp_ssh.setCheckable(True)
        grp_ssh.setChecked(False)
        sl = QFormLayout()
        self.ssh_host = QLineEdit()
        sl.addRow("Host:", self.ssh_host)
        self.ssh_user = QLineEdit()
        sl.addRow("User:", self.ssh_user)
        self.ssh_port = QLineEdit("22")
        sl.addRow("Port:", self.ssh_port)
        self.ssh_auth = QComboBox()
        self.ssh_auth.addItems(["agent", "key", "password"])
        sl.addRow("Auth:", self.ssh_auth)
        self.ssh_keyfile = QLineEdit()
        self.ssh_keyfile.setPlaceholderText("~/.ssh/id_ed25519")
        sl.addRow("Key File:", self.ssh_keyfile)
        grp_ssh.setLayout(sl)
        self.grp_ssh = grp_ssh
        form.addWidget(grp_ssh)

        # Portainer group
        grp_portainer = QGroupBox("Portainer")
        grp_portainer.setCheckable(True)
        grp_portainer.setChecked(False)
        pl = QFormLayout()
        self.portainer_url = QLineEdit("http://localhost:9000")
        pl.addRow("URL:", self.portainer_url)
        self.portainer_token = QLineEdit()
        self.portainer_token.setEchoMode(QLineEdit.Password)
        pl.addRow("API Token:", self.portainer_token)
        self.portainer_readonly = QCheckBox("Read-only mode")
        pl.addRow("", self.portainer_readonly)
        btn_test_portainer = QPushButton("Test Portainer")
        btn_test_portainer.clicked.connect(self._on_test_portainer)
        pl.addRow("", btn_test_portainer)
        grp_portainer.setLayout(pl)
        self.grp_portainer = grp_portainer
        form.addWidget(grp_portainer)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_test = QPushButton("Test Odoo")
        btn_test.clicked.connect(self._on_test_odoo)
        btn_row.addWidget(btn_test)
        btn_save = QPushButton("Save")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(btn_save)
        form.addLayout(btn_row)

        form.addStretch()
        scroll.setWidget(page)
        self.stack.addWidget(scroll)  # index 0

    # ── Profile Page ─────────────────────────────────────────────

    def _build_profile_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(16, 16, 16, 16)

        # GitHub
        grp_gh = QGroupBox("GitHub")
        gl = QFormLayout()
        self.github_token = QLineEdit()
        self.github_token.setEchoMode(QLineEdit.Password)
        self.github_token.setPlaceholderText("ghp_...")
        gl.addRow("Personal Access Token:", self.github_token)
        btn_test_gh = QPushButton("Test GitHub")
        btn_test_gh.clicked.connect(self._on_test_github)
        gl.addRow("", btn_test_gh)
        grp_gh.setLayout(gl)
        form.addWidget(grp_gh)

        # SSH Keys
        grp_keys = QGroupBox("SSH Keys")
        kl = QVBoxLayout()
        self.ssh_keys_list = QTextEdit()
        self.ssh_keys_list.setReadOnly(True)
        self.ssh_keys_list.setMaximumHeight(150)
        self.ssh_keys_list.setStyleSheet("font-family: monospace; font-size: 11px;")
        kl.addWidget(self.ssh_keys_list)

        gen_layout = QFormLayout()
        self.ssh_key_name = QLineEdit("id_ed25519")
        gen_layout.addRow("Key name:", self.ssh_key_name)
        self.ssh_key_email = QLineEdit()
        self.ssh_key_email.setPlaceholderText("your@email.com")
        gen_layout.addRow("Email:", self.ssh_key_email)
        self.ssh_key_type = QComboBox()
        self.ssh_key_type.addItems(["ed25519", "rsa"])
        gen_layout.addRow("Type:", self.ssh_key_type)
        btn_gen = QPushButton("Generate SSH Key")
        btn_gen.clicked.connect(self._on_generate_ssh_key)
        gen_layout.addRow("", btn_gen)
        kl.addLayout(gen_layout)
        grp_keys.setLayout(kl)
        form.addWidget(grp_keys)

        # Save
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_save_profile = QPushButton("Save Profile")
        btn_save_profile.clicked.connect(self._on_save_profile)
        btn_row.addWidget(btn_save_profile)
        form.addLayout(btn_row)

        form.addStretch()
        scroll.setWidget(page)
        self.stack.addWidget(scroll)  # index 1

        self._load_profile()

    # ── Sidebar ──────────────────────────────────────────────────

    def _refresh_sidebar(self, select_name=None):
        self.conn_list.clear()
        sessions = get_sessions()
        for name in self.connections:
            session = sessions.get(name)
            label = name
            if session:
                claude_id = session.get("claude_id", "?")
                label = f"\U0001f7e2 {name}  [{claude_id}]"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, name)
            self.conn_list.addItem(item)
        if select_name:
            for i in range(self.conn_list.count()):
                if self.conn_list.item(i).data(Qt.UserRole) == select_name:
                    self.conn_list.setCurrentRow(i)
                    break
        elif self.conn_list.count() > 0:
            self.conn_list.setCurrentRow(0)

    def _on_conn_selected(self, current, previous):
        if current is None:
            return
        name = current.data(Qt.UserRole)
        if name and name in self.connections:
            self._load_connection(name)
            self.stack.setCurrentIndex(0)

    def _show_profile(self):
        self.conn_list.clearSelection()
        self.stack.setCurrentIndex(1)
        self._refresh_ssh_keys()

    def _on_new(self):
        self._clear_form()
        self.conn_list.clearSelection()
        self.stack.setCurrentIndex(0)
        self.entry_name.setFocus()

    # ── Load / Save / Delete Connection ──────────────────────────

    def _load_connection(self, name):
        conn = self.connections[name]

        # Show active session
        session = get_sessions().get(name)
        if session:
            cid = session.get("claude_id", "?")
            cwd = session.get("cwd", session.get("project", ""))
            host = session.get("host", "")
            started = session.get("started", "")[:16]
            self.session_label.setText(
                f"\U0001f7e2  {cid}  |  {cwd}  |  {host}  |  {started}")
            self.session_label.setVisible(True)
        else:
            self.session_label.setVisible(False)

        self.entry_name.setText(name)
        self.entry_url.setText(conn.get("url", ""))
        self.entry_db.setText(conn.get("db", ""))
        self.entry_user.setText(conn.get("user", ""))
        self.entry_api_key.setText(conn.get("api_key", ""))

        ssh = conn.get("ssh", {})
        self.grp_ssh.setChecked(bool(ssh))
        if ssh:
            self.ssh_host.setText(ssh.get("host", ""))
            self.ssh_user.setText(ssh.get("user", ""))
            self.ssh_port.setText(str(ssh.get("port", 22)))
            idx = {"agent": 0, "key": 1, "password": 2}.get(ssh.get("auth", "agent"), 0)
            self.ssh_auth.setCurrentIndex(idx)
            self.ssh_keyfile.setText(ssh.get("identity_file", ""))
        else:
            self.ssh_host.clear()
            self.ssh_user.clear()
            self.ssh_port.setText("22")
            self.ssh_keyfile.clear()

        portainer = conn.get("portainer", {})
        self.grp_portainer.setChecked(bool(portainer))
        if portainer:
            self.portainer_url.setText(portainer.get("url", "http://localhost:9000"))
            self.portainer_token.setText(portainer.get("token", ""))
            self.portainer_readonly.setChecked(portainer.get("read_only", False))
        else:
            self.portainer_url.setText("http://localhost:9000")
            self.portainer_token.clear()
            self.portainer_readonly.setChecked(False)

    def _clear_form(self):
        for w in (self.entry_name, self.entry_url, self.entry_db,
                  self.entry_user, self.entry_api_key,
                  self.ssh_host, self.ssh_user, self.ssh_keyfile,
                  self.portainer_token):
            w.clear()
        self.ssh_port.setText("22")
        self.grp_ssh.setChecked(False)
        self.grp_portainer.setChecked(False)
        self.portainer_url.setText("http://localhost:9000")
        self.portainer_readonly.setChecked(False)

    def _on_save(self):
        name = self.entry_name.text().strip()
        url = self.entry_url.text().strip().rstrip("/")
        db = self.entry_db.text().strip()
        user = self.entry_user.text().strip()
        api_key = self.entry_api_key.text().strip()

        if not name or not url or not db:
            QMessageBox.warning(self, "Error", "Name, URL and Database are required.")
            return

        conn = {"url": url, "db": db, "user": user, "api_key": api_key}

        if self.grp_ssh.isChecked():
            host = self.ssh_host.text().strip()
            suser = self.ssh_user.text().strip()
            port = int(self.ssh_port.text().strip() or 22)
            auth = self.ssh_auth.currentText()
            keyfile = self.ssh_keyfile.text().strip()
            conn["ssh"] = {
                "host": host, "user": suser, "port": port,
                "auth": auth, "identity_file": keyfile if auth == "key" else "",
            }
            if host and suser:
                save_ssh_alias(name, host, suser, port=port, auth=auth,
                               identity_file=keyfile if auth == "key" else None)

        if self.grp_portainer.isChecked():
            conn["portainer"] = {
                "url": self.portainer_url.text().strip().rstrip("/"),
                "token": self.portainer_token.text().strip(),
                "read_only": self.portainer_readonly.isChecked(),
            }

        self.connections[name] = conn
        save_connections(self.connections)
        self._refresh_sidebar(select_name=name)
        self.statusBar().showMessage(f"Saved: {name}", 3000)

    def _on_delete(self):
        name = self.entry_name.text().strip()
        if not name or name not in self.connections:
            return
        reply = QMessageBox.question(self, "Delete",
                                      f"Delete connection '{name}'?",
                                      QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            if self.connections[name].get("ssh"):
                remove_ssh_alias(name)
            del self.connections[name]
            save_connections(self.connections)
            self._clear_form()
            self._refresh_sidebar()
            self.statusBar().showMessage(f"Deleted: {name}", 3000)

    # ── Tests ────────────────────────────────────────────────────

    def _run_async(self, func, callback):
        self.thread = QThread()
        self.worker = Worker(func)
        self.worker.moveToThread(self.thread)
        self.worker.finished.connect(callback)
        self.worker.finished.connect(self.thread.quit)
        self.thread.started.connect(self.worker.run)
        self.thread.start()

    def _on_test_odoo(self):
        url = self.entry_url.text().strip().rstrip("/")
        db = self.entry_db.text().strip()
        user = self.entry_user.text().strip()
        api_key = self.entry_api_key.text().strip()
        if not all([url, db, user, api_key]):
            QMessageBox.warning(self, "Error", "Fill all fields before testing.")
            return
        self.statusBar().showMessage("Testing Odoo connection...")

        def do():
            uid, ver = test_odoo_connection(url, db, user, api_key)
            return f"Connected! UID={uid}, Odoo {ver.get('server_version', '?')}"

        self._run_async(do, self._test_result)

    def _on_test_portainer(self):
        url = self.portainer_url.text().strip().rstrip("/")
        token = self.portainer_token.text().strip()
        if not url or not token:
            QMessageBox.warning(self, "Error", "Portainer URL and Token required.")
            return
        self.statusBar().showMessage("Testing Portainer...")

        def do():
            req = urllib.request.Request(f"{url}/api/system/status",
                                         headers={"X-API-Key": token})
            with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx()) as resp:
                data = json.loads(resp.read())
                return f"Portainer {data.get('Version', '?')} OK"

        self._run_async(do, self._test_result)

    def _on_test_github(self):
        token = self.github_token.text().strip()
        if not token:
            QMessageBox.warning(self, "Error", "GitHub Token required.")
            return
        self.statusBar().showMessage("Testing GitHub...")

        def do():
            req = urllib.request.Request("https://api.github.com/user",
                                         headers={"Authorization": f"Bearer {token}",
                                                   "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                user = data.get("login", "?")
                name = data.get("name", "")
                return f"GitHub OK: {name or user} (@{user})"

        self._run_async(do, self._test_result)

    def _test_result(self, success, message):
        if success:
            self.statusBar().showMessage(f"\u2714 {message}", 5000)
        else:
            QMessageBox.critical(self, "Connection Failed", message)
            self.statusBar().showMessage("Test failed", 3000)

    # ── Profile ──────────────────────────────────────────────────

    def _load_profile(self):
        profile = load_local_profile()
        self.github_token.setText(profile.get("github_token", ""))
        self.ssh_key_email.setText(profile.get("ssh_email", ""))

    def _on_save_profile(self):
        profile = {
            "github_token": self.github_token.text().strip(),
            "ssh_email": self.ssh_key_email.text().strip(),
        }
        save_local_profile(profile)
        self.statusBar().showMessage("Profile saved.", 3000)

    def _refresh_ssh_keys(self):
        lines = []
        if os.path.isdir(SSH_DIR):
            for fname in sorted(os.listdir(SSH_DIR)):
                if fname.endswith(".pub"):
                    fpath = os.path.join(SSH_DIR, fname)
                    try:
                        with open(fpath, "r") as f:
                            pub = f.read().strip()
                        parts = pub.split()
                        ktype = parts[0] if parts else "?"
                        comment = parts[2] if len(parts) > 2 else ""
                        lines.append(f"{fname.replace('.pub',''):20s}  {ktype}  {comment}")
                    except Exception:
                        pass
        self.ssh_keys_list.setText("\n".join(lines) if lines else "No SSH keys found.")

    def _on_generate_ssh_key(self):
        name = self.ssh_key_name.text().strip() or "id_ed25519"
        email = self.ssh_key_email.text().strip()
        ktype = self.ssh_key_type.currentText()
        fpath = os.path.join(SSH_DIR, name)

        if os.path.exists(fpath):
            QMessageBox.warning(self, "Error", f"Key '{name}' already exists!")
            return

        cmd = ["ssh-keygen", "-t", ktype, "-f", fpath, "-N", ""]
        if email:
            cmd.extend(["-C", email])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                self.statusBar().showMessage(f"SSH key '{name}' generated!", 3000)
                self._refresh_ssh_keys()
            else:
                QMessageBox.critical(self, "Error", result.stderr)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))


# ── Public API ───────────────────────────────────────────────────────

def get_connection(name=None):
    connections = load_connections()
    if name:
        return connections.get(name)
    if connections:
        return next(iter(connections.values()))
    return None


DARK_STYLE = """
/* ── Base ─────────────────────────────────────────── */
QMainWindow {
    background-color: #1e1e2e;
    color: #cdd6f4;
}
QWidget {
    background-color: transparent;
    color: #cdd6f4;
    font-size: 13px;
}

/* ── Header bar (menu bar area) ──────────────────── */
QMainWindow::separator {
    background: #313244;
}
QStatusBar {
    background-color: #181825;
    color: #6c7086;
    border-top: 1px solid #313244;
    font-size: 12px;
    padding: 4px 8px;
}

/* ── Sidebar ─────────────────────────────────────── */
QSplitter::handle {
    background-color: #313244;
    width: 1px;
}

QListWidget {
    background-color: #1e1e2e;
    border: none;
    border-right: 1px solid #313244;
    outline: none;
    font-size: 13px;
}
QListWidget::item {
    padding: 10px 16px;
    border: none;
    border-radius: 8px;
    margin: 2px 6px;
}
QListWidget::item:selected {
    background-color: #45475a;
    color: #cdd6f4;
}
QListWidget::item:hover:!selected {
    background-color: #313244;
}

/* ── Cards (GroupBox) ────────────────────────────── */
QGroupBox {
    background-color: #313244;
    border: none;
    border-radius: 12px;
    margin-top: 20px;
    padding: 20px 16px 12px 16px;
    font-weight: 600;
    font-size: 14px;
    color: #cdd6f4;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 16px;
    top: 4px;
    padding: 0 8px;
    color: #89b4fa;
}
QGroupBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
}
QGroupBox::indicator:unchecked {
    border: 2px solid #585b70;
    background: transparent;
}
QGroupBox::indicator:checked {
    border: 2px solid #89b4fa;
    background: #89b4fa;
}

/* ── Inputs ──────────────────────────────────────── */
QLineEdit, QComboBox {
    background-color: #45475a;
    border: 2px solid transparent;
    border-radius: 8px;
    padding: 8px 12px;
    color: #cdd6f4;
    selection-background-color: #89b4fa;
    selection-color: #1e1e2e;
    font-size: 13px;
}
QLineEdit:focus, QComboBox:focus {
    border: 2px solid #89b4fa;
    background-color: #45475a;
}
QLineEdit::placeholder {
    color: #6c7086;
}
QComboBox::drop-down {
    border: none;
    padding-right: 8px;
}
QComboBox QAbstractItemView {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 8px;
    selection-background-color: #89b4fa;
    selection-color: #1e1e2e;
}

QTextEdit {
    background-color: #45475a;
    border: none;
    border-radius: 8px;
    padding: 8px 12px;
    color: #a6adc8;
    font-size: 12px;
}

/* ── Buttons ─────────────────────────────────────── */
QPushButton {
    background-color: #45475a;
    border: none;
    border-radius: 8px;
    padding: 8px 20px;
    color: #cdd6f4;
    font-weight: 500;
    font-size: 13px;
    min-width: 80px;
}
QPushButton:hover {
    background-color: #585b70;
}
QPushButton:pressed {
    background-color: #89b4fa;
    color: #1e1e2e;
}
QPushButton:default {
    background-color: #89b4fa;
    color: #1e1e2e;
    font-weight: 600;
}
QPushButton:default:hover {
    background-color: #74c7ec;
}

/* ── Profile button ──────────────────────────────── */
QPushButton#profileBtn {
    background-color: #313244;
    border: none;
    border-radius: 10px;
    padding: 12px 16px;
    font-size: 14px;
    font-weight: 600;
    text-align: left;
    color: #cdd6f4;
}
QPushButton#profileBtn:hover {
    background-color: #45475a;
}

/* ── Scroll area ─────────────────────────────────── */
QScrollArea {
    border: none;
    background-color: #1e1e2e;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #45475a;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #585b70;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

/* ── Checkbox ────────────────────────────────────── */
QCheckBox {
    color: #cdd6f4;
    spacing: 8px;
    font-size: 13px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 2px solid #585b70;
    background: transparent;
}
QCheckBox::indicator:checked {
    background: #89b4fa;
    border: 2px solid #89b4fa;
}

/* ── Labels ──────────────────────────────────────── */
QLabel {
    color: #a6adc8;
    font-size: 13px;
}

/* ── Separator ───────────────────────────────────── */
QFrame[frameShape="4"] {
    color: #313244;
    max-height: 1px;
}
"""


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("Odoo Connection Manager")
    # Follow system GNOME/GTK theme on Linux (via qt6-gtk-platformtheme)
    # On Windows/macOS falls back to Fusion
    pass
    win = OdooConnectWindow()
    win.show()
    sys.exit(app.exec())
