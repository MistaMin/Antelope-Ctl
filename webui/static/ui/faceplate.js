// ---- effect faceplates (static: /webui/static/faceplate/) ----------------
// A faceplate is one effect's settings drawn as a hardware panel: a plate
// tile with knob and switch sprite strips placed by layouts/<type>.json.
// manifest.json names the sprite files. Both are optional: without them the
// effect keeps the plain knob grid in mixer.js.
const FACEPLATE_BASE = new URL('../faceplate/', document.currentScript?.src || location.href).href;
const FACEPLATE_FILES = new Map();   // path -> promise of parsed JSON, or null

function insertParamText(field, value) {
  const scale = Number(field.scale) || 1;
  const shown = scale === 1 ? String(value) : (value * scale).toFixed(String(scale).split('.')[1]?.length || 0);
  return field.unit ? `${shown} ${field.unit}` : shown;
}

function faceplateFile(path) {
  // shortcut: a failed load stays "missing" until the page is reloaded.
  if (!FACEPLATE_FILES.has(path)) {
    FACEPLATE_FILES.set(path, fetch(FACEPLATE_BASE + path)
      .then(response => response.ok ? response.json() : null).catch(() => null));
  }
  return FACEPLATE_FILES.get(path);
}

function faceplateSprite(manifest, control) {
  const knob = manifest.knobs?.[control.style], sw = manifest.switches?.[control.style];
  const pick = control.kind === 'toggle' ? (sw || knob) : (knob || sw);
  if (!pick) return null;
  return pick === knob ? {file: pick.file, count: pick.frames, aspect: 1, grow: pick.size / (pick.knob || pick.size)}
    : {file: pick.file, count: pick.states, aspect: pick.w / pick.h, grow: 1.8};  // switch frames have wide margins
}

// {layout, manifest} when the effect has a layout whose every style exists, else null.
async function loadFaceplate(type) {
  const [manifest, layout] = await Promise.all([faceplateFile('manifest.json'), faceplateFile(`layouts/${type}.json`)]);
  if (!manifest || !layout || !Array.isArray(layout.controls)) return null;
  if (!(layout.width > 0 && layout.height > 0) || !manifest.plates?.[layout.plate]) return null;
  const sound = layout.controls.every(control => ['knob', 'stepped', 'toggle'].includes(control.kind)
    && control.size > 0 && faceplateSprite(manifest, control)?.count > 0);
  return sound ? {layout, manifest} : null;
}

// Vertical drag (shift = fine) and one step per wheel notch, on a range input.
function insertKnobGestures(cell, body, input, field, changed) {
  const span = (field.max - field.min) || 1;
  const clamp = value => Math.max(field.min, Math.min(field.max, value));
  body.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    const startY = event.clientY, startValue = +input.value, id = event.pointerId;
    body.setPointerCapture(id);
    const move = next => {
      if (next.pointerId !== id) return;
      const perPixel = span / (next.shiftKey ? 1200 : 240);
      input.value = Math.round(clamp(startValue + (startY - next.clientY) * perPixel));
      changed();
    };
    const done = next => {
      if (next.pointerId !== id) return;
      body.removeEventListener('pointermove', move);
      body.removeEventListener('pointerup', done);
      body.removeEventListener('pointercancel', done);
    };
    body.addEventListener('pointermove', move);
    body.addEventListener('pointerup', done);
    body.addEventListener('pointercancel', done);
  });
  cell.addEventListener('wheel', event => {
    event.preventDefault();
    input.value = clamp(+input.value - Math.sign(event.deltaY));
    changed();
  }, {passive: false});
}

function faceplateControl(face, control, field, onChange) {
  const sprite = faceplateSprite(face.manifest, control);
  const toggle = control.kind === 'toggle';
  const span = (field.max - field.min) || 1;
  const cell = document.createElement('label');
  cell.className = `fp-control fp-${control.kind}`;
  cell.style.left = `${control.x}px`;
  cell.style.top = `${control.y}px`;
  cell.style.setProperty('--fp-half', `${control.size / 2}px`);
  cell.innerHTML = '<span class="fp-body" aria-hidden="true"></span><input>'
    + '<span class="fp-caption"><span class="fp-label"></span><output></output></span>';
  // The caption may be as wide as the gap to the nearest control beside this one, then it wraps.
  const beside = face.layout.controls.filter(other => other !== control && other.x !== control.x
    && Math.abs(other.y - control.y) < (other.size + control.size) / 2 + 30).map(other => Math.abs(other.x - control.x));
  cell.querySelector('.fp-caption').style.width = `${Math.min(face.layout.width, ...beside) - 6}px`;
  const body = cell.querySelector('.fp-body'), input = cell.querySelector('input'), output = cell.querySelector('output');
  // A knob frame is larger than the knob drawn in it (room for its shadow); size means the knob.
  const drawn = control.size * (sprite.grow || 1);
  body.style.height = `${drawn}px`;
  body.style.width = `${drawn * sprite.aspect}px`;
  body.style.backgroundImage = `url("${FACEPLATE_BASE}${sprite.file}")`;
  body.style.backgroundSize = `${sprite.count * 100}% 100%`;
  cell.querySelector('.fp-label').textContent = control.label || field.label;
  input.setAttribute('aria-label', field.label);
  const read = () => toggle ? (input.checked ? field.max : field.min) : +input.value;
  const paint = () => {
    const value = read();
    const frame = Math.round((value - field.min) / span * (sprite.count - 1));
    body.style.backgroundPositionX = sprite.count > 1 ? `${frame / (sprite.count - 1) * 100}%` : '0';
    output.textContent = control.steps?.[value - field.min] ?? insertParamText(field, value);
    if (!toggle) input.setAttribute('aria-valuetext', output.textContent);
  };
  const changed = () => { paint(); onChange(field.key, read()); };
  if (toggle) {
    // A click anywhere on the label flips the checkbox; space does when focused.
    input.type = 'checkbox';
    input.setAttribute('role', 'switch');
    input.checked = field.value >= field.max;
    input.addEventListener('change', changed);
    cell.addEventListener('wheel', event => {
      event.preventDefault();
      const on = event.deltaY < 0;
      if (input.checked !== on) { input.checked = on; changed(); }
    }, {passive: false});
  } else {
    input.type = 'range'; input.step = 1;
    input.min = field.min; input.max = field.max; input.value = field.value;
    input.addEventListener('input', changed);
    insertKnobGestures(cell, body, input, field, changed);
  }
  paint();
  return cell;
}

// Draws the panel into `container` and returns the field keys it covers;
// the caller draws every other field its own way.
function renderFaceplate(container, face, fields, onChange) {
  const {layout, manifest} = face;
  const plateStyle = manifest.plates[layout.plate];
  const byKey = new Map(fields.map(field => [field.key, field]));
  const covered = new Set();
  const plate = document.createElement('div');
  plate.className = `fp-plate fp-marks-${plateStyle.marks === 'dark' ? 'dark' : 'light'}`;
  plate.style.width = `${layout.width}px`;
  plate.style.height = `${layout.height}px`;
  plate.style.backgroundImage = `url("${FACEPLATE_BASE}${plateStyle.file}")`;
  plate.style.backgroundSize = `${plateStyle.w}px ${plateStyle.h}px`;
  (layout.sections || []).forEach(section => {
    const group = document.createElement('div');
    group.className = 'fp-section';
    group.style.cssText = `left:${+section.x}px;top:${+section.y}px;width:${+section.w}px;height:${+section.h}px`;
    const caption = document.createElement('span');
    caption.textContent = section.label || '';
    group.append(caption);
    plate.append(group);
  });
  layout.controls.forEach(control => {
    const field = byKey.get(control.key);
    if (!field || covered.has(control.key)) return;
    covered.add(control.key);
    plate.append(faceplateControl(face, control, field, onChange));
  });
  container.classList.add('fp-frame');
  container.style.maxWidth = `${layout.width}px`;
  container.replaceChildren(plate);
  // The layout is fixed-size; narrower containers get the whole plate scaled down.
  const fit = () => {
    const scale = Math.min(1, container.clientWidth / layout.width) || 1;
    plate.style.setProperty('--fp-scale', scale);
    container.style.height = `${layout.height * scale}px`;
  };
  new ResizeObserver(fit).observe(container);
  fit();
  return covered;
}
