// Offline linked-mixer interaction checks: node tools/test_webui_mixer_links.cjs
const assert = require('node:assert/strict');
const vm = require('node:vm');

const {readWebUISource} = require('./webui_sources.cjs');
const js = readWebUISource();
const source = js.slice(js.indexOf('const mixerPendingKey'), js.indexOf('function mixerSoloSnapshot'));

const makeInput = value => {
  let current = String(value);
  return {
    get value() { return current; },
    set value(next) { current = String(next); },
  };
};
const makeStrip = () => {
  const controls = {
    '[data-fader]': makeInput(-10),
    '[data-send]': makeInput(-20),
    '[data-fval]': {textContent: ''},
    '[data-sval]': {textContent: ''},
  };
  return {querySelector: selector => controls[selector] || null, controls};
};
const strips = {1: makeStrip(), 2: makeStrip()};
const posted = [];
const context = vm.createContext({
  MIXER: {ranges: {send: [0, 96]}, current: {}},
  MIXER_METERS: null,
  MIX_PENDING: {},
  MIX_PENDING_TTL: 5000,
  MIX_LINKS: {'0:0': true},
  PROFILE: {
    mixer: {
      link_readback: {
        status: 'capture-confirmed', category: 0x0b, index: 3,
        record_count: 24, selector_ranges: {'0': [0, 7], '1': [16, 23]},
      },
    },
  },
  document: {
    querySelector: selector => {
      const match = selector.match(/data-ch="(\d+)"/);
      return match ? strips[match[1]] || null : null;
    },
    querySelectorAll: () => [],
  },
  post: (path, body) => posted.push({path, body}),
  mixerFaderLabel: value => value ? `−${value} dB` : '0 dB',
  mixerSendLabel: (value, max) => value >= max ? '−∞' : `−${value} dB`,
  mixerPanLabel: value => value === 0 ? 'C' : value < 0 ? `L${-value}` : `R${value}`,
  paintMixerFader: () => {},
  paintMixerKnob: () => {},
});
vm.runInContext(source, context);

context.mirrorLinkedMixField(0, 1, 'fader', 37);
assert.equal(strips[2].controls['[data-fader]'].value, '-37');
assert.equal(strips[2].controls['[data-fval]'].textContent, '−37 dB');
assert.equal(context.MIX_PENDING['0:2:fader'].value, 37);

context.mirrorLinkedMixField(0, 1, 'send', 96);
assert.equal(strips[2].controls['[data-send]'].value, '-96');
assert.equal(strips[2].controls['[data-sval]'].textContent, '−∞');
assert.equal(context.MIX_PENDING['0:2:send'].value, 96);

context.postLinkedMix(0, 1, {send: 44});
assert.equal(JSON.stringify(posted.map(x => x.body)), JSON.stringify([
  {mix: 0, channel: 1, send: 44},
  {mix: 0, channel: 2, send: 44},
]));

// A link in Mix 1 must not make the same pair linked in another mix.
const beforeOtherMix = posted.length;
context.mirrorLinkedMixField(1, 1, 'fader', 55);
assert.equal(posted.length, beforeOtherMix);
context.postLinkedMix(1, 1, {send: 44});
context.postLinkedMix(1, 1, {mute: true});
context.postLinkedSolo(1, 1, true);
assert.equal(JSON.stringify(posted.slice(beforeOtherMix).map(x => x.body)), JSON.stringify([
  {mix: 1, channel: 1, send: 44},
  {mix: 1, channel: 1, mute: true},
  {mix: 1, channel: 1, on: true},
]));

// Pan commits share the same mix/channel scope and can be centered directly.
assert.match(js, /pan\.addEventListener\('dblclick', e => \{\s*e\.preventDefault\(\);\s*commitMixerPan\(m, ch, pan, pval, 0, field\);\s*\}\);/s);
const pan = {value: -17};
const panReadout = {textContent: ''};
context.commitMixerPan(1, 3, pan, panReadout, 0);
assert.equal(pan.value, 0);
assert.equal(panReadout.textContent, 'C');
assert.equal(context.MIX_PENDING['1:3:pan'].value, 0);
assert.equal(JSON.stringify(posted.at(-1).body), JSON.stringify({mix: 1, channel: 3, pan: 0}));
// The Gazelle Reverb send pan knob posts its own field through the same commit.
context.commitMixerPan(0, 2, pan, panReadout, -12, 'send_pan');
assert.equal(panReadout.textContent, 'L12');
assert.equal(context.MIX_PENDING['0:2:send_pan'].value, -12);
assert.equal(JSON.stringify(posted.at(-1).body), JSON.stringify({mix: 0, channel: 2, send_pan: -12}));

// The API request also carries the mix so the backend can address the
// corresponding hardware link domain.
context.setMixLink(1, 0, true);
assert.equal(JSON.stringify(posted.at(-1).body), JSON.stringify({mix: 1, pair: 0, enabled: true}));
assert.equal(context.MIX_LINKS['0:0'], true);
assert.equal(context.MIX_LINKS['1:0'], true);

// A complete Zen Go q0b/03 response seeds only the visible pair ranges;
// reserved selectors are still required for completeness but do not create
// UI pairs.
const linkedRecords = Array.from({length: 24}, (_, record_index) => ({
  record_index, linked: record_index === 1 || record_index === 17,
}));
assert.equal(context.syncMixerLinksFromReadback({layouts: [{
  category: 0x0b, index: 3, safe: true, current: {'3': linkedRecords},
}]}), true);
assert.equal(context.MIX_LINKS['0:0'], undefined);
assert.equal(context.MIX_LINKS['1:0'], undefined);
assert.equal(context.MIX_LINKS['0:1'], true);
assert.equal(context.MIX_LINKS['1:1'], true);

// Partial responses must leave the cached state untouched.
const partial = linkedRecords.slice(0, 23);
partial[0] = {record_index: 0, linked: false};
assert.equal(context.syncMixerLinksFromReadback({layouts: [{
  category: 0x0b, index: 3, safe: true, current: {'3': partial},
}]}), false);
assert.equal(context.MIX_LINKS['0:0'], undefined);
console.log('WebUI linked mixer checks passed (live fader/Send mirror and paired posts).');

// ---- Settings panel: pan law, oscillator, Line Out 1-4 trims; reverb returns ----
// vm objects come from another realm: compare them as JSON, as above.
const same = (a, b) => assert.equal(JSON.stringify(a), JSON.stringify(b));
const el = (data = {}) => {
  const node = {hidden: true, disabled: false, value: '', textContent: '', innerHTML: '', dataset: data,
    attrs: {}, on: {}, cls: new Set(), row: {hidden: true}};
  node.classList = {toggle: (c, v) => (v ? node.cls.add(c) : node.cls.delete(c))};
  node.setAttribute = (k, v) => { node.attrs[k] = v; };
  node.addEventListener = (type, fn) => { node.on[type] = fn; };
  node.closest = () => node.row;
  node.querySelector = sel => sets.panel[sel] || null;
  return node;
};
const sets = {
  '[data-set-linetrim]': el(), '[data-linetrim-link]': el(), '[data-set-osc]': el(), '[data-panlaw]': el(),
  '[data-linetrim]': [0, 1, 2, 3].map(i => el({linetrim: String(i)})),
  '[data-osc-freq], [data-osc-level], [data-osc-mute]': [
    el({oscFreq: '1'}), el({oscFreq: '4'}), el({oscLevel: '48'}), el({oscMute: '64'}), el({oscMute: '128'})],
  panel: {},
};
sets['[data-osc-freq], [data-osc-level]'] = sets['[data-osc-freq], [data-osc-level], [data-osc-mute]'].slice(0, 3);
sets['[data-osc-mute]'] = sets['[data-osc-freq], [data-osc-level], [data-osc-mute]'].slice(3);
const setPosts = [];
const setContext = vm.createContext({
  $: sel => (Array.isArray(sets[sel]) ? null : sets[sel] || null),
  $$: sel => (Array.isArray(sets[sel]) ? sets[sel] : []),
  document: {activeElement: null},
  localStorage: {getItem: () => null, setItem: () => {}},
  post: (path, body) => setPosts.push({path, body}),
  MIX_PENDING_TTL: 5000,
  STRUCTURED: null, AURAVERB: null,
  PROFILE: {
    frame: {line_trim_command: {trim_range: [0, 6], link_values: {linked: 0, unlinked: 1}}},
    params: {
      output_trim: {trim_dbu: {0: 20, 1: 19, 2: 18, 3: 17, 4: 16, 5: 15, 6: 14}},
      pan_law: {values: {0: '0 dB', 1: '-6 dB', 2: '-3 dB', 3: '-4.5 dB'}},
      oscillator: {value_mask: '0xf5', value_bits: {'0x01': '', '0x04': '', '0x30': '', '0x40': '', '0x80': ''}},
      reverb_return: {targets: {0: 'Monitor/HP1 return', 1: 'HP2 return'}, range: [0, 96]},
    },
  },
  mixerFaderLabel: value => `−${value} dB`,
  paintAuraKnob: () => {},
});
vm.runInContext(js.slice(js.indexOf('function buildSettings'), js.indexOf('// ---- settings gear (Line Out')), setContext);
vm.runInContext(js.slice(js.indexOf('// Reverb returns: level and mute'), js.indexOf('function initAuraVerbPanel')), setContext);
setContext.buildSettings();

// Pan law: options are the profile's index -> label map; the readback index (cat 0x12) is shown.
const panLaw = sets['[data-panlaw]'];
assert.equal(panLaw.row.hidden, false);
assert.match(panLaw.innerHTML, /<option value="2">-3 dB<\/option>/);
setContext.applySettings({});
assert.equal(panLaw.value, '');                   // nothing read back yet: the '–' option
assert.equal(panLaw.disabled, false);             // a pan law write needs no current value
setContext.STRUCTURED = {refresh_seconds: 45, layouts: [
  {kind: 'pan_law', safe: true, current: {'0': [{record_index: 0, index: 3}]}},
  {kind: 'line_out_trims', safe: true, current: {'0': [{link: 1, trim_1: 0, trim_2: 0, trim_3: 6, trim_4: 0}]}},
]};
setContext.applySettings({});
assert.equal(panLaw.value, 3);
panLaw.value = '2'; panLaw.on.change();
same(setPosts.at(-1), {path: '/api/pan-law', body: {value: 2}});
setContext.applySettings({});
assert.equal(panLaw.value, 2);                    // sent value held until the slow readback catches up

// Oscillator: one byte; a control changes only its own bits.
const [osc1, , level, mute1] = sets['[data-osc-freq], [data-osc-level], [data-osc-mute]'];
assert.equal(vm.runInContext("oscWith(0xb4, 0x30, 2)", setContext), 0xa4);
assert.equal(vm.runInContext("oscField(0xa4, 0x30)", setContext), 2);
osc1.on.change(); mute1.on.click();
assert.equal(setPosts.length, 1);                 // unknown byte: nothing composed or sent
setContext.applySettings({oscillator: 0x84});    // osc 2 at 440 Hz and muted, osc 1 at 1 kHz
assert.equal(level.value, 0);
osc1.value = '1'; osc1.on.change();
same(setPosts.at(-1).body, {value: 0x85});
level.value = '3'; level.on.change();
same(setPosts.at(-1).body, {value: 0xb5}); // composes over the byte just sent
mute1.on.click();
same(setPosts.at(-1), {path: '/api/oscillator', body: {value: 0xf5}});
assert.equal(mute1.cls.has('on'), true);

// Line Out 1-4: the whole frame each time; with LINK on one trim is copied to all four.
const trims = sets['[data-linetrim]'], link = sets['[data-linetrim-link]'];
assert.equal(trims[2].value, 6);
trims[1].value = '4'; trims[1].on.change();
same(setPosts.at(-1), {path: '/api/line-trim', body: {linked: false, trims: [0, 4, 6, 0]}});
link.on.click();
same(setPosts.at(-1).body, {linked: true, trims: [0, 4, 6, 0]});
assert.equal(link.textContent, 'ON');
trims[3].value = '1'; trims[3].on.change();
same(setPosts.at(-1).body, {linked: true, trims: [1, 1, 1, 1]});

// Reverb returns: level and mute share a byte, so each post carries both.
setContext.sendReverbReturn(1, {mute: true});
assert.equal(setPosts.length, 7);                 // no readback yet: nothing sent
setContext.AURAVERB = {returns: [{bus: 0, level: 96, mute: false}, {bus: 1, level: 40, mute: false}]};
setContext.sendReverbReturn(1, {mute: true});
same(setPosts.at(-1), {path: '/api/reverb-return', body: {bus: 1, level: 40, mute: true}});
setContext.sendReverbReturn(1, {level: 12});
same(setPosts.at(-1).body, {bus: 1, level: 12, mute: true});
console.log('WebUI settings checks passed (pan law, oscillator bits, Line Out trims, reverb returns).');
