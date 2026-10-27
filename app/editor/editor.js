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
proj.cuts ||= []   // монтаж по тексту: вырезанные куски [[от, до], …] в исходном времени; реплики не сдвигаются

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
  clearTimeout(histT)
  histT = setTimeout(remember, 400)   // набор текста и перетаскивание — одним шагом отмены
}
async function save() { clearTimeout(saveT); await call('save', { proj }); status('Сохранено') }
const setStyle = (k, val) => { proj.style[k] = val; changed() }

// ---------- отмена: снимки реплик и стиля ----------
const snap = () => JSON.stringify({ cues: proj.cues, style: proj.style, cuts: proj.cuts })
let hist = [snap()], pos = 0, histT
function remember() {
  clearTimeout(histT)
  const s = snap()
  if (s === hist[pos]) return
  // сколько раз реплики правили руками — мера качества распознавания и перевода (tools/bench.py --edits)
  if (JSON.stringify(JSON.parse(hist[pos]).cues) !== JSON.stringify(proj.cues)) proj.edits = (proj.edits || 0) + 1
  hist.splice(pos + 1, Infinity, s)
  if (hist.length > 300) hist.shift()
  pos = hist.length - 1
}
function undo(d) {
  remember()   // недописанная правка — тоже шаг
  if (!hist[pos + d]) return status(d < 0 ? 'Отменять нечего' : 'Повторять нечего')
  pos += d
  Object.assign(proj, JSON.parse(hist[pos]))
  caret = null
  renderPresets()
  syncPanel()
  rerender(Math.min(Math.max(sel, 0), proj.cues.length - 1))
}

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
v.addEventListener('pause', () => { $('play').textContent = '▶︎'; stopAt = null })
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
    label(i)
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
const READ_CPS = 17   // столько знаков в секунду успевают прочитать; > 21 — уже не успевают
// слова, в которых Whisper не уверен и которые ещё стоят в русском тексте: там чаще всего ошибки распознавания
const clean = w => w.toLowerCase().replace(/[^\p{L}\d-]/gu, '')
const doubts = c => (c.words || []).filter(w => (w[3] ?? 1) < 0.65 && clean(w[2]).length > 1
  && (c.ru || '').toLowerCase().includes(clean(w[2])))
const problems = c => cps(c) > 21 || !c.en.trim() || c.stale || doubts(c).length > 0
function renderList() {
  const list = $('list')
  list.innerHTML = ''
  proj.cues.forEach((c, i) => {
    const el = document.createElement('div')
    el.className = 'cue'
    el.innerHTML = `<div class="t"><span class="tm"></span><span class="acts">
      <button data-a="split" title="Разрезать по курсору в тексте или по позиции видео (⌘B)">✂︎</button>
      <button data-a="merge" title="Склеить со следующей">⤓</button>
      <button data-a="retr" title="Перевести заново по русскому тексту">↻</button>
      <button data-a="cut" title="Вырезать из ролика / вернуть (⌘E)">⌁</button>
      <button data-a="del" title="Удалить реплику (⌘⌫)">✕</button></span></div>
      <div class="warn"></div>
      <textarea class="en" rows="1" spellcheck="true"></textarea><textarea class="ru" rows="1" spellcheck="false"></textarea>
      <div class="doubt"></div>`
    const [en, ru] = el.querySelectorAll('textarea'), warn = el.querySelector('.warn'), doubt = el.querySelector('.doubt')
    el.querySelector('.tm').textContent = `${fmtT(c.start)} – ${fmtT(c.end)}`
    el.querySelector('[data-a=merge]').hidden = i === proj.cues.length - 1
    el.onclick = e => {
      const a = e.target.dataset.a
      if (a === 'split') split(i); else if (a === 'merge') merge(i); else if (a === 'retr') retranslate(i, e.target)
      else if (a === 'del') remove(i)
      else if (a === 'cut') toggleCut(i)
      else if (a === 'short') retranslate(i, e.target, Math.max(10, Math.round(READ_CPS * (c.end - c.start))))
      else if (e.target === el || e.target.closest('.t')) select(i, true)
    }
    en.onkeyup = en.onclick = () => { caret = { i, pos: en.selectionStart } }
    el.check = () => {
      warn.innerHTML = (cps(c) > 21 ? '<button data-a="short" title="Перевести короче — под время реплики">⚡ быстро · сократить</button>' : '')
        + (c.stale ? '<button data-a="retr" title="Русский правили после перевода">RU изменён · перевести</button>' : '')
        + (c.en.trim() ? '' : '<button data-a="retr">нет перевода · перевести</button>')
      doubt.innerHTML = ''
      for (const w of doubts(c)) {
        const b = Object.assign(document.createElement('button'), { textContent: '? ' + clean(w[2]), title: 'Whisper не уверен — послушать слово' })
        b.onclick = e => { e.stopPropagation(); hear(ru, c, w) }
        doubt.append(b)
      }
      for (const w of fillers(c)) {
        const cut = isCut(w[0], w[1])
        const b = Object.assign(document.createElement('button'), { textContent: (cut ? '✓ ' : '⌁ ') + w[2], className: 'fill' + (cut ? ' cut' : ''),
          title: cut ? 'Слово вырезано — вернуть' : 'Слово-паразит — вырезать из ролика' })
        b.onclick = e => { e.stopPropagation(); cut ? unCut(w[0], w[1]) : cutWord(c, w) }
        doubt.append(b)
      }
      el.classList.toggle('bad', problems(c))
      el.classList.toggle('cut', isCut(c.start, c.end))
    }
    en.value = c.en
    ru.value = c.ru || ''
    en.oninput = () => { c.en = en.value; grow(en); el.check(); label(i); changed(); bar() }
    ru.oninput = () => { c.ru = ru.value; c.stale = true; grow(ru); el.check(); changed(); bar() }
    for (const ta of [en, ru]) {
      ta.onfocus = () => { select(i, true); ta.was = ta.value }
      ta.onchange = () => offer(ta.was, ta.value, i)
    }
    list.append(el)
    el.check()
  })
  bar()
  requestAnimationFrame(() => list.querySelectorAll('textarea').forEach(grow))
}
// сомнительное слово: выделить в русском тексте и проиграть его
let stopAt = null
function hear(ru, c, w) {
  const k = ru.value.toLowerCase().indexOf(clean(w[2]))
  ru.focus()
  ru.setSelectionRange(k, k + clean(w[2]).length)
  if (!init.video) return
  v.currentTime = Math.max(c.start, w[0] - 0.3)
  // после play: pause от select() при фокусе придёт раньше и не сбросит остановку
  v.play().then(() => { stopAt = w[1] + 0.3 })
}

// ---------- монтаж по тексту: вырезки ----------
const fmtS = t => t.toFixed(1).replace('.', ',') + ' с'
const cutTotal = () => proj.cuts.reduce((t, [a, b]) => t + b - a, 0)
const cutAt = t => proj.cuts.find(([a, b]) => t >= a && t < b)
const isCut = (a, b) => proj.cuts.some(([x, y]) => x <= a + 0.05 && y >= b - 0.05)   // кусок целиком внутри вырезки
function normCuts() {
  const cs = proj.cuts.map(([a, b]) => [Math.max(0, a), Math.min(duration(), b)]).filter(([a, b]) => b - a > 0.01).sort((x, y) => x[0] - y[0])
  proj.cuts = cs.reduce((o, [a, b]) => { const l = o.at(-1); if (l && a <= l[1] + 0.01) l[1] = Math.max(l[1], b); else o.push([a, b]); return o }, [])
}
function afterCuts() {
  normCuts()
  renderCuts()
  ;[...$('list').children].forEach(el => el.check())
  const n = cutTotal()
  $('cutn').hidden = !n
  $('cutn').textContent = `вырезано ${fmtS(n)}`
  changed()
}
function addCut(a, b) { proj.cuts.push([+a.toFixed(2), +b.toFixed(2)]); afterCuts() }
function unCut(a, b) {   // вернуть кусок: выкусить [a, b] из вырезок
  proj.cuts = proj.cuts.flatMap(([x, y]) => x >= b || y <= a ? [[x, y]] : [[x, Math.max(x, a)], [Math.min(y, b), y]])
  afterCuts()
}
function toggleCut(i) {
  const c = proj.cues[i]
  if (!c) return
  isCut(c.start, c.end) ? unCut(c.start, c.end) : addCut(c.start, c.end)
}
// слова-паразиты по словам Whisper; «как бы» и «это самое» — парой
const FILLERS = new Set(['э', 'ээ', 'эм', 'мм', 'ну', 'вот', 'типа', 'короче', 'значит'])
const PAIRS = [['как', 'бы'], ['это', 'самое']]
function fillers(c) {
  const ws = c.words || [], out = []
  for (let k = 0; k < ws.length; k++) {
    const w = clean(ws[k][2]), nx = ws[k + 1] && clean(ws[k + 1][2])
    const pair = PAIRS.find(([a, b]) => a === w && b === nx)
    if (pair) { out.push([ws[k][0], ws[k + 1][1], pair.join(' ')]); k++ }
    else if (FILLERS.has(w)) out.push([ws[k][0], ws[k][1], w])
  }
  return out.filter(w => (c.ru || '').toLowerCase().includes(w[2]))
}
function cutWord(c, w) {
  // из русского текста слово тоже убираем, чтобы субтитр не показывал то, чего уже не слышно
  const rx = new RegExp(`(^|\\s)${esc(w[2])}[,.!?…]*(?=\\s|$)`, 'i')
  c.ru = (c.ru || '').replace(rx, '$1').replace(/\s{2,}/g, ' ').trim()
  const ta = $('list').children[proj.cues.indexOf(c)]?.querySelector('.ru')
  if (ta) { ta.value = c.ru; grow(ta) }
  addCut(w[0], w[1])
}
// соседние реплики с почти тем же текстом — дубль, остаётся последняя
const toks = t => (t || '').toLowerCase().split(/\s+/).map(clean).filter(Boolean)
function similar(a, b) {
  const A = toks(a), B = toks(b)
  if (A.length < 3) return false
  if (A.every((w, k) => B[k] === w)) return true   // начал, сбился, начал заново
  return A.filter(w => B.includes(w)).length / Math.max(A.length, B.length) >= 0.7
}
function autocut() {
  const n = { pause: 0, filler: 0, dup: 0 }, sp = proj.speech || [], D = duration()
  // паузы длиннее секунды: режем, оставляя по 0,25 с воздуха с каждой стороны
  let prev = 0
  for (const [a, b] of [...sp, [D, D]]) {
    if (a - prev > 1 && !isCut(prev + 0.25, a - 0.25)) { proj.cuts.push([prev ? prev + 0.25 : 0, a === D ? D : a - 0.25]); n.pause++ }
    prev = b
  }
  for (const c of proj.cues) for (const w of fillers(c)) if (!isCut(w[0], w[1])) { cutWord(c, w); n.filler++ }
  for (let i = 0; i + 1 < proj.cues.length; i++) {
    const c = proj.cues[i]
    if (similar(c.ru, proj.cues[i + 1].ru) && !isCut(c.start, c.end)) { proj.cuts.push([c.start, c.end]); n.dup++ }
  }
  afterCuts()
  status(`Вырезано: пауз ${n.pause}, слов-паразитов ${n.filler}, дублей ${n.dup} — всего ${fmtS(cutTotal())}. ⌘Z — отменить`)
}
$('autocut').onclick = autocut
function renderCuts() {
  $('cuts').innerHTML = proj.cuts.map(([a, b]) => `<i style="left:${pct(a)};width:${pct(b - a)}" title="Вырезано ${fmtS(b - a)} — клик вернёт"></i>`).join('')
}

// ---------- панель над списком: поиск и замена, проблемные, пакетный перевод ----------
const esc = t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const findRx = (f = 'i') => $('find').value ? new RegExp(esc($('find').value), f) : null
function bar() {
  const rx = findRx(), only = $('onlybad').checked
  const hit = c => !rx || rx.test(c.en) || rx.test(c.ru || '')
  let found = 0
  proj.cues.forEach((c, i) => {
    const h = hit(c), el = $('list').children[i]
    found += !!(rx && h)
    if (el) el.hidden = !h || only && !problems(c)
  })
  const bad = proj.cues.filter(problems).length, stale = proj.cues.filter(c => c.stale).length
  $('badn').textContent = bad ? `(${bad})` : ''
  $('replrow').hidden = $('remrow').hidden = !rx
  $('replall').textContent = `Заменить (${found})`
  $('replall').disabled = !found
  $('retrall').hidden = !stale
  $('retrall').textContent = `↻ Перевести изменённые (${stale})`
}
$('find').oninput = $('onlybad').onchange = bar
$('find').onkeydown = e => { if (e.key === 'Escape') { $('find').value = ''; bar(); $('find').blur() } }
$('replall').onclick = async () => {
  const rx = findRx('gi'), to = $('repl').value
  let n = 0
  for (const c of proj.cues) {
    const en = c.en.replace(rx, to), ru = (c.ru || '').replace(rx, to)
    n += en !== c.en || ru !== (c.ru || '')
    Object.assign(c, { en, ru })
  }
  if ($('remember').checked && $('find').value.trim().length > 2) await call('remember_fix', { find: $('find').value.trim(), repl: to })
  $('find').value = ''
  rerender(Math.max(sel, 0))
  status(`Заменено в ${n} репл.` + ($('remember').checked ? ' · запомнено для новых видео' : ''))
}
// что правка поменяла в тексте: одно место до трёх слов, как word_fixes в srt_utils; короткое «было» не берём —
// замена подстрокой, «и → а» испортило бы весь текст
function wordFix(was, now) {
  const a = was.match(/[\p{L}\d-]+/gu) || [], b = now.match(/[\p{L}\d-]+/gu) || [], lo = s => s.toLowerCase()
  let i = 0, j = 0
  while (i < a.length && i < b.length && lo(a[i]) === lo(b[i])) i++
  while (j < a.length - i && j < b.length - i && lo(a.at(-1 - j)) === lo(b.at(-1 - j))) j++
  const x = a.slice(i, a.length - j), y = b.slice(i, b.length - j)
  return x.length && y.length && x.length <= 3 && y.length <= 3 && x.join(' ').length >= 4
    && lo(x.join(' ')) !== lo(y.join(' ')) ? [x.join(' '), y.join(' ')] : null
}
// поправил слово в одной реплике — предложить то же в остальных: имя или термин Whisper путает не один раз
function offer(was, now, i) {
  const f = wordFix(was || '', now), rx = f && new RegExp(esc(f[0]), 'i')
  const n = rx ? proj.cues.filter((c, k) => k !== i && (rx.test(c.en) || rx.test(c.ru || ''))).length : 0
  $('offer').hidden = !n
  if (!n) return
  $('offer').textContent = `«${f[0]}» → «${f[1]}» ещё в ${n} репл.`
  $('offer').onclick = () => {
    $('find').value = f[0]; $('repl').value = f[1]; $('offer').hidden = true
    bar(); $('replall').focus()
  }
}
$('retrall').onclick = async () => {
  $('retrall').disabled = true
  for (let i = 0; i < proj.cues.length; i++) if (proj.cues[i].stale) await retranslate(i, $('retrall'))
  $('retrall').disabled = false
}

// ---------- разрезать / склеить / перевести заново ----------
let caret = null   // где стоял курсор в английском тексте: по нему режем
function split(i) {
  const c = proj.cues[i], words = c.en.split(/\s+/).filter(Boolean)
  if (words.length < 2) return status('Разрезать нечего: в реплике одно слово')
  const starts = wordStarts(c, words)
  let k
  if (caret && caret.i === i) k = c.en.slice(0, caret.pos).split(/\s+/).filter(Boolean).length
  else if (v.currentTime > c.start && v.currentTime < c.end) k = starts.filter(t => t < v.currentTime).length
  else k = Math.round(words.length / 2)
  k = Math.min(words.length - 1, Math.max(1, k))
  const t = +starts[k].toFixed(2)
  // русский делим там же по времени: по словам Whisper, если текст не правили, иначе — по доле реплики
  const ru = (c.ru || '').split(/\s+/).filter(Boolean), rw = c.words || []
  let kr = rw.length === ru.length ? rw.filter(w => w[0] < t).length : Math.round(ru.length * (t - c.start) / (c.end - c.start))
  kr = ru.length > 1 ? Math.min(ru.length - 1, Math.max(1, kr)) : ru.length
  proj.cues.splice(i, 1,
    { ...c, end: t, en: words.slice(0, k).join(' '), ru: ru.slice(0, kr).join(' '), words: rw.filter(w => w[0] < t) },
    { ...c, start: t, en: words.slice(k).join(' '), ru: ru.slice(kr).join(' '), words: rw.filter(w => w[0] >= t) })
  caret = null
  rerender(i)
}
function merge(i) {
  const [a, b] = proj.cues.slice(i, i + 2)
  if (!b) return
  const join = (x, y) => [x, y].filter(Boolean).join(' ')
  proj.cues.splice(i, 2, { ...a, end: b.end, en: join(a.en, b.en), ru: join(a.ru, b.ru), words: [...(a.words || []), ...(b.words || [])] })
  rerender(i)
}
function remove(i) {
  proj.cues.splice(i, 1)
  rerender(Math.min(i, proj.cues.length - 1))
}
// новая реплика в паузе под курсором видео: Whisper пропустил фразу
function insert() {
  const t = v.currentTime, k = proj.cues.findIndex(c => c.end > t), next = proj.cues[k], prev = proj.cues[k < 0 ? proj.cues.length - 1 : k - 1]
  if (next && next.start <= t) return status('Поставьте курсор видео в паузу между репликами')
  const start = +Math.max(t, prev ? prev.end : 0).toFixed(2), end = +Math.min(start + 2, next ? next.start : duration()).toFixed(2)
  if (end - start < 0.3) return status('Пауза слишком короткая для реплики')
  const i = k < 0 ? proj.cues.length : k
  proj.cues.splice(i, 0, { start, end, ru: '', en: '', words: [] })
  rerender(i)
  $('list').children[i].querySelector('.en').focus()
}
// край выбранной реплики — на позицию видео
function setEdge(side) {
  const i = active >= 0 ? active : sel, c = proj.cues[i]
  if (!c) return
  const t = side === 'start' ? Math.min(Math.max(v.currentTime, proj.cues[i - 1]?.end ?? 0), c.end - 0.2)
                             : Math.max(Math.min(v.currentTime, proj.cues[i + 1]?.start ?? duration()), c.start + 0.2)
  c[side] = +t.toFixed(2)
  rerender(i)
}
function rerender(i) { renderList(); renderTrack(); active = -1; select(i, false); changed() }
async function retranslate(i, btn, limit = null) {
  btn.disabled = true
  status(limit ? 'Сокращаю перевод…' : `Перевожу реплику ${i + 1}…`)
  try {
    const c = proj.cues[i]
    c.en = await call('retranslate', { i, proj, limit })
    delete c.stale
    const el = $('list').children[i], ta = el?.querySelector('.en')
    if (ta) { ta.value = c.en; grow(ta); el.check() }
    label(i)
    changed()
    bar()
    status(limit && cps(c) > 21 ? 'Короче не вышло — поправьте вручную или растяните реплику' : 'Переведено')
  } catch (e) { status('Не перевелось: ' + (e.message || e)) }
  btn.disabled = false
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
let zoom = 1, edge = null
const pct = t => t / duration() * 100 + '%'
const place = (el, c) => { el.style.left = pct(c.start); el.style.width = pct(c.end - c.start) }
const label = i => { const b = $('blocks').children[i]; if (b) b.querySelector('span').textContent = proj.cues[i].en }
function renderTrack() {
  $('speech').innerHTML = (proj.speech || []).map(([a, b]) => `<i style="left:${pct(a)};width:${pct(b - a)}"></i>`).join('')
  $('blocks').innerHTML = proj.cues.map(() => '<div class="b"><i class="e l"></i><span></span><i class="e r"></i></div>').join('')
  ;[...$('blocks').children].forEach((el, i) => { place(el, proj.cues[i]); label(i) })
  renderCuts()
  mark()
}
let range = null   // ⌥-перетаскивание по дорожке — вырезать кусок
const timeAt = x => { const r = $('lane').getBoundingClientRect(); return Math.min(duration(), Math.max(0, (x - r.left) / r.width * duration())) }
$('track').addEventListener('pointerdown', e => {
  const b = e.target.closest('.b'), i = b ? [...$('blocks').children].indexOf(b) : -1
  const cut = e.target.closest('#cuts i')
  if (cut) {
    const k = [...$('cuts').children].indexOf(cut)
    unCut(...proj.cuts[k])
    return status('Кусок возвращён в ролик')
  }
  if (e.altKey) {
    range = { t0: timeAt(e.clientX), el: Object.assign(document.createElement('i'), { className: 'tmp' }) }
    $('cuts').append(range.el)
    $('track').setPointerCapture(e.pointerId)
    v.pause()
    return
  }
  if (e.target.classList.contains('e')) {
    // край реплики: тянем, не залезая на соседние; видео встаёт на край — видно, где начинается фраза
    const side = e.target.classList.contains('l') ? 'start' : 'end'
    edge = { i, side, t0: proj.cues[i][side], x0: timeAt(e.clientX), cx: e.clientX }   // сдвиг от точки захвата: край не прыгает под курсор
    $('track').setPointerCapture(e.pointerId)
    v.pause()
    select(i, false)
  } else {
    v.currentTime = timeAt(e.clientX)
    if (i >= 0) select(i, false)
  }
})
$('track').addEventListener('pointermove', e => {
  if (range) {
    const t = timeAt(e.clientX), a = Math.min(range.t0, t), b = Math.max(range.t0, t)
    Object.assign(range, { a, b })
    range.el.style.left = pct(a); range.el.style.width = pct(b - a)
    v.currentTime = t
    return
  }
  if (!edge || !edge.moved && Math.abs(e.clientX - edge.cx) < 3) return   // клик по краю — ещё не перетаскивание
  edge.moved = true
  const c = proj.cues[edge.i], prev = proj.cues[edge.i - 1], next = proj.cues[edge.i + 1]
  let t = edge.t0 + timeAt(e.clientX) - edge.x0
  // липнем к ближайшему началу или концу голоса, если он ближе 6 px
  const px = duration() / $('lane').clientWidth * 6
  const near = (proj.speech || []).flat().reduce((m, s) => Math.abs(s - t) < Math.abs(m - t) ? s : m, Infinity)
  if (Math.abs(near - t) < px) t = near
  t = edge.side === 'start' ? Math.min(Math.max(t, prev ? prev.end : 0), c.end - 0.2)
                            : Math.max(Math.min(t, next ? next.start : duration()), c.start + 0.2)
  c[edge.side] = +t.toFixed(2)
  place($('blocks').children[edge.i], c)
  $('list').children[edge.i].querySelector('.tm').textContent = `${fmtT(c.start)} – ${fmtT(c.end)}`
  v.currentTime = t
  changed()
})
$('track').addEventListener('pointerup', () => {
  if (range) {
    range.el.remove()
    if (range.b - range.a > 0.1) { addCut(range.a, range.b); status(`Вырезано ${fmtS(range.b - range.a)}`) }
    range = null
  }
  edge = null
  remember()
})
// колёсико — масштаб вокруг курсора, тачпад вбок — прокрутка
$('track').addEventListener('wheel', e => {
  if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) return
  e.preventDefault()
  const tr = $('track'), t = timeAt(e.clientX), x = e.clientX - tr.getBoundingClientRect().left
  zoom = Math.min(40, Math.max(1, zoom * Math.exp(-e.deltaY / 300)))
  $('lane').style.width = zoom * 100 + '%'
  tr.scrollLeft = t / duration() * $('lane').clientWidth - x
}, { passive: false })
v.addEventListener('loadedmetadata', renderTrack)

function tick() {
  const t = v.currentTime || 0
  $('time').textContent = fmtT(t)
  $('head').style.left = pct(t)
  if (!v.paused && zoom > 1) {   // при воспроизведении дорожка едет за курсором
    const tr = $('track'), x = t / duration() * $('lane').clientWidth
    if (x < tr.scrollLeft || x > tr.scrollLeft + tr.clientWidth) tr.scrollLeft = x - 40
  }
  const a = proj.cues.findIndex(c => t >= c.start && t < c.end)
  if (a !== active) {
    active = a
    mark()
    if (a >= 0 && !v.paused) $('list').children[a]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }
  if (!drag && st().words && !v.paused) drawBox()
  if (stopAt && t >= stopAt) { v.pause(); stopAt = null }
  // при просмотре вырезанное пропускаем — видно, как ролик будет смонтирован
  const skip = !v.paused && !range && cutAt(t)
  if (skip) v.currentTime = Math.min(skip[1] + 0.001, duration())
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
  if (state === 'soft') {
    // превью обычно играет этот же файл, а байты в нём уже другие — перечитать с того же места
    if (init.video) {
      const t = v.currentTime
      v.addEventListener('loadedmetadata', () => { v.currentTime = t }, { once: true })
      v.src = 'kadr://video/clip'
    }
    return status(`Дорожки субтитров обновлены: ${text}. Прожигаю…`)
  }
  $('rebuild').disabled = false
  status(state === 'done' ? `Готово: ${text}` : `Ошибка: ${text}`)
}
$('reveal').onclick = () => call('reveal')
$('toref').onclick = async () => {
  clearTimeout(saveT)
  status('Сохраняю эталон…')
  try { status(`В эталонах: ${await call('to_refs', { proj })} — проверка в «Обучении модели»`) }
  catch (e) { status(`Ошибка: ${e.message || e}`) }
}

$('add').onclick = insert
const inField = () => /TEXTAREA|INPUT|SELECT/.test(document.activeElement.tagName)
addEventListener('keydown', e => {
  if (e.metaKey && e.key === 's') { e.preventDefault(); save() }
  else if (e.metaKey && e.key === 'f') { e.preventDefault(); $('find').focus(); $('find').select() }
  else if (e.metaKey && e.key.toLowerCase() === 'z' && document.activeElement.id !== 'pname') { e.preventDefault(); undo(e.shiftKey ? 1 : -1) }
  else if (e.metaKey && e.key === 'Backspace' && !inField()) { e.preventDefault(); if (sel >= 0) remove(sel) }
  else if (e.metaKey && e.key === 'e') { e.preventDefault(); toggleCut(active >= 0 ? active : sel) }
  else if (inField() || e.metaKey) return
  else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
    e.preventDefault()
    const i = Math.min(proj.cues.length - 1, Math.max(0, (active >= 0 ? active : sel) + (e.key === 'ArrowUp' ? -1 : 1)))
    select(i, true)
    $('list').children[i]?.scrollIntoView({ block: 'nearest' })
  }
  else if (e.key === '[') setEdge('start')
  else if (e.key === ']') setEdge('end')
  else if (e.metaKey && e.key === 'b') {
    e.preventDefault()
    const i = caret && document.activeElement.classList.contains('en') ? caret.i : active >= 0 ? active : sel
    if (i >= 0) split(i)
  }
  else if (e.key === ' ') { e.preventDefault(); togglePlay() }
})

renderList()
renderPresets()
syncPanel()
renderTrack()
layout()
$('cutn').hidden = !cutTotal()
$('cutn').textContent = `вырезано ${fmtS(cutTotal())}`
if (proj.cues.length) select(0, false)
requestAnimationFrame(tick)
status('Готов')
console.log(`editor ready: ${proj.cues.length} cues, ${proj.format}, video=${init.video}`)
