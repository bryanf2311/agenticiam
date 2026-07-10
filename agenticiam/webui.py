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
  ['setup', 'Setup'], ['identities', 'Identities'], ['groups', 'Groups'], ['roles', 'Roles'],
  ['keys', 'Tokens & Keys'], ['mcp', 'MCP / Agent Setup'], ['audit', 'Audit Log'],
];
let currentSection = 'identities';

function showApp() {
  root.innerHTML = `
    <div class="shell">
      <nav class="sidebar">
        <div class="brand">AgenticIAM</div>
        <button id="nav-new-agent" style="margin:0 12px 14px;width:calc(100% - 24px)">+ New Agent</button>
        <div id="section-links">
          ${SECTIONS.map(([id, label]) => `<a data-section="${id}">${esc(label)}</a>`).join('')}
        </div>
        <div class="spacer"></div>
        <div class="who">Signed in as <strong>${esc(who.name)}</strong><br>
          ${who.scopes && who.scopes.includes('*') ? 'full admin' : esc((who.scopes || []).join(', ') || 'no permissions')}
        </div>
        <a id="logout-link" style="padding-top:0">Log out</a>
      </nav>
      <main id="main"></main>
    </div>`;
  document.querySelectorAll('#section-links a[data-section]').forEach(a => a.addEventListener('click', () => selectSection(a.dataset.section)));
  document.getElementById('nav-new-agent').addEventListener('click', () => { resetWizard(); selectSection('newagent'); });
  document.getElementById('logout-link').addEventListener('click', logout);
  selectSection(currentSection);
}

function selectSection(id) {
  currentSection = id;
  document.querySelectorAll('#section-links a[data-section]').forEach(a => a.classList.toggle('active', a.dataset.section === id));
  const renderers = {
    setup: renderSetup, identities: renderIdentities, groups: renderGroups, roles: renderRoles, keys: renderKeys,
    mcp: renderMcp, audit: renderAudit, newagent: renderWizard,
  };
  renderers[id]();
}

function errBox(e) { return '<div class="err">' + esc(e.message || String(e)) + '</div>'; }

let wizardStep = 1;
let wizardState = {};

function resetWizard() {
  wizardStep = 1;
  wizardState = {
    status: null, target: 'goose', name: '', provider: 'ollama', model: '', apiKey: '',
    contextLimit: '', permissions: [], customPermissions: '',
    dispatchWildcard: false, dispatchTargets: [], group: '',
    setDefault: false, cmd: '', args: '', result: null,
  };
}

const WIZARD_COMMON_PERMISSIONS = [
  ['shell:exec', 'Run shell commands'],
  ['files:read', 'Read files'],
  ['files:write', 'Write/modify files'],
  ['browser:control', 'Control a browser'],
  ['email:send', 'Send email'],
];

const WIZARD_PROVIDERS = [
  ['ollama', 'Ollama (local, free)'],
  ['anthropic', 'Anthropic (Claude — API key)'],
  ['google', 'Google (Gemini — API key)'],
];

async function renderWizard() {
  const main = document.getElementById('main');
  main.innerHTML = '<h2>New Agent</h2><div id="wizard-body">Loading…</div>';
  if (wizardStep === 1) return renderWizardStep1();
  if (wizardStep === 2) return renderWizardStep2();
  if (wizardStep === 3) return renderWizardStep3();
  if (wizardStep === 4) return renderWizardStep4();
  if (wizardStep === 5) return renderWizardStep5();
}

async function renderWizardStep1() {
  const body = document.getElementById('wizard-body');
  try {
    wizardState.status = await api('/v1/admin/goose/status');
  } catch (err) { body.innerHTML = errBox(err); return; }
  const s = wizardState.status;
  const target = wizardState.target;
  const ready = target === 'goose' ? s.goose_installed : s.openclaw_installed;
  const targetRow = target === 'goose'
    ? `<tr><td>Goose CLI</td><td>${s.goose_installed ? '<span class="badge on">found</span> <span class="hint mono">' + esc(s.goose_path) + '</span>' : '<span class="badge off">not found</span>'}</td></tr>`
    : `<tr><td>OpenClaw</td><td>${s.openclaw_installed ? '<span class="badge on">found</span> <span class="hint mono">' + esc(s.openclaw_path) + '</span>' : '<span class="badge off">not found</span>'}</td></tr>`;
  body.innerHTML = `
    <div class="panel">
      <h3>Step 1 of 4 — Target & prerequisites</h3>
      <p class="hint">This wires a new AgenticIAM agent identity into a runtime on this machine as an MCP tool — permissions and all. First, where should it run?</p>
      <label style="display:flex;align-items:center;gap:8px;margin:8px 0">
        <input type="radio" name="wiz-target" id="wiz-target-goose" value="goose" ${target === 'goose' ? 'checked' : ''} style="width:auto">
        <span><strong>Goose</strong> <span class="hint">— a single interactive local session per agent (goose session / goose run)</span></span>
      </label>
      <label style="display:flex;align-items:center;gap:8px;margin:8px 0 16px">
        <input type="radio" name="wiz-target" id="wiz-target-openclaw" value="openclaw" ${target === 'openclaw' ? 'checked' : ''} style="width:auto">
        <span><strong>OpenClaw</strong> <span class="hint">— a persistent gateway persona reachable from chat apps (Discord, Telegram, WhatsApp, ...)</span></span>
      </label>
      <table><tbody>
        ${targetRow}
        <tr><td>Ollama <span class="hint">(only needed for local models)</span></td><td>${s.ollama_reachable ? '<span class="badge on">running</span>' : (s.ollama_installed ? '<span class="badge off">installed, not running</span>' : '<span class="badge off">not found</span>')}</td></tr>
      </tbody></table>
      ${!ready ? '<p class="hint">Not found. See the <a href="#" id="wiz-goto-setup">Setup</a> tab for install commands.</p>' : ''}
      ${!s.ollama_reachable ? '<p class="hint">Want a local model instead of a cloud API key? See the Setup tab for install commands, then run <span class="mono">ollama serve</span> and pull a model, e.g. <span class="mono">ollama pull llama3.1</span>.</p>' : ''}
      ${target === 'goose'
        ? `<p class="hint">Goose config will be written to <span class="mono">${esc(s.config_path)}</span> (a backup of any existing file is kept alongside it).</p>`
        : `<p class="hint">OpenClaw's own config is managed through its CLI (<span class="mono">openclaw mcp add</span> / <span class="mono">openclaw agents add</span>) — the final step gives you the exact commands to run, rather than AgenticIAM editing OpenClaw's config file directly.</p>`}
      <div class="submit-row row">
        <button class="secondary" id="wiz-recheck">Re-check</button>
        <button id="wiz-next" ${ready ? '' : 'disabled'}>Next</button>
      </div>
    </div>`;
  document.getElementById('wiz-recheck').addEventListener('click', renderWizardStep1);
  document.getElementById('wiz-next').addEventListener('click', () => { wizardStep = 2; renderWizard(); });
  document.querySelectorAll('input[name="wiz-target"]').forEach(r => r.addEventListener('change', (e) => {
    wizardState.target = e.target.value;
    renderWizardStep1();
  }));
  const gotoSetup = document.getElementById('wiz-goto-setup');
  if (gotoSetup) gotoSetup.addEventListener('click', (e) => { e.preventDefault(); selectSection('setup'); });
}

async function renderWizardStep2() {
  const body = document.getElementById('wizard-body');
  const isCloud = wizardState.provider !== 'ollama';
  body.innerHTML = `
    <div class="panel">
      <h3>Step 2 of 4 — Name, provider & model</h3>
      <label>Agent name</label>
      <input id="wiz-name" value="${esc(wizardState.name)}" placeholder="research-bot">
      <label>Provider</label>
      <select id="wiz-provider">
        ${WIZARD_PROVIDERS.map(([id, label]) => `<option value="${id}" ${id === wizardState.provider ? 'selected' : ''}>${esc(label)}</option>`).join('')}
      </select>
      <div id="wiz-key-row" style="display:${isCloud ? 'block' : 'none'}">
        <label>API key</label>
        <div class="row">
          <input id="wiz-api-key" type="password" value="${esc(wizardState.apiKey)}" style="flex:1" placeholder="paste your key">
          <button class="secondary" type="button" id="wiz-load-models">Load models</button>
        </div>
        <p class="hint">Used only to build the launch command in step 4 — AgenticIAM never stores this key anywhere (not in Goose's config.yaml, not in its own database).</p>
      </div>
      <label>Model</label>
      <div id="wiz-model-wrap">${isCloud ? '<span class="hint">Enter your API key above and click "Load models".</span>' : 'Loading…'}</div>
      <div class="submit-row row">
        <button class="secondary" id="wiz-back">Back</button>
        <button id="wiz-next" disabled>Next</button>
      </div>
    </div>`;

  document.getElementById('wiz-back').addEventListener('click', () => {
    wizardState.name = document.getElementById('wiz-name').value.trim();
    wizardStep = 1; renderWizard();
  });
  document.getElementById('wiz-provider').addEventListener('change', (e) => {
    wizardState.name = document.getElementById('wiz-name').value.trim();
    wizardState.provider = e.target.value;
    wizardState.model = '';
    renderWizardStep2();
  });

  async function loadModels() {
    const wrap = document.getElementById('wiz-model-wrap');
    const nextBtn = document.getElementById('wiz-next');
    wrap.innerHTML = 'Loading…';
    nextBtn.disabled = true;
    const provider = document.getElementById('wiz-provider').value;
    const apiKeyInput = document.getElementById('wiz-api-key');
    const apiKey = apiKeyInput ? apiKeyInput.value : '';
    try {
      const res = await api('/v1/admin/goose/models', { method: 'POST', json: { provider, api_key: apiKey || undefined } });
      if (res.available && res.models.length) {
        wrap.innerHTML = `<select id="wiz-model">${res.models.map(m => `<option value="${esc(m)}" ${m === wizardState.model ? 'selected' : ''}>${esc(m)}</option>`).join('')}</select>`;
        nextBtn.disabled = false;
      } else {
        wrap.innerHTML = `<div class="err">${esc(res.error || 'No models found')}</div>`;
      }
    } catch (err) { wrap.innerHTML = errBox(err); }
  }

  if (isCloud) {
    document.getElementById('wiz-load-models').addEventListener('click', loadModels);
  } else {
    loadModels();
  }

  document.getElementById('wiz-next').addEventListener('click', () => {
    const nameInput = document.getElementById('wiz-name').value.trim();
    if (!nameInput) { alert('Please enter a name.'); return; }
    wizardState.name = nameInput;
    wizardState.provider = document.getElementById('wiz-provider').value;
    const modelSel = document.getElementById('wiz-model');
    wizardState.model = modelSel ? modelSel.value : '';
    const apiKeyInput = document.getElementById('wiz-api-key');
    wizardState.apiKey = apiKeyInput ? apiKeyInput.value : '';
    wizardStep = 3; renderWizard();
  });
}

async function renderWizardStep3() {
  const body = document.getElementById('wizard-body');
  body.innerHTML = 'Loading…';
  let existingAgents = [];
  let existingGroups = [];
  try { existingAgents = await api('/v1/admin/identities?kind=agent'); } catch (err) { /* per-agent target list just won't show; wildcard checkbox still works */ }
  try { existingGroups = await api('/v1/admin/groups'); } catch (err) { /* group field still works, just no autocomplete */ }

  const groupSection = `
    <h3 style="margin-top:26px">Group (optional)</h3>
    <p class="hint">Put "${esc(wizardState.name)}" in a group with its teammates — e.g. create/reuse "marketing" for a marketing-boss + marketing-manager + marketing-intern team. Pick an existing one or type a new name.</p>
    <input id="wiz-group" list="wiz-group-options" value="${esc(wizardState.group)}" placeholder="e.g. marketing">
    <datalist id="wiz-group-options">
      ${existingGroups.map(g => `<option value="${esc(g.name)}">`).join('')}
    </datalist>`;

  // The wildcard checkbox doesn't need any other agents to exist yet (it
  // grants dispatch:* — covers workers created later too), so it's always
  // shown; only the per-agent target list needs existingAgents.
  const managerSection = `
    <h3 style="margin-top:26px">Manager permissions (optional)</h3>
    <p class="hint">Let "${esc(wizardState.name)}" delegate tasks to other agents and get their response back — a "manager" dispatching to "workers". Dispatch only works against Ollama-backed agents (AgenticIAM never stores the API keys cloud providers would need). Checking any box here preloads "${esc(wizardState.name)}" with a system prompt explaining how to call the dispatch tool correctly (via a Goose recipe file) — no need to paste it in yourself.</p>
    <label style="display:flex;align-items:center;gap:8px;margin:8px 0">
      <input type="checkbox" id="wiz-dispatch-wildcard" ${wizardState.dispatchWildcard ? 'checked' : ''} style="width:auto">
      <span>Can dispatch to <strong>any</strong> agent, including ones created later (<span class="mono">dispatch:*</span>)</span>
    </label>
    ${existingAgents.length ? `
    <div id="wiz-dispatch-list" style="${wizardState.dispatchWildcard ? 'opacity:.4;pointer-events:none' : ''}">
      ${existingAgents.map(a => `
        <label style="display:flex;align-items:center;gap:8px;margin:6px 0">
          <input type="checkbox" class="wiz-dispatch-target" value="${esc(a.name)}" ${wizardState.dispatchTargets.includes(a.name) ? 'checked' : ''} style="width:auto">
          <span>${esc(a.name)} <span class="hint mono">${a.metadata && a.metadata.goose ? esc(a.metadata.goose.provider + '/' + a.metadata.goose.model) : 'not a Goose agent'}</span></span>
        </label>`).join('')}
    </div>` : '<p class="hint">No other agents exist yet to dispatch to individually — create this one first, then grant it dispatch rights to specific agents later from the Roles tab, or just use the wildcard above.</p>'}`;

  body.innerHTML = `
    <div class="panel">
      <h3>Step 3 of 4 — Permissions</h3>
      <p class="hint">What should "${esc(wizardState.name)}" be allowed to do? This becomes a role scoped just to this agent.</p>
      ${WIZARD_COMMON_PERMISSIONS.map(([perm, label]) => `
        <label style="display:flex;align-items:center;gap:8px;margin:8px 0">
          <input type="checkbox" class="wiz-perm-checkbox" value="${perm}" ${wizardState.permissions.includes(perm) ? 'checked' : ''} style="width:auto">
          <span>${esc(label)} <span class="mono hint">(${perm})</span></span>
        </label>`).join('')}
      <label>Additional permissions (space-separated, e.g. files:* custom:scope)</label>
      <input id="wiz-custom-perms" value="${esc(wizardState.customPermissions)}">
      ${groupSection}
      ${managerSection}
      <div class="submit-row row">
        <button class="secondary" id="wiz-back">Back</button>
        <button id="wiz-next">Next</button>
      </div>
    </div>`;
  document.getElementById('wiz-back').addEventListener('click', () => {
    wizardState.group = document.getElementById('wiz-group').value.trim();
    wizardStep = 2; renderWizard();
  });
  const wildcardCb = document.getElementById('wiz-dispatch-wildcard');
  if (wildcardCb) {
    wildcardCb.addEventListener('change', (e) => {
      const list = document.getElementById('wiz-dispatch-list');
      if (!list) return;
      list.style.opacity = e.target.checked ? '.4' : '1';
      list.style.pointerEvents = e.target.checked ? 'none' : 'auto';
    });
  }
  document.getElementById('wiz-next').addEventListener('click', () => {
    wizardState.permissions = Array.from(body.querySelectorAll('.wiz-perm-checkbox:checked')).map(i => i.value);
    wizardState.customPermissions = document.getElementById('wiz-custom-perms').value;
    wizardState.group = document.getElementById('wiz-group').value.trim();
    wizardState.dispatchWildcard = wildcardCb ? wildcardCb.checked : false;
    wizardState.dispatchTargets = Array.from(body.querySelectorAll('.wiz-dispatch-target:checked')).map(i => i.value);
    wizardStep = 4; renderWizard();
  });
}

function renderWizardStep4() {
  const body = document.getElementById('wizard-body');
  const s = wizardState.status || {};
  if (!wizardState.cmd) wizardState.cmd = s.suggested_cmd || 'agenticiam';
  if (!wizardState.args) wizardState.args = (s.suggested_args || ['mcp']).join(' ');
  const dispatchPerms = wizardState.dispatchWildcard ? ['dispatch:*'] : wizardState.dispatchTargets.map(n => `dispatch:${n}`);
  const allPerms = wizardState.permissions.concat((wizardState.customPermissions || '').split(/\\s+/).filter(Boolean)).concat(dispatchPerms);
  const providerLabel = (WIZARD_PROVIDERS.find(([id]) => id === wizardState.provider) || [wizardState.provider, wizardState.provider])[1];
  body.innerHTML = `
    <div class="panel">
      <h3>Step 4 of 4 — Review & create</h3>
      <table><tbody>
        <tr><td>Name</td><td>${esc(wizardState.name)}</td></tr>
        <tr><td>Target</td><td>${wizardState.target === 'goose' ? 'Goose' : 'OpenClaw'}</td></tr>
        <tr><td>Provider</td><td>${esc(providerLabel)}</td></tr>
        <tr><td>Model</td><td>${esc(wizardState.model)}</td></tr>
        <tr><td>Group</td><td>${esc(wizardState.group || '(none)')}</td></tr>
        <tr><td>Permissions</td><td class="mono">${esc(allPerms.join(', ') || '(none)')}</td></tr>
        ${wizardState.target === 'goose' ? `<tr><td>Goose config</td><td class="mono">${esc(s.config_path || '')}</td></tr>` : ''}
      </tbody></table>
      <label>Context window (tokens, optional — leave blank for the model's default)</label>
      <input id="wiz-context-limit" type="number" min="1" value="${esc(wizardState.contextLimit)}" placeholder="e.g. 32000">
      <label>Command ${wizardState.target === 'goose' ? 'Goose' : 'OpenClaw'} should run for this agent's MCP tools</label>
      <input id="wiz-cmd" value="${esc(wizardState.cmd)}">
      <label>Arguments (space-separated)</label>
      <input id="wiz-args" value="${esc(wizardState.args)}">
      ${wizardState.target === 'goose' ? `
      <label style="display:flex;align-items:center;gap:8px;margin-top:14px">
        <input type="checkbox" id="wiz-default" ${wizardState.setDefault ? 'checked' : ''} style="width:auto">
        <span>Also set this as Goose's default provider/model/context window (affects <em>all</em> Goose sessions, not just this agent)</span>
      </label>` : ''}
      <div class="submit-row row">
        <button class="secondary" id="wiz-back">Back</button>
        <button id="wiz-create">Create agent</button>
      </div>
      <div id="wiz-create-msg"></div>
    </div>`;
  document.getElementById('wiz-back').addEventListener('click', () => {
    wizardState.contextLimit = document.getElementById('wiz-context-limit').value.trim();
    wizardStep = 3; renderWizard();
  });
  document.getElementById('wiz-create').addEventListener('click', async () => {
    wizardState.cmd = document.getElementById('wiz-cmd').value.trim();
    wizardState.args = document.getElementById('wiz-args').value.trim();
    const defaultCb = document.getElementById('wiz-default');
    wizardState.setDefault = defaultCb ? defaultCb.checked : false;
    wizardState.contextLimit = document.getElementById('wiz-context-limit').value.trim();
    const msg = document.getElementById('wiz-create-msg');
    msg.innerHTML = 'Creating…';
    try {
      const res = await api('/v1/admin/goose/agents', { method: 'POST', json: {
        name: wizardState.name,
        target: wizardState.target,
        model: wizardState.model,
        provider: wizardState.provider,
        api_key: wizardState.apiKey || undefined,
        context_limit: wizardState.contextLimit ? parseInt(wizardState.contextLimit, 10) : undefined,
        permissions: allPerms,
        group: wizardState.group || undefined,
        set_as_default: wizardState.setDefault,
        cmd: wizardState.cmd,
        args: wizardState.args.split(/\\s+/).filter(Boolean),
      } });
      wizardState.result = res;
      wizardStep = 5;
      renderWizard();
    } catch (err) { msg.innerHTML = errBox(err); }
  });
}

function renderCommandShells(commands, copyKeyPrefix) {
  const shells = [['bash', 'macOS / Linux (bash, zsh)'], ['powershell', 'Windows PowerShell'], ['cmd', 'Windows cmd.exe']];
  return shells.map(([key, label]) => `
    <div style="margin:10px 0">
      <div class="hint">${esc(label)}</div>
      <div class="row" style="align-items:stretch">
        <div class="secret-box" style="flex:1;margin:4px 0">${esc(commands[key])}</div>
        <button class="secondary" data-copy="${copyKeyPrefix}::${key}">Copy</button>
      </div>
    </div>`).join('');
}

function renderWizardStep5() {
  const body = document.getElementById('wizard-body');
  const r = wizardState.result;
  const managerNote = r.is_manager
    ? (r.target === 'goose'
        ? (r.manager_recipe_path
            ? `<p>Manager recipe written to <span class="mono">${esc(r.manager_recipe_path)}</span> — the dispatch system prompt loads automatically with the command below.</p>`
            : `<div class="err">Couldn't write the manager recipe file: ${esc(r.manager_recipe_error)}. The agent was still created, but you'll need to paste the system prompt in by hand — see docs/manager-system-prompt.md.</div>`)
        : (r.manager_soul_path
            ? `<p>Manager <span class="mono">SOUL.md</span> written to <span class="mono">${esc(r.manager_soul_path)}</span> — the dispatch system prompt loads automatically once you run the commands below.</p>`
            : `<div class="err">Couldn't write SOUL.md: ${esc(r.manager_soul_error)}. The agent was still created, but you'll need to paste the system prompt into its workspace's SOUL.md by hand — see docs/manager-system-prompt.md.</div>`))
    : '';

  if (r.target === 'openclaw') {
    body.innerHTML = `
      <div class="panel">
        <h3>Agent created</h3>
        <p class="ok">"${esc(r.identity.name)}" is ready.${r.group ? ` Added to group "${esc(r.group)}".` : ''}</p>
        ${managerNote}
        <p class="hint">Run these two commands (same machine as the OpenClaw Gateway) to finish wiring it up:</p>
        <label>1. ${esc(r.openclaw_commands.register_tools.title)}</label>
        ${renderCommandShells(r.openclaw_commands.register_tools, 'register_tools')}
        <label style="margin-top:18px">2. ${esc(r.openclaw_commands.create_agent.title)}</label>
        ${renderCommandShells(r.openclaw_commands.create_agent, 'create_agent')}
        <p class="hint">Then run <span class="mono">openclaw gateway restart</span> and either bind a channel to this new agent or chat with it directly from the Control UI (<span class="mono">openclaw dashboard</span>).</p>
        <div class="submit-row row">
          <button id="wiz-another">Create another agent</button>
        </div>
      </div>`;
    body.querySelectorAll('button[data-copy]').forEach(btn => btn.addEventListener('click', () => {
      const [group, key] = btn.dataset.copy.split('::');
      navigator.clipboard.writeText(r.openclaw_commands[group][key]);
    }));
    document.getElementById('wiz-another').addEventListener('click', () => { resetWizard(); renderWizard(); });
    return;
  }

  body.innerHTML = `
    <div class="panel">
      <h3>Agent created</h3>
      <p class="ok">"${esc(r.identity.name)}" is ready.${r.group ? ` Added to group "${esc(r.group)}".` : ''}</p>
      ${managerNote}
      ${r.goose_config_written
        ? `<p>Goose extension registered at <span class="mono">${esc(r.config_path)}</span>.</p>`
        : `<div class="err">Couldn't write Goose config automatically: ${esc(r.goose_config_error)}</div>
           <p>Add this to <span class="mono">${esc(r.config_path)}</span> by hand:</p>
           <textarea rows="9" readonly>${esc(r.manual_extension_snippet)}</textarea>`}
      <label>Run this to start chatting with your agent — pick the line for your terminal:</label>
      ${renderCommandShells(r.launch_commands, 'launch')}
      <div class="submit-row row">
        <button id="wiz-another">Create another agent</button>
      </div>
    </div>`;
  body.querySelectorAll('button[data-copy]').forEach(btn => btn.addEventListener('click', () => {
    const key = btn.dataset.copy.split('::')[1];
    navigator.clipboard.writeText(r.launch_commands[key]);
  }));
  document.getElementById('wiz-another').addEventListener('click', () => { resetWizard(); renderWizard(); });
}

async function renderSetup() {
  const main = document.getElementById('main');
  main.innerHTML = '<h2>Setup</h2><div id="content">Loading…</div>';
  const content = document.getElementById('content');
  const shells = [['bash', 'macOS / Linux (bash, zsh)'], ['powershell', 'Windows PowerShell'], ['cmd', 'Windows cmd.exe / notes']];
  try {
    const commands = await api('/v1/admin/setup/install-commands');
    const tools = [
      ['ollama', 'Ollama', 'Runs models locally — needed for local (free, private) models with either Goose or OpenClaw.'],
      ['goose', 'Goose', 'A local AI agent CLI — one runtime option for the New Agent wizard.'],
      ['openclaw', 'OpenClaw', 'A self-hosted gateway connecting chat apps (Discord, Telegram, WhatsApp, ...) to AI agents — the other runtime option for the New Agent wizard.'],
    ];
    content.innerHTML = `
      <p class="hint">One-time install commands for the tools the New Agent wizard drives. Run them on the machine where <span class="mono">agenticiam serve</span>/<span class="mono">gui</span> is running.</p>
      ${tools.map(([key, label, hint]) => `
        <div class="panel">
          <h3>${esc(label)}</h3>
          <p class="hint">${esc(hint)}</p>
          ${shells.map(([shellKey, shellLabel]) => `
            <div style="margin:8px 0">
              <div class="hint">${esc(shellLabel)}</div>
              <div class="row" style="align-items:stretch">
                <div class="secret-box" style="flex:1;margin:4px 0;white-space:pre-wrap">${esc(commands[key][shellKey])}</div>
                <button class="secondary" data-copy-tool="${key}::${shellKey}">Copy</button>
              </div>
            </div>`).join('')}
        </div>`).join('')}
      <div class="panel">
        <h3>Model recommendations for your hardware</h3>
        <p class="hint">Sizes are approximate (Q4_K_M quantization, Ollama's common default) — a starting point, not an exact fit.</p>
        <label>System RAM (GB)</label>
        <input id="setup-ram" type="number" min="1" placeholder="e.g. 64">
        <label>GPU VRAM (GB, optional — leave blank for CPU-only / integrated graphics)</label>
        <input id="setup-vram" type="number" min="0" placeholder="e.g. 8">
        <div class="submit-row"><button id="setup-recommend">Recommend models</button></div>
        <div id="setup-recommendations"></div>
      </div>`;
    content.querySelectorAll('button[data-copy-tool]').forEach(btn => btn.addEventListener('click', () => {
      const [tool, shellKey] = btn.dataset.copyTool.split('::');
      navigator.clipboard.writeText(commands[tool][shellKey]);
    }));
    document.getElementById('setup-recommend').addEventListener('click', async () => {
      const out = document.getElementById('setup-recommendations');
      const ram = parseFloat(document.getElementById('setup-ram').value);
      const vram = document.getElementById('setup-vram').value.trim();
      if (!ram || ram <= 0) { out.innerHTML = '<div class="err">Enter your system RAM in GB.</div>'; return; }
      out.innerHTML = 'Loading…';
      try {
        const rec = await api('/v1/admin/setup/recommend-models', { method: 'POST', json: {
          ram_gb: ram, vram_gb: vram ? parseFloat(vram) : undefined,
        } });
        const renderTier = (title, hint, models) => `
          <h4 style="margin-top:16px">${esc(title)}</h4>
          <p class="hint">${esc(hint)}</p>
          ${models.length ? `<table><thead><tr><th>Model</th><th>Params</th><th>Tags</th><th>~Size</th><th></th></tr></thead><tbody>
            ${models.map(m => `<tr>
              <td class="mono">${esc(m.id)}</td><td>${esc(m.family)}</td><td class="hint">${esc(m.tags.join(', '))}</td>
              <td>${m.approx_size_gb} GB</td>
              <td><button class="secondary" data-copy-model="${esc(m.pull_command)}">Copy pull command</button></td>
            </tr>`).join('')}
          </tbody></table>` : '<p class="hint">Nothing in this tier for your hardware.</p>'}`;
        out.innerHTML = `
          ${renderTier('Fast (fully GPU-accelerated)', 'Whole model fits in VRAM with headroom for context.', rec.fast)}
          ${renderTier('Usable (partial GPU + CPU RAM)', 'Runs, but slower — part or all of it spills into system RAM.', rec.usable)}
          <p class="hint" style="margin-top:12px">${esc(rec.note)}</p>`;
        out.querySelectorAll('button[data-copy-model]').forEach(btn => btn.addEventListener('click', () =>
          navigator.clipboard.writeText(btn.dataset.copyModel)));
      } catch (err) { out.innerHTML = errBox(err); }
    });
  } catch (err) { content.innerHTML = errBox(err); }
}

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
      const res = await api('/v1/admin/identities/' + encodeURIComponent(name), { method: 'DELETE' });
      if (res && Object.prototype.hasOwnProperty.call(res, 'goose_config_updated')) {
        renderIdentityDeleteResult(res);
      } else {
        renderIdentities();
      }
    } else if (act === 'rotate') {
      const res = await api('/v1/admin/identities/' + encodeURIComponent(name) + '/rotate-secret', { method: 'POST', json: {} });
      alert('New secret for ' + name + ' (shown once):\\n\\n' + res.secret);
    } else if (act === 'perms') {
      const res = await api('/v1/admin/identities/' + encodeURIComponent(name) + '/permissions');
      alert('Effective permissions for ' + name + ':\\n\\n' + (res.length ? res.join('\\n') : '(none)'));
    }
  } catch (err) { alert(err.message); }
}

function renderIdentityDeleteResult(res) {
  const main = document.getElementById('main');
  let body;
  if (res.goose_config_updated) {
    body = `<p class="ok">Removed the "${esc(res.identity)}" extension entry from <span class="mono">${esc(res.config_path)}</span>.</p>`;
  } else if (res.goose_config_error) {
    body = `
      <div class="err">Deleted "${esc(res.identity)}" from AgenticIAM, but couldn't automatically update Goose's config: ${esc(res.goose_config_error)}</div>
      <p>Remove it from <span class="mono">${esc(res.config_path)}</span> by hand — run this, or open the file and delete the block yourself:</p>
      <textarea rows="3" readonly>${esc(res.manual_removal_instructions || '')}</textarea>`;
  } else {
    body = `<p class="hint">"${esc(res.identity)}" was deleted. ${esc(res.goose_config_note || 'No matching entry was found in Goose\\'s config.yaml — nothing to clean up there.')}</p>`;
  }
  main.innerHTML = `
    <h2>Identity deleted</h2>
    <div class="panel">
      ${body}
      <div class="submit-row"><button id="back-to-identities">Back to Identities</button></div>
    </div>`;
  document.getElementById('back-to-identities').addEventListener('click', renderIdentities);
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
      <div id="member-msg"></div>
      <div class="submit-row"><button class="secondary" id="btn-start-group">Show start commands for this team</button></div>
      <div id="group-start-commands"></div>`;
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
    document.getElementById('btn-start-group').addEventListener('click', () => renderGroupStartCommands(name));
  } catch (err) { detail.innerHTML = errBox(err); }
}

async function renderGroupStartCommands(name) {
  const wrap = document.getElementById('group-start-commands');
  wrap.innerHTML = 'Loading…';
  const shells = [['bash', 'macOS / Linux (bash, zsh)'], ['powershell', 'Windows PowerShell'], ['cmd', 'Windows cmd.exe']];
  try {
    const res = await api('/v1/admin/groups/' + encodeURIComponent(name) + '/launch-commands');
    const renderShellRows = (commands, copyKey) => shells.map(([key, label]) => `
      <div style="margin:8px 0">
        <div class="hint">${esc(label)}</div>
        <div class="row" style="align-items:stretch">
          <div class="secret-box" style="flex:1;margin:4px 0">${esc(commands[key])}</div>
          <button class="secondary" data-copy-cmd="${copyKey}::${key}">Copy</button>
        </div>
      </div>`).join('');
    wrap.innerHTML = `
      <h3 style="margin-top:22px">Start "${esc(name)}"</h3>
      <p class="hint">One set of commands per teammate — open a terminal tab per agent and paste the line for your shell. Agents not created through the wizard have no start command and are skipped.</p>
      ${res.agents.map(a => {
        if (a.target === 'openclaw' && a.openclaw_commands) {
          return `
            <div class="panel">
              <strong>${esc(a.name)}</strong> <span class="hint">(OpenClaw)</span>${a.goose && a.goose.is_manager ? ' <span class="badge on">manager — dispatch prompt preloaded</span>' : ''}
              <label style="margin-top:8px">1. ${esc(a.openclaw_commands.register_tools.title)}</label>
              ${renderShellRows(a.openclaw_commands.register_tools, `${esc(a.name)}::register_tools`)}
              <label>2. ${esc(a.openclaw_commands.create_agent.title)}</label>
              ${renderShellRows(a.openclaw_commands.create_agent, `${esc(a.name)}::create_agent`)}
            </div>`;
        }
        if (a.launch_commands) {
          return `
            <div class="panel">
              <strong>${esc(a.name)}</strong>${a.goose && a.goose.is_manager ? ' <span class="badge on">manager — dispatch prompt preloaded</span>' : ''}
              ${renderShellRows(a.launch_commands, `${esc(a.name)}::launch`)}
            </div>`;
        }
        return `<div class="panel hint">${esc(a.name)}: not a Goose/OpenClaw agent, no start command.</div>`;
      }).join('') || '<p class="hint">No members yet.</p>'}`;
    wrap.querySelectorAll('button[data-copy-cmd]').forEach(btn => btn.addEventListener('click', () => {
      const [agentName, group, shellKey] = btn.dataset.copyCmd.split('::');
      const agent = res.agents.find(a => a.name === agentName);
      const text = group === 'launch' ? agent.launch_commands[shellKey] : agent.openclaw_commands[group][shellKey];
      navigator.clipboard.writeText(text);
    }));
  } catch (err) { wrap.innerHTML = errBox(err); }
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
