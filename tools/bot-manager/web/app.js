'use strict';
let token = document.querySelector('meta[name="panel-token"]').content;
const $ = id => document.getElementById(id);
let currentStatus = null;
let currentLogs = [];
let selectedService = 'all';
let paused = false;
let exited = false;
let pollBusy = false;
let toastTimer;
let logSignature = '';
const fmt = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 1});

function text(id, value) { $(id).textContent = value; }
function percent(used, total) { return total ? Math.round(used / total * 100) : 0; }
function bytes(value, decimals = 1) {
  if (value == null) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let n = Math.max(0, value), i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? decimals : 0)} ${units[i]}`;
}
function duration(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), mins = Math.floor(seconds % 3600 / 60);
  return days ? `${days} 天 ${hours} 小时` : hours ? `${hours} 小时 ${mins} 分` : `${mins} 分钟`;
}
function parseTime(value) { return new Date(String(value).replace(/(\.\d{3})\d+/, '$1')); }
function badge(id, label, state = '') { text(id, label); $(id).className = `badge ${state}`; }
function toast(message) { text('toast', message); $('toast').classList.remove('hidden'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.add('hidden'), 3200); }
async function api(path, options = {}) {
  const send = () => fetch(path, {...options, headers: {'X-Panel-Token': token, ...(options.headers || {})}, cache: 'no-store'});
  let response = await send();
  // A restarted launcher creates a new local session. Refresh the session without
  // making an already-open tab remain permanently disconnected.
  if (response.status === 403) {
    const home = await fetch('/', {cache: 'no-store'});
    if (home.ok) {
      const html = await home.text();
      const refreshed = new DOMParser().parseFromString(html, 'text/html').querySelector('meta[name="panel-token"]')?.content;
      if (refreshed && refreshed !== token) { token = refreshed; response = await send(); }
    }
  }
  if (!response.ok) {
    let message = `本地面板请求失败 (${response.status})`;
    try { const detail = await response.json(); if (detail.error) message = detail.error; } catch {}
    throw new Error(message);
  }
  return response.json();
}

function render(status) {
  currentStatus = status;
  renderCloudRecovery(status.cloud_recovery);
  const connection = status.connection;
  const connected = connection.state === 'connected';
  const stale = connection.state === 'stale';
  const snap = status.snapshot;
  const good = connected && !!snap;
  text('sidebarIp', status.host);
  const labels = {connecting: '正在连接服务器', reconnecting: '正在重新连接', connected: 'SSH 隧道已连接', stale: '数据更新延迟'};
  text('connectionText', labels[connection.state] || '连接不可用');
  text('connectionDetail', good ? `${status.user}@${status.host} · 3 个应用入口已就绪` : connection.retry_in > 0 ? `${connection.retry_in} 秒后自动重连 · 已重试 ${connection.reconnect_count || 0} 次` : stale ? '正在检查连接，超时后会自动重连' : '连接恢复后，服务状态和日志会自动更新');
  text('sideState', good ? '隧道连接正常' : labels[connection.state] || '未连接');
  $('sideDot').className = `dot ${good ? 'online' : 'pending'}`;
  $('connectionDot').className = `dot ${good ? 'online' : 'pending'}`;
  $('connectionBar').className = `connection-bar ${good ? 'good' : stale || connection.error ? 'bad' : ''}`;
  if (connection.last_update) {
    const ago = Math.max(0, Math.floor(Date.now() / 1000 - connection.last_update));
    text('lastUpdate', `更新于 ${ago < 2 ? '刚刚' : ago + ' 秒前'}`);
  }
  const errors = [connection.error, ...(snap?.errors || [])].filter(Boolean);
  $('errorBanner').classList.toggle('hidden', errors.length === 0);
  text('errorBanner', errors.join(' · '));
  document.querySelectorAll('[data-app]').forEach(link => {
    const name = link.dataset.app;
    if (status.links[name]) link.href = status.links[name];
    link.classList.toggle('disabled', !good);
    link.setAttribute('aria-disabled', String(!good));
  });
  if (!snap) {
    for (const id of ['astrbotBadge', 'snowlumaBadge', 'novncBadge', 'botBadge']) badge(id, '等待连接');
    text('botAccount', status.known_bot?.uin ? `已部署账号 · ${status.known_bot.uin}` : '尚未获取账号信息');
    text('botConnection', '等待 SSH 连接后检测');
    text('astrbotVersion', status.known_bot?.astrbot_version ? `${status.known_bot.astrbot_version} · 部署记录` : '版本 —');
    text('snowlumaVersion', status.known_bot?.snowluma_version ? `${status.known_bot.snowluma_version} · 部署记录` : '版本 —');
    updateLiveLabel(false);
    return;
  }
  const server = snap.server || {};
  const cpu = server.cpu_percent;
  text('cpuValue', cpu == null ? '—' : fmt.format(cpu));
  text('cpuCores', `${server.cpu_cores || '—'} 核处理器`);
  text('loadValue', `负载 ${server.load ? server.load[0].toFixed(2) : '—'}`);
  const gig = 1024 ** 3;
  text('memoryValue', server.memory_used == null ? '—' : (server.memory_used / gig).toFixed(2));
  text('memoryTotal', `/ ${server.memory_total ? (server.memory_total / gig).toFixed(2) : '—'} GB`);
  const memPercent = percent(server.memory_used, server.memory_total);
  $('memoryBar').value = memPercent;
  text('memoryPercent', `使用率 ${memPercent}%`);
  text('swapValue', `交换 ${bytes(server.swap_used)} / ${bytes(server.swap_total)}`);
  text('diskValue', server.disk_used == null ? '—' : (server.disk_used / gig).toFixed(1));
  text('diskTotal', `/ ${server.disk_total ? (server.disk_total / gig).toFixed(1) : '—'} GB`);
  const diskPercent = percent(server.disk_used, server.disk_total);
  $('diskBar').value = diskPercent;
  text('diskPercent', `使用率 ${diskPercent}%`);
  text('diskFree', `可用 ${bytes(server.disk_total - server.disk_used)}`);
  text('uptimeValue', duration(server.uptime_seconds));
  text('networkRx', good ? `${bytes(server.network?.rx)}/s` : '—');
  text('networkTx', good ? `${bytes(server.network?.tx)}/s` : '—');
  const history = (status.history || []).filter(row => row.cpu != null);
  const points = history.map((row, index) => `${history.length === 1 ? 90 : index / (history.length - 1) * 180},${32 - Math.min(100, row.cpu) / 100 * 29}`).join(' ');
  $('cpuSpark').setAttribute('points', points);
  const services = snap.services || [];
  text('onlineServices', good ? `运行中 ${services.filter(s => s.running).length} / 2` : '状态待更新');
  for (const name of ['astrbot', 'snowluma']) {
    const service = services.find(s => s.name === name) || {};
    const endpoint = snap.endpoints?.[name];
    badge(`${name}Badge`, !good ? '状态待更新' : service.running ? endpoint === false ? '启动中' : '运行中' : service.status === 'unknown' ? '未检测到' : '已停止', !good ? '' : service.running ? endpoint === false ? 'warning' : 'online' : 'offline');
    text(`${name}Version`, service.version ? service.version : '版本 —');
    text(`${name}Memory`, `内存 ${bytes(service.memory_bytes, 0)}`);
  }
  const snowRunning = services.find(s => s.name === 'snowluma')?.running;
  badge('novncBadge', !good ? '状态待更新' : snowRunning && snap.endpoints?.novnc ? '可访问' : snowRunning ? '启动中' : '不可访问', !good ? '' : snowRunning && snap.endpoints?.novnc ? 'online' : 'warning');
  const bot = snap.bot || {};
  const botOnline = good && bot.connected;
  text('botAccount', bot.active_uin ? `QQ · ${bot.active_uin}` : bot.accounts?.length ? `已保存账号 · ${bot.accounts.join('、')}` : '尚未登录 QQ，请打开 QQ 登录桌面扫码');
  text('botTitle', botOnline ? 'QQ 机器人已就绪' : good ? 'QQ 机器人' : 'QQ 机器人 · 状态待更新');
  badge('botBadge', botOnline ? '已连接' : !good ? '状态待更新' : '等待连接', botOnline ? 'online' : good ? 'warning' : '');
  $('botAvatarDot').className = `dot ${botOnline ? 'online' : ''}`;
  text('qqLoginLabel', botOnline ? 'QQ 已登录' : good ? '等待登录' : '状态待更新');
  text('botConnection', botOnline ? 'SnowLuma ↔ AstrBot 连接正常' : !good ? 'SSH 未就绪，暂无法检测' : '尚未建立协议连接');
  text('modelStatus', bot.model_count ? `已配置 ${bot.model_count} 个模型服务` : '尚未配置 · 可前往 AstrBot 添加');
  const astr = services.find(s => s.name === 'astrbot');
  text('botUptime', astr?.running && astr.started_at ? duration((Date.now() - parseTime(astr.started_at).getTime()) / 1000) : '—');
  text('botNote', botOnline ? 'QQ 已登录，机器人协议连接正常。' : good ? '如 QQ 尚未登录，请打开上方的 QQ 登录桌面。' : '以下内容保留最后一次采集结果，恢复连接后自动更新。');
  text('hostname', server.hostname || '—');
  text('osVersion', server.os || '—');
  text('architecture', server.arch ? `${server.arch} · ${server.cpu_cores} 核` : '—');
  text('serverTime', server.time ? parseTime(server.time).toLocaleString('zh-CN', {hour12: false, timeZone: 'Asia/Shanghai'}) : '—');
  updateLiveLabel(good);
}

function updateLiveLabel(connected = currentStatus?.connection.state === 'connected') {
  const el = $('logLive');
  const dot = document.createElement('i'); dot.className = `dot ${!paused && connected ? 'online' : 'pending'}`;
  el.replaceChildren(dot, document.createTextNode(paused ? '已暂停' : connected ? '实时' : '等待连接'));
}

function renderCloudRecovery(state) {
  if (!state) return;
  if (currentStatus) currentStatus.cloud_recovery = state;
  const labels = {unconfigured:'待配置',paused:'已暂停',monitoring:'已启用',waiting_connection:'等待连接',fault_countdown:'故障确认中',checking:'正在复核',rebooting:'正在重启',waiting_boot:'等待恢复',cooldown:'冷却中',daily_limit:'达到上限',error:'检查失败',blocked:'已拦截',ledger_error:'已暂停'};
  const healthy = state.phase === 'monitoring' && state.configured && state.enabled;
  badge('cloudRecoveryBadge', labels[state.phase] || '待检查', healthy ? 'online' : state.phase==='error' || state.phase==='ledger_error' ? 'offline' : 'warning');
  text('cloudRecoveryMessage',state.message);
  text('cloudRecoveryCount',` · 24 小时内已尝试重启 ${state.reboots_24h} 次`);
  $('cloudToggle').disabled = !state.configured;
  $('cloudValidate').disabled = !state.configured;
  text('cloudToggle',state.enabled ? '暂停自动重启' : '开启自动重启');
}

$('cloudConfigure').addEventListener('click',() => {
  $('cloudConfigError').classList.add('hidden'); $('cloudDialog').showModal();
});
$('cloudDialogClose').addEventListener('click',() => { $('cloudDialog').close(); $('cloudKeySecret').value=''; });
$('cloudDialog').addEventListener('close',() => { $('cloudKeySecret').value=''; });
$('cloudCredentialForm').addEventListener('submit',async event => {
  event.preventDefault();
  const keyId=$('cloudKeyId').value.trim(), keySecret=$('cloudKeySecret').value.trim();
  $('cloudSave').disabled=true; text('cloudSave','正在校验…'); $('cloudConfigError').classList.add('hidden');
  try {
    const state=await api('/api/cloud/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({access_key_id:keyId,access_key_secret:keySecret})});
    renderCloudRecovery(state); $('cloudKeyId').value=''; $('cloudKeySecret').value=''; $('cloudDialog').close();
    toast('权限预检通过，自动恢复已配置。没有执行真实重启。'); poll();
  } catch (error) { text('cloudConfigError',error.message); $('cloudConfigError').classList.remove('hidden'); }
  finally { $('cloudSave').disabled=false; text('cloudSave','保存并校验权限'); $('cloudKeySecret').value=''; }
});
$('cloudToggle').addEventListener('click',async () => {
  try { const state=await api('/api/cloud/enabled',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!currentStatus?.cloud_recovery?.enabled})}); renderCloudRecovery(state); poll(); }
  catch (error) { toast(error.message); }
});
$('cloudValidate').addEventListener('click',async () => {
  $('cloudValidate').disabled=true;
  try { const result=await api('/api/cloud/validate',{method:'POST'}); toast(result.message); }
  catch (error) { toast(error.message); }
  finally { poll(); }
});
function filteredLogs() {
  const level = $('levelFilter').value, query = $('logSearch').value.trim().toLowerCase();
  return currentLogs.filter(row => (selectedService === 'all' || row.service === selectedService)
    && (level === 'all' || row.level === level) && (!query || row.message.toLowerCase().includes(query)));
}
function renderLogs(force = false) {
  const rows = filteredLogs();
  const signature = `${rows.length}:${rows.at(-1)?.id}:${rows[0]?.id}:${selectedService}:${$('levelFilter').value}:${$('logSearch').value}`;
  text('logCounter', `${rows.length} 条日志${rows.length !== currentLogs.length ? ` / 共 ${currentLogs.length} 条` : ''}`);
  if (!force && signature === logSignature) return;
  logSignature = signature;
  const consoleEl = $('logConsole'), previousScroll = consoleEl.scrollTop;
  const fragment = document.createDocumentFragment();
  if (!rows.length) {
    const empty = document.createElement('div'); empty.className = 'log-empty';
    empty.textContent = currentLogs.length ? '没有匹配的日志，试试调整筛选条件。' : currentStatus?.snapshot ? '暂时没有新的运行日志。' : '正在等待服务器日志…';
    fragment.appendChild(empty);
  }
  for (const row of rows) {
    const line = document.createElement('div'); line.className = `log-row ${row.level}`;
    const time = document.createElement('span'); time.className = 'log-time';
    const date = parseTime(row.time); time.textContent = Number.isNaN(date.getTime()) ? '--:--:--' : date.toLocaleTimeString('zh-CN', {hour12:false, timeZone:'Asia/Shanghai'}); time.title = row.time;
    const service = document.createElement('span'); service.className = `log-service ${row.service}`; service.textContent = row.service === 'astrbot' ? 'AstrBot' : 'SnowLuma';
    const level = document.createElement('span'); level.className = `log-level ${row.level}`; level.textContent = row.level.toUpperCase();
    const message = document.createElement('span'); message.className = 'log-message'; message.textContent = row.message;
    line.append(time, service, level, message); fragment.appendChild(line);
  }
  consoleEl.replaceChildren(fragment);
  consoleEl.scrollTop = $('autoScroll').checked ? consoleEl.scrollHeight : previousScroll;
}

async function poll() {
  if (pollBusy || exited) return;
  pollBusy = true;
  try {
    const status = await api('/api/status');
    render(status);
    if (!paused) { const response = await api('/api/logs'); currentLogs = response.logs; renderLogs(); }
  } catch (error) {
    if (!exited) {
      text('connectionText', '本地面板连接已断开');
      text('connectionDetail', '启动窗口可能已关闭，请重新双击桌面快捷方式。');
      $('connectionBar').className = 'connection-bar bad';
      $('connectionDot').className = 'dot error';
      $('sideDot').className = 'dot error'; text('sideState', '本地面板已断开');
      for (const id of ['astrbotBadge','snowlumaBadge','novncBadge','botBadge']) badge(id, '状态待更新');
      document.querySelectorAll('[data-app]').forEach(a => a.classList.add('disabled'));
      updateLiveLabel(false);
    }
  } finally { pollBusy = false; }
}

$('reconnectBtn').addEventListener('click', async () => {
  try { await api('/api/reconnect', {method:'POST'}); toast('正在重新建立 SSH 连接…'); setTimeout(poll, 1000); }
  catch (error) { toast(error.message); }
});
$('exitBtn').addEventListener('click', async () => {
  if (!confirm('断开本地 SSH 连接并关闭管理面板？\n\n服务器上的 AstrBot、SnowLuma 和 QQ 会继续运行。')) return;
  try {
    await api('/api/shutdown', {method:'POST'}); exited = true;
    document.body.classList.add('exited');
    text('connectionText', '已安全断开 SSH 连接'); text('connectionDetail', '云端机器人继续运行。下次双击桌面快捷方式即可重新连接。');
    text('lastUpdate', '本地面板已停止'); text('sideState', '已断开');
    $('connectionBar').className = 'connection-bar'; $('sideDot').className = 'dot'; $('connectionDot').className = 'dot';
    for (const id of ['astrbotBadge','snowlumaBadge','novncBadge','botBadge']) badge(id, '未连接');
    $('reconnectBtn').disabled = true; $('exitBtn').disabled = true; updateLiveLabel(false);
    toast('SSH 已断开，启动窗口将自动关闭。');
  } catch (error) { toast(error.message); }
});
$('pauseLogs').addEventListener('click', () => { paused = !paused; text('pauseLogs', paused ? '▷ 继续' : 'Ⅱ 暂停'); updateLiveLabel(); if (!paused) poll(); });
document.querySelectorAll('[data-log-service]').forEach(button => button.addEventListener('click', () => {
  selectedService = button.dataset.logService;
  document.querySelectorAll('[data-log-service]').forEach(b => b.classList.toggle('active', b === button)); renderLogs(true);
}));
$('levelFilter').addEventListener('change', () => renderLogs(true));
$('logSearch').addEventListener('input', () => renderLogs(true));
$('autoScroll').addEventListener('change', () => { if ($('autoScroll').checked) $('logConsole').scrollTop = $('logConsole').scrollHeight; });
$('exportLogs').addEventListener('click', () => {
  const rows = filteredLogs();
  if (!rows.length) return toast('当前没有可导出的日志。');
  const content = rows.map(row => `${row.time} [${row.service}] [${row.level.toUpperCase()}] ${row.message}`).join('\r\n');
  const url = URL.createObjectURL(new Blob(['\ufeff',content], {type:'text/plain;charset=utf-8'}));
  const link = document.createElement('a'); link.href = url; link.download = `bot-logs-${selectedService}-${new Date().toISOString().slice(0,10)}.txt`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 3000);
  toast(`已导出 ${rows.length} 条日志`);
});
$('copyIp').addEventListener('click', async () => { try { await navigator.clipboard.writeText('YOUR_SERVER_IP'); toast('服务器 IP 已复制'); } catch { toast('服务器 IP：YOUR_SERVER_IP'); } });
document.querySelectorAll('.nav-item').forEach(link => link.addEventListener('click', () => { document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('active', item === link)); }));
function clock() { text('clock', new Date().toLocaleTimeString('zh-CN', {hour12:false,timeZone:'Asia/Shanghai'})); }
clock(); setInterval(clock, 1000); poll(); setInterval(poll, 3000);
