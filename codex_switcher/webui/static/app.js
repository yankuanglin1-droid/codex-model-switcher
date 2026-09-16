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
  renderProviders();
  if (SELECTED) {
    const still = STATE.providers.find((p) => p.id === SELECTED);
    if (still) renderDetail(still);
    else { SELECTED = null; $('detail').innerHTML = '<div class="placeholder">从左边选一个平台。</div>'; }
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
  $('thread-banner-title').textContent =
    `${info.mismatch} 个旧任务的服务商和模型对不上`;
  $('thread-banner-detail').textContent =
    `在这些任务里换模型会报 “model is not supported”。一键修复后即可正常继续，改动前会自动备份。`;
}

async function repairThreads(dryRun) {
  try {
    const result = await api('repair', dryRun ? { dry_run: true } : {});
    if (dryRun) {
      const lines = (result.items || []).map((item) =>
        `${item.id}  ${item.from} → ${item.to}  （${item.model}）`);
      alert(`预演：将修复 ${result.items.length} 个任务\n\n` + lines.join('\n'));
      return;
    }
    if (result.state) STATE = result.state;
    renderThreadBanner();
    toast(result.fixed ? `已修复 ${result.fixed} 个任务，重启 Codex 后生效` : '没有需要修复的任务');
  } catch (error) {
    toast(error.message, true);
  }
}

function renderHeader() {
  const current = STATE.current || {};
  $('current-status').textContent = current.model_provider
    ? `当前默认：${current.model_provider} · ${current.model || '未设置'}`
    : '还没有检测到 Codex 配置';
  $('secret-backend').textContent = `v${STATE.version || '?'} · 密钥存储：${STATE.secret_backend || '—'}`;
  $('about-version').textContent = `codex（ChatGPT App）多平台模型切换 v${STATE.version || '?'}`;
  if (STATE.project_url) $('about-repo').href = STATE.project_url;
  renderUpdate(STATE.update);
}

function renderUpdate(info) {
  const node = $('about-update');
  if (!info) { node.textContent = ''; return; }
  if (info.status !== 'ok') { node.textContent = ''; return; }
  if (info.up_to_date) {
    node.className = 'hint';
    node.textContent = '已是最新版本';
  } else {
    node.className = 'hint update-new';
    node.textContent = `有新版本 ${info.latest}（当前 v${info.current}）`;
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
    body.appendChild(el('div', 'meta',
      `${provider.models.length} 个模型 · ${provider.has_key ? provider.key_hint : '未配置密钥'}`));
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
  const balanceButton = el('button', 'btn ghost', '查询额度');
  balanceButton.onclick = () => queryBalance(provider, balanceButton);
  const refreshButton = el('button', 'btn ghost', '刷新模型');
  refreshButton.onclick = () => refreshModels(provider.id, refreshButton);
  const useButton = el('button', 'btn primary', '设为默认平台');
  useButton.onclick = () => switchTo(provider.id, provider.default_model || provider.models[0]);
  const removeButton = el('button', 'btn danger', '删除平台');
  removeButton.onclick = () => removeProvider(provider);
  actions.append(balanceButton, refreshButton, useButton, removeButton);
  head.appendChild(actions);
  detail.appendChild(head);

  const cards = el('div', 'cards');
  const keyCard = el('div', 'card');
  keyCard.appendChild(el('div', 'label', 'API Key'));
  keyCard.appendChild(el('div', 'value small', provider.has_key ? provider.key_hint : '未配置'));
  keyCard.appendChild(el('div', 'note', '只存在系统钥匙串，不写进配置文件'));
  cards.appendChild(keyCard);

  const modelCard = el('div', 'card');
  modelCard.appendChild(el('div', 'label', '可用模型'));
  modelCard.appendChild(el('div', 'value', String(provider.models.length)));
  // 老记录没有同步时间，但模型是实打实在的，不要显示成“尚未同步”那样的异常状态
  modelCard.appendChild(el('div', 'note', provider.models_synced_at
    ? `更新于 ${provider.models_synced_at.replace('T', ' ')}`
    : '已从平台导入，可随时点「刷新模型」更新'));
  cards.appendChild(modelCard);

  if (provider.transport === 'native') {
    const nativeCard = el('div', 'card ok');
    nativeCard.appendChild(el('div', 'label', '连接方式'));
    nativeCard.appendChild(el('div', 'value small', '原生 Responses'));
    nativeCard.appendChild(el('div', 'note', '平台自带 Codex 需要的接口，直连最快'));
    cards.appendChild(nativeCard);
  } else {
    const bridgeCard = el('div', 'card' + (provider.bridge_running ? ' ok' : ' warn'));
    bridgeCard.appendChild(el('div', 'label', '本地协议桥'));
    bridgeCard.appendChild(el('div', 'value small', provider.bridge_running ? '运行中' : '未运行'));
    bridgeCard.appendChild(el('div', 'note', provider.bridge_running
      ? 'Codex 通过本机 127.0.0.1:8787 访问该平台'
      : '该平台只支持 Chat Completions，未运行协议桥时请求会失败'));
    cards.appendChild(bridgeCard);
  }

  if (provider.usage) {
    const usageCard = el('div', 'card');
    usageCard.appendChild(el('div', 'label', '本机用量（近似）'));
    usageCard.appendChild(el('div', 'value', provider.usage.used_tokens_human || '0'));
    if (provider.usage.percent !== undefined) {
      usageCard.appendChild(el('div', 'note',
        `套餐额度 ${provider.usage.quota_tokens_human} · 已用 ${provider.usage.percent}% · 约剩 ${provider.usage.remaining_tokens_human}`));
      const bar = el('div', 'progress' + (provider.usage.percent >= 90 ? ' err' : (provider.usage.percent >= 70 ? ' warn' : '')));
      const fill = el('span');
      fill.style.width = `${Math.max(2, provider.usage.percent)}%`;
      bar.appendChild(fill);
      usageCard.appendChild(bar);
    } else {
      usageCard.appendChild(el('div', 'note', '没设套餐额度，只能看已用量；点下面按钮可以补上'));
    }
    if (provider.local_usage) {
      usageCard.appendChild(el('div', 'note',
        `${provider.local_usage.sessions} 个任务 · ${provider.local_usage.turns} 轮对话`));
    }
    const quotaButton = el('button', 'btn ghost small', provider.usage.quota_tokens ? '修改套餐额度' : '设置套餐额度');
    quotaButton.onclick = () => setQuota(provider);
    usageCard.appendChild(quotaButton);
    cards.appendChild(usageCard);
  } else if (provider.local_usage) {
    const usageCard = el('div', 'card');
    usageCard.appendChild(el('div', 'label', '本机用量（近似）'));
    usageCard.appendChild(el('div', 'value', provider.local_usage.total_tokens_human));
    usageCard.appendChild(el('div', 'note', `${provider.local_usage.sessions} 个任务 · ${provider.local_usage.turns} 轮对话`));
    cards.appendChild(usageCard);
  }

  if (provider.balance) {
    cards.appendChild(balanceCard(provider.balance));
  } else if (provider.console_url) {
    const card = el('div', 'card');
    card.appendChild(el('div', 'label', '额度'));
    card.appendChild(el('div', 'value small', '需在官网查看'));
    const link = el('a', 'note', '打开官网 ↗');
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
  detail.appendChild(cards);

  if (provider.notes) {
    detail.appendChild(el('div', 'notice', provider.notes));
  }

  const modelsHead = el('div', 'models-head');
  modelsHead.appendChild(el('h3', null, '模型列表'));
  const addModelButton = el('button', 'btn ghost', '手动加模型');
  addModelButton.onclick = () => addModel(provider.id);
  modelsHead.appendChild(addModelButton);
  detail.appendChild(modelsHead);

  const grid = el('div', 'models');
  if (!provider.models.length) {
    grid.appendChild(el('div', 'hint', '这个平台还没有模型。点「刷新模型」，或用「手动加模型」。'));
  } else if (!models.length) {
    grid.appendChild(el('div', 'hint', `没有匹配「${QUERY}」的模型。`));
  }
  for (const model of models) {
    const item = el('div', 'model' + (provider.is_current && provider.default_model === model ? ' current' : ''));
    const left = el('div');
    left.appendChild(el('span', 'id', model));
    item.appendChild(left);
    const button = el('button', 'btn ghost', '切换');
    button.onclick = () => switchTo(provider.id, model);
    item.appendChild(button);
    grid.appendChild(item);
  }
  detail.appendChild(grid);

  detail.appendChild(el('div', 'notice',
    '切换成功后要完全退出（⌘Q）并重新打开 Codex 才生效。已经存在的旧任务仍绑定原来的平台，不会跟着切换；要在旧对话里继续，请对它使用「分叉」，或新建任务。'));
}

function balanceCard(balance) {
  const card = el('div', 'card');
  if (balance.status === 'ok') card.classList.add('ok');
  else if (balance.status === 'error') card.classList.add('err');
  else card.classList.add('warn');
  card.appendChild(el('div', 'label', '余额 / 额度'));
  card.appendChild(el('div', 'value', balance.display || '—'));
  if (balance.message) card.appendChild(el('div', 'note', balance.message));
  for (const field of balance.fields || []) {
    card.appendChild(el('div', 'note', `${field.label}：${field.value}`));
  }
  if (balance.console_url) {
    const link = el('a', 'note', '打开官网 ↗');
    link.href = balance.console_url;
    link.target = '_blank';
    link.rel = 'noreferrer';
    card.appendChild(link);
  }
  return card;
}

function transportLabel(transport) {
  if (transport === 'native') return '原生 Responses 直连';
  if (transport === 'bridge') return '本地协议桥';
  return '自动探测';
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
  const raw = prompt(
    `输入这个平台套餐的总 token 数（例如 500000000）。\n留空表示清除设置。\n\n` +
    `这个数字由你自己填，工具只用它来算本机用量占比，不会去猜。`,
    current ? String(current) : '');
  if (raw === null) return;
  const trimmed = raw.trim();
  try {
    const payload = trimmed
      ? { provider: provider.id, tokens: Number(trimmed) }
      : { provider: provider.id, clear: true };
    if (trimmed && (!Number.isFinite(payload.tokens) || payload.tokens <= 0)) {
      toast('请输入正整数', true);
      return;
    }
    const result = await api('quota', payload);
    STATE = result.state;
    const item = STATE.providers.find((p) => p.id === provider.id);
    if (item) { provider.usage = item.usage; renderDetail(provider); }
    toast('已更新额度设置');
  } catch (error) {
    toast(error.message, true);
  }
}

async function queryBalance(provider, button) {
  if (button) {
    button.disabled = true;
    button.textContent = '查询中…';
  }
  try {
    const result = await api('balance', { provider: provider.id });
    provider.balance = result;
    renderDetail(provider);
  } catch (error) {
    toast(error.message, true);
    if (button) {
      button.disabled = false;
      button.textContent = '查询额度';
    }
  }
}

async function refreshModels(providerId, button) {
  button.disabled = true;
  button.textContent = '更新中…';
  try {
    const result = await api('refresh', { provider: providerId });
    toast(`已更新 ${result.models.length} 个模型`);
    STATE = result.state;
    renderHeader(); renderProviders();
    const provider = STATE.providers.find((p) => p.id === providerId);
    if (provider) renderDetail(provider);
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = '刷新模型';
  }
}

async function switchTo(provider, model) {
  try {
    const result = await api('switch', { provider, model });
    toast(`已切换为 ${result.label || result.provider} · ${result.model}`);
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
  if (!confirm(`确定删除「${provider.label || provider.id}」？\n会同时移除配置并删除钥匙串里的密钥。`)) return;
  try {
    const result = await api('remove', { provider: provider.id });
    toast('已删除');
    STATE = result.state;
    SELECTED = null;
    renderHeader(); renderProviders();
    $('detail').innerHTML = '<div class="placeholder">从左边选一个平台。</div>';
  } catch (error) {
    toast(error.message, true);
  }
}

async function addModel(providerId) {
  const raw = prompt('输入模型名，多个用逗号分隔：');
  if (!raw) return;
  const models = raw.split(',').map((item) => item.trim()).filter(Boolean);
  if (!models.length) return;
  try {
    const result = await api('add-model', { provider: providerId, models });
    toast(`已加入 ${models.length} 个模型`);
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
  if (select.options.length > 1) return;
  for (const preset of STATE.presets || []) {
    const option = document.createElement('option');
    option.value = preset.id;
    option.textContent = `${preset.label}${preset.has_balance_api ? '（支持额度查询）' : ''}`;
    select.appendChild(option);
  }
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
  if (!payload.base_url) { message.textContent = '请填写 Base URL。'; return; }
  message.textContent = '正在拉取模型…';
  try {
    const result = await api('add', payload);
    STATE = result.state;
    renderHeader(); renderProviders();
    const provider = STATE.providers.find((p) => p.id === result.provider);
    if (provider) { SELECTED = provider.id; renderProviders(); renderDetail(provider); }
    closeManual();
    $('f-key').value = '';
    if (result.discovery_error) {
      toast(`平台已保存，但自动获取模型失败：${result.discovery_error}`, true);
    } else {
      toast(`已接入 ${result.models.length} 个模型`);
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
  button.textContent = '刷新中…';
  try {
    await api('refresh', { provider: 'all' });
  } catch (error) {
    /* 单个平台的失败不影响其它平台 */
  }
  await loadState();
  button.disabled = false;
  button.textContent = '全部刷新模型';
  toast('模型列表已刷新');
};
$('btn-restore').onclick = async () => {
  if (!confirm('恢复成官方 OpenAI 登录？\n第三方配置会保留，随时可以再切回去。')) return;
  try {
    await api('restore', {});
    await loadState();
    toast('已恢复官方 OpenAI，重启 Codex 后生效');
  } catch (error) {
    toast(error.message, true);
  }
};

$('btn-check-update').onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = '检查中…';
  try {
    const result = await api('update-check', {});
    renderUpdate(result);
    toast(result.description || '已检查');
  } catch (error) {
    toast('检查更新失败：' + error.message, true);
  }
  button.disabled = false;
  button.textContent = '检查更新';
};

loadState().catch((error) => toast(error.message, true));
