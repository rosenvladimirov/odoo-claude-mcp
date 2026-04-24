"""Admin UI — Filestore Manager (`/admin/filestore`).

Browse/edit the `/shared-data` volume (per-tenant scope enforced at the
volume mount level — client stacks only expose their own `/shared-data`).

Env:
  SHARED_DATA_ROOT          — sandbox root, defaults to "/shared-data".
  ADMIN_FS_MAX_UPLOAD_MB    — max upload size in MB, default 50.
  ADMIN_FS_READONLY         — if "1", disables write/delete/upload/mkdir/mv.

Destructive ops (DELETE, WRITE, UPLOAD, MKDIR, MV) require the
`X-Admin-Rechallenge` header with the admin's password.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Optional

from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse,
                                 PlainTextResponse, Response)
from starlette.routing import Route

from admin_ui import (ADMIN_PATH_PREFIX, _apply_sec_headers, _audit,
                      _check_csrf, _client_ip, _gate, _html_shell,
                      _load_user_auth, _nav, _read_session, _verify_password)

logger = logging.getLogger("admin_filestore")

SANDBOX_ROOT = Path(os.environ.get("SHARED_DATA_ROOT", "/shared-data")).resolve()
MAX_UPLOAD_BYTES = int(os.environ.get("ADMIN_FS_MAX_UPLOAD_MB", "50")) * 1024 * 1024
READONLY = os.environ.get("ADMIN_FS_READONLY", "0") == "1"

EDITABLE_EXT = {
    ".md", ".txt", ".json", ".yml", ".yaml", ".xml", ".py", ".sh",
    ".html", ".css", ".js", ".csv", ".ini", ".toml", ".conf", ".log",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp"}


# ── Path sandbox ───────────────────────────────────────────

def _safe_path(user_path: str) -> Path:
    """Resolve a user-supplied path inside SANDBOX_ROOT. Raises ValueError if escapes."""
    if not user_path or user_path == "/":
        return SANDBOX_ROOT
    # strip leading slashes so Path joins correctly
    rel = user_path.lstrip("/")
    p = (SANDBOX_ROOT / rel).resolve()
    try:
        p.relative_to(SANDBOX_ROOT)
    except ValueError:
        raise ValueError(f"path '{user_path}' escapes sandbox")
    return p


def _rel(p: Path) -> str:
    """Return the sandbox-relative path prefixed with /."""
    try:
        rel = p.resolve().relative_to(SANDBOX_ROOT)
    except ValueError:
        return "/"
    s = str(rel)
    return "/" if s in ("", ".") else "/" + s


# ── Guards (auth + re-challenge + readonly) ───────────────

def _rechallenge_ok(req: Request, sess: dict) -> bool:
    pw = req.headers.get("x-admin-rechallenge") or ""
    if not pw:
        return False
    au = _load_user_auth(sess["login"])
    if not au or not au.get("password_hash"):
        return False
    try:
        return _verify_password(pw, au["password_hash"])
    except Exception:
        return False


def _guard(req: Request) -> tuple[dict, Optional[Response]]:
    gate = _gate(req)
    if gate:
        return {}, gate
    sess = _read_session(req)
    if not sess:
        return {}, _apply_sec_headers(JSONResponse({"error": "unauthorized"}, status_code=401))
    return sess, None


def _write_guard(req: Request, sess: dict) -> Optional[Response]:
    if READONLY:
        return _apply_sec_headers(JSONResponse({"error": "read-only mode"}, status_code=403))
    if not _rechallenge_ok(req, sess):
        return _apply_sec_headers(JSONResponse({"error": "rechallenge failed"}, status_code=401))
    return None


# ── HTML page ───────────────────────────────────────────────

PAGE_CSS = """
.fs-split { display: grid; grid-template-columns: 320px 1fr; gap: 1rem; min-height: 60vh; }
.fs-tree { background: #fff; border: 1px solid rgba(113,75,160,.12); border-radius: .5rem; padding: .5rem; overflow: auto; max-height: 75vh; }
.fs-tree li { list-style: none; padding: 0; }
.fs-tree .entry { cursor: pointer; padding: .2rem .4rem; border-radius: .25rem; font-family: 'JetBrains Mono', monospace; font-size: .85rem; display:flex; align-items:center; gap:.35rem; }
.fs-tree .entry:hover { background: rgba(113,75,160,.08); }
.fs-tree .entry.active { background: rgba(113,75,160,.15); font-weight: 600; }
.fs-detail { background: #fff; border: 1px solid rgba(113,75,160,.12); border-radius: .5rem; padding: 1rem; }
.fs-editor { width: 100%; min-height: 55vh; font-family: 'JetBrains Mono', monospace; font-size: .85rem; }
.fs-img-preview { max-width: 100%; max-height: 60vh; border: 1px solid rgba(0,0,0,.12); border-radius: .35rem; }
.fs-crumb { font-family: 'JetBrains Mono', monospace; font-size: .85rem; }
.fs-crumb a { text-decoration: none; }
.fs-badge-ro { background: #6c757d; color: #fff; font-size: .7rem; padding: .1rem .4rem; border-radius: .25rem; }
"""


async def _page_dashboard(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return _apply_sec_headers(
            HTMLResponse('<meta http-equiv="refresh" content="0; url=' + ADMIN_PATH_PREFIX + '/login">')
        )
    title = "Filestore"
    p = ADMIN_PATH_PREFIX
    ro_badge = '<span class="fs-badge-ro ms-2">READ-ONLY</span>' if READONLY else ""
    extra_head = f"<style>{PAGE_CSS}</style>"
    body = f"""
{_nav(sess)}
<main class="container-fluid" style="max-width: 1400px; margin: 0 auto; padding: 1rem 1.5rem;">
  <div class="d-flex align-items-center mb-3">
    <h3 class="mb-0">Filestore {ro_badge}</h3>
    <span class="ms-3 text-muted small">sandbox: <code>{SANDBOX_ROOT}</code></span>
    <div class="ms-auto">
      <button class="btn btn-sm btn-outline-secondary" onclick="fsReload()"><i class="bi bi-arrow-clockwise"></i></button>
      <button class="btn btn-sm btn-outline-primary ms-1" onclick="fsMkdir()" {"disabled" if READONLY else ""}><i class="bi bi-folder-plus"></i> New folder</button>
      <button class="btn btn-sm btn-outline-primary ms-1" onclick="document.getElementById('fsUpload').click()" {"disabled" if READONLY else ""}><i class="bi bi-upload"></i> Upload</button>
      <input type="file" id="fsUpload" multiple style="display:none" onchange="fsUpload(this)">
    </div>
  </div>
  <div class="fs-crumb mb-2" id="fs-crumb">/</div>
  <div class="fs-split">
    <div class="fs-tree" id="fs-tree"></div>
    <div class="fs-detail" id="fs-detail"><div class="text-muted small">Select a file.</div></div>
  </div>
</main>

<script>
const P = "{p}";
const RO = {"true" if READONLY else "false"};
let CWD = '/';
let SELECTED = null;

async function fsFetch(url, opts) {{
  opts = opts || {{}};
  opts.headers = Object.assign({{'Accept':'application/json'}}, opts.headers || {{}});
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(r.status + ': ' + await r.text());
  const ct = r.headers.get('content-type','');
  return ct.includes('json') ? r.json() : r.text();
}}
async function fsRechall(msg) {{
  const pw = prompt(msg || 'Confirm with admin password:');
  return pw;
}}
function humanBytes(n) {{
  if (n == null) return '-';
  const u = ['B','KB','MB','GB','TB']; let i=0;
  while (n >= 1024 && i < u.length-1) {{ n /= 1024; i++; }}
  return (i===0 ? n : n.toFixed(1)) + ' ' + u[i];
}}
function iconFor(entry) {{
  if (entry.type === 'dir') return '<i class="bi bi-folder-fill text-warning"></i>';
  const ext = (entry.name.split('.').pop() || '').toLowerCase();
  if (['md','txt','json','yml','yaml','xml','py','sh','html','css','js'].includes(ext))
    return '<i class="bi bi-file-earmark-text"></i>';
  if (['png','jpg','jpeg','webp','gif','svg'].includes(ext))
    return '<i class="bi bi-file-earmark-image"></i>';
  return '<i class="bi bi-file-earmark"></i>';
}}
function fmtCrumb(path) {{
  const segs = path.split('/').filter(Boolean);
  let acc = '';
  let html = `<a href="#" onclick="fsNav('/'); return false">/</a>`;
  for (const s of segs) {{
    acc += '/' + s;
    html += `<a href="#" onclick="fsNav('${{acc}}'); return false">${{s}}</a> <span class="text-muted">/</span> `;
  }}
  return html;
}}
async function fsReload() {{ await fsNav(CWD); }}
async function fsNav(path) {{
  CWD = path;
  document.getElementById('fs-crumb').innerHTML = fmtCrumb(path);
  const data = await fsFetch(P+'/filestore/api/ls?path='+encodeURIComponent(path));
  const tree = document.getElementById('fs-tree');
  const parent = path !== '/' ? `<li><div class="entry" onclick="fsNav('${{path.replace(/\\/[^\\/]+$/, '')||'/'}}')"><i class="bi bi-arrow-return-left"></i> ..</div></li>` : '';
  tree.innerHTML = '<ul class="mb-0 ps-0">' + parent + (data.entries||[]).map(e => `
    <li>
      <div class="entry" data-path="${{e.full}}" data-type="${{e.type}}" onclick="fsClick(this)">
        ${{iconFor(e)}}
        <span class="flex-grow-1 text-truncate">${{e.name}}${{e.type==='dir'?'/':''}}</span>
        <small class="text-muted">${{e.type==='file' ? humanBytes(e.size) : ''}}</small>
      </div>
    </li>`).join('') + '</ul>';
}}
async function fsClick(el) {{
  document.querySelectorAll('.fs-tree .entry').forEach(x => x.classList.remove('active'));
  el.classList.add('active');
  const path = el.dataset.path;
  const type = el.dataset.type;
  SELECTED = path;
  if (type === 'dir') {{ await fsNav(path); return; }}
  await fsShowFile(path);
}}
async function fsShowFile(path) {{
  const info = await fsFetch(P+'/filestore/api/info?path='+encodeURIComponent(path));
  const ext = (path.split('.').pop() || '').toLowerCase();
  let html = `
    <div class="d-flex align-items-center mb-2">
      <code class="me-3">${{path}}</code>
      <span class="text-muted small">${{humanBytes(info.size)}} · ${{(info.mtime||'').slice(0,19).replace('T',' ')}}</span>
      <div class="ms-auto">
        <a class="btn btn-sm btn-outline-secondary" href="${{P}}/filestore/api/raw?path=${{encodeURIComponent(path)}}" target="_blank"><i class="bi bi-download"></i></a>
        <button class="btn btn-sm btn-outline-warning ms-1" onclick="fsRename('${{path}}')" ${{RO?'disabled':''}}><i class="bi bi-pencil"></i></button>
        <button class="btn btn-sm btn-outline-danger ms-1" onclick="fsDelete('${{path}}')" ${{RO?'disabled':''}}><i class="bi bi-trash"></i></button>
      </div>
    </div>`;
  if (['png','jpg','jpeg','webp','gif','svg','bmp'].includes(ext)) {{
    html += `<img class="fs-img-preview" src="${{P}}/filestore/api/raw?path=${{encodeURIComponent(path)}}">`;
  }} else if (info.editable) {{
    const body = await fsFetch(P+'/filestore/api/read?path='+encodeURIComponent(path));
    html += `
      <textarea class="fs-editor form-control" id="fs-edit">${{(typeof body==='string'?body:JSON.stringify(body,null,2)).replace(/</g,'&lt;')}}</textarea>
      <div class="mt-2 text-end">
        <button class="btn btn-primary btn-sm" onclick="fsSave('${{path}}')" ${{RO?'disabled':''}}><i class="bi bi-save"></i> Save</button>
      </div>`;
  }} else {{
    html += '<div class="alert alert-secondary">Binary file — use Download.</div>';
  }}
  document.getElementById('fs-detail').innerHTML = html;
}}
async function fsSave(path) {{
  const pw = await fsRechall('Save "' + path + '"?');
  if (!pw) return;
  const body = document.getElementById('fs-edit').value;
  await fsFetch(P+'/filestore/api/write?path='+encodeURIComponent(path), {{
    method:'PUT', headers:{{'Content-Type':'text/plain','X-Admin-Rechallenge': pw}}, body
  }});
  alert('Saved.');
}}
async function fsDelete(path) {{
  const pw = await fsRechall('Delete "' + path + '"?');
  if (!pw) return;
  await fsFetch(P+'/filestore/api/rm?path='+encodeURIComponent(path), {{
    method:'DELETE', headers:{{'X-Admin-Rechallenge': pw}}
  }});
  SELECTED = null;
  document.getElementById('fs-detail').innerHTML = '<div class="text-muted small">Select a file.</div>';
  fsReload();
}}
async function fsMkdir() {{
  const name = prompt('New folder name:');
  if (!name) return;
  const pw = await fsRechall('Create folder?');
  if (!pw) return;
  const full = (CWD === '/' ? '' : CWD) + '/' + name;
  await fsFetch(P+'/filestore/api/mkdir?path='+encodeURIComponent(full), {{
    method:'POST', headers:{{'X-Admin-Rechallenge': pw}}
  }});
  fsReload();
}}
async function fsRename(path) {{
  const newName = prompt('Rename to:', path.split('/').pop());
  if (!newName) return;
  const pw = await fsRechall('Rename?');
  if (!pw) return;
  const dst = path.replace(/\\/[^\\/]+$/, '/' + newName);
  await fsFetch(P+'/filestore/api/mv', {{
    method:'POST',
    headers:{{'Content-Type':'application/json','X-Admin-Rechallenge': pw}},
    body: JSON.stringify({{src: path, dst: dst}})
  }});
  fsReload();
}}
async function fsUpload(input) {{
  const pw = await fsRechall('Upload ' + input.files.length + ' file(s)?');
  if (!pw) {{ input.value = ''; return; }}
  const fd = new FormData();
  for (const f of input.files) fd.append('file', f, f.name);
  const r = await fetch(P+'/filestore/api/upload?path='+encodeURIComponent(CWD), {{
    method:'POST', headers:{{'X-Admin-Rechallenge': pw}}, body: fd
  }});
  input.value = '';
  if (!r.ok) alert('Upload failed: ' + r.status + ' ' + await r.text());
  fsReload();
}}
fsNav('/');
</script>
"""
    return _apply_sec_headers(HTMLResponse(_html_shell(title, body, extra_head)))


# ── JSON API ───────────────────────────────────────────────

async def _api_ls(req: Request):
    sess, err = _guard(req)
    if err: return err
    path = req.query_params.get("path", "/")
    try:
        p = _safe_path(path)
    except ValueError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=400))
    if not p.is_dir():
        return _apply_sec_headers(JSONResponse({"error": "not a directory"}, status_code=400))
    entries = []
    for child in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        try:
            st = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "full": _rel(child),
            "type": "dir" if child.is_dir() else "file",
            "size": st.st_size if child.is_file() else None,
            "mtime": str_time(st.st_mtime),
        })
    return _apply_sec_headers(JSONResponse({"path": _rel(p), "entries": entries}))


def str_time(ts: float) -> str:
    from datetime import datetime as _dt, timezone as _tz
    return _dt.fromtimestamp(ts, tz=_tz.utc).isoformat(timespec="seconds")


async def _api_info(req: Request):
    sess, err = _guard(req)
    if err: return err
    try:
        p = _safe_path(req.query_params.get("path", "/"))
    except ValueError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=400))
    if not p.exists():
        return _apply_sec_headers(JSONResponse({"error": "not found"}, status_code=404))
    st = p.stat()
    ext = p.suffix.lower()
    return _apply_sec_headers(JSONResponse({
        "path": _rel(p),
        "type": "dir" if p.is_dir() else "file",
        "size": st.st_size if p.is_file() else None,
        "mtime": str_time(st.st_mtime),
        "editable": p.is_file() and ext in EDITABLE_EXT,
        "mime": (mimetypes.guess_type(p.name)[0] or "application/octet-stream"),
    }))


async def _api_read(req: Request):
    sess, err = _guard(req)
    if err: return err
    try:
        p = _safe_path(req.query_params.get("path", ""))
    except ValueError as exc:
        return _apply_sec_headers(PlainTextResponse(str(exc), status_code=400))
    if not p.is_file():
        return _apply_sec_headers(PlainTextResponse("not a file", status_code=400))
    if p.suffix.lower() not in EDITABLE_EXT:
        return _apply_sec_headers(PlainTextResponse("not editable (binary)", status_code=415))
    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _apply_sec_headers(PlainTextResponse("not utf-8", status_code=415))
    return _apply_sec_headers(PlainTextResponse(content))


async def _api_raw(req: Request):
    sess, err = _guard(req)
    if err: return err
    try:
        p = _safe_path(req.query_params.get("path", ""))
    except ValueError as exc:
        return _apply_sec_headers(PlainTextResponse(str(exc), status_code=400))
    if not p.is_file():
        return _apply_sec_headers(PlainTextResponse("not found", status_code=404))
    return FileResponse(p, filename=p.name)


async def _api_write(req: Request):
    sess, err = _guard(req)
    if err: return err
    wg = _write_guard(req, sess)
    if wg: return wg
    try:
        p = _safe_path(req.query_params.get("path", ""))
    except ValueError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=400))
    if p.suffix.lower() not in EDITABLE_EXT:
        return _apply_sec_headers(JSONResponse({"error": "extension not editable"}, status_code=415))
    body = await req.body()
    if len(body) > MAX_UPLOAD_BYTES:
        return _apply_sec_headers(JSONResponse({"error": "body too large"}, status_code=413))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    except OSError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=500))
    _audit(sess["login"], "filestore_write", _rel(p),
           _client_ip(req), req.headers.get("user-agent", ""), {"bytes": len(body)})
    return _apply_sec_headers(JSONResponse({"ok": True, "size": len(body)}))


async def _api_upload(req: Request):
    sess, err = _guard(req)
    if err: return err
    wg = _write_guard(req, sess)
    if wg: return wg
    try:
        target_dir = _safe_path(req.query_params.get("path", "/"))
    except ValueError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=400))
    if not target_dir.is_dir():
        return _apply_sec_headers(JSONResponse({"error": "target not a directory"}, status_code=400))
    form = await req.form()
    saved = []
    for f in form.getlist("file"):
        if not hasattr(f, "filename"):
            continue
        name = os.path.basename(f.filename or "")
        if not name:
            continue
        data = await f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            return _apply_sec_headers(JSONResponse({"error": f"{name}: too large"}, status_code=413))
        dst = target_dir / name
        try:
            # sandbox recheck after join
            _safe_path(_rel(dst))
            dst.write_bytes(data)
            saved.append({"name": name, "size": len(data)})
        except (ValueError, OSError) as exc:
            return _apply_sec_headers(JSONResponse({"error": f"{name}: {exc}"}, status_code=400))
    _audit(sess["login"], "filestore_upload", _rel(target_dir),
           _client_ip(req), req.headers.get("user-agent", ""), {"files": saved})
    return _apply_sec_headers(JSONResponse({"ok": True, "saved": saved}))


async def _api_rm(req: Request):
    sess, err = _guard(req)
    if err: return err
    wg = _write_guard(req, sess)
    if wg: return wg
    try:
        p = _safe_path(req.query_params.get("path", ""))
    except ValueError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=400))
    if p == SANDBOX_ROOT:
        return _apply_sec_headers(JSONResponse({"error": "refuse to delete sandbox root"}, status_code=400))
    if not p.exists():
        return _apply_sec_headers(JSONResponse({"error": "not found"}, status_code=404))
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    except OSError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=500))
    _audit(sess["login"], "filestore_rm", _rel(p),
           _client_ip(req), req.headers.get("user-agent", ""))
    return _apply_sec_headers(JSONResponse({"ok": True, "deleted": _rel(p)}))


async def _api_mkdir(req: Request):
    sess, err = _guard(req)
    if err: return err
    wg = _write_guard(req, sess)
    if wg: return wg
    try:
        p = _safe_path(req.query_params.get("path", ""))
    except ValueError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=400))
    try:
        p.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _apply_sec_headers(JSONResponse({"error": "already exists"}, status_code=409))
    except OSError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=500))
    _audit(sess["login"], "filestore_mkdir", _rel(p),
           _client_ip(req), req.headers.get("user-agent", ""))
    return _apply_sec_headers(JSONResponse({"ok": True, "path": _rel(p)}))


async def _api_mv(req: Request):
    sess, err = _guard(req)
    if err: return err
    wg = _write_guard(req, sess)
    if wg: return wg
    body = await req.json()
    try:
        src = _safe_path(body.get("src", ""))
        dst = _safe_path(body.get("dst", ""))
    except ValueError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=400))
    if not src.exists():
        return _apply_sec_headers(JSONResponse({"error": "src not found"}, status_code=404))
    if dst.exists():
        return _apply_sec_headers(JSONResponse({"error": "dst exists"}, status_code=409))
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=500))
    _audit(sess["login"], "filestore_mv", f"{_rel(src)}->{_rel(dst)}",
           _client_ip(req), req.headers.get("user-agent", ""))
    return _apply_sec_headers(JSONResponse({"ok": True, "src": _rel(src), "dst": _rel(dst)}))


# ── Route registration ─────────────────────────────────────

def get_routes() -> list:
    p = ADMIN_PATH_PREFIX
    return [
        Route(f"{p}/filestore", _page_dashboard),
        Route(f"{p}/filestore/api/ls", _api_ls),
        Route(f"{p}/filestore/api/info", _api_info),
        Route(f"{p}/filestore/api/read", _api_read),
        Route(f"{p}/filestore/api/raw", _api_raw),
        Route(f"{p}/filestore/api/write", _api_write, methods=["PUT"]),
        Route(f"{p}/filestore/api/upload", _api_upload, methods=["POST"]),
        Route(f"{p}/filestore/api/rm", _api_rm, methods=["DELETE"]),
        Route(f"{p}/filestore/api/mkdir", _api_mkdir, methods=["POST"]),
        Route(f"{p}/filestore/api/mv", _api_mv, methods=["POST"]),
    ]
