/* Executed in the Creaa tab's MAIN world. Uses the site's own authentication,
 * submission tickets and challenge UI; never copies browser credentials out.
 * Contract observed in Creaa image_edit_chat.min.js, 2026-09-08. */
async function creaaPageOperation(operation, input = {}) {
  const fail = (code, message) => { const e = new Error(message); e.code = code; throw e; };
  const ledgerKey = 'flow2api:creaa:attempts:v1';
  const readLedger = () => { try { return JSON.parse(localStorage.getItem(ledgerKey) || '{}'); } catch { return {}; } };
  const writeLedger = (id, value) => {
    const ledger = readLedger(); ledger[id] = {...value, updated_at: Date.now()};
    const entries = Object.entries(ledger).sort((a, b) => b[1].updated_at - a[1].updated_at);
    // Never evict unresolved attempts: they are needed to prevent duplicates.
    const keep = entries.filter(([, v], i) => i < 100 || !v.provider_task_id);
    localStorage.setItem(ledgerKey, JSON.stringify(Object.fromEntries(keep)));
  };
  try {
    if (location.origin !== 'https://creaa.ai') fail('wrong_origin', 'Open https://creaa.ai/image in this profile.');
    const app = window.imageEditChat;
    if (!app || typeof app.generateMediaTask !== 'function' || typeof app.fetchMediaTaskStatus !== 'function') {
      fail('page_not_ready', 'Creaa creation page is not ready, or its website adapter has changed.');
    }
    const account = () => {
      if (!app.isUserAuthenticated?.()) fail('needs_login', 'Sign in to Creaa in this Chrome profile.');
      const id = String(app.getCurrentUnlimitedCacheUserId?.() || '');
      if (!id) fail('needs_login', 'Creaa did not expose a stable account identity.');
      if (input.account_id && input.account_id !== id) fail('account_changed', 'The Creaa account changed. Reconnect the intended account.');
      return id;
    };
    const accountId = account();
    const cleanModel = (m, media_type) => ({
      id: m.model_id, media_type, label: m.display_name || m.model_id,
      capabilities: m.capabilities || [], params: m.params || {}, pricing: m.pricing || {},
      billing_mode: 'check_at_submit'
    });
    if (operation === 'inspect') {
      if (!await app.ensureUnifiedModelConfig({requireVideo: true, forceNetwork: true})) fail('catalog_unavailable', 'Cannot refresh Creaa models.');
      const fresh = await app.refreshUnlimitedConfig({force: true, recomputeUi: false, reason: 'flow2api'});
      account();
      const models = [
        ...(window.IMAGE_MODELS_DATA || []).filter(m => m.is_enabled !== false).map(m => cleanModel(m, 'image')),
        ...(window.VIDEO_MODELS_DATA || []).filter(m => m.is_enabled !== false).map(m => cleanModel(m, 'video')),
      ];
      return {ok: true, account_id: accountId, account_label: `Creaa ${accountId}`, models,
        capabilities: {adapter_version: 1, direct_generation: true, image_references: true,
          video_image_references: true, unlimited_config_fresh: !!fresh,
          unlimited_image_eligibility: fresh ? window.UNLIMITED_IMAGE_ELIGIBILITY || [] : [],
          unlimited_video_eligibility: fresh ? window.UNLIMITED_VIDEO_ELIGIBILITY || [] : []}};
    }
    if (operation === 'recover') {
      const entry = readLedger()[input.job_id];
      if (entry && (entry.attempt_id !== input.attempt_id || entry.account_id !== accountId)) fail('attempt_mismatch', 'Stored attempt belongs to another request.');
      return {ok: true, entry: entry || null};
    }
    if (operation === 'poll') {
      const id = String(input.provider_task_id || '');
      if (!id || !['image', 'video'].includes(input.media_type)) fail('invalid_task', 'Missing task identity.');
      const raw = await app.fetchMediaTaskStatus(encodeURIComponent(id), input.media_type);
      account();
      const data = raw.data || raw;
      const upstream = raw.raw || {};
      const state = String(data.status || raw.status || upstream.status || '').toUpperCase();
      const urls = [];
      const add = v => {
        if (Array.isArray(v)) { v.forEach(add); return; }
        if (v && typeof v === 'object') { add(v.url || v.image_url || v.video_url || v.edited_urls || v.proxy_urls); return; }
        if (typeof v !== 'string') return;
        try { const u = new URL(v, location.origin); if (['https:', 'http:'].includes(u.protocol) && !urls.includes(u.href)) urls.push(u.href); } catch {}
      };
      add(upstream.artifacts); add(data.completed_images); add(data.images); add(data.video_result);
      add(data.video_url); add(data.result?.images); add(data.result?.urls); add(data.result?.image_urls);
      add(data.result?.edited_image_urls); add(data.result?.video_url); add(data.result?.url);
      if (['SUCCEEDED', 'SUCCESS', 'COMPLETED', 'DONE'].includes(state)) {
        if (!urls.length) return {ok: true, state: 'running', progress: 99, error: 'Waiting for original artifact URLs.'};
        return {ok: true, state: 'succeeded', progress: 100, result: {urls, media_type: input.media_type}};
      }
      if (['FAILED', 'FAILURE', 'ERROR', 'CANCELLED', 'CANCELED'].includes(state)) {
        return {ok: true, state: 'failed', error: String(data.error || raw.error || 'Creaa generation failed.')};
      }
      if (raw.success === false) fail('poll_failed', String(raw.error || 'Creaa status request failed.'));
      return {ok: true, state: 'running', progress: Math.max(0, Math.min(99, Number(data.progress || raw.progress || 0)))};
    }
    if (!['prepare', 'submit'].includes(operation)) fail('unknown_operation', 'Unsupported Creaa operation.');
    const req = input.request || {};
    const media = input.media_type;
    const modelId = String(req.model || '').replace(/^creaa\//, '');
    if (!['image', 'video'].includes(media)) fail('invalid_media', 'Choose image or video.');
    if (!await app.ensureUnifiedModelConfig({requireVideo: media === 'video', forceNetwork: true})) fail('catalog_unavailable', 'Cannot refresh model settings.');
    const list = media === 'video' ? window.VIDEO_MODELS_DATA : window.IMAGE_MODELS_DATA;
    const model = (list || []).find(m => m.model_id === modelId && m.is_enabled !== false);
    if (!model) fail('model_unavailable', 'Model is not available in this Creaa account.');
    const p = model.params || {};
    const rawRefs = req.references || [];
    if (!Array.isArray(rawRefs)) fail('invalid_references', 'References must be an array.');
    const refs = rawRefs.map(r => typeof r === 'string' ? r : r?.url || r?.data);
    const requiredCapability = media === 'image' ? (refs.length ? 'image_to_image' : 'text_to_image') : (refs.length ? 'image_to_video' : 'text_to_video');
    if (!(model.capabilities || []).includes(requiredCapability)) fail('unsupported_mode', 'This model does not support the requested workflow.');
    if (typeof req.prompt !== 'string' || !req.prompt.trim() || req.prompt.length > (p.max_prompt_length || 20000)) fail('invalid_prompt', 'Prompt is empty or exceeds model limits.');
    if ((req.n || 1) !== 1) fail('unsupported_count', 'This adapter creates one output per job.');
    if (!Array.isArray(refs) || refs.length > Number(p.max_upload_images || 0)) fail('invalid_references', 'Too many reference images for this model.');
    const ratio = req.aspect_ratio || p.default_aspect_ratio || (media === 'image' ? '1:1' : '9:16');
    const ratios = p.aspect_ratios || (media === 'image' ? window.DEFAULT_RATIO_OPTIONS : []);
    const ratioOptions = (ratios || []).map(x => typeof x === 'string' ? x : x.value || x.ratio);
    if (ratioOptions.length && !ratioOptions.includes(ratio)) fail('unsupported_ratio', 'Aspect ratio is unsupported by the selected model.');
    if (!/^auto$|^\d{1,2}:\d{1,2}$/.test(ratio)) fail('unsupported_ratio', 'Use a supported aspect ratio.');
    const size = String(req.image_size || p.default_image_size || '1k').toLowerCase();
    const quality = String(req.quality || p.default_quality || 'medium').toLowerCase();
    const sizes = p.image_size_options || p.supported_resolutions || [];
    if (media === 'image' && sizes.length && !sizes.map(x => String(x).toLowerCase()).includes(size)) fail('unsupported_size', 'Image size is unsupported.');
    if (media === 'image' && p.quality_options?.length && !p.quality_options.map(x => String(x).toLowerCase()).includes(quality)) fail('unsupported_quality', 'Image quality is unsupported.');
    const duration = Number(req.duration ?? p.default_duration ?? 6);
    let resolution = String(req.resolution || p.default_resolution || '720p');
    if (media === 'video') {
      if (!Number.isInteger(duration) || (p.supported_durations?.length && !p.supported_durations.includes(duration)) ||
          (p.min_duration != null && duration < p.min_duration) || (p.max_duration != null && duration > p.max_duration)) fail('unsupported_duration', 'Video duration is unsupported.');
      const options = (p.supported_resolutions || []).map(String);
      // Prefer an exact dimension matching the ratio where the catalog offers it.
      const dimensions = {'9:16': '720x1280', '16:9': '1280x720', '1:1': '720x720', '4:3': '960x720', '3:4': '720x960', '21:9': '1680x720'};
      if (!req.resolution && options.includes(dimensions[ratio])) resolution = dimensions[ratio];
      const canonical = options.find(x => x.toLowerCase() === resolution.toLowerCase());
      if (options.length && !canonical) fail('unsupported_resolution', 'Video resolution is unsupported.');
      resolution = canonical || resolution;
      if (/^\d+x\d+$/.test(resolution)) {
        const [w, h] = resolution.split('x').map(Number), [rw, rh] = ratio.split(':').map(Number);
        if (Math.abs(w / h - rw / rh) > 0.025) fail('resolution_ratio_mismatch', 'Resolution and aspect ratio disagree.');
      }
    }
    // Only this extension-owned tab is configured. Restore its parameters after each operation.
    const previousParams = app.currentParams;
    const previousMode = app.currentMode;
    app.currentMode = media;
    app.currentParams = {...previousParams, selectedModel: model, videoModel: media === 'video' ? model : null,
      imageModelId: model.model_id, ratio, aspect_ratio: ratio, imageSize: size, imageQuality: quality,
      resolution, duration, fps: p.default_fps || 24, numImages: 1, unlimited: true, videoInputMode: 'omni_reference'};
    try {
      if (!await app.refreshUnlimitedConfig({force: true, recomputeUi: false, reason: 'flow2api_preflight'})) fail('eligibility_unavailable', 'Cannot refresh account billing eligibility.');
      account();
      const unlimited = app.hasUnlimitedAccessForModel(model, media) === true;
      if (req.billing_policy !== 'allow_credits' && !unlimited) fail('not_unlimited', 'This model/settings combination is not currently unlimited on this account.');
      const credits = unlimited ? 0 : Number(media === 'image' ? app.getImageCostForImageParams(size, quality, model) : app.getVideoCostForCurrentParams(model, {referenceDurationSeconds: 0}));
      if (!unlimited && (!Number.isFinite(credits) || credits <= 0 || !Number.isFinite(req.max_credits) || credits > req.max_credits)) fail('credit_limit', 'The estimated credit charge is unknown or exceeds max_credits.');
      const payload = {model_id: modelId, prompt: req.prompt, num_images: 1, aspect_ratio: ratio,
        force_credit_charge: false, session_id: `flow2api-${input.job_id}`};
      if (media === 'image') {
        payload.provider = model.provider; payload.dynamic_model_config = app.getSerializableModelInfo(model);
        payload.image_size = size.toUpperCase();
        if (p.quality_options?.length) payload.quality = quality;
        payload.edit_mode = 'compose';
      } else {
        payload.model = modelId; payload.duration = duration; payload.resolution = resolution;
        payload.fps = p.default_fps || 24;
        payload.model_config = {provider: model.provider, model_id: modelId, display_name: model.display_name, capabilities: model.capabilities};
      }
      if (operation === 'prepare') return {ok: true, billing: {mode: unlimited ? 'unlimited' : 'credits', estimated_credits: credits}, settings: {model: modelId, ratio, size, quality, duration, resolution}};
      const existing = readLedger()[input.job_id];
      if (existing) {
        if (existing.attempt_id !== input.attempt_id || existing.account_id !== accountId) fail('attempt_mismatch', 'A different attempt already exists for this job.');
        return {ok: true, provider_task_id: existing.provider_task_id || null, already_attempted: true};
      }
      // Image uploads do not initiate generation. Local data images follow the site's upload helper.
      const uploaded = [];
      for (const ref of refs) {
        if (typeof ref !== 'string') fail('invalid_reference', 'References must be image URLs or data URLs.');
        if (ref.startsWith('data:image/')) {
          const match = /^data:(image\/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\r\n]+)$/.exec(ref);
          if (!match) fail('invalid_reference', 'Supported data images: PNG, JPEG, WebP.');
          const bytes = Uint8Array.from(atob(match[2]), c => c.charCodeAt(0));
          if (bytes.length > 10 * 1024 * 1024) fail('reference_too_large', 'Reference image exceeds 10 MB.');
          const asset = await app.uploadTempImageAsset(new File([bytes], 'reference.' + match[1].split('/')[1], {type: match[1]}));
          const url = typeof asset === 'string' ? asset : asset?.url || asset?.oss_url;
          if (!url) fail('upload_failed', 'Creaa did not return an uploaded image URL.');
          uploaded.push(url);
        } else {
          const url = new URL(ref);
          if (url.protocol !== 'https:') fail('invalid_reference', 'Use HTTPS image URLs.');
          uploaded.push(url.href);
        }
      }
      if (uploaded.length && media === 'image') payload.input_images = uploaded;
      if (uploaded.length && media === 'video') {
        payload.image_url = uploaded[0]; payload.first_frame_url = uploaded[0];
        payload.image_mode = 'omni_reference';
        if (uploaded.length > 1) payload.reference_images_urls = uploaded.slice(1);
      }
      account();
      if (media === 'video' && !await app.checkVideoGenerationPreflight(payload)) fail('preflight_rejected', 'Creaa rejected video preflight.');
      account();
      writeLedger(input.job_id, {attempt_id: input.attempt_id, account_id: accountId, provider_task_id: null});
      // Calls the exact website generation function, including submit ticket and normal challenge UI.
      // Once invoked, errors are ambiguous; never automatically submit this attempt again.
      try {
        const response = await app.generateMediaTask(media, payload);
        const taskId = response?.task_id || response?.data?.task_id;
        if (taskId) {
          writeLedger(input.job_id, {attempt_id: input.attempt_id, account_id: accountId, provider_task_id: String(taskId)});
          return {ok: true, provider_task_id: String(taskId), billing: {mode: unlimited ? 'unlimited' : 'credits', estimated_credits: credits}};
        }
        return {ok: false, code: 'submit_uncertain', error: String(response?.error || response?.detail || 'No durable task ID returned; inspect Creaa history before resolving.')};
      } catch (e) { return {ok: false, code: 'submit_uncertain', error: String(e.message || e)}; }
    } finally { app.currentParams = previousParams; app.currentMode = previousMode; }
  } catch (e) { return {ok: false, code: e.code || 'adapter_error', error: String(e.message || e)}; }
}
if (typeof module !== 'undefined') module.exports = {creaaPageOperation};
