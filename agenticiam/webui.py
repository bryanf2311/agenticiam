"""The web admin console: a single self-contained HTML page (inline CSS/JS,
no CDN or build step) served from GET /. It talks to nothing but this same
server's existing JSON API, so it works fully offline and needs zero extra
PyInstaller bundling config."""

PAGE_HTML = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgenticIAM</title>
<style>
  :root {
    --bg: #f5f6f8; --panel: #ffffff; --text: #1a1d23; --muted: #6b7280;
    --border: #e2e5ea; --accent: #4f46e5; --accent-text: #ffffff;
    --danger: #dc2626; --ok: #16a34a; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a; --panel: #1c1f26; --text: #e5e7eb; --muted: #9aa1ac;
      --border: #2c313a; --accent: #818cf8; --accent-text: #14161a;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text);
  }
  #root { min-height: 100vh; }
  .center-screen { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 28px; width: 100%; max-width: 380px;
  }
  .card h1 { font-size: 20px; margin: 0 0 4px; }
  .card p.sub { color: var(--muted); margin: 0 0 20px; font-size: 13px; }
  label { display: block; font-size: 13px; margin: 12px 0 4px; color: var(--muted); }
  input, select, textarea {
    width: 100%; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); font-size: 14px; font-family: inherit;
  }
  textarea { font-family: var(--mono); font-size: 12px; }
  button {
    cursor: pointer; border: none; border-radius: 8px; padding: 8px 14px; font-size: 13px;
    font-weight: 600; background: var(--accent); color: var(--accent-text);
  }
  button.secondary { background: transparent; border: 1px solid var(--border); color: var(--text); }
  button.danger { background: var(--danger); color: #fff; }
  button:disabled { opacity: .5; cursor: default; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .err { color: var(--danger); font-size: 13px; margin-top: 10px; white-space: pre-wrap; }
  .ok { color: var(--ok); font-size: 13px; margin-top: 10px; }
  .submit-row { margin-top: 18px; }

  .shell { display: flex; min-height: 100vh; }
  nav.sidebar {
    width: 210px; flex: none; border-right: 1px solid var(--border); background: var(--panel);
    display: flex; flex-direction: column; padding: 16px 0;
  }
  nav.sidebar .brand { font-weight: 700; padding: 0 16px 16px; font-size: 15px; }
  nav.sidebar a {
    display: block; padding: 9px 16px; color: var(--text); text-decoration: none; font-size: 14px; cursor: pointer;
    border-left: 3px solid transparent;
  }
  nav.sidebar a.active { border-left-color: var(--accent); background: var(--bg); font-weight: 600; }
  nav.sidebar .spacer { flex: 1; }
  nav.sidebar .who { padding: 10px 16px; font-size: 12px; color: var(--muted); border-top: 1px solid var(--border); }
  main { flex: 1; padding: 28px 32px; max-width: 980px; }
  main h2 { margin-top: 0; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 14px 0; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; }
  tr:hover td { background: var(--bg); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
  .badge.on { background: rgba(22,163,74,.15); color: var(--ok); }
  .badge.off { background: rgba(220,38,38,.15); color: var(--danger); }
  .mono { font-family: var(--mono); font-size: 12px; word-break: break-all; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 18px; }
  .panel h3 { margin: 0 0 12px; font-size: 14px; }
  .secret-box {
    background: var(--bg); border: 1px dashed var(--accent); border-radius: 8px; padding: 10px 12px;
    margin: 10px 0; font-family: var(--mono); font-size: 12px; word-break: break-all;
  }
  .actions button { margin-right: 6px; padding: 4px 9px; font-size: 12px; }
  .hint { color: var(--muted); font-size: 12px; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 800px) { .two-col { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div id="root"></div>
<script>
const root = document.getElementById('root');
let token = localStorage.getItem('aiam_token') || null;
let who = null;

function esc(s) {
  return String(s === undefined || s === null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

async function api(path, opts) {
  opts = opts || {};
  const headers = opts.headers || {};
  if (token) headers['Authorization'] = 'Bearer ' + token;
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.json);
  }
  opts.headers = headers;
  const res = await fetch(path, opts);
  if (res.status === 401) { logout(); throw new Error('Session expired, please log in again.'); }
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
  if (!res.ok) throw new Error((data && (data.error_description || data.error)) || res.statusText || ('HTTP ' + res.status));
  return data;
}

function logout() {
  token = null; who = null;
  localStorage.removeItem('aiam_token');
  boot();
}

async function boot() {
  let status;
  try { status = await api('/v1/setup/status'); }
  catch (e) { root.innerHTML = '<div class="center-screen"><div class="card"><h1>Can\\'t reach AgenticIAM</h1><p class="err">' + esc(e.message) + '</p></div></div>'; return; }
  if (!status.initialized) return showSetup();
  if (!token) return showLogin();
  try {
    who = await api('/v1/whoami');
    showApp();
  } catch (e) { showLogin(); }
}

function showSetup() {
  root.innerHTML = `
    <div class="center-screen"><div class="card">
      <h1>Welcome to AgenticIAM</h1>
      <p class="sub">No directory admin exists yet. Create one to get started — this is the only time this form will be shown.</p>
      <form id="f">
        <label>Admin username</label>
        <input name="username" autocomplete="username" required>
        <label>Password (min 8 characters)</label>
        <input name="password" type="password" autocomplete="new-password" minlength="8" required>
        <div class="submit-row"><button type="submit" style="width:100%">Create admin & continue</button></div>
      </form>
      <div id="msg"></div>
    </div></div>`;
  document.getElementById('f').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const msg = document.getElementById('msg');
    msg.innerHTML = '';
    try {
      const res = await api('/v1/setup/bootstrap', { method: 'POST', json: { username: fd.get('username'), password: fd.get('password') } });
      token = res.access_token;
      localStorage.setItem('aiam_token', token);
      boot();
    } catch (err) { msg.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; }
  });
}

function showLogin() {
  root.innerHTML = `
    <div class="center-screen"><div class="card">
      <h1>AgenticIAM</h1>
      <p class="sub">Sign in to manage the directory.</p>
      <form id="f">
        <label>Username</label>
        <input name="username" autocomplete="username" required>
        <label>Password</label>
        <input name="password" type="password" autocomplete="current-password" required>
        <div class="submit-row"><button type="submit" style="width:100%">Sign in</button></div>
      </form>
      <div id="msg"></div>
    </div></div>`;
  document.getElementById('f').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const msg = document.getElementById('msg');
    msg.innerHTML = '';
    try {
      const res = await api('/v1/login', { method: 'POST', json: { username: fd.get('username'), password: fd.get('password') } });
      token = res.access_token;
      localStorage.setItem('aiam_token', token);
      boot();
    } catch (err) { msg.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; }
  });
}

const SECTIONS = [
  ['identities', 'Identities'], ['groups', 'Groups'], ['roles', 'Roles'],
  ['keys', 'Tokens & Keys'], ['mcp', 'MCP / Agent Setup'], ['audit', 'Audit Log'],
];
let currentSection = 'identities';

function showApp() {
  root.innerHTML = `
    <div class="shell">
      <nav class="sidebar">
        <div class="brand">AgenticIAM</div>
        ${SECTIONS.map(([id, label]) => `<a data-section="${id}">${esc(label)}</a>`).join('')}
        <div class="spacer"></div>
        <div class="who">Signed in as <strong>${esc(who.name)}</strong><br>
          ${who.scopes && who.scopes.includes('*') ? 'full admin' : esc((who.scopes || []).join(', ') || 'no permissions')}
        </div>
        <a id="logout-link" style="padding-top:0">Log out</a>
      </nav>
      <main id="main"></main>
    </div>`;
  root.querySelectorAll('a[data-section]').forEach(a => a.addEventListener('click', () => selectSection(a.dataset.section)));
  document.getElementById('logout-link').addEventListener('click', logout);
  selectSection(currentSection);
}

function selectSection(id) {
  currentSection = id;
  document.querySelectorAll('a[data-section]').forEach(a => a.classList.toggle('active', a.dataset.section === id));
  const renderers = { identities: renderIdentities, groups: renderGroups, roles: renderRoles, keys: renderKeys, mcp: renderMcp, audit: renderAudit };
  renderers[id]();
}

function errBox(e) { return '<div class="err">' + esc(e.message || String(e)) + '</div>'; }

async function renderIdentities() {
  const main = document.getElementById('main');
  main.innerHTML = '<h2>Identities</h2><div id="content">Loading…</div>';
  const content = document.getElementById('content');
  try {
    const items = await api('/v1/admin/identities');
    content.innerHTML = `
      <table><thead><tr><th>Name</th><th>Kind</th><th>Status</th><th>Created</th><th></th></tr></thead>
      <tbody>${items.map(i => `
        <tr>
          <td>${esc(i.name)}</td><td>${esc(i.kind)}</td>
          <td><span class="badge ${i.enabled ? 'on' : 'off'}">${i.enabled ? 'enabled' : 'disabled'}</span></td>
          <td class="hint">${esc((i.created_at || '').slice(0, 19).replace('T', ' '))}</td>
          <td class="actions">
            <button class="secondary" data-act="toggle" data-name="${esc(i.name)}" data-enabled="${i.enabled}">${i.enabled ? 'Disable' : 'Enable'}</button>
            <button class="secondary" data-act="perms" data-name="${esc(i.name)}">Permissions</button>
            ${i.kind !== 'user' ? `<button class="secondary" data-act="rotate" data-name="${esc(i.name)}">Rotate secret</button>` : ''}
            <button class="danger" data-act="delete" data-name="${esc(i.name)}">Delete</button>
          </td>
        </tr>`).join('')}</tbody></table>
      <div class="panel"><h3>Add identity</h3>
        <form id="f-add">
          <div class="row">
            <div style="flex:1"><label>Kind</label><select name="kind"><option value="agent">agent</option><option value="service">service</option><option value="user">user</option></select></div>
            <div style="flex:2"><label>Name</label><input name="name" required></div>
          </div>
          <label id="pw-label" style="display:none">Password (min 8 characters, users only)</label>
          <input id="pw-input" name="password" type="password" style="display:none">
          <div class="submit-row"><button type="submit">Create</button></div>
        </form>
        <div id="add-result"></div>
      </div>`;
    content.querySelector('select[name=kind]').addEventListener('change', (e) => {
      const isUser = e.target.value === 'user';
      document.getElementById('pw-label').style.display = isUser ? '' : 'none';
      document.getElementById('pw-input').style.display = isUser ? '' : 'none';
    });
    content.querySelectorAll('button[data-act]').forEach(btn => btn.addEventListener('click', () => handleIdentityAction(btn)));
    document.getElementById('f-add').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const body = { kind: fd.get('kind'), name: fd.get('name') };
      if (fd.get('kind') === 'user') body.secret = fd.get('password');
      const out = document.getElementById('add-result');
      out.innerHTML = '';
      try {
        const res = await api('/v1/admin/identities', { method: 'POST', json: body });
        out.innerHTML = res.secret
          ? `<div class="ok">Created. Client secret (shown once):</div><div class="secret-box">${esc(res.secret)}</div>`
          : '<div class="ok">Created.</div>';
        renderIdentities();
      } catch (err) { out.innerHTML = errBox(err); }
    });
  } catch (err) { content.innerHTML = errBox(err); }
}

async function handleIdentityAction(btn) {
  const name = btn.dataset.name, act = btn.dataset.act;
  try {
    if (act === 'toggle') {
      await api('/v1/admin/identities/' + encodeURIComponent(name) + '/enabled', { method: 'POST', json: { enabled: btn.dataset.enabled !== 'true' } });
      renderIdentities();
    } else if (act === 'delete') {
      if (!confirm('Delete identity "' + name + '"? This cannot be undone.')) return;
      await api('/v1/admin/identities/' + encodeURIComponent(name), { method: 'DELETE' });
      renderIdentities();
    } else if (act === 'rotate') {
      const res = await api('/v1/admin/identities/' + encodeURIComponent(name) + '/rotate-secret', { method: 'POST', json: {} });
      alert('New secret for ' + name + ' (shown once):\\n\\n' + res.secret);
    } else if (act === 'perms') {
      const res = await api('/v1/admin/identities/' + encodeURIComponent(name) + '/permissions');
      alert('Effective permissions for ' + name + ':\\n\\n' + (res.length ? res.join('\\n') : '(none)'));
    }
  } catch (err) { alert(err.message); }
}

async function renderGroups() {
  const main = document.getElementById('main');
  main.innerHTML = '<h2>Groups</h2><div id="content">Loading…</div>';
  const content = document.getElementById('content');
  try {
    const groups = await api('/v1/admin/groups');
    content.innerHTML = `
      <div class="two-col">
        <div>
          <table><thead><tr><th>Name</th><th>Description</th><th></th></tr></thead>
          <tbody>${groups.map(g => `<tr><td>${esc(g.name)}</td><td class="hint">${esc(g.description || '')}</td>
            <td><button class="secondary" data-group="${esc(g.name)}">Manage</button></td></tr>`).join('')}</tbody></table>
          <div class="panel"><h3>Create group</h3>
            <form id="f-group"><label>Name</label><input name="name" required>
            <label>Description</label><input name="description">
            <div class="submit-row"><button type="submit">Create</button></div></form>
            <div id="group-msg"></div>
          </div>
        </div>
        <div id="group-detail" class="panel"><span class="hint">Select a group to manage its members.</span></div>
      </div>`;
    content.querySelectorAll('button[data-group]').forEach(b => b.addEventListener('click', () => renderGroupDetail(b.dataset.group)));
    document.getElementById('f-group').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const msg = document.getElementById('group-msg');
      try {
        await api('/v1/admin/groups', { method: 'POST', json: { name: fd.get('name'), description: fd.get('description') || null } });
        renderGroups();
      } catch (err) { msg.innerHTML = errBox(err); }
    });
  } catch (err) { content.innerHTML = errBox(err); }
}

async function renderGroupDetail(name) {
  const detail = document.getElementById('group-detail');
  detail.innerHTML = 'Loading…';
  try {
    const members = await api('/v1/admin/groups/' + encodeURIComponent(name) + '/members');
    detail.innerHTML = `
      <h3>${esc(name)} members</h3>
      <table><tbody>${members.map(m => `<tr><td>${esc(m.name)}</td><td>${esc(m.kind)}</td>
        <td><button class="danger" data-rm="${esc(m.name)}">Remove</button></td></tr>`).join('') || '<tr><td class="hint">No members yet</td></tr>'}</tbody></table>
      <form id="f-member"><label>Add identity by name</label><input name="identity" required>
      <div class="submit-row"><button type="submit">Add to group</button></div></form>
      <div id="member-msg"></div>`;
    detail.querySelectorAll('button[data-rm]').forEach(b => b.addEventListener('click', async () => {
      try { await api('/v1/admin/groups/' + encodeURIComponent(name) + '/members/' + encodeURIComponent(b.dataset.rm), { method: 'DELETE' }); renderGroupDetail(name); }
      catch (err) { alert(err.message); }
    }));
    document.getElementById('f-member').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const msg = document.getElementById('member-msg');
      try {
        await api('/v1/admin/groups/' + encodeURIComponent(name) + '/members', { method: 'POST', json: { identity: fd.get('identity') } });
        renderGroupDetail(name);
      } catch (err) { msg.innerHTML = errBox(err); }
    });
  } catch (err) { detail.innerHTML = errBox(err); }
}

async function renderRoles() {
  const main = document.getElementById('main');
  main.innerHTML = '<h2>Roles</h2><div id="content">Loading…</div>';
  const content = document.getElementById('content');
  try {
    const roles = await api('/v1/admin/roles');
    content.innerHTML = `
      <div class="two-col">
        <div>
          <table><thead><tr><th>Name</th><th>Description</th><th></th></tr></thead>
          <tbody>${roles.map(r => `<tr><td>${esc(r.name)}</td><td class="hint">${esc(r.description || '')}</td>
            <td><button class="secondary" data-role="${esc(r.name)}">Manage</button></td></tr>`).join('')}</tbody></table>
          <div class="panel"><h3>Create role</h3>
            <form id="f-role"><label>Name</label><input name="name" required>
            <label>Description</label><input name="description">
            <div class="submit-row"><button type="submit">Create</button></div></form>
            <div id="role-msg"></div>
          </div>
        </div>
        <div id="role-detail" class="panel"><span class="hint">Select a role to manage its permissions and assignments.</span></div>
      </div>`;
    content.querySelectorAll('button[data-role]').forEach(b => b.addEventListener('click', () => renderRoleDetail(b.dataset.role)));
    document.getElementById('f-role').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const msg = document.getElementById('role-msg');
      try {
        await api('/v1/admin/roles', { method: 'POST', json: { name: fd.get('name'), description: fd.get('description') || null } });
        renderRoles();
      } catch (err) { msg.innerHTML = errBox(err); }
    });
  } catch (err) { content.innerHTML = errBox(err); }
}

async function renderRoleDetail(name) {
  const detail = document.getElementById('role-detail');
  detail.innerHTML = 'Loading…';
  try {
    const perms = await api('/v1/admin/roles/' + encodeURIComponent(name) + '/permissions');
    detail.innerHTML = `
      <h3>${esc(name)} permissions</h3>
      <table><tbody>${perms.map(p => `<tr><td class="mono">${esc(p)}</td>
        <td><button class="danger" data-rmperm="${esc(p)}">Revoke</button></td></tr>`).join('') || '<tr><td class="hint">No permissions granted</td></tr>'}</tbody></table>
      <form id="f-perm"><label>Grant permission (e.g. files:*, shell:exec, *)</label><input name="permission" required>
      <div class="submit-row"><button type="submit">Grant</button></div></form>
      <div id="perm-msg"></div>
      <h3 style="margin-top:22px">Assign to</h3>
      <form id="f-assign" class="row">
        <select name="principal_type" style="flex:1"><option value="identity">identity</option><option value="group">group</option></select>
        <input name="principal" placeholder="name" style="flex:2" required>
        <button type="submit">Assign</button>
      </form>
      <div id="assign-msg"></div>`;
    detail.querySelectorAll('button[data-rmperm]').forEach(b => b.addEventListener('click', async () => {
      try { await api('/v1/admin/roles/' + encodeURIComponent(name) + '/permissions/' + encodeURIComponent(b.dataset.rmperm), { method: 'DELETE' }); renderRoleDetail(name); }
      catch (err) { alert(err.message); }
    }));
    document.getElementById('f-perm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const msg = document.getElementById('perm-msg');
      try {
        await api('/v1/admin/roles/' + encodeURIComponent(name) + '/permissions', { method: 'POST', json: { permission: fd.get('permission') } });
        renderRoleDetail(name);
      } catch (err) { msg.innerHTML = errBox(err); }
    });
    document.getElementById('f-assign').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const msg = document.getElementById('assign-msg');
      try {
        await api('/v1/admin/roles/' + encodeURIComponent(name) + '/assignments', { method: 'POST', json: { principal_type: fd.get('principal_type'), principal: fd.get('principal') } });
        msg.innerHTML = '<div class="ok">Assigned.</div>';
      } catch (err) { msg.innerHTML = errBox(err); }
    });
  } catch (err) { detail.innerHTML = errBox(err); }
}

async function identityOptions(selectEl) {
  try {
    const items = await api('/v1/admin/identities');
    selectEl.innerHTML = items.map(i => `<option value="${esc(i.name)}">${esc(i.name)} (${esc(i.kind)})</option>`).join('');
  } catch (err) { selectEl.innerHTML = '<option>' + esc(err.message) + '</option>'; }
}

async function renderKeys() {
  const main = document.getElementById('main');
  main.innerHTML = `<h2>Tokens & API Keys</h2>
    <div class="panel">
      <h3>Issue an API key</h3>
      <p class="hint">Long-lived and individually revocable — the recommended credential for agent gateways (OpenClaw, etc). Leave scopes blank to grant whatever the identity's roles currently allow.</p>
      <form id="f-key">
        <label>Identity</label><select name="identity" id="key-identity"></select>
        <label>Scopes (space-separated, optional)</label><input name="scopes" placeholder="shell:exec files:read">
        <label>Expires in seconds (optional)</label><input name="ttl" type="number" min="1">
        <div class="submit-row"><button type="submit">Issue key</button></div>
      </form>
      <div id="key-result"></div>
    </div>
    <div class="panel">
      <h3>Existing keys</h3>
      <label>For identity</label><select id="list-identity"></select>
      <div id="key-list"></div>
    </div>`;
  identityOptions(document.getElementById('key-identity'));
  const listSelect = document.getElementById('list-identity');
  await identityOptions(listSelect);
  listSelect.addEventListener('change', () => loadKeyList(listSelect.value));
  if (listSelect.value) loadKeyList(listSelect.value);

  document.getElementById('f-key').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const out = document.getElementById('key-result');
    const scopes = (fd.get('scopes') || '').trim();
    const body = {};
    if (scopes) body.scopes = scopes.split(/\\s+/);
    if (fd.get('ttl')) body.ttl_seconds = parseInt(fd.get('ttl'), 10);
    try {
      const res = await api('/v1/admin/identities/' + encodeURIComponent(fd.get('identity')) + '/api-keys', { method: 'POST', json: body });
      out.innerHTML = '<div class="ok">Key issued (shown once):</div><div class="secret-box">' + esc(res.key) + '</div>';
      loadKeyList(listSelect.value);
    } catch (err) { out.innerHTML = errBox(err); }
  });
}

async function loadKeyList(identity) {
  const el = document.getElementById('key-list');
  if (!identity) { el.innerHTML = ''; return; }
  el.innerHTML = 'Loading…';
  try {
    const keys = await api('/v1/admin/identities/' + encodeURIComponent(identity) + '/api-keys');
    el.innerHTML = `<table><thead><tr><th>ID</th><th>Scopes</th><th>Status</th><th>Expires</th><th></th></tr></thead>
      <tbody>${keys.map(k => `<tr><td class="mono">${esc(k.id.slice(0, 12))}…</td><td class="hint">${esc((k.scopes || []).join(', ') || 'inherit role perms')}</td>
        <td><span class="badge ${k.revoked ? 'off' : 'on'}">${k.revoked ? 'revoked' : 'active'}</span></td>
        <td class="hint">${esc(k.expires_at ? k.expires_at.slice(0, 19).replace('T', ' ') : 'never')}</td>
        <td>${k.revoked ? '' : `<button class="danger" data-revoke="${esc(k.id)}">Revoke</button>`}</td></tr>`).join('') || '<tr><td class="hint">No keys yet</td></tr>'}</tbody></table>`;
    el.querySelectorAll('button[data-revoke]').forEach(b => b.addEventListener('click', async () => {
      try { await api('/v1/admin/api-keys/' + encodeURIComponent(b.dataset.revoke), { method: 'DELETE' }); loadKeyList(identity); }
      catch (err) { alert(err.message); }
    }));
  } catch (err) { el.innerHTML = errBox(err); }
}

async function renderMcp() {
  const main = document.getElementById('main');
  main.innerHTML = `<h2>MCP / Agent Setup</h2>
    <div class="panel">
      <p class="hint">Generates a ready-to-paste MCP client config (Claude Desktop, Claude Code, Claude Cowork) for an agent identity, backed by a fresh API key. For OpenClaw or any HTTP gateway, use the REST base URL and API key directly against <span class="mono">/v1/authorize</span>.</p>
      <label>Agent identity</label><select id="mcp-identity"></select>
      <label>Path to the agenticiam executable on the machine running the MCP client</label>
      <input id="mcp-path" value="agenticiam" placeholder="/path/to/agenticiam or agenticiam.exe">
      <div class="submit-row"><button id="mcp-generate">Generate config</button></div>
      <div id="mcp-result"></div>
    </div>`;
  await identityOptions(document.getElementById('mcp-identity'));
  document.getElementById('mcp-generate').addEventListener('click', async () => {
    const identity = document.getElementById('mcp-identity').value;
    const path = document.getElementById('mcp-path').value || 'agenticiam';
    const out = document.getElementById('mcp-result');
    if (!identity) { out.innerHTML = errBox(new Error('No identity selected')); return; }
    try {
      const res = await api('/v1/admin/identities/' + encodeURIComponent(identity) + '/api-keys', { method: 'POST', json: {} });
      const config = { mcpServers: {} };
      config.mcpServers[identity] = { command: path, args: ['mcp'], env: { AGENTICIAM_TOKEN: res.key } };
      const json = JSON.stringify(config, null, 2);
      out.innerHTML = '<div class="ok">Paste this into your MCP client config:</div><textarea rows="9" readonly>' + esc(json) + '</textarea>' +
        '<div class="submit-row"><button class="secondary" id="mcp-copy">Copy to clipboard</button></div>';
      document.getElementById('mcp-copy').addEventListener('click', () => navigator.clipboard.writeText(json));
    } catch (err) { out.innerHTML = errBox(err); }
  });
}

async function renderAudit() {
  const main = document.getElementById('main');
  main.innerHTML = '<h2>Audit Log</h2><div id="content">Loading…</div>';
  const content = document.getElementById('content');
  try {
    const entries = await api('/v1/admin/audit?limit=200');
    content.innerHTML = `<table><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Resource</th><th>Result</th></tr></thead>
      <tbody>${entries.map(e => `<tr><td class="hint">${esc((e.ts || '').slice(0, 19).replace('T', ' '))}</td>
        <td>${esc(e.actor_name || '—')}</td><td class="mono">${esc(e.action)}</td>
        <td class="hint">${esc(e.resource || '')}</td>
        <td><span class="badge ${['success', 'allowed'].includes(e.result) ? 'on' : 'off'}">${esc(e.result)}</span></td></tr>`).join('')}</tbody></table>`;
  } catch (err) { content.innerHTML = errBox(err); }
}

boot();
</script>
</body>
</html>
'''
