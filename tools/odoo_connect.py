#!/usr/bin/env python3
"""
Odoo Connection Manager — GNOME Settings style with sidebar navigation.
"""
import json
import os
import re
import threading
import xmlrpc.client

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GLib, Gio  # noqa: E402

CONFIG_DIR = os.environ.get(
    "ODOO_CONNECTIONS_DIR",
    os.path.join(os.path.expanduser("~"), "odoo-claude-connections"),
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "connections.json")
SSH_CONFIG = os.path.expanduser("~/.ssh/config")


# ── Data helpers ──────────────────────────────────────────────────────


def load_connections():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_connections(connections):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(connections, f, indent=2, ensure_ascii=False)


def test_connection(url, db, user, api_key):
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, user, api_key, {})
    if not uid:
        raise Exception("Authentication failed.")
    version = common.version()
    return uid, version


# ── SSH helpers ───────────────────────────────────────────────────────


def _parse_ssh_hosts():
    hosts = set()
    if not os.path.exists(SSH_CONFIG):
        return hosts
    with open(SSH_CONFIG, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\s*Host\s+(.+)", line, re.IGNORECASE)
            if m:
                for h in m.group(1).split():
                    if "*" not in h and "?" not in h:
                        hosts.add(h)
    return hosts


def save_ssh_alias(alias, hostname, ssh_user, port=22, auth="agent", identity_file=None):
    if alias in _parse_ssh_hosts():
        return False, f"SSH alias '{alias}' already exists."
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
    os.chmod(SSH_CONFIG, 0o600)
    return True, f"SSH alias '{alias}' added."


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
        os.chmod(SSH_CONFIG, 0o600)
    return removed


# ── Application ───────────────────────────────────────────────────────


class OdooConnectApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.blconsulting.odoo_connect",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.connections = load_connections()

    def do_activate(self):
        win = OdooConnectWindow(application=self, connections=self.connections)
        win.present()


class OdooConnectWindow(Adw.ApplicationWindow):
    def __init__(self, connections, **kwargs):
        super().__init__(**kwargs)
        self.connections = connections
        self.set_title("Odoo Connections")
        self.set_default_size(860, 560)

        # Toast overlay wraps everything
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # Navigation split view (sidebar + content) like GNOME Settings
        self.split = Adw.NavigationSplitView()
        self.toast_overlay.set_child(self.split)

        # ── Sidebar ──────────────────────────────────────────────
        sidebar_page = Adw.NavigationPage(title="Connections")
        self.split.set_sidebar(sidebar_page)

        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_page.set_child(sidebar_box)

        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_show_end_title_buttons(False)
        btn_add = Gtk.Button(icon_name="list-add-symbolic")
        btn_add.set_tooltip_text("New connection")
        btn_add.connect("clicked", self._on_new)
        sidebar_header.pack_start(btn_add)
        sidebar_box.append(sidebar_header)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        sidebar_box.append(scroll)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self._on_row_selected)
        scroll.set_child(self.listbox)

        # ── Content area ─────────────────────────────────────────
        self.content_page = Adw.NavigationPage(title="Connection Details")
        self.split.set_content(self.content_page)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content_page.set_child(content_box)

        content_header = Adw.HeaderBar()
        self.spinner = Gtk.Spinner()
        content_header.pack_start(self.spinner)

        btn_test = Gtk.Button(label="Test")
        btn_test.add_css_class("suggested-action")
        btn_test.connect("clicked", self._on_test)
        content_header.pack_end(btn_test)

        btn_save = Gtk.Button(label="Save")
        btn_save.connect("clicked", self._on_save)
        content_header.pack_end(btn_save)

        btn_delete = Gtk.Button(icon_name="user-trash-symbolic")
        btn_delete.add_css_class("destructive-action")
        btn_delete.set_tooltip_text("Delete connection")
        btn_delete.connect("clicked", self._on_delete)
        content_header.pack_end(btn_delete)

        content_box.append(content_header)

        # Scrollable form
        form_scroll = Gtk.ScrolledWindow(vexpand=True)
        content_box.append(form_scroll)

        clamp = Adw.Clamp(maximum_size=560, margin_top=16, margin_bottom=16,
                          margin_start=16, margin_end=16)
        form_scroll.set_child(clamp)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(form)

        # ── Odoo connection group ────────────────────────────────
        grp = Adw.PreferencesGroup(title="Server")
        form.append(grp)

        self.entry_name = Adw.EntryRow(title="Name")
        grp.add(self.entry_name)
        self.entry_url = Adw.EntryRow(title="URL")
        grp.add(self.entry_url)
        self.entry_db = Adw.EntryRow(title="Database")
        grp.add(self.entry_db)

        grp_auth = Adw.PreferencesGroup(title="Authentication")
        form.append(grp_auth)

        self.entry_user = Adw.EntryRow(title="User")
        grp_auth.add(self.entry_user)
        self.entry_api_key = Adw.PasswordEntryRow(title="API Key")
        grp_auth.add(self.entry_api_key)

        # ── SSH group (expander) ─────────────────────────────────
        grp_ssh = Adw.PreferencesGroup(title="SSH Tunnel")
        form.append(grp_ssh)

        self.ssh_expander = Adw.ExpanderRow(
            title="SSH Access",
            subtitle="Create alias in ~/.ssh/config",
            show_enable_switch=True,
            enable_expansion=False,
        )
        grp_ssh.add(self.ssh_expander)

        self.ssh_host = Adw.EntryRow(title="Host")
        self.ssh_expander.add_row(self.ssh_host)
        self.ssh_user = Adw.EntryRow(title="User")
        self.ssh_expander.add_row(self.ssh_user)
        self.ssh_port = Adw.EntryRow(title="Port")
        self.ssh_expander.add_row(self.ssh_port)

        self.ssh_auth_row = Adw.ComboRow(title="Auth")
        self.ssh_auth_row.set_model(Gtk.StringList.new(["agent", "key", "password"]))
        self.ssh_auth_row.connect("notify::selected", self._on_ssh_auth_changed)
        self.ssh_expander.add_row(self.ssh_auth_row)

        self.ssh_keyfile = Adw.EntryRow(title="Key File")
        self.ssh_keyfile.set_visible(False)
        self.ssh_expander.add_row(self.ssh_keyfile)

        self.ssh_password = Adw.PasswordEntryRow(title="SSH Password")
        self.ssh_password.set_visible(False)
        self.ssh_expander.add_row(self.ssh_password)

        # SSH test button row
        ssh_test_row = Adw.ActionRow(title="Test SSH", subtitle="Try connecting via SSH")
        btn_ssh_test = Gtk.Button(icon_name="network-server-symbolic")
        btn_ssh_test.set_valign(Gtk.Align.CENTER)
        btn_ssh_test.add_css_class("suggested-action")
        btn_ssh_test.set_tooltip_text("Test SSH connection")
        btn_ssh_test.connect("clicked", self._on_test_ssh)
        ssh_test_row.add_suffix(btn_ssh_test)
        self.ssh_spinner = Gtk.Spinner()
        self.ssh_spinner.set_valign(Gtk.Align.CENTER)
        ssh_test_row.add_suffix(self.ssh_spinner)
        self.ssh_expander.add_row(ssh_test_row)

        # Populate sidebar
        self._refresh_sidebar()

    # ── Sidebar management ────────────────────────────────────────

    def _refresh_sidebar(self, select_name=None):
        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)

        for name in self.connections:
            conn = self.connections[name]
            row = Adw.ActionRow(title=name, subtitle=conn.get("url", ""))
            row.set_activatable(True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row._conn_name = name
            self.listbox.append(row)

        # Select requested or first
        target = select_name or (list(self.connections.keys())[0] if self.connections else None)
        if target:
            for i in range(len(self.connections)):
                row = self.listbox.get_row_at_index(i)
                if row and getattr(row, "_conn_name", None) == target:
                    self.listbox.select_row(row)
                    break

    def _on_row_selected(self, listbox, row):
        if row is None:
            return
        name = getattr(row, "_conn_name", None)
        if name and name in self.connections:
            self._load_connection(name)
            self.content_page.set_title(name)

    def _on_new(self, _btn):
        self._clear_form()
        self.content_page.set_title("New Connection")
        self.listbox.unselect_all()
        self.entry_name.grab_focus()

    # ── Load into form ────────────────────────────────────────────

    def _load_connection(self, name):
        conn = self.connections[name]
        self.entry_name.set_text(name)
        self.entry_url.set_text(conn.get("url", ""))
        self.entry_db.set_text(conn.get("db", ""))
        self.entry_user.set_text(conn.get("user", ""))
        self.entry_api_key.set_text(conn.get("api_key", ""))

        ssh = conn.get("ssh", {})
        if ssh:
            self.ssh_expander.set_enable_expansion(True)
            self.ssh_expander.set_expanded(True)
            self.ssh_host.set_text(ssh.get("host", ""))
            self.ssh_user.set_text(ssh.get("user", ""))
            self.ssh_port.set_text(str(ssh.get("port", 22)))
            auth_idx = {"agent": 0, "key": 1, "password": 2}.get(ssh.get("auth", "agent"), 0)
            self.ssh_auth_row.set_selected(auth_idx)
            self.ssh_keyfile.set_text(ssh.get("identity_file", ""))
            # Toggle visibility based on auth
            self.ssh_keyfile.set_visible(auth_idx == 1)
            self.ssh_password.set_visible(auth_idx == 2)
            self.ssh_password.set_text("")
        else:
            self.ssh_expander.set_enable_expansion(False)
            self.ssh_expander.set_expanded(False)
            self.ssh_host.set_text("")
            self.ssh_user.set_text("")
            self.ssh_port.set_text("")
            self.ssh_keyfile.set_text("")
            self.ssh_password.set_text("")
            self.ssh_auth_row.set_selected(0)
            self.ssh_keyfile.set_visible(False)
            self.ssh_password.set_visible(False)

    def _on_ssh_auth_changed(self, combo, _pspec):
        idx = combo.get_selected()
        # 0=agent, 1=key, 2=password
        self.ssh_keyfile.set_visible(idx == 1)
        self.ssh_password.set_visible(idx == 2)

    def _clear_form(self):
        for w in (self.entry_name, self.entry_url, self.entry_db,
                  self.entry_user, self.entry_api_key,
                  self.ssh_host, self.ssh_user, self.ssh_port,
                  self.ssh_keyfile, self.ssh_password):
            w.set_text("")
        self.ssh_expander.set_enable_expansion(False)
        self.ssh_expander.set_expanded(False)
        self.ssh_auth_row.set_selected(0)
        self.ssh_keyfile.set_visible(False)
        self.ssh_password.set_visible(False)

    # ── Save ──────────────────────────────────────────────────────

    def _on_save(self, _btn):
        name = self.entry_name.get_text().strip()
        url = self.entry_url.get_text().strip().rstrip("/")
        db = self.entry_db.get_text().strip()
        user = self.entry_user.get_text().strip()
        api_key = self.entry_api_key.get_text().strip()

        if not name or not url or not db:
            self._toast("Name, URL and Database are required.")
            return

        conn = {"url": url, "db": db, "user": user, "api_key": api_key}

        if self.ssh_expander.get_enable_expansion():
            host = self.ssh_host.get_text().strip()
            suser = self.ssh_user.get_text().strip()
            port = int(self.ssh_port.get_text().strip() or 22)
            auth_idx = self.ssh_auth_row.get_selected()
            auth = ["agent", "key", "password"][auth_idx]
            keyfile = self.ssh_keyfile.get_text().strip()

            conn["ssh"] = {
                "host": host, "user": suser, "port": port,
                "auth": auth, "identity_file": keyfile if auth == "key" else "",
            }
            if host and suser:
                save_ssh_alias(name, host, suser, port=port, auth=auth,
                               identity_file=keyfile if auth == "key" else None)

        self.connections[name] = conn
        save_connections(self.connections)
        self._refresh_sidebar(select_name=name)
        self._toast(f"Saved: {name}")

    # ── Delete ────────────────────────────────────────────────────

    def _on_delete(self, _btn):
        name = self.entry_name.get_text().strip()
        if not name or name not in self.connections:
            return
        if self.connections[name].get("ssh"):
            remove_ssh_alias(name)
        del self.connections[name]
        save_connections(self.connections)
        self._clear_form()
        self._refresh_sidebar()
        self.content_page.set_title("Connection Details")
        self._toast(f"Deleted: {name}")

    # ── Test (async) ──────────────────────────────────────────────

    def _on_test(self, _btn):
        url = self.entry_url.get_text().strip().rstrip("/")
        db = self.entry_db.get_text().strip()
        user = self.entry_user.get_text().strip()
        api_key = self.entry_api_key.get_text().strip()

        if not all([url, db, user, api_key]):
            self._toast("Fill all fields before testing.")
            return

        self.spinner.start()

        def _do():
            try:
                uid, version = test_connection(url, db, user, api_key)
                sv = version.get("server_version", "?")
                GLib.idle_add(self._test_done, True, f"Connected! UID={uid}, Odoo {sv}")
            except Exception as e:
                GLib.idle_add(self._test_done, False, str(e))

        threading.Thread(target=_do, daemon=True).start()

    def _test_done(self, success, message):
        self.spinner.stop()
        if success:
            self._toast(f"\u2714 {message}")
        else:
            # Show error in a dialog so the user can read it
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="Connection Failed",
                body=message,
            )
            dialog.add_response("ok", "OK")
            dialog.present()

    # ── Test SSH ──────────────────────────────────────────────────

    def _on_test_ssh(self, _btn):
        host = self.ssh_host.get_text().strip()
        user = self.ssh_user.get_text().strip()
        port = int(self.ssh_port.get_text().strip() or 22)

        if not host or not user:
            self._toast("SSH Host and User are required.")
            return

        self.ssh_spinner.start()

        def _do():
            import subprocess
            try:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                     "-p", str(port), f"{user}@{host}", "echo ok"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    GLib.idle_add(self._ssh_test_done, True, f"SSH OK: {user}@{host}:{port}")
                else:
                    err = result.stderr.strip() or f"Exit code {result.returncode}"
                    GLib.idle_add(self._ssh_test_done, False, err)
            except subprocess.TimeoutExpired:
                GLib.idle_add(self._ssh_test_done, False, "SSH connection timed out (5s)")
            except Exception as e:
                GLib.idle_add(self._ssh_test_done, False, str(e))

        threading.Thread(target=_do, daemon=True).start()

    def _ssh_test_done(self, success, message):
        self.ssh_spinner.stop()
        if success:
            self._toast(f"\u2714 {message}")
        else:
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="SSH Connection Failed",
                body=message,
            )
            dialog.add_response("ok", "OK")
            dialog.present()

    # ── Toast ─────────────────────────────────────────────────────

    def _toast(self, message):
        self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=4))


# ── Public API ────────────────────────────────────────────────────────


def get_connection(name=None):
    connections = load_connections()
    if name:
        return connections.get(name)
    if connections:
        return next(iter(connections.values()))
    return None


if __name__ == "__main__":
    app = OdooConnectApp()
    app.run(None)
