"""Admin UI — Backup Manager (`/admin/backups`).

Per-tenant S3 backup browser + auto-rotation config. Hooks into the same
session store, CSRF, and knock gate as `admin_ui.py`.

Env:
  MCP_OAUTH_CLIENT_ID      — "odoo-rpc-mcp" (main) or client id; drives scope.
  S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY — required.
  S3_ENDPOINT_URL          — defaults to https://eu2.contabostorage.com.
  S3_REGION                — defaults to "default".
  BACKUP_ROTATION_CONFIG   — path, defaults /shared-data/backup_rotation.json.
  BACKUP_ROTATION_LOG      — path, defaults /shared-data/backup_rotation.log.

Scope:
  Main admin  (CLIENT_ID in {'', 'odoo-rpc-mcp'}) → every `mcp-backup-*` bucket.
  Client admin → only `mcp-backup-<CLIENT_ID>`.

Destructive ops (DELETE) require the `X-Admin-Rechallenge` header with the
admin's password (re-verified against the user auth store).
"""
from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

try:
    import boto3
    from botocore.config import Config as BotoConfig
    _BOTO = True
except ImportError:
    _BOTO = False

from starlette.requests import Request
from starlette.responses import (HTMLResponse, JSONResponse, Response,
                                 StreamingResponse, PlainTextResponse)
from starlette.routing import Route

# Shared helpers from admin_ui (same session/csrf/audit plumbing).
from admin_ui import (ADMIN_PATH_PREFIX, _apply_sec_headers, _audit,
                      _check_csrf, _client_ip, _gate, _html_shell,
                      _load_user_auth, _nav, _read_session, _verify_password)

logger = logging.getLogger("admin_backup")

TENANT_ID = (os.environ.get("MCP_OAUTH_CLIENT_ID") or "odoo-rpc-mcp").strip()
IS_MAIN = TENANT_ID in ("", "odoo-rpc-mcp")

S3_ACCESS = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT_URL", "https://eu2.contabostorage.com")
S3_REGION = os.environ.get("S3_REGION", "default")

ROTATION_CONFIG = os.environ.get("BACKUP_ROTATION_CONFIG", "/shared-data/backup_rotation.json")
ROTATION_LOG = os.environ.get("BACKUP_ROTATION_LOG", "/shared-data/backup_rotation.log")


# ── S3 helpers ───────────────────────────────────────────────

def _s3():
    if not _BOTO:
        raise RuntimeError("boto3 not installed; add to requirements.txt")
    if not (S3_ACCESS and S3_SECRET):
        raise RuntimeError("S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY not set")
    return boto3.client(
        "s3", endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS, aws_secret_access_key=S3_SECRET,
        config=BotoConfig(s3={"addressing_style": "path"},
                          retries={"max_attempts": 3}),
        region_name=S3_REGION,
    )


def _allowed_bucket(bucket: str) -> bool:
    """Enforce per-tenant isolation."""
    if not bucket.startswith("mcp-backup-"):
        return False
    if IS_MAIN:
        return True
    return bucket == f"mcp-backup-{TENANT_ID}"


def _list_allowed_buckets() -> list[str]:
    s3 = _s3()
    all_buckets = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    return sorted(b for b in all_buckets if _allowed_bucket(b))


def _bucket_stats(bucket: str) -> dict:
    s3 = _s3()
    count, total, oldest, newest = 0, 0, None, None
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            count += 1
            total += o["Size"]
            if oldest is None or o["LastModified"] < oldest["LastModified"]:
                oldest = o
            if newest is None or o["LastModified"] > newest["LastModified"]:
                newest = o
    return {
        "name": bucket, "object_count": count, "total_bytes": total,
        "oldest_key": oldest["Key"] if oldest else None,
        "oldest_modified": oldest["LastModified"].isoformat() if oldest else None,
        "newest_key": newest["Key"] if newest else None,
        "newest_modified": newest["LastModified"].isoformat() if newest else None,
    }


# ── Re-challenge (admin password re-entry for destructive ops) ──

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


def _guard_admin(req: Request, require_admin: bool = True) -> tuple[dict, Optional[Response]]:
    gate = _gate(req)
    if gate:
        return {}, gate
    sess = _read_session(req)
    if not sess:
        return {}, _apply_sec_headers(JSONResponse({"error": "unauthorized"}, status_code=401))
    if require_admin and not sess.get("is_admin") and IS_MAIN:
        # main: only admins can touch backups
        return {}, _apply_sec_headers(JSONResponse({"error": "forbidden"}, status_code=403))
    return sess, None


# ── Rotation config ─────────────────────────────────────────

_DEFAULT_ROTATION = {
    "buckets": {},        # filled on demand
    "run_at": "03:00",    # HH:MM local tz
    "timezone": "Europe/Sofia",
    "default_keep_days": 90,
    "default_min_objects": 10,
}


def _load_rotation() -> dict:
    if os.path.isfile(ROTATION_CONFIG):
        try:
            with open(ROTATION_CONFIG) as f:
                cfg = json.load(f)
            for k, v in _DEFAULT_ROTATION.items():
                cfg.setdefault(k, v)
            return cfg
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("rotation config read failed: %s", exc)
    return dict(_DEFAULT_ROTATION)


def _save_rotation(cfg: dict) -> None:
    try:
        os.makedirs(os.path.dirname(ROTATION_CONFIG), exist_ok=True)
        with open(ROTATION_CONFIG, "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.error("rotation config write failed: %s", exc)
        raise


def _log_rotation(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"{ts} {msg}\n"
    try:
        os.makedirs(os.path.dirname(ROTATION_LOG), exist_ok=True)
        with open(ROTATION_LOG, "a") as f:
            f.write(line)
    except OSError:
        pass
    logger.info(msg)


def rotate_once(dry_run: bool = False) -> dict:
    """Apply retention policy to every allowed bucket. Returns per-bucket counts."""
    cfg = _load_rotation()
    per_bucket = cfg.get("buckets", {})
    default_days = int(cfg.get("default_keep_days", 90))
    default_min = int(cfg.get("default_min_objects", 10))
    results = {}
    cutoff = datetime.now(timezone.utc)
    for bucket in _list_allowed_buckets():
        b_cfg = per_bucket.get(bucket, {})
        keep_days = int(b_cfg.get("keep_days", default_days))
        min_objects = int(b_cfg.get("min_objects", default_min))
        threshold = cutoff - timedelta(days=keep_days)
        s3 = _s3()
        candidates = []  # older-than-threshold
        keepers = []     # newer-than-threshold
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for o in page.get("Contents", []):
                if o["LastModified"] < threshold:
                    candidates.append(o)
                else:
                    keepers.append(o)
        # If total objects ≤ min_objects, skip deletion entirely
        total = len(candidates) + len(keepers)
        to_delete = candidates
        if total - len(candidates) < min_objects:
            # keep newest items (across both lists) until we reach min_objects
            all_sorted = sorted(candidates + keepers,
                                key=lambda o: o["LastModified"], reverse=True)
            to_delete = all_sorted[min_objects:]
            to_delete = [o for o in to_delete if o["LastModified"] < threshold]
        deleted_keys = []
        if to_delete and not dry_run:
            for i in range(0, len(to_delete), 500):
                chunk = to_delete[i:i + 500]
                s3.delete_objects(Bucket=bucket,
                                  Delete={"Objects": [{"Key": o["Key"]} for o in chunk]})
                deleted_keys.extend(o["Key"] for o in chunk)
        results[bucket] = {
            "keep_days": keep_days, "min_objects": min_objects,
            "total_objects": total,
            "candidates_older_than_threshold": len(candidates),
            "would_delete": len(to_delete), "deleted": len(deleted_keys),
            "dry_run": dry_run,
        }
        if to_delete:
            _log_rotation(
                f"rotate bucket={bucket} keep_days={keep_days} "
                f"min_objects={min_objects} deleted={len(deleted_keys)} "
                f"dry_run={dry_run}"
            )
    return {"ran_at": cutoff.isoformat(timespec="seconds"), "buckets": results}


# ── HTML page ───────────────────────────────────────────────

def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


PAGE_CSS = """
.bk-tabs .nav-link { font-family: 'JetBrains Mono', monospace; font-size: .85rem; }
.bk-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); gap: .75rem; }
.bk-stat { background: #fff; border: 1px solid rgba(113,75,160,.12); border-radius: .5rem; padding: .75rem; }
.bk-stat-label { font-size: .75rem; color: #6c757d; text-transform: uppercase; }
.bk-stat-value { font-size: 1.25rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
.bk-obj-row { font-family: 'JetBrains Mono', monospace; font-size: .85rem; }
.bk-obj-row td { padding: .35rem .5rem; vertical-align: middle; }
.bk-obj-row .btn { padding: .125rem .45rem; font-size: .75rem; }
.bk-json { max-height: 60vh; overflow: auto; background: #0f1115; color: #e6e6e6; padding: .75rem;
           border-radius: .35rem; font-family: 'JetBrains Mono', monospace; font-size: .8rem; }
"""


async def _page_dashboard(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return _apply_sec_headers(
            HTMLResponse('<meta http-equiv="refresh" content="0; url=' + ADMIN_PATH_PREFIX + '/login">')
        )
    title = "Backup Manager"
    p = ADMIN_PATH_PREFIX
    extra_head = f"<style>{PAGE_CSS}</style>"
    body = f"""
{_nav(sess)}
<main class="container-fluid" style="max-width: 1400px; margin: 0 auto; padding: 1rem 1.5rem;">
  <div class="d-flex align-items-center mb-3">
    <h3 class="mb-0">Backup Manager</h3>
    <span class="ms-3 text-muted small">tenant: <code>{TENANT_ID}</code></span>
    <div class="ms-auto">
      <button class="btn btn-sm btn-outline-secondary" onclick="bkRefresh()"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
      <button class="btn btn-sm btn-outline-warning ms-1" onclick="bkRotateNow()"><i class="bi bi-clock-history"></i> Run rotation now</button>
      <button class="btn btn-sm btn-outline-primary ms-1" data-bs-toggle="modal" data-bs-target="#retentionModal"><i class="bi bi-gear"></i> Retention</button>
    </div>
  </div>

  <ul class="nav nav-pills bk-tabs mb-3" id="bk-tabs"></ul>
  <div id="bk-pane"></div>
</main>

<div class="modal fade" id="jsonModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-lg modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="jsonModalTitle">JSON</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body"><pre class="bk-json" id="jsonModalBody"></pre></div>
    </div>
  </div>
</div>

<div class="modal fade" id="retentionModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header"><h5 class="modal-title">Retention policy</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
      <div class="modal-body">
        <div class="row mb-2"><label class="col-4 col-form-label">Default keep days</label>
          <div class="col-8"><input class="form-control" id="retDefDays" type="number" min="1"></div></div>
        <div class="row mb-2"><label class="col-4 col-form-label">Default min objects</label>
          <div class="col-8"><input class="form-control" id="retDefMin" type="number" min="0"></div></div>
        <div class="row mb-2"><label class="col-4 col-form-label">Daily run at (HH:MM)</label>
          <div class="col-8"><input class="form-control" id="retRunAt" type="text" placeholder="03:00"></div></div>
        <div class="row mb-2"><label class="col-4 col-form-label">Timezone</label>
          <div class="col-8"><input class="form-control" id="retTz" type="text" placeholder="Europe/Sofia"></div></div>
        <hr>
        <h6>Per-bucket overrides</h6>
        <div id="retPerBucket"></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" onclick="bkSaveRetention()">Save</button>
      </div>
    </div>
  </div>
</div>

<script>
const P = "{p}";
let BUCKETS = [];
let CURRENT = null;

async function bkFetch(url, opts) {{
  opts = opts || {{}};
  opts.headers = Object.assign({{'Accept':'application/json'}}, opts.headers || {{}});
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(r.status + ' ' + await r.text());
  return r.headers.get('content-type','').includes('json') ? r.json() : r.text();
}}
async function bkRechall(msg) {{
  msg = msg || 'Confirm with admin password:';
  const pw = prompt(msg);
  return pw;
}}
function humanBytes(n) {{
  if (n == null) return '-';
  const u = ['B','KB','MB','GB','TB']; let i=0;
  while (n >= 1024 && i < u.length-1) {{ n /= 1024; i++; }}
  return (i===0 ? n : n.toFixed(1)) + ' ' + u[i];
}}
function fmtDate(iso) {{ return iso ? iso.replace('T',' ').slice(0,19) : '-'; }}
async function bkRefresh() {{
  const data = await bkFetch(P+'/backups/api/buckets');
  BUCKETS = data.buckets || [];
  const tabs = document.getElementById('bk-tabs');
  tabs.innerHTML = BUCKETS.map((b,i) =>
    `<li class="nav-item"><a class="nav-link ${{i===0?'active':''}}" href="#" data-bucket="${{b.name}}">${{b.name}} <small class="text-muted">(${{b.object_count}})</small></a></li>`
  ).join('');
  tabs.querySelectorAll('a').forEach(a => a.onclick = (e) => {{
    e.preventDefault();
    tabs.querySelectorAll('a').forEach(x => x.classList.remove('active'));
    a.classList.add('active');
    bkLoadBucket(a.dataset.bucket);
  }});
  if (BUCKETS.length) bkLoadBucket(BUCKETS[0].name);
  else document.getElementById('bk-pane').innerHTML = '<div class="alert alert-warning">No backup buckets visible for this tenant.</div>';
}}
async function bkLoadBucket(bucket) {{
  CURRENT = bucket;
  const stats = BUCKETS.find(b => b.name === bucket) || {{}};
  const objs = await bkFetch(P+'/backups/api/objects?bucket=' + encodeURIComponent(bucket));
  const byDay = {{}};
  for (const o of (objs.objects||[])) {{
    const d = o.key.split('/')[0] || '(root)';
    (byDay[d] = byDay[d] || []).push(o);
  }}
  const days = Object.keys(byDay).sort().reverse();
  const statHtml = `
    <div class="bk-stats mb-3">
      <div class="bk-stat"><div class="bk-stat-label">Objects</div><div class="bk-stat-value">${{stats.object_count ?? '-'}}</div></div>
      <div class="bk-stat"><div class="bk-stat-label">Total size</div><div class="bk-stat-value">${{humanBytes(stats.total_bytes ?? 0)}}</div></div>
      <div class="bk-stat"><div class="bk-stat-label">Oldest</div><div class="bk-stat-value" style="font-size:.9rem">${{fmtDate(stats.oldest_modified)}}</div></div>
      <div class="bk-stat"><div class="bk-stat-label">Newest</div><div class="bk-stat-value" style="font-size:.9rem">${{fmtDate(stats.newest_modified)}}</div></div>
    </div>
    <div class="mb-2">
      <button class="btn btn-sm btn-outline-danger" onclick="bkDeletePrefix('${{bucket}}')"><i class="bi bi-trash"></i> Delete prefix…</button>
      <button class="btn btn-sm btn-outline-secondary" onclick="bkExportZip('${{bucket}}')"><i class="bi bi-file-earmark-zip"></i> Export zip</button>
    </div>
  `;
  const listHtml = days.map(d => `
    <details class="mb-2" open>
      <summary class="fw-bold">${{d}} <small class="text-muted">(${{byDay[d].length}})</small></summary>
      <table class="table table-sm bk-obj-row mb-0"><tbody>
        ${{byDay[d].map(o => `
          <tr>
            <td style="width:20%">${{o.modified.slice(11,19)}}</td>
            <td>${{o.key}}</td>
            <td class="text-end text-muted">${{humanBytes(o.size)}}</td>
            <td class="text-end" style="width:12rem;white-space:nowrap">
              <button class="btn btn-outline-info" onclick="bkView('${{bucket}}','${{o.key}}')"><i class="bi bi-eye"></i></button>
              <a class="btn btn-outline-secondary" href="${{P}}/backups/api/object?bucket=${{encodeURIComponent(bucket)}}&key=${{encodeURIComponent(o.key)}}" target="_blank"><i class="bi bi-download"></i></a>
              <button class="btn btn-outline-danger" onclick="bkDelete('${{bucket}}','${{o.key}}')"><i class="bi bi-trash"></i></button>
            </td>
          </tr>`).join('')}}
      </tbody></table>
    </details>
  `).join('');
  document.getElementById('bk-pane').innerHTML = statHtml + (listHtml || '<div class="alert alert-info">Empty bucket.</div>');
}}
async function bkView(bucket, key) {{
  document.getElementById('jsonModalTitle').textContent = key;
  document.getElementById('jsonModalBody').textContent = 'Loading…';
  new bootstrap.Modal('#jsonModal').show();
  try {{
    const r = await fetch(P+'/backups/api/object?bucket='+encodeURIComponent(bucket)+'&key='+encodeURIComponent(key));
    const txt = await r.text();
    try {{ document.getElementById('jsonModalBody').textContent = JSON.stringify(JSON.parse(txt), null, 2); }}
    catch {{ document.getElementById('jsonModalBody').textContent = txt; }}
  }} catch (e) {{ document.getElementById('jsonModalBody').textContent = 'Error: ' + e; }}
}}
async function bkDelete(bucket, key) {{
  const pw = await bkRechall('Confirm delete "'+key+'"');
  if (!pw) return;
  await bkFetch(P+'/backups/api/object?bucket='+encodeURIComponent(bucket)+'&key='+encodeURIComponent(key), {{
    method: 'DELETE', headers: {{'X-Admin-Rechallenge': pw}}
  }});
  bkLoadBucket(bucket);
}}
async function bkDeletePrefix(bucket) {{
  const prefix = prompt('Delete objects with prefix:', '2026-');
  if (!prefix) return;
  const pw = await bkRechall('Confirm delete prefix "'+prefix+'"');
  if (!pw) return;
  const res = await bkFetch(P+'/backups/api/prefix?bucket='+encodeURIComponent(bucket)+'&prefix='+encodeURIComponent(prefix), {{
    method: 'DELETE', headers: {{'X-Admin-Rechallenge': pw}}
  }});
  alert('Deleted ' + (res.deleted || 0) + ' objects');
  bkLoadBucket(bucket);
}}
function bkExportZip(bucket) {{
  window.location = P+'/backups/api/zip?bucket='+encodeURIComponent(bucket);
}}
async function bkRotateNow() {{
  const pw = await bkRechall('Confirm run rotation now');
  if (!pw) return;
  const res = await bkFetch(P+'/backups/api/rotate-now', {{
    method: 'POST', headers: {{'X-Admin-Rechallenge': pw}}
  }});
  alert('Rotation run. See buckets: ' + Object.keys(res.buckets||{{}}).length);
  bkRefresh();
}}
async function bkOpenRetention() {{
  const cfg = await bkFetch(P+'/backups/api/retention');
  document.getElementById('retDefDays').value = cfg.default_keep_days;
  document.getElementById('retDefMin').value = cfg.default_min_objects;
  document.getElementById('retRunAt').value = cfg.run_at;
  document.getElementById('retTz').value = cfg.timezone;
  const pb = document.getElementById('retPerBucket');
  pb.innerHTML = BUCKETS.map(b => {{
    const row = cfg.buckets[b.name] || {{}};
    return `<div class="row mb-1 align-items-center">
      <div class="col-5 small"><code>${{b.name}}</code></div>
      <div class="col-3"><input class="form-control form-control-sm ret-days" data-b="${{b.name}}" type="number" min="1" placeholder="days" value="${{row.keep_days ?? ''}}"></div>
      <div class="col-3"><input class="form-control form-control-sm ret-min" data-b="${{b.name}}" type="number" min="0" placeholder="min" value="${{row.min_objects ?? ''}}"></div>
    </div>`;
  }}).join('');
}}
async function bkSaveRetention() {{
  const cfg = {{
    default_keep_days: Number(document.getElementById('retDefDays').value || 90),
    default_min_objects: Number(document.getElementById('retDefMin').value || 10),
    run_at: document.getElementById('retRunAt').value || '03:00',
    timezone: document.getElementById('retTz').value || 'Europe/Sofia',
    buckets: {{}}
  }};
  document.querySelectorAll('.ret-days').forEach(el => {{
    const b = el.dataset.b;
    cfg.buckets[b] = cfg.buckets[b] || {{}};
    if (el.value !== '') cfg.buckets[b].keep_days = Number(el.value);
  }});
  document.querySelectorAll('.ret-min').forEach(el => {{
    const b = el.dataset.b;
    cfg.buckets[b] = cfg.buckets[b] || {{}};
    if (el.value !== '') cfg.buckets[b].min_objects = Number(el.value);
  }});
  await bkFetch(P+'/backups/api/retention', {{
    method: 'PUT', headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify(cfg)
  }});
  bootstrap.Modal.getInstance(document.getElementById('retentionModal')).hide();
  alert('Saved.');
}}
document.getElementById('retentionModal').addEventListener('show.bs.modal', bkOpenRetention);
bkRefresh();
</script>
"""
    return _apply_sec_headers(HTMLResponse(_html_shell(title, body, extra_head)))


# ── JSON API ───────────────────────────────────────────────

async def _api_buckets(req: Request):
    sess, err = _guard_admin(req)
    if err: return err
    try:
        names = _list_allowed_buckets()
        result = [_bucket_stats(n) for n in names]
    except Exception as exc:
        logger.exception("list buckets failed")
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=500))
    return _apply_sec_headers(JSONResponse({"buckets": result, "tenant": TENANT_ID}))


async def _api_objects(req: Request):
    sess, err = _guard_admin(req)
    if err: return err
    bucket = req.query_params.get("bucket", "")
    prefix = req.query_params.get("prefix", "")
    limit = min(int(req.query_params.get("limit", 1000)), 10000)
    if not _allowed_bucket(bucket):
        return _apply_sec_headers(JSONResponse({"error": "bucket not allowed"}, status_code=403))
    try:
        s3 = _s3()
        out = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                out.append({
                    "key": o["Key"], "size": o["Size"],
                    "modified": o["LastModified"].isoformat(),
                    "etag": o.get("ETag", "").strip('"'),
                })
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
    except Exception as exc:
        logger.exception("list objects failed")
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=500))
    return _apply_sec_headers(JSONResponse({"bucket": bucket, "prefix": prefix,
                                             "objects": out}))


async def _api_object(req: Request):
    sess, err = _guard_admin(req)
    if err: return err
    bucket = req.query_params.get("bucket", "")
    key = req.query_params.get("key", "")
    if not _allowed_bucket(bucket) or not key:
        return _apply_sec_headers(JSONResponse({"error": "invalid params"}, status_code=400))

    if req.method == "DELETE":
        if not _rechallenge_ok(req, sess):
            return _apply_sec_headers(JSONResponse({"error": "rechallenge failed"}, status_code=401))
        try:
            _s3().delete_object(Bucket=bucket, Key=key)
            _audit(sess["login"], "backup_delete", f"{bucket}/{key}",
                   _client_ip(req), req.headers.get("user-agent", ""))
        except Exception as exc:
            return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=500))
        return _apply_sec_headers(JSONResponse({"deleted": {"bucket": bucket, "key": key}}))

    # GET → stream
    try:
        obj = _s3().get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=404))
    ctype = obj.get("ContentType") or ("application/json" if key.endswith(".json")
                                        else "application/octet-stream")
    fname = key.rsplit("/", 1)[-1]
    return StreamingResponse(
        obj["Body"].iter_chunks(chunk_size=64 * 1024),
        media_type=ctype,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


async def _api_prefix_delete(req: Request):
    sess, err = _guard_admin(req)
    if err: return err
    if not _rechallenge_ok(req, sess):
        return _apply_sec_headers(JSONResponse({"error": "rechallenge failed"}, status_code=401))
    bucket = req.query_params.get("bucket", "")
    prefix = req.query_params.get("prefix", "")
    if not _allowed_bucket(bucket) or not prefix:
        return _apply_sec_headers(JSONResponse({"error": "invalid params"}, status_code=400))
    try:
        s3 = _s3()
        deleted = 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
                deleted += len(keys)
        _audit(sess["login"], "backup_prefix_delete", f"{bucket}/{prefix}",
               _client_ip(req), req.headers.get("user-agent", ""),
               {"deleted": deleted})
    except Exception as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=500))
    return _apply_sec_headers(JSONResponse({"bucket": bucket, "prefix": prefix, "deleted": deleted}))


async def _api_zip(req: Request):
    sess, err = _guard_admin(req)
    if err: return err
    bucket = req.query_params.get("bucket", "")
    prefix = req.query_params.get("prefix", "")
    if not _allowed_bucket(bucket):
        return _apply_sec_headers(JSONResponse({"error": "bucket not allowed"}, status_code=403))

    def _gen():
        buf = io.BytesIO()
        s3 = _s3()
        zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                data = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read()
                zf.writestr(o["Key"], data)
                if buf.tell() > 5 * 1024 * 1024:
                    buf.seek(0); chunk = buf.read(); buf.seek(0); buf.truncate()
                    yield chunk
        zf.close()
        buf.seek(0)
        yield buf.read()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"{bucket}_{stamp}.zip"
    return StreamingResponse(_gen(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


async def _api_rotate_now(req: Request):
    sess, err = _guard_admin(req)
    if err: return err
    if not _rechallenge_ok(req, sess):
        return _apply_sec_headers(JSONResponse({"error": "rechallenge failed"}, status_code=401))
    try:
        result = rotate_once(dry_run=False)
        _audit(sess["login"], "backup_rotate_run", "manual",
               _client_ip(req), req.headers.get("user-agent", ""), result)
    except Exception as exc:
        logger.exception("rotate_once failed")
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=500))
    return _apply_sec_headers(JSONResponse(result))


async def _api_retention(req: Request):
    sess, err = _guard_admin(req)
    if err: return err
    if req.method == "GET":
        return _apply_sec_headers(JSONResponse(_load_rotation()))
    # PUT
    if not _check_csrf(req, sess):
        return _apply_sec_headers(JSONResponse({"error": "csrf failed"}, status_code=403))
    try:
        body = await req.json()
        cfg = _load_rotation()
        for k in ("default_keep_days", "default_min_objects", "run_at", "timezone"):
            if k in body:
                cfg[k] = body[k]
        if "buckets" in body and isinstance(body["buckets"], dict):
            cfg["buckets"] = body["buckets"]
        _save_rotation(cfg)
        _audit(sess["login"], "backup_retention_update", "",
               _client_ip(req), req.headers.get("user-agent", ""), cfg)
    except Exception as exc:
        return _apply_sec_headers(JSONResponse({"error": str(exc)}, status_code=400))
    return _apply_sec_headers(JSONResponse(cfg))


# ── Scheduler (optional) ───────────────────────────────────

def start_scheduler():
    """Install APScheduler cron for the configured run_at. No-op if APScheduler missing."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.info("APScheduler not installed; manual rotation only")
        return None
    cfg = _load_rotation()
    run_at = cfg.get("run_at", "03:00")
    tz = cfg.get("timezone", "Europe/Sofia")
    try:
        hh, mm = (int(x) for x in run_at.split(":"))
    except ValueError:
        logger.warning("bad run_at %r, skipping scheduler", run_at)
        return None
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(lambda: rotate_once(dry_run=False),
                  CronTrigger(hour=hh, minute=mm, timezone=tz),
                  name="backup_rotate", max_instances=1, coalesce=True)
    sched.start()
    logger.info("backup rotation scheduled daily at %s %s", run_at, tz)
    return sched


# ── Route registration ─────────────────────────────────────

def get_routes() -> list:
    p = ADMIN_PATH_PREFIX
    return [
        Route(f"{p}/backups", _page_dashboard),
        Route(f"{p}/backups/api/buckets", _api_buckets),
        Route(f"{p}/backups/api/objects", _api_objects),
        Route(f"{p}/backups/api/object", _api_object, methods=["GET", "DELETE"]),
        Route(f"{p}/backups/api/prefix", _api_prefix_delete, methods=["DELETE"]),
        Route(f"{p}/backups/api/zip", _api_zip),
        Route(f"{p}/backups/api/rotate-now", _api_rotate_now, methods=["POST"]),
        Route(f"{p}/backups/api/retention", _api_retention, methods=["GET", "PUT"]),
    ]
