/* Optional Creaa module. Independent socket/tab/state: Flow workers are unchanged. */
importScripts('creaa-page.js');
const CreaaWorker = (() => {
  const ALARM = 'flow2api_creaa_keepalive';
  const TAB_URL = 'https://creaa.ai/image?flow2api_worker=1';
  let socket = null, connecting = false, enabled = false, timer = null, connectTimer = null;
  let tabPromise = null, accountId = null, pageQueue = Promise.resolve();
  const active = new Map(), acks = new Map();
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  function stoppedError(code = 'stale_attempt') { const e = new Error('Attempt superseded or connection replaced.'); e.code = code; return e; }
  function assertCurrent(job) {
    const slot = active.get(job.id);
    if (!slot || slot.cancelled || slot.attempt_id !== job.attempt_id) throw stoppedError();
  }
  function abandonConnection() {
    clearInterval(timer); timer = null;
    for (const slot of active.values()) slot.cancelled = true;
    active.clear(); state.active_jobs = 0;
    for (const item of acks.values()) item.reject(stoppedError('connection_lost'));
    acks.clear();
  }
  const state = {connected: false, account_id: null, error: null, active_jobs: 0};
  async function config() {
    const [flow, settings] = await Promise.all([getSettings(), chrome.storage.local.get(['creaaEnabled', 'creaaServerBase', 'creaaApiKey', 'creaaDeviceId'])]);
    let deviceId = settings.creaaDeviceId;
    if (!deviceId) { deviceId = 'creaa-' + crypto.randomUUID(); await chrome.storage.local.set({creaaDeviceId: deviceId}); }
    return {enabled: settings.creaaEnabled === true, server: settings.creaaServerBase || flow.serverBase,
      key: settings.creaaApiKey || flow.apiKey, deviceId};
  }
  async function readJob(id) { return (await chrome.storage.local.get('creaaJob:' + id))['creaaJob:' + id]; }
  async function saveJob(id, patch) {
    // The backend owns the full request. Persist only recovery metadata in Chrome;
    // base64 references would exhaust chrome.storage.local and are not needed to poll.
    if (patch.job) patch = {...patch, job: {...patch.job, request: {model: patch.job.request?.model}}};
    const data = {...(await readJob(id) || {}), ...patch, updated_at: Date.now()};
    await chrome.storage.local.set({['creaaJob:' + id]: data}); return data;
  }
  async function ensureTab() {
    if (tabPromise) return tabPromise;
    tabPromise = (async () => {
      const {creaaTabId} = await chrome.storage.local.get('creaaTabId');
      let tab;
      if (creaaTabId) { try { tab = await chrome.tabs.get(creaaTabId); } catch {} }
      if (!tab || !tab.url?.startsWith('https://creaa.ai/')) {
        const matches = await chrome.tabs.query({url: 'https://creaa.ai/*'});
        tab = matches.find(t => t.url?.includes('flow2api_worker=1'));
        if (!tab) tab = await chrome.tabs.create({url: TAB_URL, active: false});
        await chrome.storage.local.set({creaaTabId: tab.id});
      }
      if (tab.discarded) await chrome.tabs.reload(tab.id);
      for (let i = 0; i < 40; i++) {
        tab = await chrome.tabs.get(tab.id);
        if (tab.status === 'complete') return tab.id;
        await sleep(500);
      }
      throw new Error('Creaa page did not finish loading.');
    })();
    try { return await tabPromise; } finally { tabPromise = null; }
  }
  async function rawPage(operation, args = {}) {
    const tabId = await ensureTab();
    const results = await chrome.scripting.executeScript({target: {tabId}, world: 'MAIN', func: creaaPageOperation, args: [operation, args]});
    const result = results?.[0]?.result;
    if (!result) throw new Error('Creaa page returned no response.');
    return result;
  }
  function page(operation, args = {}) {
    const result = pageQueue.then(() => rawPage(operation, args));
    pageQueue = result.catch(() => {});
    return result;
  }
  function send(data) {
    if (!socket || socket.readyState !== WebSocket.OPEN) throw new Error('Creaa bridge disconnected.');
    socket.send(JSON.stringify(data));
  }
  async function event(job, payload) {
    assertCurrent(job);
    const message = {type: 'job_event', job_id: job.id, attempt_id: job.attempt_id, ...payload};
    await saveJob(job.id, {job, pending: message, state: payload.state, provider_task_id: payload.provider_task_id || job.provider_task_id || null});
    assertCurrent(job);
    const key = `${job.id}:${job.attempt_id}:${payload.state}`;
    const wait = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { acks.delete(key); reject(new Error('Creaa event acknowledgement timed out.')); }, 15000);
      acks.set(key, {resolve: () => { clearTimeout(timeout); resolve(); }, reject: e => { clearTimeout(timeout); reject(e); }});
    });
    try { send(message); } catch (e) { acks.get(key)?.reject(e); acks.delete(key); }
    try { await wait; }
    catch (e) {
      if (e.obsolete) {
        const stored = await readJob(job.id);
        if (stored?.job?.attempt_id === job.attempt_id) await saveJob(job.id, {pending: null});
      }
      throw e;
    }
    assertCurrent(job);
    await saveJob(job.id, {pending: null});
  }
  async function run(job, resume) {
    const prior = active.get(job.id);
    if (prior?.attempt_id === job.attempt_id && !prior.cancelled) return;
    if (prior) prior.cancelled = true;
    const slot = {attempt_id: job.attempt_id, cancelled: false};
    active.set(job.id, slot); state.active_jobs = active.size;
    const providerKnownByServer = Boolean(job.provider_task_id);
    const task = (async () => {
      const args = () => ({account_id: job.account_id, job_id: job.id, attempt_id: job.attempt_id,
        media_type: job.media_type, request: job.request, provider_task_id: job.provider_task_id});
      try {
        const local = await readJob(job.id);
        assertCurrent(job);
        if (local?.pending && local.pending.state !== 'submitting' && local.job?.attempt_id === job.attempt_id) {
          await event(job, {...local.pending});
          if (['succeeded', 'failed', 'needs_review', 'needs_login'].includes(local.pending.state)) return;
        }
        if (local?.provider_task_id && local.job?.attempt_id === job.attempt_id) job.provider_task_id = local.provider_task_id;
        if (!job.provider_task_id) {
          const recovered = await page('recover', args());
          if (!recovered.ok) throw new Error(recovered.error);
          if (recovered.entry?.provider_task_id) job.provider_task_id = recovered.entry.provider_task_id;
          else if (recovered.entry || (local?.attempted && local.job?.attempt_id === job.attempt_id) || resume) {
            await event(job, {state: 'needs_review', error: 'A previous submit may have reached Creaa. Check website history; this job was not resubmitted.'}); return;
          }
        }
        if (!job.provider_task_id) {
          const prepared = await page('prepare', args());
          if (!prepared.ok) { await event(job, {state: prepared.code === 'needs_login' ? 'needs_login' : 'failed', error: prepared.error}); return; }
          // Durable intent and server acknowledgement BOTH precede the website action.
          await saveJob(job.id, {job, attempted: true, state: 'submitting'});
          await event(job, {state: 'submitting'});
          if (!(await config()).enabled) { await event(job, {state: 'failed', error: 'Creaa worker paused before submission.'}); return; }
          assertCurrent(job);
          const submitted = await page('submit', args());
          assertCurrent(job);
          if (!submitted.ok || !submitted.provider_task_id) {
            const uncertain = submitted.code === 'submit_uncertain' || submitted.already_attempted;
            await event(job, {state: uncertain ? 'needs_review' : submitted.code === 'needs_login' ? 'needs_login' : 'failed', error: submitted.error || 'Previous attempt has no recoverable task ID.'}); return;
          }
          job.provider_task_id = submitted.provider_task_id;
        }
        if (!providerKnownByServer) await event(job, {state: 'submitted', provider_task_id: job.provider_task_id});
        const deadline = Date.now() + 45 * 60 * 1000;
        let consecutiveErrors = 0;
        while (Date.now() < deadline) {
          if (!socket || socket.readyState !== WebSocket.OPEN) throw new Error('Disconnected; task will resume on reconnect.');
          assertCurrent(job);
          let status;
          try { status = await page('poll', args()); }
          catch (e) { status = {ok: false, code: 'poll_transport_error', error: String(e.message || e)}; }
          assertCurrent(job);
          if (!status.ok) {
            if (status.code === 'needs_login' || status.code === 'account_changed') {
              await event(job, {state: 'needs_login', provider_task_id: job.provider_task_id, error: status.error}); return;
            }
            if (++consecutiveErrors >= 6) throw new Error(status.error);
          } else {
            consecutiveErrors = 0;
            await event(job, {state: status.state, provider_task_id: job.provider_task_id, progress: status.progress,
              ...(status.result ? {result: status.result} : {}), ...(status.error ? {error: status.error} : {})});
            if (['succeeded', 'failed'].includes(status.state)) return;
          }
          await sleep(Math.min(30000, 5000 * (consecutiveErrors + 1)));
        }
        await event(job, {state: 'needs_review', provider_task_id: job.provider_task_id, error: 'Tracking timed out after 45 minutes; inspect or resume this task, do not regenerate.'});
      } catch (e) {
        if (e.obsolete || ['stale_attempt', 'connection_lost'].includes(e.code) || slot.cancelled) return;
        state.error = String(e.message || e);
        // Leave durable intent/task identity intact. If connected, hold the account for review.
        if (socket?.readyState === WebSocket.OPEN) {
          try { await event(job, {state: 'needs_review', provider_task_id: job.provider_task_id || null, error: state.error}); } catch {}
        }
      }
    })();
    slot.promise = task;
    try { await task; } finally { if (active.get(job.id) === slot) active.delete(job.id); state.active_jobs = active.size; }
  }
  async function register() {
    const cfg = await config();
    let info;
    for (let i = 0; i < 20; i++) {
      info = await page('inspect');
      if (info.ok || info.code !== 'page_not_ready') break;
      await sleep(500);
    }
    if (!info.ok) throw new Error(info.error);
    accountId = info.account_id; state.account_id = accountId;
    send({type: 'register', device_id: cfg.deviceId, account_id: accountId,
      account_label: info.account_label, models: info.models, capabilities: info.capabilities});
  }
  async function flushPending() {
    const all = await chrome.storage.local.get(null);
    for (const [key, value] of Object.entries(all)) {
      if (!key.startsWith('creaaJob:') || !value?.pending || value.job?.account_id !== accountId || active.has(value.job.id)) continue;
      run(value.job, true).catch(e => { state.error = String(e.message); });
    }
  }
  async function connect() {
    if (connecting || (socket && socket.readyState !== WebSocket.CLOSED && socket.readyState !== 3)) return;
    const cfg = await config(); enabled = cfg.enabled;
    if (!enabled) return;
    connecting = true;
    try {
      const url = new URL('/creaa_ws', cfg.server);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      url.searchParams.set('key', cfg.key);
      const ws = new WebSocket(url.href); socket = ws;
      ws.onopen = async () => {
        if (socket !== ws) return;
        connecting = false;
        timer = setInterval(() => { try { send({type: 'ping'}); } catch {} }, 15000);
        try { await register(); } catch (e) { state.error = String(e.message); ws.close(); }
      };
      ws.onmessage = e => {
        if (socket !== ws) return;
        let data; try { data = JSON.parse(e.data); } catch { return; }
        if (data.type === 'register_ack') {
          state.connected = true; state.error = null; flushPending().catch(() => {});
          for (const pending of data.pending_attempts || []) {
            readJob(pending.job_id).then(local => {
              if (local?.job?.attempt_id === pending.attempt_id) run(local.job, true).catch(() => {});
            });
          }
        }
        if (data.type === 'event_ack') {
          const key = `${data.job_id}:${data.attempt_id}:${data.state}`;
          if ((data.error || data.accepted === false) && data.job_state !== data.state) {
            const error = new Error(data.error || 'Event rejected.');
            error.obsolete = ['attempt_mismatch', 'terminal_state', 'unknown_job', 'device_mismatch'].includes(data.error);
            acks.get(key)?.reject(error);
          }
          else acks.get(key)?.resolve();
          acks.delete(key);
        }
        if (data.type === 'error') state.error = String(data.error || data.message || 'Creaa bridge rejected the request.');
        if (['execute', 'resume'].includes(data.type) && data.job?.account_id === accountId) run(data.job, data.type === 'resume').catch(() => {});
      };
      ws.onclose = () => {
        if (socket !== ws) return;
        abandonConnection(); socket = null; connecting = false; state.connected = false;
        clearTimeout(connectTimer);
        connectTimer = setTimeout(() => connect().catch(() => {}), 10000);
      };
      ws.onerror = () => ws.close();
    } catch (e) { connecting = false; state.error = String(e.message); }
  }
  async function restart() {
    clearTimeout(connectTimer);
    const previous = socket;
    abandonConnection();
    socket = null; connecting = false; state.connected = false;
    if (previous) previous.close();
    await connect();
  }
  chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM) connect().catch(() => {}); });
  chrome.runtime.onMessage.addListener((req, sender, reply) => {
    if (sender.id !== chrome.runtime.id) return;
    if (req.action === 'creaaDefaults') { config().then(c => reply({server: c.server, key: c.key})); return true; }
    if (req.action === 'creaaStatus') { reply({...state, enabled, active_jobs: active.size}); return; }
    if (req.action === 'creaaReconnect') { restart().then(() => reply({ok: true})).catch(e => reply({error: e.message})); return true; }
    if (req.action === 'creaaOpenTab') { ensureTab().then(id => chrome.tabs.update(id, {active: true})).then(() => reply({ok: true})); return true; }
  });
  chrome.alarms.create(ALARM, {periodInMinutes: 1});
  connect().catch(e => { state.error = String(e.message); });
  return {connect, restart};
})();
