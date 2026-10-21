// Редактор субтитров: список реплик, видео с превью (libass в wasm — тот же рендер, что у прожига), стиль.
// ASS собирает Python (app/style.py) — страница только шлёт ему проект и показывает результат.
import JASSUB from './vendor/jassub/jassub.js'

const $ = id => document.getElementById(id)
// туда и обратно — JSON-строки: вложенные объекты из WebKit приходят в Python как NSDictionary
const call = (cmd, data = {}) => webkit.messageHandlers.kadr.postMessage(JSON.stringify({ cmd, ...data })).then(JSON.parse)
const url = p => new URL(p, location).href
const v = $('v'), stage = $('stage'), box = $('box'), inline = $('inline')

// безопасные зоны, % кадра: интерфейс площадок перекрывает края. Значения примерные — по шаблонам площадок
const ZONES = {
  '9:16': { reels: ['Instagram Reels', 11.5, 22, 4, 11], tiktok: ['TikTok', 7, 25, 5.5, 13], shorts: ['YouTube Shorts', 10, 18, 4, 12] },
  '16:9': { youtube: ['YouTube', 6, 13, 3, 3], title: ['Безопасная зона 10%', 10, 10, 10, 10] },
  '1:1': { feed: ['Лента 5%', 5, 5, 5, 5] },
}

const init = await call('init')
let { project: proj, presets, defaults } = init
let jassub = null, sel = -1, active = -1, drag = null

$('title').textContent = init.title
$('fmt').textContent = `${proj.format} · ${proj.width}×${proj.height}`
// шрифты и в CSS: для выпадающего списка и чтобы мерить ширину текста под рамку
document.head.append(Object.assign(document.createElement('style'), {
  textContent: Object.entries(init.fonts).map(([k, f]) => `@font-face{font-family:"K ${k}";src:url("fonts/${f}")}`).join(''),
}))

const st = () => {
  const s = { ...(presets[proj.style.preset] || presets.classic), ...proj.style }
  if (typeof s.y === 'object') s.y = s.y[proj.format]
  return s
}
const fmtT = t => `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, '0')}`
const status = t => { $('status').textContent = t }

// ---------- рендер и сохранение ----------
let queued = false, saveT
function changed() {
  if (!queued) {
    queued = true
    requestAnimationFrame(async () => {
      queued = false
      const ass = await call('ass', { proj })
      if (jassub) { await jassub.renderer.setTrack(ass); if (v.paused) jassub.resize(true) }
      drawBox()
    })
  }
  clearTimeout(saveT)
  status('Есть изменения…')
  saveT = setTimeout(save, 800)
}
async function save() { clearTimeout(saveT); await call('save', { proj }); status('Сохранено') }
const setStyle = (k, val) => { proj.style[k] = val; changed() }

// ---------- видео ----------
if (init.video) {
  v.src = 'kadr://video/clip'
  jassub = new JASSUB({
    video: v, subContent: init.ass,
    workerUrl: url('vendor/jassub/jassub-worker.js'), wasmUrl: url('vendor/jassub/jassub-worker.wasm'),
    modernWasmUrl: url('vendor/jassub/jassub-worker-modern.wasm'),
    fonts: [...new Set(Object.values(init.fonts))].map(f => url('fonts/' + f)),
  })
} else {
  stage.hidden = true
  $('novideo').hidden = false
}
const duration = () => v.duration || proj.duration || 1
const togglePlay = () => v.paused ? v.play() : v.pause()
v.addEventListener('play', () => { $('play').textContent = '❚❚' })
v.addEventListener('pause', () => { $('play').textContent = '▶︎' })
$('play').onclick = togglePlay

function layout() {
  const a = $('area'), W = a.clientWidth - 32, H = a.clientHeight - 32, r = proj.width / proj.height
  let w = W, h = W / r
  if (h > H) { h = H; w = H * r }
  stage.style.width = w + 'px'
  stage.style.height = h + 'px'
  drawGrid()
  drawBox()
}
new ResizeObserver(layout).observe($('area'))
// ширину рамки меряем нашим шрифтом: пока он не загружен, canvas мерит запасным
document.fonts.addEventListener('loadingdone', () => drawBox())

// ---------- рамка субтитра: перетаскивание и правка на кадре ----------
const measure = document.createElement('canvas').getContext('2d')
// начала слов перевода — та же раскладка, что style._timed_words: по слогам, только там, где звучит голос
function wordStarts(c, words) {
  let spans = (proj.speech || []).filter(([a, b]) => b > c.start && a < c.end)
    .map(([a, b]) => [Math.max(a, c.start), Math.min(b, c.end)])
  if (spans.reduce((t, [a, b]) => t + b - a, 0) < 0.3) {
    const ru = c.words || [], a = ru.length ? Math.max(ru[0][0], c.start) : c.start
    spans = [[a, Math.min(Math.max(ru.length ? ru.at(-1)[1] : c.end, a + 0.3), c.end)]]
  }
  const total = spans.reduce((t, [a, b]) => t + b - a, 0)
  const syl = words.map(w => Math.max(1, (w.toLowerCase().match(/[aeiouyаеёиоуыэюя]+/g) || []).length))
  const sum = syl.reduce((x, y) => x + y, 0)
  let acc = 0
  return syl.map(n => {
    let x = total * acc / sum
    acc += n
    for (const [a, b] of spans) { if (x < b - a) return a + x; x -= b - a }
    return spans.at(-1)[1]
  })
}
function shownText(c, s) {
  if (!s.words) return c.en
  // «слов на экране»: та группа, что звучит сейчас
  const words = c.en.split(/\s+/).filter(Boolean), n = s.words
  const k = Math.max(0, wordStarts(c, words).findLastIndex(t => t <= v.currentTime))
  const g = Math.floor(k / n) * n
  return words.slice(g, g + n).join(' ')
}
function drawBox() {
  const c = proj.cues[active >= 0 ? active : sel]
  if (!c || !init.video || inline.hidden === false) { box.hidden = true; return }
  const s = st(), W = stage.clientWidth, H = stage.clientHeight, k = W / proj.width
  const size = Math.min(proj.width, proj.height) * s.size / 100 * k
  let text = shownText(c, s)
  if (s.upper) text = text.toUpperCase()
  measure.font = `${size}px "K ${s.font}"`
  if (!document.fonts.check(measure.font)) document.fonts.load(measure.font)
  // перенос, как у libass: по словам в 90% ширины
  const lines = [''], maxW = W * 0.9
  for (const w of text.split(/\s+/)) {
    const t = lines.at(-1) ? lines.at(-1) + ' ' + w : w
    if (measure.measureText(t).width > maxW && lines.at(-1)) lines.push(w); else lines[lines.length - 1] = t
  }
  const width = Math.max(...lines.map(l => measure.measureText(l).width)) + size * 0.6
  const height = lines.length * size * 1.22 + (s.bilingual && c.ru ? size * 0.9 : 0) + size * 0.3
  const cx = W * s.x / 100, bottom = H * s.y / 100
  Object.assign(box.style, { left: cx - width / 2 + 'px', top: bottom - height + 'px', width: width + 'px', height: height + 'px' })
  box.hidden = false
}
box.addEventListener('pointerdown', e => {
  const s = st()
  drag = { x: e.clientX, y: e.clientY, sx: s.x, sy: s.y }
  box.setPointerCapture(e.pointerId)
  box.classList.add('drag')
  e.stopPropagation()
})
box.addEventListener('pointermove', e => {
  if (!drag) return
  const clamp = (a, lo, hi) => Math.round(Math.min(hi, Math.max(lo, a)) * 2) / 2
  proj.style.x = clamp(drag.sx + (e.clientX - drag.x) / stage.clientWidth * 100, 5, 95)
  proj.style.y = clamp(drag.sy + (e.clientY - drag.y) / stage.clientHeight * 100, 5, 99)
  syncPanel()
  changed()
})
box.addEventListener('pointerup', () => { drag = null; box.classList.remove('drag') })
box.addEventListener('dblclick', e => {
  const i = active >= 0 ? active : sel
  if (i < 0) return
  v.pause()
  Object.assign(inline.style, { left: box.style.left, top: box.style.top, width: box.style.width, height: box.style.height })
  inline.value = proj.cues[i].en
  inline.dataset.i = i
  inline.hidden = false
  box.hidden = true
  inline.focus()
  inline.select()
  e.stopPropagation()
})
function closeInline(commit) {
  if (inline.hidden) return
  const i = +inline.dataset.i
  inline.hidden = true
  if (commit && inline.value.trim() !== proj.cues[i].en) {
    proj.cues[i].en = inline.value.trim()
    const ta = $('list').children[i]?.querySelector('.en')
    if (ta) { ta.value = proj.cues[i].en; grow(ta) }
    changed()
  }
  drawBox()
}
inline.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); closeInline(true) }
  if (e.key === 'Escape') closeInline(false)
  e.stopPropagation()
})
inline.addEventListener('blur', () => closeInline(true))
stage.addEventListener('click', e => { if (e.target === v || e.target === stage) togglePlay() })

// ---------- сетка безопасных зон ----------
const zones = ZONES[proj.format] || {}
$('gridsel').innerHTML = '<option value="">Без сетки</option>' +
  Object.entries(zones).map(([id, z]) => `<option value="${id}">${z[0]}</option>`).join('')
try { $('gridsel').value = localStorage.getItem('grid-' + proj.format) || '' } catch {}
$('gridsel').onchange = () => { try { localStorage.setItem('grid-' + proj.format, $('gridsel').value) } catch {} drawGrid() }
function drawGrid() {
  const z = zones[$('gridsel').value], g = $('grid')
  g.innerHTML = ''
  if (!z) return
  const [, t, b, l, r] = z
  for (const [css] of [[`left:0;right:0;top:0;height:${t}%`], [`left:0;right:0;bottom:0;height:${b}%`],
    [`left:0;top:${t}%;bottom:${b}%;width:${l}%`], [`right:0;top:${t}%;bottom:${b}%;width:${r}%`]]) {
    g.insertAdjacentHTML('beforeend', `<div class="zone" style="${css}"></div>`)
  }
  g.insertAdjacentHTML('beforeend', '<div class="mid" style="left:50%;top:0;bottom:0;width:1px"></div>')
}

// ---------- список реплик ----------
const grow = ta => { ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 'px' }
const cps = c => c.en.length / Math.max(0.1, c.end - c.start)
function renderList() {
  const list = $('list')
  list.innerHTML = ''
  proj.cues.forEach((c, i) => {
    const el = document.createElement('div')
    el.className = 'cue'
    el.innerHTML = `<div class="t"><span>${fmtT(c.start)} – ${fmtT(c.end)}</span><span class="warn"></span></div>
      <textarea class="en" rows="1" spellcheck="true"></textarea><textarea class="ru" rows="1" spellcheck="false"></textarea>`
    const [en, ru] = el.querySelectorAll('textarea'), warn = el.querySelector('.warn')
    const check = () => { warn.textContent = cps(c) > 21 ? '⚡ быстро для чтения' : '' }
    en.value = c.en
    ru.value = c.ru || ''
    en.oninput = () => { c.en = en.value; grow(en); check(); $('blocks').children[i] && ($('blocks').children[i].textContent = c.en); changed() }
    ru.oninput = () => { c.ru = ru.value; grow(ru); changed() }
    for (const ta of [en, ru]) ta.onfocus = () => select(i, true)
    el.onclick = e => { if (e.target === el || e.target.closest('.t')) select(i, true) }
    list.append(el)
    check()
  })
}
// высоту полей считаем, когда у списка уже есть ширина — и заново при каждом её изменении
new ResizeObserver(() => $('list').querySelectorAll('textarea').forEach(grow)).observe($('list'))
function select(i, seek) {
  sel = i
  mark()
  if (seek && init.video && proj.cues[i]) { v.pause(); v.currentTime = proj.cues[i].start + 0.01 }
}
function mark() {
  const on = active >= 0 ? active : sel
  ;[...$('list').children].forEach((el, i) => el.classList.toggle('on', i === on))
  ;[...$('blocks').children].forEach((el, i) => el.classList.toggle('on', i === on))
}

// ---------- дорожка ----------
function renderTrack() {
  const d = duration()
  $('blocks').innerHTML = proj.cues.map(c =>
    `<div class="b" style="left:${c.start / d * 100}%;width:${(c.end - c.start) / d * 100}%"></div>`).join('')
  ;[...$('blocks').children].forEach((el, i) => { el.textContent = proj.cues[i].en })
}
$('track').addEventListener('pointerdown', e => {
  const r = $('track').getBoundingClientRect()
  v.currentTime = (e.clientX - r.left) / r.width * duration()
})
v.addEventListener('loadedmetadata', renderTrack)

function tick() {
  const t = v.currentTime || 0
  $('time').textContent = fmtT(t)
  $('head').style.left = t / duration() * 100 + '%'
  const a = proj.cues.findIndex(c => t >= c.start && t < c.end)
  if (a !== active) {
    active = a
    mark()
    if (a >= 0 && !v.paused) $('list').children[a]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }
  if (!drag && st().words && !v.paused) drawBox()
  requestAnimationFrame(tick)
}

// ---------- стиль ----------
function renderPresets() {
  $('presets').innerHTML = ''
  for (const [id, p] of Object.entries(presets)) {
    const b = document.createElement('button')
    b.textContent = p.name
    b.style.fontFamily = `"K ${p.font}"`
    b.classList.toggle('on', id === proj.style.preset)
    b.onclick = () => { proj.style = { preset: id }; renderPresets(); syncPanel(); changed() }
    $('presets').append(b)
  }
  const isDef = defaults[proj.format] === proj.style.preset
  $('makedefault').textContent = isDef ? `✓ По умолчанию для ${proj.format}` : `Сделать по умолчанию для ${proj.format}`
  $('makedefault').disabled = isDef
}
$('makedefault').onclick = async () => {
  defaults = await call('set_default', { fmt: proj.format, preset: proj.style.preset })
  renderPresets()
}
$('font').innerHTML = Object.keys(init.fonts).map(f => `<option style='font-family:"K ${f}"'>${f}</option>`).join('')
const inputs = [...document.querySelectorAll('#panel [data-k]')]
function syncPanel() {
  const s = st()
  for (const el of inputs) {
    const val = s[el.dataset.k]
    if (el.type === 'checkbox') el.checked = !!val; else el.value = val
    const out = el.type === 'range' && el.parentElement.querySelector('output')
    if (out) out.textContent = el.dataset.k === 'words' && !+val ? 'вся реплика' : val
  }
  $('hl-on').checked = !!s.highlight
  $('hl-color').value = s.highlight || '#ffd400'
}
for (const el of inputs) {
  el.addEventListener('input', () => {
    const val = el.type === 'checkbox' ? el.checked : el.type === 'range' ? +el.value : el.value
    setStyle(el.dataset.k, val)
    syncPanel()
  })
}
const setHl = () => { setStyle('highlight', $('hl-on').checked ? $('hl-color').value : null) }
$('hl-on').onchange = setHl
$('hl-color').oninput = () => { $('hl-on').checked = true; setHl() }

$('savepreset').onclick = () => { $('pname').value = ''; $('namedlg').showModal() }
$('namedlg').addEventListener('close', async () => {
  if ($('namedlg').returnValue !== 'ok' || !$('pname').value.trim()) return
  const s = st(), base = presets[proj.style.preset] || presets.classic
  const { preset, name, ...values } = s
  values.y = { ...(typeof base.y === 'object' ? base.y : {}), [proj.format]: s.y }   // y храним по форматам
  const r = await call('save_preset', { name: $('pname').value.trim(), values })
  presets = r.presets
  defaults = r.defaults
  proj.style = { preset: r.id }
  renderPresets()
  syncPanel()
  changed()
})

// ---------- пересборка ----------
$('rebuild').onclick = async () => {
  $('rebuild').disabled = true
  status('Пересобираю видео…')
  clearTimeout(saveT)
  await call('rebuild', { proj })
}
window.onRebuild = (state, text) => {
  $('rebuild').disabled = false
  status(state === 'done' ? `Готово: ${text}` : `Ошибка: ${text}`)
}
$('reveal').onclick = () => call('reveal')

addEventListener('keydown', e => {
  if (e.metaKey && e.key === 's') { e.preventDefault(); save() }
  else if (e.key === ' ' && !/TEXTAREA|INPUT|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); togglePlay() }
})

renderList()
renderPresets()
syncPanel()
renderTrack()
layout()
if (proj.cues.length) select(0, false)
requestAnimationFrame(tick)
status('Готов')
console.log(`editor ready: ${proj.cues.length} cues, ${proj.format}, video=${init.video}`)
