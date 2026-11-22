"use strict";

// Virtual mixer and Gazelle Reverb UI.

// ---- compact virtual mixer ---------------------------------------------
function loadMixerSources() {
  const key = 'mixerSources:' + (UI_FEATURES.profile || 'default');
  try { MIXER_SOURCES = JSON.parse(localStorage.getItem(key) || '{}') || {}; }
  catch (_) { MIXER_SOURCES = {}; }
}
function saveMixerSources() {
  const key = 'mixerSources:' + (UI_FEATURES.profile || 'default');
  try { localStorage.setItem(key, JSON.stringify(MIXER_SOURCES)); } catch (_) {}
}
function mixerSourceFeature(mix) {
  const spec = UI_FEATURES?.mixer_sources;
  return spec?.enabled && (spec.mixes || []).includes(mix) ? spec : null;
}
function mixerSourceKey(spec, mix, ch) {
  let sawRoute = false;
  for (const dest of (spec.routing_destinations || [])) {
    const route = ROUTING?.current?.[String(dest)]?.[ch];
    if (!route) continue;
    sawRoute = true;
    const option = (spec.options || []).find(o =>
      Number(o.bank) === Number(route.bank) && Number(o.index) === Number(route.idx));
    if (option) return option.key;
    const raw = rawSourceKey(route);
    if (raw) return raw;
  }
  // A route record that exists but contains an unknown/unconfirmed source
  // must not be replaced with a stale browser-local selection.  It is returned
  // as a disabled option so the current hardware value is visible instead of
  // looking like an unassigned input.
  return sawRoute ? '' : (MIXER_SOURCES[`${mix}:${ch}`] || '');
}
function mixerSourceOptions(spec, mix, ch) {
  const selected = mixerSourceKey(spec, mix, ch);
  const unknown = selected.startsWith('raw:')
    ? `<option value="${selected}" selected disabled>${rawSourceLabel(selected)}</option>` : '';
  const options = (spec.options || []).map(option =>
    `<option value="${option.key}"${option.key === selected ? ' selected' : ''}${option.selectable === false ? ' disabled' : ''}>${option.label}</option>`).join('');
  return `<option value="">INPUT SOURCE</option>${unknown}${options}`;
}

const AV_PARAM_META = [
  ['color', 'COLOR'],
  ['pre_delay', 'PRE-DELAY'],
  ['early_reflection_gain', 'EARLY REF GAIN'],
  ['late_reflection_delay', 'LATE REF DELAY'],
  ['richness', 'RICHNESS'],
  ['reverb_time', 'REVERB TIME'],
  ['room_size', 'ROOM SIZE'],
  ['reverb_level', 'OUTPUT LEVEL'],
];
const AV_DISPLAY_NAME = 'Gazelle Reverb';
const avCommand = () => PROFILE?.frame?.auraverb_command || {};
function avDefaultParams() { return avCommand().defaults || {}; }
function avRemote() {
  const mix = Number(UI_FEATURES?.auraverb?.mix || 0);
  return AURAVERB?.current?.[mix] || null;
}
function avCurrent() {
  const remote = avRemote(), local = AURAVERB_LOCAL;
  if (!remote && !local) return null;
  return {
    ...(remote || {}),
    params: {...avDefaultParams(), ...(remote?.params || {}), ...(local?.params || {})},
    enabled: local?.enabled ?? remote?.enabled ?? false,
  };
}
function avRemember(patch) {
  const local = AURAVERB_LOCAL || (AURAVERB_LOCAL = {params:{}, enabled:null});
  if (patch.params) Object.assign(local.params, patch.params);
  if ('enabled' in patch) local.enabled = patch.enabled;
}
function avForgetAcknowledged() {
  const remote = avRemote(), local = AURAVERB_LOCAL;
  if (!remote || !local) return;
  Object.entries(local.params).forEach(([key, value]) => {
    if (remote.params?.[key] === value) delete local.params[key];
  });
  if (local.enabled != null && remote.enabled === local.enabled) local.enabled = null;
  if (!Object.keys(local.params).length && local.enabled == null) AURAVERB_LOCAL = null;
}
function paintAuraKnob(input) {
  if (!input) return;
  const lo = +input.min, hi = +input.max, value = +input.value;
  const fraction = hi === lo ? 0 : Math.max(0, Math.min(1, (value - lo) / (hi - lo)));
  const knob = input.closest('.av-knob');
  knob?.style.setProperty('--av-angle', (fraction * 270).toFixed(1) + 'deg');
  knob?.querySelector('i')?.style.setProperty(
    'transform', `translateX(-50%) rotate(${(-135 + fraction * 270).toFixed(1)}deg)`);
}
function refreshAuraVerb() {
  const panel = $('#auraverbpanel');
  if (!panel || !UI_FEATURES?.auraverb?.enabled) return;
  avForgetAcknowledged();
  const state = avCurrent(), params = state?.params || avDefaultParams();
  panel.querySelectorAll('[data-av-param]').forEach(input => {
    const key = input.dataset.avParam;
    const value = Number(params[key] ?? 0);
    if (document.activeElement !== input) input.value = value;
    panel.querySelector(`[data-av-value="${key}"]`).textContent = value;
    paintAuraKnob(input);
  });
  const enabled = state?.enabled === true;
  const toggle = panel.querySelector('[data-av-enable]');
  if (toggle) {
    toggle.textContent = enabled ? 'ON' : 'OFF';
    toggle.classList.toggle('on', enabled);
  }
  const status = panel.querySelector('[data-av-status]');
  if (status) status.textContent = state ? (enabled ? 'FX ON' : 'FX OFF') : 'WAITING FOR READBACK';
  refreshReverbReturns();
}
// Reverb returns: level and mute share one device byte, so every write carries both.
// The shown pair is the readback (GET /api/auraverb `returns`) or the last one sent until it is read back.
function reverbReturn(bus) {
  const dev = AURAVERB?.returns?.find(r => r.bus === bus);
  const shown = settingShown('return:' + bus, dev && JSON.stringify({level: dev.level, mute: dev.mute}), readbackHold());
  return shown ? JSON.parse(shown) : null;
}
function sendReverbReturn(bus, patch) {
  const cur = reverbReturn(bus);
  if (!cur) return;                 // unknown pair: never write a guessed mute or level
  const next = {...cur, ...patch};
  settingSent('return:' + bus, JSON.stringify(next));
  post('/api/reverb-return', {bus, ...next});
  refreshReverbReturns();
}
function refreshReverbReturns() {
  const panel = $('#auraverbpanel');
  if (!panel || panel.hidden) return;
  panel.querySelectorAll('[data-av-return]').forEach(input => {
    const bus = +input.dataset.avReturn, r = reverbReturn(bus);
    const mute = panel.querySelector(`[data-av-return-mute="${bus}"]`);
    input.disabled = mute.disabled = !r;
    if (document.activeElement !== input) {
      if (r) input.value = -r.level;
      panel.querySelector(`[data-av-return-value="${bus}"]`).textContent = r ? mixerFaderLabel(r.level) : '–';   // raw dB attenuation; 96 is not documented as off
      paintAuraKnob(input);
    }
    mute.textContent = r ? (r.mute ? 'ON' : 'OFF') : '–';
    mute.classList.toggle('on', !!r?.mute);
    mute.setAttribute('aria-pressed', String(!!r?.mute));
  });
}
function initAuraVerbPanel() {
  const panel = $('#auraverbpanel');
  if (!panel || !UI_FEATURES?.auraverb?.enabled) { if (panel) panel.hidden = true; return; }
  if (!panel.children.length) {
    const [lo, hi] = AURAVERB?.range || avCommand().param_range || [0, 100];
    const span = key => AURAVERB?.ranges?.[key] || [lo, hi];   // Zen Go: delays 0..150, level 1..100
    const rr = PROFILE?.params?.reverb_return;
    panel.innerHTML = `<div class="av-hd"><span class="av-logo">Gazelle <span>Reverb</span></span>
      <span class="av-sub">MIX 1 REVERB</span><button class="modal-x" data-av-close aria-label="close">&times;</button></div>
      <div class="av-body"><div class="av-controls">${AV_PARAM_META.map(([key, label]) =>
        `<div class="av-control"><span class="av-value" data-av-value="${key}">–</span>
          <div class="av-knob"><i></i><input type="range" data-av-param="${key}" min="${span(key)[0]}" max="${span(key)[1]}" value="${span(key)[0]}" aria-label="${AV_DISPLAY_NAME} ${label.toLowerCase()}"></div>
          <span class="av-label">${label}</span></div>`).join('')}</div>
        ${rr ? `<div class="av-controls">${Object.entries(rr.targets).map(([bus, label]) =>
          `<div class="av-control"><span class="av-value" data-av-return-value="${bus}">–</span>
            <div class="av-knob"><i></i><input type="range" data-av-return="${bus}" min="${-rr.range[1]}" max="${-rr.range[0]}" value="${-rr.range[1]}" disabled aria-label="${AV_DISPLAY_NAME} ${label} level"></div>
            <span class="av-label">${label}</span>
            <span class="av-toggle">MUTE <button type="button" data-av-return-mute="${bus}" disabled aria-label="mute ${AV_DISPLAY_NAME} ${label}" aria-pressed="false">–</button></span></div>`).join('')}</div>` : ''}
        <div class="av-center"><span data-av-status>WAITING FOR READBACK</span><span class="av-screen">ROOM_SIZE</span>
          <span class="av-toggle">FX <button type="button" data-av-enable>OFF</button></span></div>
        <p class="av-note">${AV_DISPLAY_NAME} is the bundled Mix 1 effect. Each change sends the complete read-modify-write state.</p>
      </div>`;
    const hd = panel.querySelector('.av-hd');
    let dragging = false, dx = 0, dy = 0, pid = null;
    hd.addEventListener('pointerdown', e => {
      if (e.target.closest('.modal-x')) return;
      const r = panel.getBoundingClientRect();
      dragging = true; dx = e.clientX - r.left; dy = e.clientY - r.top; pid = e.pointerId;
      panel.style.transform = 'none'; panel.style.right = 'auto';
      panel.style.left = r.left + 'px'; panel.style.top = r.top + 'px';
      hd.setPointerCapture(pid);
    });
    hd.addEventListener('pointermove', e => {
      if (!dragging) return;
      const maxX = Math.max(4, innerWidth - panel.offsetWidth - 4);
      const maxY = Math.max(4, innerHeight - panel.offsetHeight - 4);
      panel.style.left = Math.max(4, Math.min(maxX, e.clientX - dx)) + 'px';
      panel.style.top = Math.max(4, Math.min(maxY, e.clientY - dy)) + 'px';
    });
    const stop = () => {
      if (!dragging) return;
      dragging = false;
      if (pid != null) { try { hd.releasePointerCapture(pid); } catch (_) {} pid = null; }
    };
    hd.addEventListener('pointerup', stop);
    hd.addEventListener('pointercancel', stop);
    hd.addEventListener('lostpointercapture', stop);
    panel.querySelector('[data-av-close]').addEventListener('click', () => { panel.hidden = true; });
    panel.querySelectorAll('[data-av-param]').forEach(input => {
      wirePrecisionRange(input);
      input.addEventListener('input', () => {
        panel.querySelector(`[data-av-value="${input.dataset.avParam}"]`).textContent = input.value;
        paintAuraKnob(input);
      });
      input.addEventListener('change', () => {
        const value = +input.value, key = input.dataset.avParam;
        avRemember({params: {[key]: value}});
        post('/api/auraverb', {mix: 0, param: key, value});
      });
    });
    panel.querySelectorAll('[data-av-return]').forEach(input => {
      const bus = +input.dataset.avReturn;
      wirePrecisionRange(input);
      input.addEventListener('input', () => {
        panel.querySelector(`[data-av-return-value="${bus}"]`).textContent = mixerFaderLabel(-input.value);
        paintAuraKnob(input);
      });
      input.addEventListener('change', () => sendReverbReturn(bus, {level: -input.value}));
      panel.querySelector(`[data-av-return-mute="${bus}"]`).addEventListener('click', () =>
        sendReverbReturn(bus, {mute: !reverbReturn(bus)?.mute}));
    });
    panel.querySelector('[data-av-enable]').addEventListener('click', e => {
      const on = !(avCurrent()?.enabled === true);
      avRemember({enabled: on});
      refreshAuraVerb();
      post('/api/auraverb', {mix: 0, enabled: on});
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && panel && !panel.hidden) panel.hidden = true;
    });
  }
  refreshAuraVerb();
}
function openAuraVerb() {
  initAuraVerbPanel();
  const panel = $('#auraverbpanel');
  if (panel && UI_FEATURES?.auraverb?.enabled) { panel.hidden = false; refreshAuraVerb(); }
}

function mixerFaderLabel(att) {
  att = Math.max(0, Math.round(+att || 0));
  return att ? '−' + att + ' dB' : '0 dB';
}
function mixerSendLabel(value, max) {
  const att = Math.max(0, Math.round(+value || 0));
  return att >= (+max || 96) ? '−∞' : (att ? '−' + att : '0') + ' dB';
}
function mixerPanLabel(value) {
  value = Math.round(+value || 0);
  return value === 0 ? 'C' : value < 0 ? 'L' + Math.abs(value) : 'R' + value;
}

const mixerPendingKey = (mix, ch, field) => `${mix}:${ch}:${field}`;
const mixerHasMaster = () => MIXER?.has_master !== false;
const mixerInputStart = () => mixerHasMaster() ? 1 : 0;
const mixLinkPair = ch => {
  const input = ch - mixerInputStart();
  return input >= 0 ? Math.floor(input / 2) : -1;
};
const mixLinkPartner = ch => {
  const input = ch - mixerInputStart();
  return input >= 0 ? mixerInputStart() + (input ^ 1) : -1;
};
const mixLinkKey = (mix, pair) => `${mix}:${pair}`;
const mixIsLinked = (mix, ch) => {
  const pair = mixLinkPair(ch);
  return pair >= 0 && !!MIX_LINKS[mixLinkKey(mix, pair)];
};
function loadMixLinks() {
  try {
    const saved = JSON.parse(localStorage.getItem('mixerLinks') || '{}');
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return {};
    const links = {};
    for (const [key, enabled] of Object.entries(saved)) {
      if (!enabled) continue;
      if (/^\d+:\d+$/.test(key)) links[key] = true;
      // Older builds stored only the pair, which made one link appear in
      // every mix. Keep those links in Mix 1 during the format migration,
      // rather than reviving the cross-mix behaviour.
      else if (/^\d+$/.test(key)) links[mixLinkKey(0, +key)] = true;
    }
    return links;
  }
  catch (_) { return {}; }
}
function saveMixLinks() {
  try { localStorage.setItem('mixerLinks', JSON.stringify(MIX_LINKS)); } catch (_) {}
}

// Profiles may promote a complete nested link-table response into the
// browser's mixer-pair state. Until that response arrives, localStorage keeps
// the UI useful across reconnects and for profiles without a link map.
function mixerLinkReadbackSpec() {
  const spec = PROFILE?.mixer?.link_readback;
  if (!spec || !['confirmed', 'capture-confirmed'].includes(
      String(spec.status || '').trim().toLowerCase())) return null;
  return spec;
}
function mixerSurfaceSelectionSpec() {
  const spec = PROFILE?.mixer?.surface_selection;
  if (!spec || !['confirmed', 'capture-confirmed'].includes(
      String(spec.status || '').trim().toLowerCase())) return null;
  return spec;
}
function recordLinked(record) {
  return record?.linked === true || Number(record?.linked) !== 0;
}
function syncMixerLinksFromReadback(structured) {
  const spec = mixerLinkReadbackSpec();
  if (!spec || !Array.isArray(structured?.layouts)) return false;
  const category = Number(spec.category), index = Number(spec.index);
  const layout = structured.layouts.find(item =>
    item && Number(item.category) === category && Number(item.index) === index
      && item.safe === true);
  const records = layout?.current?.[String(index)];
  const expectedCount = Number(spec.record_count);
  if (!Array.isArray(records) || !Number.isInteger(expectedCount)
      || expectedCount <= 0 || records.length < expectedCount) return false;

  const ranges = spec.selector_ranges;
  if (!ranges || typeof ranges !== 'object' || Array.isArray(ranges)) return false;
  const expectedSelectors = [];
  const selectorToPair = new Map();
  for (const [mixKey, bounds] of Object.entries(ranges)) {
    const mix = Number(mixKey);
    if (!Number.isInteger(mix) || !Array.isArray(bounds) || bounds.length !== 2) return false;
    const lo = Number(bounds[0]), hi = Number(bounds[1]);
    if (!Number.isInteger(lo) || !Number.isInteger(hi) || hi < lo
        || lo < 0 || hi >= expectedCount) return false;
    for (let selector = lo; selector <= hi; selector++) {
      expectedSelectors.push(selector);
      selectorToPair.set(selector, [mix, selector - lo]);
    }
  }
  if (!expectedSelectors.length) return false;

  const bySelector = new Map();
  for (const record of records) {
    const selector = Number(record?.record_index);
    if (Number.isInteger(selector)) bySelector.set(selector, record);
  }
  // Require every returned slot, including reserved bytes, before changing
  // local state. A partial response must not clear valid cached links.
  for (let selector = 0; selector < expectedCount; selector++) {
    if (!bySelector.has(selector)) return false;
  }

  let changed = false;
  for (const selector of expectedSelectors) {
    const [mix, pair] = selectorToPair.get(selector);
    const key = mixLinkKey(mix, pair), on = recordLinked(bySelector.get(selector));
    if (!!MIX_LINKS[key] !== on) changed = true;
    if (on) MIX_LINKS[key] = true; else delete MIX_LINKS[key];
  }
  if (changed) { saveMixLinks(); refreshMixLinks(); }
  return changed;
}
function markMixerPending(mix, ch, field, value) {
  MIX_PENDING[mixerPendingKey(mix, ch, field)] = {value, ts: Date.now()};
}
function mixerPendingActive(mix, ch, field, actual) {
  const key = mixerPendingKey(mix, ch, field), pending = MIX_PENDING[key];
  if (!pending) return false;
  if (actual === pending.value || Date.now() - pending.ts > MIX_PENDING_TTL) {
    delete MIX_PENDING[key];
    return false;
  }
  return true;
}
function commitMixerPan(m, ch, pan, pval, value, field = 'pan') {
  value = +value;
  pan.value = value;
  pval.textContent = mixerPanLabel(value);
  paintMixerKnob(pan);
  markMixerPending(m, ch, field, value);
  post('/api/mix', {mix: m, channel: ch, [field]: value});
}

function applyMixerMeters(meters) {
  MIXER_METERS = meters || null;
  const rawRange = Array.isArray(meters?.raw_range) && meters.raw_range.length === 2
    ? meters.raw_range.map(Number) : [0, 96];
  const rawLo = rawRange[0], rawHi = rawRange[1];
  const silenceRaw = Number.isFinite(Number(meters?.silence_raw))
    ? Number(meters.silence_raw) : rawHi;
  // JSON null means that this profile has no declared noise floor.  Do not
  // pass it through Number(): Number(null) is 0, which would mark every
  // non-negative meter sample as silence and flatten all virtual-mixer bars.
  const noiseFloorValue = meters?.noise_floor_raw;
  const noiseFloorRaw = noiseFloorValue != null
    && Number.isFinite(Number(noiseFloorValue))
    ? Number(noiseFloorValue) : null;
  document.querySelectorAll('#mixer [data-mix-pane] .mixer-strip').forEach(strip => {
    const pane = strip.closest('[data-mix-pane]');
    const meter = strip.querySelector('[data-mix-meter]');
    if (!pane || !meter) return;
    const ch = +strip.dataset.ch;
    const selected = meters && +meters.mix === +pane.dataset.mixPane
      && (!mixerHasMaster() || ch > 0);
    const sample = selected && Array.isArray(meters.strips)
      ? meters.strips.find(x => +x.ch === ch) : null;
    const raw = Number.isFinite(sample?.raw) ? sample.raw : null;
    const valid = raw != null && Number.isFinite(raw)
      && Number.isFinite(rawLo) && Number.isFinite(rawHi)
      && raw >= Math.min(rawLo, rawHi) && raw <= Math.max(rawLo, rawHi);
    const silent = sample?.silence === true || raw === silenceRaw
      || (noiseFloorRaw != null && raw >= noiseFloorRaw);
    const span = rawHi - rawLo;
    const pct = valid && !silent && span > 0
      ? (rawHi - raw) / span * 100 : 0;
    meter.classList.toggle('unavailable', !valid);
    meter.classList.toggle('peak', valid && raw === rawLo);
    meter.style.height = pct.toFixed(1) + '%';
    meter.parentElement.title = !valid ? 'Meter unavailable'
      : `Raw ${raw}${silent ? ' · silence/noise floor' : ' · uncalibrated'}`;
  });
}

function buildMixer() {
  const wrap = $('#mixer'); wrap.innerHTML = '';
  if (!MIXER.available) return;
  const nMix = Number(MIXER.n_mixes) || 0;
  const selector = PROFILE?.frame?.state_report?.mixer_window_selection;
  const surfaceSelection = mixerSurfaceSelectionSpec();
  const hasSurfaceSelection = !!surfaceSelection;
  const hasAuraVerb = UI_FEATURES?.auraverb?.enabled && UI_FEATURES.auraverb.mix === 0;
  const sourceFeature = UI_FEATURES?.mixer_sources?.enabled ? UI_FEATURES.mixer_sources : null;
  const auraVerbButton = hasAuraVerb
    ? `<button type="button" class="secbtn auraverb-open" data-auraverb-open title="Open ${AV_DISPLAY_NAME} controls for Mix 1">${AV_DISPLAY_NAME} · Mix 1</button>`
    : '';
  const panes = Array.from({length: nMix}, (_, m) =>
    `<div class="mixer-pane" data-mix-pane="${m}" role="tabpanel" hidden>
       <div class="mixer-pane-hd">
         <span class="hint">Mix ${m + 1}${selector || surfaceSelection ? ` meter bank · selector ${m}` : ''}</span>
       </div>
       <div class="mixer-scroll"><div class="mixer-strips" data-strips></div></div>
     </div>`).join('');
  const hasMaster = mixerHasMaster();
  wrap.innerHTML = `<div class="mixer-shell">
    <div class="mixer-hd">
      <strong>Virtual mixer</strong>
      <span class="hint">${MIXER.channels_per_mix} input strips${hasMaster ? ' + master' : ''}</span>
      ${auraVerbButton}
    </div>${panes}
    <p class="hint mixer-note">Fader values are attenuation below unity. ${sourceFeature
      ? sourceFeature.note
      : selector || surfaceSelection ? 'The thin green meter follows the selected mixer surface.'
      : 'Meter mapping is not declared for this profile yet.'}</p>
  </div>`;
  wrap.querySelector('[data-auraverb-open]')?.addEventListener('click', openAuraVerb);
  const activeRoute = $('#routetabs .tabbtn.active')?.dataset.rtab || '';
  const routeMix = /^mix([1-4])$/.exec(activeRoute);
  const initial = routeMix ? +routeMix[1] - 1 : 0;
  // Orion's legacy mixer-window selector is just as important as Zen Go's
  // confirmed surface selector.  On a fresh load the Routing tab is normally
  // active, so without this write the device may still expose Mix 2/3/4's
  // shared meter bank while the UI is displaying Mix 1.
  selectMixer(initial, !!routeMix || !!selector || hasSurfaceSelection);
}

function refreshMixLinks() {
  document.querySelectorAll('#mixer .mixer-strip').forEach(strip => {
    const pane = strip.closest('[data-mix-pane]');
    const m = pane ? +pane.dataset.mixPane : 0;
    const ch = +strip.dataset.ch, linked = mixIsLinked(m, ch);
    const button = strip.querySelector('[data-mix-link]');
    if (button) button.classList.toggle('on', linked);
    strip.classList.toggle('linkstart', linked && mixLinkPartner(ch) > ch);
    strip.classList.toggle('linkend', linked && mixLinkPartner(ch) < ch);
  });
}

function setMixLink(m, pair, on) {
  const key = mixLinkKey(m, pair);
  if (pair < 0 || !!MIX_LINKS[key] === !!on) return;
  if (on) MIX_LINKS[key] = true; else delete MIX_LINKS[key];
  saveMixLinks();
  post('/api/mix-link', {mix: m, pair, enabled: on});
  if (on) {
    const sourceCh = mixerInputStart() + pair * 2;
    const source = (MIXER.current || {})[String(m)]?.[sourceCh];
    if (source) {
      const partner = sourceCh + 1;
      for (const field of ['fader', 'send', 'mute', 'solo']) {
        if (field === 'send' && !(MIXER.send_mixes || []).includes(m)) continue;
        markMixerPending(m, partner, field, source[field]);
        post('/api/mix', {mix: m, channel: partner, [field]: source[field]});
        if (field === 'fader' || field === 'send')
          paintMixerField(m, partner, field, source[field]);
      }
    }
  }
  refreshMixLinks();
}

function paintMixerField(m, ch, field, value) {
  const strip = document.querySelector(
    `[data-mix-pane="${m}"] .mixer-strip[data-ch="${ch}"]`);
  if (!strip) return;
  if (field === 'fader') {
    const input = strip.querySelector('[data-fader]');
    if (!input) return;
    input.value = -value;
    strip.querySelector('[data-fval]').textContent = mixerFaderLabel(value);
    paintMixerFader(strip, input.value);
  } else if (field === 'send') {
    const input = strip.querySelector('[data-send]');
    if (!input) return;
    input.value = -value;
    strip.querySelector('[data-sval]').textContent = mixerSendLabel(
      value, MIXER?.ranges?.send?.[1] ?? 96);
    paintMixerKnob(input);
  }
}

function mirrorLinkedMixField(m, ch, field, value) {
  if (!mixIsLinked(m, ch) || (field !== 'fader' && field !== 'send')) return;
  const partner = mixLinkPartner(ch);
  markMixerPending(m, partner, field, value);
  paintMixerField(m, partner, field, value);
}

function postLinkedMix(m, ch, patch) {
  const field = Object.keys(patch)[0], value = patch[field];
  markMixerPending(m, ch, field, value);
  post('/api/mix', {mix: m, channel: ch, ...patch});
  if (!mixIsLinked(m, ch)) return;
  const partner = mixLinkPartner(ch);
  markMixerPending(m, partner, field, value);
  post('/api/mix', {mix: m, channel: partner, ...patch});
  if (field === 'fader' || field === 'send') paintMixerField(m, partner, field, value);
}

function postLinkedSolo(m, ch, on) {
  post('/api/mix-solo', {mix: m, channel: ch, on});
  if (mixIsLinked(m, ch))
    post('/api/mix-solo', {mix: m, channel: mixLinkPartner(ch), on});
}

function selectMixer(m, notify = true) {
  if (!MIXER?.available) return;
  const nMix = Number(MIXER.n_mixes) || 0;
  if (!Number.isInteger(m) || m < 0 || m >= nMix) return;
  ACTIVE_MIX = m;
  const auraButton = document.querySelector('[data-auraverb-open]');
  if (auraButton) auraButton.hidden = m !== 0;
  if (m !== 0) {
    const panel = $('#auraverbpanel');
    if (panel) panel.hidden = true;
  }
  document.querySelectorAll('[data-mix-pane]').forEach(pane => {
    pane.hidden = +pane.dataset.mixPane !== m;
  });
  const pane = document.querySelector(`[data-mix-pane="${m}"]`);
  if (pane) renderMix(pane, m);
  applyMixerMeters(MIXER_METERS);
  if (notify && (PROFILE?.frame?.state_report?.mixer_window_selection
                 || mixerSurfaceSelectionSpec()))
    post('/api/mixer-select', {mix: m});
}

function mixerSoloSnapshot(m) {
  const slots = (MIXER.current || {})[String(m)] || [];
  const n = (Number(MIXER.channels_per_mix) || 0) + (mixerHasMaster() ? 1 : 0);
  return {
    mutes: Array.from({length: n}, (_, ch) => !!slots[ch]?.mute),
    solos: Array.from({length: n}, (_, ch) => !!slots[ch]?.solo),
  };
}
function activeMixerSoloChannels(pane) {
  return new Set([...pane.querySelectorAll('[data-solo].on')]
    .map(btn => +btn.closest('.mixer-strip').dataset.ch));
}
function paintMixSolo(pane, active, restore) {
  const soloedChannels = new Set(active);
  const soloMode = soloedChannels.size > 0;
  pane.querySelectorAll('.mixer-strip').forEach(strip => {
    const ch = +strip.dataset.ch;
    const mute = strip.querySelector('[data-mute]');
    const solo = strip.querySelector('[data-solo]');
    if (!solo) return;                       // master is fader + mute only
    const muted = soloMode ? !soloedChannels.has(ch) : !!restore?.mutes?.[ch];
    const soloed = soloMode ? soloedChannels.has(ch) : !!restore?.solos?.[ch];
    mute.classList.toggle('on', muted);
    solo.classList.toggle('on', soloed);
  });
}

function paintMixerFader(strip, value) {
  const fader = strip.querySelector('[data-fader]');
  const thumb = strip.querySelector('.mixer-fader-thumb');
  if (!fader || !thumb) return;
  const lo = +fader.min, hi = +fader.max, span = fader.clientHeight || 158;
  const fraction = hi === lo ? 0 : (hi - (+value || 0)) / (hi - lo);
  const handle = 50;
  const pos = handle / 2 + Math.max(0, Math.min(1, fraction)) * (span - handle);
  thumb.style.setProperty('--fader-pos', pos.toFixed(1) + 'px');
}

function paintMixerKnob(input) {
  if (!input) return;
  const lo = +input.min, hi = +input.max, value = +input.value;
  const fraction = hi === lo ? 0 : Math.max(0, Math.min(1, (value - lo) / (hi - lo)));
  const pointer = input.closest('.mixer-knob')?.querySelector('i');
  if (pointer) pointer.style.transform = `translateX(-50%) rotate(${(-135 + fraction * 270).toFixed(1)}deg)`;
}

// Native range controls map a click directly to the pointer position. The
// mixer and Gazelle Reverb controls are rotary, so use the same relative drag model
// as the physical knobs: vertical movement changes the value gradually and a
// plain click does not jump it to a new position. Frequency-tagged EQ ranges
// use the same drag distance in log-frequency space for finer low-end control.
function wirePrecisionRange(input) {
  if (!input || input.dataset.precisionWired) return;
  input.dataset.precisionWired = '1';
  const lo = +input.min, hi = +input.max;
  const step = Number(input.step) > 0 ? Number(input.step) : 1;
  const configuredPixels = Number(input.dataset.precisionDragPixels);
  const dragPixels = Number.isFinite(configuredPixels) && configuredPixels > 0
    ? configuredPixels : KNOB_DRAG_PIXELS;
  let dragging = false, moved = false, startY = 0, startValue = 0, pid = null;
  const clamp = value => {
    const snapped = lo + Math.round((value - lo) / step) * step;
    return Math.max(lo, Math.min(hi, snapped));
  };
  const logarithmic = input.dataset.logarithmic === 'true'
    && lo > 0 && hi > lo;
  const dragValue = (value, dy) => {
    if (!logarithmic) return value + dy * ((hi - lo) / dragPixels);
    const logRange = Math.log(hi) - Math.log(lo);
    return Math.exp(Math.log(value) + dy * logRange / dragPixels);
  };
  const emit = type => input.dispatchEvent(new Event(type, {bubbles: true}));
  const finish = commit => {
    if (!dragging) return;
    const wasMoved = moved;
    dragging = false;
    if (pid != null) {
      try { if (input.hasPointerCapture?.(pid)) input.releasePointerCapture(pid); } catch (_) {}
      pid = null;
    }
    if (commit && wasMoved) emit('change');
  };
  input.addEventListener('pointerdown', e => {
    e.preventDefault();
    try { input.focus({preventScroll: true}); } catch (_) { input.focus(); }
    dragging = true; moved = false; startY = e.clientY; startValue = +input.value;
    pid = e.pointerId; input.setPointerCapture(pid);
  });
  input.addEventListener('pointermove', e => {
    if (!dragging) return;
    const dy = startY - e.clientY;
    if (!moved && Math.abs(dy) < 2) return;
    moved = true;
    input.value = clamp(dragValue(startValue, dy));
    emit('input');
  });
  input.addEventListener('pointerup', () => finish(true));
  input.addEventListener('pointercancel', () => finish(true));
  input.addEventListener('lostpointercapture', () => finish(true));
  input.addEventListener('wheel', e => {
    if (!input.matches(':hover')) return;
    e.preventDefault();
    e.stopPropagation();
    input.value = clamp(+input.value - Math.sign(e.deltaY) * step);
    emit('input'); emit('change');
  }, {passive: false});
}

// ---- read-only AFX inserts (profile `afx_inserts`) ---------------------
// Shows what the device reports in each rack. Nothing here writes to the
// device; a label ending in "?" is a name not yet confirmed on this model.
function mixerInsertSpec() {
  const spec = PROFILE?.afx_inserts;
  return spec && Array.isArray(spec.mixer_channels) ? spec : null;
}

function mixerInsertBlock(ch, isMaster) {
  const spec = mixerInsertSpec();
  if (!spec) return '';
  // every strip gets the box so faders stay aligned; only rack channels fill it
  return !isMaster && spec.mixer_channels.includes(ch)
    ? `<div class="mx-inserts" data-ins-ch="${ch}"></div>`
    : '<div class="mx-inserts none" aria-hidden="true"></div>';
}

function paintMixerInserts() {
  const spec = mixerInsertSpec();
  if (!spec) return;
  const layout = (STRUCTURED?.layouts || []).find(l => l.kind === spec.readback_kind);
  document.querySelectorAll('#mixer [data-ins-ch]').forEach(box => {
    const index = spec.record_index_for_channel?.[box.dataset.insCh];
    const records = layout?.current?.[String(index)];
    const head = document.createElement('span');
    head.className = 'mx-ins-hd';
    head.textContent = 'INSERTS';
    const items = [head];
    const note = text => {
      const el = document.createElement('span');
      el.className = 'mx-ins-empty';
      el.textContent = text;
      items.push(el);
    };
    if (!records) {
      box.title = 'No readback from the device yet';
      note('not read');
    } else {
      const loaded = records.filter(r => r.type);
      box.title = `Device readback, rack record ${index} (read-only)`;
      if (!loaded.length) note('empty');
      loaded.forEach(r => {
        const known = spec.type_labels?.[String(r.type)];
        const el = document.createElement('span');
        el.className = 'mx-ins';
        el.textContent = known
          ? known.label + (known.status === 'confirmed' ? '' : '?')
          : 'type ' + r.type;
        el.title = `${el.textContent} · type ${r.type}, instance ${r.inst}`
          + (known && known.status !== 'confirmed' ? ' · name not confirmed on this device' : '');
        items.push(el);
      });
    }
    box.replaceChildren(...items);
  });
}

// ---- insert racks: load / remove (server: /api/afx-inserts) ------------
// The server owns the rules (which racks, which effects, verification).
// This only draws its state and sends one change at a time.
let INSERT_RACKS = null, INSERT_BUSY = false;

function renderInsertRacks() {
  const section = $('#insertsec'), box = $('#insertracks');
  if (!section || !box) return;
  const state = INSERT_RACKS;
  section.hidden = !state?.available;
  if (!state?.available) { box.replaceChildren(); return; }
  const canWrite = state.online && state.writes_enabled && !INSERT_BUSY;
  box.replaceChildren(...Object.keys(state.racks).map(key => {
    const channel = +key, rows = state.racks[key];
    const editable = canWrite && state.editable_channels.includes(channel);
    const card = document.createElement('div');
    card.className = 'insert-rack';
    const head = document.createElement('h3');
    head.textContent = 'CH ' + (channel + 1);
    card.append(head);
    if (rows === null) {
      const note = document.createElement('p');
      note.className = 'insert-note';
      note.textContent = 'not read yet';
      card.append(note);
      return card;
    }
    rows.forEach((row, slot) => {
      const line = document.createElement('div');
      line.className = 'insert-row';
      const name = document.createElement('span');
      name.textContent = row.label;
      name.title = `type ${row.type}, instance ${row.instance}`;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.title = `Remove ${row.label}`;
      remove.setAttribute('aria-label', `Remove ${row.label} from channel ${channel + 1}`);
      remove.disabled = !editable;
      remove.addEventListener('click', () => changeInsertRack({channel, operation: 'remove', slot, expected_type: row.type, expected_instance: row.instance}));
      if (editable && state.reorder && rows.length > 1) {
        // Drag one effect onto another of the same rack: one whole-rack write in the new order.
        line.draggable = true;
        line.title = 'Drag onto another effect of this rack to reorder';
        line.addEventListener('dragstart', e => e.dataTransfer.setData('text/plain', `${channel}:${slot}`));
        line.addEventListener('dragover', e => e.preventDefault());
        line.addEventListener('drop', e => {
          e.preventDefault();
          const [from, source] = e.dataTransfer.getData('text/plain').split(':').map(Number);
          // The server refuses if the dragged slot no longer holds what this rack shows.
          if (from === channel && source !== slot && rows[source]) changeInsertRack({channel, operation: 'move', source, slot,
            expected_type: rows[source].type, expected_instance: rows[source].instance});
        });
      }
      if (state.mapped_types?.includes(row.type)) {
        const edit = document.createElement('button');
        edit.type = 'button';
        edit.className = 'insert-edit';
        edit.textContent = 'Edit';
        edit.title = `Edit ${row.label} settings`;
        edit.disabled = !state.online;
        edit.addEventListener('click', () => openInsertParams(channel, slot, {type: row.type, instance: row.instance}));
        line.append(name, edit, remove);
      } else line.append(name, remove);
      card.append(line);
    });
    if (!rows.length) {
      const note = document.createElement('p');
      note.className = 'insert-note';
      note.textContent = 'empty';
      card.append(note);
    }
    const add = document.createElement('select');
    add.setAttribute('aria-label', `Add effect to channel ${channel + 1}`);
    add.disabled = !editable || rows.length >= state.max_slots;
    add.append(new Option(state.editable_channels.includes(channel) ? 'Add effect…' : 'Not editable yet', ''));
    const groups = new Map();
    [...state.effects].sort((a, b) => a.label.localeCompare(b.label)).forEach(effect => {
      const key = effect.group || '';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(new Option(effect.label, effect.type));
    });
    groups.forEach((options, key) => {
      if (!key) { add.append(...options); return; }
      const group = document.createElement('optgroup');
      group.label = key;
      group.append(...options);
      add.append(group);
    });
    add.addEventListener('change', () => {
      if (add.value) changeInsertRack({channel, operation: 'load', type: +add.value});
    });
    card.append(add);
    return card;
  }));
  if (state.failure) reportMessage('inserts', 'Effect racks', state.failure);
}

// ---- effect settings (server: /api/afx-inserts/parameters) -------------
// One effect at a time. A knob shows the value the device last confirmed;
// while dragging, the newest value waits until the previous write returns.
let INSERT_PARAMS = null;   // {channel, slot, data, face, pending: {key: value}, busy}

function renderInsertParams() {
  const box = $('#insertparams');
  if (!box) return;
  const view = INSERT_PARAMS;
  box.hidden = !view;
  if (!view) { box.replaceChildren(); return; }
  const head = document.createElement('div');
  head.className = 'insert-params-hd';
  const title = document.createElement('h3');
  title.textContent = view.data ? `${view.data.label} · CH ${view.channel + 1}` : 'Reading settings…';
  const close = document.createElement('button');
  close.type = 'button';
  close.textContent = 'Close';
  close.addEventListener('click', () => { INSERT_PARAMS = null; renderInsertParams(); });
  head.append(title, close);
  // The hardware-style panel draws the fields its layout covers; the rest keep the plain knobs.
  const fields = view.data?.fields || [];
  const panel = document.createElement('div');
  const covered = view.face ? renderFaceplate(panel, view.face, fields, queueInsertParam) : new Set();
  const knobs = document.createElement('div');
  knobs.className = 'insert-knobs';
  fields.filter(field => !covered.has(field.key)).forEach(field => {
    const cell = document.createElement('label');
    cell.className = 'insert-knob';
    const span = (field.max - field.min) || 1;
    const paint = value => {
      cell.style.setProperty('--turn', `${-135 + (value - field.min) / span * 270}deg`);
      cell.querySelector('output').textContent = insertParamText(field, value);
    };
    cell.innerHTML = '<span class="insert-knob-label"></span><span class="insert-knob-body" aria-hidden="true"><i></i></span>'
      + '<input type="range" step="1"><output></output>';
    cell.querySelector('.insert-knob-label').textContent = field.label;
    const input = cell.querySelector('input');
    input.step = field.step || 1;
    input.min = field.min; input.max = field.max; input.value = field.value;
    input.setAttribute('aria-label', field.label);
    // A selector with gaps (field.values) snaps to the nearest allowed value.
    const snap = value => field.values ? field.values.reduce((a, b) => Math.abs(b - value) < Math.abs(a - value) ? b : a) : value;
    const changed = () => { input.value = snap(+input.value); paint(+input.value); queueInsertParam(field.key, +input.value); };
    input.addEventListener('input', changed);
    insertKnobGestures(cell, cell.querySelector('.insert-knob-body'), input, field, changed);
    paint(field.value);
    knobs.append(cell);
  });
  const note = document.createElement('p');
  note.className = 'hint';
  note.textContent = 'Values are the device\'s own numbers, as the vendor panel shows them. Each change is written and read back.';
  box.replaceChildren(head, ...(covered.size ? [panel] : []), ...(knobs.children.length || !covered.size ? [knobs] : []), note);
}

async function openInsertParams(channel, slot, expected) {
  // expected {type, instance}: the effect the rack showed; the server refuses if the slot now holds another.
  INSERT_PARAMS = {channel, slot, expected, data: null, pending: {}, busy: false};
  renderInsertParams();
  try {
    const response = await fetch(`/api/afx-inserts/parameters?channel=${channel}&slot=${slot}`
      + `&expected_type=${expected.type}&expected_instance=${expected.instance}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'The settings could not be read.');
    const face = await loadFaceplate(result.type);
    if (INSERT_PARAMS?.channel === channel && INSERT_PARAMS.slot === slot) Object.assign(INSERT_PARAMS, {data: result, face});
  } catch (error) {
    reportMessage('inserts', 'Effect settings', error.message);
    INSERT_PARAMS = null;
  }
  renderInsertParams();
}

async function queueInsertParam(key, value) {
  const view = INSERT_PARAMS;
  if (!view) return;
  view.pending[key] = value;
  if (view.busy) return;
  view.busy = true;
  while (INSERT_PARAMS === view && Object.keys(view.pending).length) {
    const [nextKey, nextValue] = Object.entries(view.pending)[0];
    delete view.pending[nextKey];
    try {
      const response = await fetch('/api/afx-inserts/parameters', {method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({channel: view.channel, slot: view.slot, key: nextKey, value: nextValue,
                              expected_type: view.expected.type, expected_instance: view.expected.instance})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'The change was refused.');
      view.data = result;
      clearMessage('inserts');
    } catch (error) {
      reportMessage('inserts', 'Effect settings', error.message);
      view.pending = {};
      view.busy = false;
      if (INSERT_PARAMS === view) openInsertParams(view.channel, view.slot, view.expected);
      return;
    }
  }
  view.busy = false;
}

async function loadInsertRacks() {
  try { INSERT_RACKS = await getJSON('/api/afx-inserts'); } catch (e) { return; }
  renderInsertRacks();
}

async function changeInsertRack(change) {
  if (INSERT_BUSY) return;
  INSERT_BUSY = true;
  renderInsertRacks();
  try {
    const response = await fetch('/api/afx-inserts', {method: 'POST',
      headers: {'content-type': 'application/json'}, body: JSON.stringify(change)});
    const result = await response.json();
    if (response.ok) { INSERT_RACKS = result; clearMessage('inserts'); INSERT_PARAMS = null; renderInsertParams(); }
    else reportMessage('inserts', 'Effect racks', result.error || 'The change was refused.');
  } catch (e) {
    reportMessage('inserts', 'Effect racks', 'The server did not answer.');
  }
  INSERT_BUSY = false;
  await loadInsertRacks();
  try { STRUCTURED = await getJSON('/api/readbacks'); } catch (e) { return; }
  paintMixerInserts();
}

setInterval(async () => {
  if (!ONLINE || INSERT_BUSY) return;
  loadInsertRacks();
  if (!mixerInsertSpec()) return;
  try { STRUCTURED = await getJSON('/api/readbacks'); } catch (e) { return; }
  paintMixerInserts();
}, 15000);

function renderMix(pane, m) {
  const slots = (MIXER.current || {})[String(m)] || [];
  const [fLo, fHi] = MIXER.ranges.fader, [pLo, pHi] = MIXER.ranges.pan, [sLo, sHi] = MIXER.ranges.send;
  const hasMaster = mixerHasMaster();
  const rows = pane.querySelector('[data-strips]');
  const hasSend = MIXER.has_send !== false && (MIXER.send_mixes || []).includes(m);
  const hasSendPan = hasSend && !!PROFILE?.frame?.reverb_send_command;   // Zen Go / Discrete 4: the send has its own pan
  const sourceFeature = mixerSourceFeature(m);
  const n = Math.max((Number(MIXER.channels_per_mix) || 0) + (hasMaster ? 1 : 0), slots.length);
  rows.innerHTML = Array.from({length: n}, (_, ch) => {
    const s = slots[ch] || {fader: 0, pan: 0, send: 0, mute: false, solo: false};
    const fader = Number(s.fader) || 0, pan = Number(s.pan) || 0, send = Number(s.send) || 0;
    const isMaster = hasMaster && ch === 0;
    // MIXER.link_pairs lists the pairs a profile has captured; absent = all
    const linkStart = mixLinkPair(ch) >= 0 && mixLinkPartner(ch) > ch
      && (!MIXER.link_pairs || (MIXER.link_pairs[String(m)] || []).includes(mixLinkPair(ch)));
    const label = isMaster ? 'MASTER' : 'CH ' + String(hasMaster ? ch : ch + 1).padStart(2, '0');
    const linkLabel = `link CH${hasMaster ? ch : ch + 1}+CH${hasMaster ? ch + 1 : ch + 2}`;
    return `<div class="mixer-strip${isMaster ? ' master' : ''}" data-ch="${ch}">
      ${sourceFeature && !isMaster ? `<select class="mxsource" data-mix-source ${sourceFeature.writable && !(sourceFeature.locked_channels || []).includes(ch) ? '' : 'disabled'} title="${sourceFeature.note}">${mixerSourceOptions(sourceFeature, m, ch)}</select>` : ''}
      <span class="mxname">${label}</span>
      ${mixerInsertBlock(ch, isMaster)}
      ${linkStart ? `<button class="linkbtn" data-mix-link title="${linkLabel}">${LINK_ICON}</button>` : ''}
      <span class="mxval" data-fval>${mixerFaderLabel(fader)}</span>
      ${hasSend && !isMaster ? `<div class="mixer-knob-control">
        <output data-sval class="mixer-readout">${mixerSendLabel(send, sHi)}</output>
        <div class="mixer-knob"><i></i><input type="range" data-send min="${-sHi}" max="${-sLo}" value="${-send}" aria-label="channel ${ch} ${AV_DISPLAY_NAME} send"></div>
        <label class="mixer-knob-label">SEND</label>
      </div>` : ''}
      ${hasSendPan && !isMaster ? `<div class="mixer-knob-control">
        <output data-spval class="mixer-readout">${mixerPanLabel(s.send_pan)}</output>
        <div class="mixer-knob"><i></i><input type="range" data-send-pan min="${pLo}" max="${pHi}" value="${Number(s.send_pan) || 0}" aria-label="channel ${ch} ${AV_DISPLAY_NAME} send pan"></div>
        <label class="mixer-knob-label">SEND PAN</label>
      </div>` : ''}
      ${!isMaster ? `<div class="mixer-knob-control">
        <output data-pval class="mixer-readout">${mixerPanLabel(pan)}</output>
        <div class="mixer-knob"><i></i><input type="range" data-pan min="${pLo}" max="${pHi}" value="${pan}" aria-label="channel ${ch} pan"></div>
        <label class="mixer-knob-label">PAN</label>
      </div>` : ''}
      <div class="mixer-fader-row">
        <div class="mixer-fader-well">
          <span class="mixer-fader-thumb" aria-hidden="true">
            <img class="mixer-fader-art" src="/webui/assets/fader-shadow.svg" alt="" draggable="false">
          </span>
          <input class="mixer-fader" type="range" data-fader min="${-fHi}" max="${-fLo}" value="${-fader}" aria-label="${isMaster ? 'master' : 'channel ' + ch} fader">
        </div>
        ${!isMaster ? '<div class="mixer-meter" title="Meter unavailable"><i data-mix-meter class="unavailable"></i></div>' : ''}
      </div>
      <div class="mixer-buttons">
        <button type="button" data-mute class="${s.mute ? 'on' : ''}" aria-label="${isMaster ? 'master' : 'channel ' + ch} mute">M</button>
        ${!isMaster ? `<button type="button" data-solo class="solo ${s.solo ? 'on' : ''}" aria-label="channel ${ch} solo">S</button>` : ''}
      </div>
    </div>`;
  }).join('');
  paintMixerInserts();

  rows.querySelectorAll('.mixer-strip').forEach(strip => {
    const ch = +strip.dataset.ch;
    const source = strip.querySelector('[data-mix-source]');
    if (source) source.addEventListener('change', e => {
      const key = e.target.value;
      if (key.startsWith('raw:')) {
        reportMessage('zen-routing', 'Zen Go source selectors',
          `${rawSourceLabel(key)} is present in the device map but is not a selectable source in the profile.`);
        source.value = mixerSourceKey(sourceFeature, m, ch);
        return;
      }
      if (key) {
        MIXER_SOURCES[`${m}:${ch}`] = key;
        saveMixerSources();
      }
      if (sourceFeature.writable) {
        if (key) post('/api/mixer-source', {mix: m, channel: ch, source: key});
      } else {
        reportMessage('zen-routing', 'Zen Go source selectors', sourceFeature.note);
      }
    });
    const fader = strip.querySelector('[data-fader]'), fval = strip.querySelector('[data-fval]');
    paintMixerFader(strip, fader.value);
    fader.addEventListener('input', () => {
      const value = -(+fader.value);
      markMixerPending(m, ch, 'fader', value);
      fval.textContent = mixerFaderLabel(value);
      paintMixerFader(strip, fader.value);
      mirrorLinkedMixField(m, ch, 'fader', value);
    });
    fader.addEventListener('change', () => {
      const value = -(+fader.value);
      postLinkedMix(m, ch, {fader: value});
    });
    for (const [field, sel, out] of [['pan', '[data-pan]', '[data-pval]'], ['send_pan', '[data-send-pan]', '[data-spval]']]) {
      const pan = strip.querySelector(sel), pval = strip.querySelector(out);
      if (!pan) continue;
      wirePrecisionRange(pan);
      paintMixerKnob(pan);
      pan.addEventListener('input', e => { pval.textContent = mixerPanLabel(e.target.value); paintMixerKnob(pan); });
      pan.addEventListener('change', e => {
        commitMixerPan(m, ch, pan, pval, e.target.value, field);
      });
      pan.addEventListener('dblclick', e => {
        e.preventDefault();
        commitMixerPan(m, ch, pan, pval, 0, field);
      });
    }
    const send = strip.querySelector('[data-send]'), sval = strip.querySelector('[data-sval]');
    if (send) {
      wirePrecisionRange(send);
      paintMixerKnob(send);
      send.addEventListener('input', e => {
        const value = -(+e.target.value);
        markMixerPending(m, ch, 'send', value);
        sval.textContent = mixerSendLabel(value, sHi);
        paintMixerKnob(send);
        mirrorLinkedMixField(m, ch, 'send', value);
      });
      send.addEventListener('change', e => {
        const value = -(+e.target.value);
        postLinkedMix(m, ch, {send: value});
      });
    }
    strip.querySelector('[data-mute]').addEventListener('click', e => {
      const on = !e.currentTarget.classList.contains('on');
      e.currentTarget.classList.toggle('on', on);
      postLinkedMix(m, ch, {mute: on});
    });
    const solo = strip.querySelector('[data-solo]');
    if (solo) solo.addEventListener('click', e => {
      const on = !e.currentTarget.classList.contains('on');
      const active = activeMixerSoloChannels(pane);
      if (on) {
        if (!MIX_SOLO_RESTORE[m]) MIX_SOLO_RESTORE[m] = mixerSoloSnapshot(m);
        active.add(ch);
        if (mixIsLinked(m, ch)) active.add(mixLinkPartner(ch));
        paintMixSolo(pane, active);
      } else {
        const restore = MIX_SOLO_RESTORE[m];
        active.delete(ch);
        if (mixIsLinked(m, ch)) active.delete(mixLinkPartner(ch));
        if (active.size) {
          paintMixSolo(pane, active);
        } else {
          paintMixSolo(pane, active, restore);
          delete MIX_SOLO_RESTORE[m];
        }
      }
      postLinkedSolo(m, ch, on);
    });
    const link = strip.querySelector('[data-mix-link]');
    if (link) link.addEventListener('click', () =>
      setMixLink(m, mixLinkPair(ch), !mixIsLinked(m, ch)));
  });
  refreshMixLinks();
  applyMixerMeters(MIXER_METERS);
}

function refreshMixer() {
  document.querySelectorAll('#mixer [data-mix-pane] .mixer-strip').forEach(strip => {
    const pane = strip.closest('[data-mix-pane]');
    const m = +pane.dataset.mixPane;
    const slots = (MIXER.current || {})[String(m)] || [];
    const ch = +strip.dataset.ch, s = slots[ch]; if (!s) return;
    const fader = strip.querySelector('[data-fader]');
    if (!mixerPendingActive(m, ch, 'fader', s.fader) && document.activeElement !== fader) {
      fader.value = -s.fader;
      strip.querySelector('[data-fval]').textContent = mixerFaderLabel(s.fader);
    }
    paintMixerFader(strip, fader.value);
    for (const [field, sel, out] of [['pan', '[data-pan]', '[data-pval]'], ['send_pan', '[data-send-pan]', '[data-spval]']]) {
      const pan = strip.querySelector(sel);
      if (pan && s[field] != null && !mixerPendingActive(m, ch, field, s[field]) && document.activeElement !== pan) {
        pan.value = s[field];
        strip.querySelector(out).textContent = mixerPanLabel(s[field]);
        paintMixerKnob(pan);
      }
    }
    const send = strip.querySelector('[data-send]');
    if (send && !mixerPendingActive(m, ch, 'send', s.send) && document.activeElement !== send) {
      send.value = -s.send;
      strip.querySelector('[data-sval]').textContent = mixerSendLabel(s.send, MIXER.ranges.send[1]);
      paintMixerKnob(send);
    }
    const source = strip.querySelector('[data-mix-source]');
    const sourceFeature = mixerSourceFeature(m);
    if (source && sourceFeature && document.activeElement !== source) {
      source.value = mixerSourceKey(sourceFeature, m, ch);
    }
    if (!mixerPendingActive(m, ch, 'mute', s.mute)) {
      strip.querySelector('[data-mute]').classList.toggle('on', s.mute);
    }
    strip.querySelector('[data-solo]')?.classList.toggle('on', s.solo);
  });
  applyMixerMeters(MIXER_METERS);
}
