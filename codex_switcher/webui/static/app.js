/* codex（ChatGPT App）多平台模型切换 —— 前端逻辑 */

const TOKEN = new URLSearchParams(location.search).get('t') || '';
const api = (path, body) => {
  const url = `/api/${path}?t=${encodeURIComponent(TOKEN)}`;
  return fetch(url, body ? {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  } : undefined).then(async (response) => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  });
};

let STATE = { providers: [], presets: [], current: {} };
let SELECTED = null;
let QUERY = '';
const BALANCE_TRIED = new Set();

const $ = (id) => document.getElementById(id);

function toast(message, isError = false) {
  const node = $('toast');
  node.textContent = message;
  node.classList.toggle('err', !!isError);
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 4200);
}

function shortTokens(value) {
  if (value === null || value === undefined) return '—';
  let n = Number(value);
  for (const unit of ['', 'K', 'M', 'B']) {
    if (Math.abs(n) < 1000 || unit === 'B') {
      return unit ? `${n.toFixed(1).replace('.0', '')}${unit}` : String(Math.round(n));
    }
    n /= 1000;
  }
  return String(value);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function loadState(withBalance = false) {
  STATE = await api('state' + (withBalance ? '?balance=1' : ''));
  renderHeader();
  renderThreadBanner();
  renderGuardBanner();
  renderProviders();
  if (SELECTED) {
    const still = STATE.providers.find((p) => p.id === SELECTED);
    if (still) renderDetail(still);
    else {
      SELECTED = null;
      $('detail').innerHTML = '<div class="placeholder"></div>';
      $('detail').firstChild.textContent = t('detail.pick_provider');
    }
  } else {
    // 首次打开时自动展示当前正在使用的平台
    const current = STATE.providers.find((p) => p.is_current) || STATE.providers[0];
    if (current) {
      SELECTED = current.id;
      renderProviders();
      renderDetail(current);
    }
  }
}

function renderThreadBanner() {
  const info = STATE.threads || {};
  const banner = $('thread-banner');
  if (!banner) return;
  if (!info.mismatch) { banner.hidden = true; return; }
  banner.hidden = false;
  $('thread-banner-title').textContent = t('banner.detail', { n: info.mismatch });
  $('thread-banner-detail').textContent = t('banner.note');
}

function renderGuardBanner() {
  const banner = $('guard-banner');
  if (!banner) return;
  const g = STATE.guard;
  // 只在真有风险时出现：装得下就别打扰用户
  if (!g || !g.ratio || (g.action !== 'fork' && g.action !== 'compact-first')) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  const num = (n) => (n || 0).toLocaleString('en-US');
  const advice = g.action === 'fork' ? t('guard.action_fork') : t('guard.action_compact');
  const note = g.action === 'fork' ? t('guard.note_fork') : t('guard.note_compact');
  $('guard-banner-title').textContent = t('guard.title');
  $('guard-banner-detail').textContent = t('guard.detail', {
    tokens: num(g.tokens), window: num(g.window),
    percent: Math.round(g.ratio * 100), advice: advice,
  }) + ' ' + note;
}

async function repairThreads(dryRun) {
  try {
    const result = await api('repair', dryRun ? { dry_run: true } : {});
    if (dryRun) {
      const lines = (result.items || []).map((item) =>
        `${item.id}  ${item.from} → ${item.to}  （${item.model}）`);
      alert(t('repair.preview_title', { count: result.items.length }) + '\n\n' + lines.join('\n'));
      return;
    }
    if (result.state) STATE = result.state;
    renderThreadBanner();
    toast(result.fixed ? t('repair.fixed', { count: result.fixed }) : t('repair.nothing'));
  } catch (error) {
    toast(error.message, true);
  }
}

function renderHeader() {
  const current = STATE.current || {};
  $('current-status').textContent = current.model_provider
    ? t('status.current', { provider: current.model_provider, model: current.model || t('status.unset') })
    : t('status.no_config');
  // 优先用服务端给的机器可读代号，按当前语言翻译；拿不到才退回服务端的中文串
  const backendKey = 'keys.' + String(STATE.secret_backend_id || '').replace(/-/g, '_');
  const backend = (STATE.secret_backend_id && I18N[LANG][backendKey])
    ? t(backendKey) : (STATE.secret_backend || '—');
  $('secret-backend').textContent = 'v' + (STATE.version || '?') + ' · '
    + t('topbar.secret', { backend: backend });
  $('about-version').textContent = t('status.version', { version: STATE.version || '?' });
  if (STATE.project_url) $('about-repo').href = STATE.project_url;
  renderUpdate(STATE.update);
}

function renderUpdate(info) {
  const node = $('about-update');
  if (!info) { node.textContent = ''; return; }
  if (info.status !== 'ok') { node.textContent = ''; return; }
  if (info.up_to_date) {
    node.className = 'hint';
    node.textContent = t('update.up_to_date');
  } else {
    node.className = 'hint update-new';
    node.textContent = t('update.available', { latest: info.latest, current: info.current });
  }
}

function renderProviders() {
  const list = $('provider-list');
  list.innerHTML = '';
  const needle = QUERY.trim().toLowerCase();
  const visible = STATE.providers.filter((provider) => {
    if (!needle) return true;
    const haystack = [provider.id, provider.label, ...(provider.models || [])].join(' ').toLowerCase();
    return haystack.includes(needle);
  });
  $('empty-hint').hidden = visible.length > 0;
  for (const provider of visible) {
    const card = el('div', 'provider-card' + (provider.id === SELECTED ? ' active' : ''));
    card.appendChild(el('div', 'provider-avatar', (provider.label || provider.id).slice(0, 1).toUpperCase()));
    const body = el('div', 'provider-body');
    const row = el('div', 'row');
    row.appendChild(el('span', 'name', provider.label || provider.id));
    row.appendChild(el('span', 'dot' + (provider.is_current ? ' on' : '')));
    body.appendChild(row);
    body.appendChild(el('div', 'meta', t('provider.meta', {
      count: provider.models.length,
      hint: provider.has_key ? provider.key_hint : t('provider.no_key'),
    })));
    card.appendChild(body);
    card.onclick = () => { SELECTED = provider.id; renderProviders(); renderDetail(provider); };
    list.appendChild(card);
  }
}

function renderDetail(provider) {
  const models = visibleModels(provider);
  const detail = $('detail');
  detail.innerHTML = '';

  const head = el('div', 'detail-head');
  const info = el('div');
  info.appendChild(el('h2', null, provider.label || provider.id));
  info.appendChild(el('div', 'url',
    `${provider.upstream_base_url || provider.base_url} · ${transportLabel(provider.transport)}`));
  head.appendChild(info);

  const actions = el('div', 'detail-actions');
  const balanceButton = el('button', 'btn ghost', t('btn.balance'));
  balanceButton.onclick = () => queryBalance(provider, balanceButton);
  const refreshButton = el('button', 'btn ghost', t('btn.refresh'));
  refreshButton.onclick = () => refreshModels(provider.id, refreshButton);
  const useButton = el('button', 'btn primary', t('btn.use'));
  useButton.onclick = () => switchTo(provider.id, provider.default_model || provider.models[0]);
  const removeButton = el('button', 'btn danger', t('btn.remove'));
  removeButton.onclick = () => removeProvider(provider);
  actions.append(balanceButton, refreshButton, useButton, removeButton);
  head.appendChild(actions);
  detail.appendChild(head);

  const cards = el('div', 'cards');
  const keyCard = el('div', 'card');
  keyCard.appendChild(el('div', 'label', 'API Key'));
  keyCard.appendChild(el('div', 'value small',
    provider.has_key ? provider.key_hint : t('provider.unconfigured')));
  keyCard.appendChild(el('div', 'note', t('provider.key_note')));
  cards.appendChild(keyCard);

  const modelCard = el('div', 'card');
  modelCard.appendChild(el('div', 'label', t('card.models')));
  modelCard.appendChild(el('div', 'value', String(provider.models.length)));
  // 老记录没有同步时间，但模型是实打实在的，不要显示成“尚未同步”那样的异常状态
  modelCard.appendChild(el('div', 'note', provider.models_synced_at
    ? t('card.synced_at', { time: provider.models_synced_at.replace('T', ' ') })
    : t('card.imported')));
  cards.appendChild(modelCard);

  if (provider.transport === 'native') {
    const nativeCard = el('div', 'card ok');
    nativeCard.appendChild(el('div', 'label', t('card.transport')));
    nativeCard.appendChild(el('div', 'value small', t('card.native')));
    nativeCard.appendChild(el('div', 'note', t('card.native_note')));
    cards.appendChild(nativeCard);
  } else {
    const bridgeCard = el('div', 'card' + (provider.bridge_running ? ' ok' : ' warn'));
    bridgeCard.appendChild(el('div', 'label', t('card.bridge')));
    bridgeCard.appendChild(el('div', 'value small',
      provider.bridge_running ? t('card.running') : t('card.not_running')));
    bridgeCard.appendChild(el('div', 'note',
      provider.bridge_running ? t('card.bridge_note_on') : t('card.bridge_note_off')));
    cards.appendChild(bridgeCard);
  }

  if (provider.usage) {
    const usageCard = el('div', 'card');
    usageCard.appendChild(el('div', 'label', t('card.usage')));
    usageCard.appendChild(el('div', 'value', provider.usage.used_tokens_human || '0'));
    if (provider.usage.percent !== undefined) {
      usageCard.appendChild(el('div', 'note', t('card.quota_line', {
        total: provider.usage.quota_tokens_human,
        percent: provider.usage.percent,
        remain: provider.usage.remaining_tokens_human,
      })));
      const bar = el('div', 'progress' + (provider.usage.percent >= 90 ? ' err' : (provider.usage.percent >= 70 ? ' warn' : '')));
      const fill = el('span');
      fill.style.width = `${Math.max(2, provider.usage.percent)}%`;
      bar.appendChild(fill);
      usageCard.appendChild(bar);
    } else {
      usageCard.appendChild(el('div', 'note', t('card.quota_none')));
    }
    if (provider.local_usage) {
      usageCard.appendChild(el('div', 'note', t('card.turns', {
        sessions: provider.local_usage.sessions, turns: provider.local_usage.turns })));
    }
    const quotaButton = el('button', 'btn ghost small',
      provider.usage.quota_tokens ? t('card.quota_edit') : t('card.quota_set'));
    quotaButton.onclick = () => setQuota(provider);
    usageCard.appendChild(quotaButton);
    cards.appendChild(usageCard);
  } else if (provider.local_usage) {
    const usageCard = el('div', 'card');
    usageCard.appendChild(el('div', 'label', t('card.usage')));
    usageCard.appendChild(el('div', 'value', provider.local_usage.total_tokens_human));
    usageCard.appendChild(el('div', 'note', t('card.turns', {
      sessions: provider.local_usage.sessions, turns: provider.local_usage.turns })));
    cards.appendChild(usageCard);
  }

  if (provider.balance) {
    cards.appendChild(balanceCard(provider.balance));
  } else if (provider.console_url) {
    const card = el('div', 'card');
    card.appendChild(el('div', 'label', t('card.balance')));
    card.appendChild(el('div', 'value small', t('card.check_on_site')));
    const link = el('a', 'note', t('card.open_site'));
    link.href = provider.console_url;
    link.target = '_blank';
    link.rel = 'noreferrer';
    card.appendChild(link);
    cards.appendChild(card);
  }
  // 选中平台后自动查一次额度（每个平台每次打开只自动查一次，避免反复请求）
  if (!provider.balance && !BALANCE_TRIED.has(provider.id)) {
    BALANCE_TRIED.add(provider.id);
    setTimeout(() => queryBalance(provider, null), 80);
  }
  cards.appendChild(contextCard(provider));
  cards.appendChild(effortCard(provider));
  cards.appendChild(capabilityCard(provider));
  detail.appendChild(cards);

  if (provider.notes) {
    detail.appendChild(el('div', 'notice', provider.notes));
  }

  const modelsHead = el('div', 'models-head');
  modelsHead.appendChild(el('h3', null, t('models.title')));
  const addModelButton = el('button', 'btn ghost', t('models.add'));
  addModelButton.onclick = () => addModel(provider.id);
  modelsHead.appendChild(addModelButton);
  detail.appendChild(modelsHead);

  const grid = el('div', 'models');
  if (!provider.models.length) {
    grid.appendChild(el('div', 'hint', t('models.empty')));
  } else if (!models.length) {
    grid.appendChild(el('div', 'hint', t('models.no_match', { query: QUERY })));
  }
  for (const model of models) {
    const item = el('div', 'model' + (provider.is_current && provider.default_model === model ? ' current' : ''));
    const left = el('div');
    left.appendChild(el('span', 'id', model));
    item.appendChild(left);
    const button = el('button', 'btn ghost', t('models.switch'));
    button.onclick = () => switchTo(provider.id, model);
    item.appendChild(button);
    grid.appendChild(item);
  }
  detail.appendChild(grid);

  detail.appendChild(el('div', 'notice', t('switch.notice')));
}

function contextCard(provider) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'label', t('card.context')));
  card.appendChild(el('div', 'note', t('context.hint')));

  const row = el('div', 'context-row');
  const select = el('select');
  const windows = provider.model_windows || {};
  const names = provider.models && provider.models.length ? provider.models : Object.keys(windows);
  for (const name of names) {
    const option = el('option', null, name);
    option.value = name;
    if (name === provider.default_model) option.selected = true;
    select.appendChild(option);
  }
  const input = el('input');
  input.type = 'number';
  input.min = '4096';
  input.max = '10000000';
  input.step = '1024';
  const sync = () => { input.value = windows[select.value] || ''; };
  select.onchange = sync;
  sync();

  const save = el('button', 'btn ghost small', t('context.set'));
  save.onclick = async () => {
    const value = Number(input.value);
    if (!Number.isFinite(value) || value < 4096 || value > 10000000) {
      toast(t('context.invalid'), true);
      return;
    }
    try {
      const result = await api('set_context', {
        provider: provider.id, model: select.value, window: value });
      if (result.error) { toast(result.error, true); return; }
      if (result.state) STATE = result.state;
      renderDetail(STATE.providers.find((p) => p.id === provider.id) || provider);
      toast(t('context.saved', { model: select.value, tokens: String(value) }));
    } catch (error) { toast(error.message, true); }
  };

  const reset = el('button', 'btn ghost small', t('context.reset'));
  reset.onclick = async () => {
    try {
      const result = await api('set_context', {
        provider: provider.id, model: select.value, window: 0 });
      if (result.error) { toast(result.error, true); return; }
      if (result.state) STATE = result.state;
      renderDetail(STATE.providers.find((p) => p.id === provider.id) || provider);
      toast(t('context.reset_done'));
    } catch (error) { toast(error.message, true); }
  };

  row.append(select, input, save, reset);
  card.appendChild(row);
  return card;
}

const EFFORT_LEVELS = ['none', 'low', 'medium', 'high', 'xhigh'];

function effortCard(provider) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'label', t('card.effort')));
  card.appendChild(el('div', 'note', t('effort.hint')));

  const row = el('div', 'context-row');
  const select = el('select');
  const efforts = provider.model_efforts || {};
  const names = provider.models && provider.models.length ? provider.models : Object.keys(efforts);
  for (const name of names) {
    const option = el('option', null, name);
    option.value = name;
    if (name === provider.default_model) option.selected = true;
    select.appendChild(option);
  }

  const levelSelect = el('select');
  const sync = () => {
    const info = efforts[select.value] || {};
    const allowed = (info.levels && info.levels.length) ? info.levels : EFFORT_LEVELS;
    levelSelect.innerHTML = '';
    for (const level of allowed) {
      const option = el('option', null, t('effort.' + level));
      option.value = level;
      levelSelect.appendChild(option);
    }
    const current = info.current && allowed.includes(info.current)
      ? info.current : (allowed.includes('high') ? 'high' : allowed[0]);
    levelSelect.value = current;
  };
  select.onchange = sync;
  sync();

  const apply = el('button', 'btn ghost small', t('effort.set'));
  apply.onclick = async () => {
    try {
      const result = await api('set_effort', {
        provider: provider.id, model: select.value, effort: levelSelect.value });
      if (result.error) { toast(result.error, true); return; }
      if (result.state) STATE = result.state;
      renderDetail(STATE.providers.find((p) => p.id === provider.id) || provider);
      toast(t('effort.saved', { model: select.value, level: t('effort.' + levelSelect.value) }));
    } catch (error) { toast(error.message, true); }
  };

  const reset = el('button', 'btn ghost small', t('effort.reset'));
  reset.onclick = async () => {
    try {
      const result = await api('set_effort', {
        provider: provider.id, model: select.value, effort: '' });
      if (result.error) { toast(result.error, true); return; }
      if (result.state) STATE = result.state;
      renderDetail(STATE.providers.find((p) => p.id === provider.id) || provider);
      toast(t('effort.reset_done'));
    } catch (error) { toast(error.message, true); }
  };

  row.append(select, levelSelect, apply, reset);
  card.appendChild(row);
  return card;
}

function capChip(value) {
  const key = (value === 'yes' || value === 'no' || value === 'unknown') ? value : 'unknown';
  return el('span', 'chip ' + key, t('caps.' + key));
}

function capabilityCard(provider) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'label', t('card.caps')));
  card.appendChild(el('div', 'note', t('caps.hint')));

  const caps = provider.model_capabilities || {};
  const table = el('div', 'caps-table');
  const names = provider.models && provider.models.length ? provider.models : Object.keys(caps);
  for (const name of names) {
    const info = caps[name] || { vision: 'unknown', reasoning: 'unknown', tools: 'unknown' };
    const row = el('div', 'caps-row');
    row.appendChild(el('span', 'name', name));
    row.appendChild(el('span', 'chip', t('caps.vision')));
    row.appendChild(capChip(info.vision));
    row.appendChild(el('span', 'chip', t('caps.reasoning')));
    row.appendChild(capChip(info.reasoning));
    row.appendChild(el('span', 'chip', t('caps.tools')));
    row.appendChild(capChip(info.tools));
    const source = info.source ? t('caps.source_' + info.source) : '';
    if (source) row.appendChild(el('span', 'chip', source));
    table.appendChild(row);
  }
  if (!names.length) table.appendChild(el('div', 'hint', t('models.empty')));
  card.appendChild(table);

  const row = el('div', 'context-row');
  const probe = el('button', 'btn ghost small', t('caps.probe'));
  probe.onclick = () => runProbe(provider, false);
  const probeApply = el('button', 'btn ghost small', t('caps.probe_apply'));
  probeApply.onclick = () => runProbe(provider, true);
  row.append(probe, probeApply);
  card.appendChild(row);
  card.appendChild(el('div', 'note', t('caps.note_openai_only')));
  card.appendChild(el('div', 'note', t('caps.note_generation')));
  return card;
}

async function runProbe(provider, doApply) {
  toast(t('caps.probing'));
  try {
    const result = await api('probe_capabilities', { provider: provider.id, apply: doApply });
    if (result.error) { toast(t('caps.probe_failed', { error: result.error }), true); return; }
    if (result.state) STATE = result.state;
    renderDetail(STATE.providers.find((p) => p.id === provider.id) || provider);
    toast(t('caps.probe_done'));
  } catch (error) { toast(t('caps.probe_failed', { error: error.message }), true); }
}

function balanceCard(balance) {
  const card = el('div', 'card');
  if (balance.status === 'ok') card.classList.add('ok');
  else if (balance.status === 'error') card.classList.add('err');
  else card.classList.add('warn');
  card.appendChild(el('div', 'label', t('card.balance')));
  card.appendChild(el('div', 'value', balance.display || '—'));
  if (balance.message) card.appendChild(el('div', 'note', balance.message));
  for (const field of balance.fields || []) {
    card.appendChild(el('div', 'note', `${field.label}：${field.value}`));
  }
  if (balance.console_url) {
    const link = el('a', 'note', t('card.open_site'));
    link.href = balance.console_url;
    link.target = '_blank';
    link.rel = 'noreferrer';
    card.appendChild(link);
  }
  return card;
}

function transportLabel(transport) {
  if (transport === 'native') return t('transport.native');
  if (transport === 'bridge') return t('transport.bridge');
  return t('transport.auto');
}

function visibleModels(provider) {
  const needle = QUERY.trim().toLowerCase();
  if (!needle) return provider.models;
  const providerMatches = [provider.id, provider.label || ''].join(' ').toLowerCase().includes(needle);
  if (providerMatches) return provider.models;
  return provider.models.filter((model) => model.toLowerCase().includes(needle));
}

async function setQuota(provider) {
  const current = provider.usage && provider.usage.quota_tokens;
  const raw = prompt(t('prompt.quota'), current ? String(current) : '');
  if (raw === null) return;
  const trimmed = raw.trim();
  try {
    const payload = trimmed
      ? { provider: provider.id, tokens: Number(trimmed) }
      : { provider: provider.id, clear: true };
    if (trimmed && (!Number.isFinite(payload.tokens) || payload.tokens <= 0)) {
      toast(t('toast.positive_int'), true);
      return;
    }
    const result = await api('quota', payload);
    STATE = result.state;
    const item = STATE.providers.find((p) => p.id === provider.id);
    if (item) { provider.usage = item.usage; renderDetail(provider); }
    toast(t('toast.quota_updated'));
  } catch (error) {
    toast(error.message, true);
  }
}

async function queryBalance(provider, button) {
  if (button) {
    button.disabled = true;
    button.textContent = t('btn.balancing');
  }
  try {
    const result = await api('balance', { provider: provider.id });
    provider.balance = result;
    renderDetail(provider);
  } catch (error) {
    toast(error.message, true);
    if (button) {
      button.disabled = false;
      button.textContent = t('btn.balance');
    }
  }
}

async function refreshModels(providerId, button) {
  button.disabled = true;
  button.textContent = t('btn.refreshing');
  try {
    const result = await api('refresh', { provider: providerId });
    toast(t('toast.models_updated', { count: result.models.length }));
    STATE = result.state;
    renderHeader(); renderProviders();
    const provider = STATE.providers.find((p) => p.id === providerId);
    if (provider) renderDetail(provider);
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = t('btn.refresh');
  }
}

async function switchTo(provider, model) {
  try {
    const result = await api('switch', { provider, model });
    toast(t('toast.switched', {
      label: result.label || result.provider, model: result.model }));
    STATE = result.state;
    SELECTED = provider;
    renderHeader(); renderProviders();
    const item = STATE.providers.find((p) => p.id === provider);
    if (item) renderDetail(item);
  } catch (error) {
    toast(error.message, true);
  }
}

async function removeProvider(provider) {
  if (!confirm(t('confirm.delete', { label: provider.label || provider.id }))) return;
  try {
    const result = await api('remove', { provider: provider.id });
    toast(t('toast.deleted'));
    STATE = result.state;
    SELECTED = null;
    renderHeader(); renderProviders();
    $('detail').innerHTML = '<div class="placeholder"></div>';
    $('detail').firstChild.textContent = t('detail.pick_provider');
  } catch (error) {
    toast(error.message, true);
  }
}

async function addModel(providerId) {
  const raw = prompt(t('prompt.model'));
  if (!raw) return;
  const models = raw.split(',').map((item) => item.trim()).filter(Boolean);
  if (!models.length) return;
  try {
    const result = await api('add-model', { provider: providerId, models });
    toast(t('toast.models_added', { count: models.length }));
    STATE = result.state;
    renderProviders();
    const provider = STATE.providers.find((p) => p.id === providerId);
    if (provider) renderDetail(provider);
  } catch (error) {
    toast(error.message, true);
  }
}

/* ------------------------------------------------------------------ 手动添加 */

function openManual() {
  $('manual-modal').hidden = false;
  $('form-message').textContent = '';
  fillPresets();
}

function closeManual() { $('manual-modal').hidden = true; }

function fillPresets() {
  const select = $('f-preset');
  const keep = select.value;
  select.innerHTML = '';
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = t('modal.preset_none');
  select.appendChild(placeholder);
  for (const preset of STATE.presets || []) {
    const option = document.createElement('option');
    option.value = preset.id;
    option.textContent = preset.label + (preset.has_balance_api ? t('preset.balance_badge') : '');
    select.appendChild(option);
  }
  select.value = keep;
  select.onchange = () => {
    const preset = (STATE.presets || []).find((item) => item.id === select.value);
    if (!preset) return;
    $('f-label').value = preset.label;
    $('f-base-url').value = preset.base_url || '';
    $('f-transport').value = preset.transport || 'auto';
    $('f-console-url').value = preset.console_url || '';
    $('f-requires-key').checked = preset.requires_key !== false;
    if ((preset.known_models || []).length) $('f-models').value = preset.known_models.join('\n');
  };
}

async function saveManual() {
  const message = $('form-message');
  const payload = {
    preset_id: $('f-preset').value || null,
    label: $('f-label').value.trim(),
    base_url: $('f-base-url').value.trim(),
    models_url: $('f-models-url').value.trim(),
    key: $('f-key').value.trim(),
    transport: $('f-transport').value,
    console_url: $('f-console-url').value.trim(),
    requires_key: $('f-requires-key').checked,
    balance_url: $('f-balance-url').value.trim(),
    balance_path: $('f-balance-path').value.trim(),
    balance_currency: $('f-balance-currency').value.trim(),
    models: $('f-models').value.split('\n').map((item) => item.trim()).filter(Boolean),
  };
  if (!payload.base_url) { message.textContent = t('form.need_base_url'); return; }
  message.textContent = t('form.fetching');
  try {
    const result = await api('add', payload);
    STATE = result.state;
    renderHeader(); renderProviders();
    const provider = STATE.providers.find((p) => p.id === result.provider);
    if (provider) { SELECTED = provider.id; renderProviders(); renderDetail(provider); }
    closeManual();
    $('f-key').value = '';
    if (result.discovery_error) {
      toast(t('toast.saved_discovery_failed', { error: result.discovery_error }), true);
    } else {
      toast(t('toast.saved', { count: result.models.length }));
    }
  } catch (error) {
    message.textContent = error.message;
  }
}

/* ---------------------------------------------------------------------- 绑定 */

$('btn-manual').onclick = openManual;
$('btn-repair-threads').onclick = () => repairThreads(false);
$('btn-thread-preview').onclick = () => repairThreads(true);
$('platform-search').addEventListener('input', (event) => {
  QUERY = event.target.value;
  renderProviders();
  if (SELECTED) {
    const item = STATE.providers.find((p) => p.id === SELECTED);
    if (item) renderDetail(item);
  }
});
$('btn-close-manual').onclick = closeManual;
$('btn-save-manual').onclick = saveManual;
$('btn-refresh-all').onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = t('btn.refreshing_all');
  try {
    await api('refresh', { provider: 'all' });
  } catch (error) {
    /* 单个平台的失败不影响其它平台 */
  }
  await loadState();
  button.disabled = false;
  button.textContent = t('topbar.refresh_all');
  toast(t('toast.all_refreshed'));
};
$('btn-restore').onclick = async () => {
  if (!confirm(t('confirm.restore'))) return;
  try {
    await api('restore', {});
    await loadState();
    toast(t('toast.restored'));
  } catch (error) {
    toast(error.message, true);
  }
};

$('btn-check-update').onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = t('update.checking');
  try {
    const result = await api('update-check', {});
    renderUpdate(result);
    toast(t('update.checked'));
  } catch (error) {
    toast(t('update.failed', { message: error.message }), true);
  }
  button.disabled = false;
  button.textContent = t('update.check');
};

/* ---------------------------------------------------------------- 语言切换 */

function renderFooterHelp() {
  const node = $('footer-help');
  if (!node) return;
  // 这句里有 <code>，所以用 innerHTML；文本全部来自词典，不含用户输入
  node.innerHTML = t('footer.help')
    .split('{doctor}').join('<code>codex-switcher doctor</code>')
    .split('{stop}').join('<code>codex-switcher stop</code>');
}

$('btn-lang').onclick = toggleLang;

window.addEventListener('langchange', () => {
  renderFooterHelp();
  renderHeader();
  renderThreadBanner();
  renderGuardBanner();
  renderProviders();
  if (SELECTED) {
    const item = STATE.providers.find((p) => p.id === SELECTED);
    if (item) renderDetail(item);
  } else {
    $('detail').innerHTML = '<div class="placeholder"></div>';
    $('detail').firstChild.textContent = t('detail.pick_provider');
  }
  // 表单里的预设下拉是动态填充的，语言变了要重建
  fillPresets();
});

applyI18n();
renderFooterHelp();

loadState().catch((error) => toast(error.message, true));
