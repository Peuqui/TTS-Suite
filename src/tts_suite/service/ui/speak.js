import { service } from './api.js'
import { cardTable, h, replaceChildren } from './dom.js'
import { hostLabel, t } from './i18n.js'
import { Player } from './player.js'

const UNITS = ['sentence', 'paragraph', 'whole']
const UNIT_KEYS = { sentence: 'unitSentence', paragraph: 'unitParagraph', whole: 'unitWhole' }

// Trying a reading: a text and a list of {engine, host, unit} put together here, played as it arrives, and
// the course of the reading afterwards (who spoke, who failed or was passed over, and why).
export function speakView({ show, fail }) {
  const root = h('div')
  const player = new Player()
  let rows = []       // the catalog rows, in the order of this list
  let use = {}        // "engine@host" -> bool
  let units = {}      // "engine@host" -> unit
  let voices = {}     // "engine@host" -> chosen voice: every row has its own
  let voiceLists = {} // engine -> [{name, label}]
  let text = 'Hallo Welt. Das ist ein Test der Sprachausgabe.'
  let language = 'de'
  // The service requires the pause; this page starts with a usual one.
  let pauseMs = 700
  let savedOrder = [] // the order kept on the server: it counts the first time the list is built
  let restored = false
  let speaking = null // the AbortController of a reading in progress
  let summary = null

  // "engine@host", or "engine@" for this machine: the key of a row here and in the request's `voices`.
  const place = (row) => `${row.engine}@${row.host ?? ''}`

  // What Save keeps on the server (the same on every device): the first time this tab is opened it is
  // read; after that what is on the page counts until Save.
  async function restore() {
    const saved = await service.speakTest()
    restored = true
    if (saved.text === undefined) return
    ;({ text, language } = saved)
    pauseMs = saved.pause_ms
    use = saved.use
    units = saved.units
    voices = saved.voices
    savedOrder = saved.order
  }

  async function load() {
    try {
      if (!restored) await restore()
      const catalog = (await service.engines()).filter((row) => !row.browser)
      // Keep the order and the choices made so far; new rows join at the end. The first time, the saved order counts.
      const known = new Set(rows.map(place))
      const savedRank = (row) => { const index = savedOrder.indexOf(place(row)); return index === -1 ? savedOrder.length : index }
      if (rows.length === 0) rows = [...catalog].sort((a, b) => savedRank(a) - savedRank(b))
      else rows = [...rows.filter((row) => catalog.some((candidate) => place(candidate) === place(row))), ...catalog.filter((row) => !known.has(place(row)))]
      for (const row of rows) { use[place(row)] ??= false; units[place(row)] ??= row.unit_default }
      await Promise.all([...new Set(rows.map((row) => row.engine))].map(async (engine) => {
        if (!voiceLists[engine]) voiceLists[engine] = await service.voices(engine)
      }))
      for (const row of rows) voices[place(row)] ??= voiceLists[row.engine][0]?.name ?? ''
      render()
    } catch (error) { fail(error) }
  }

  function move(index, delta) {
    const target = index + delta
    if (target < 0 || target >= rows.length) return
    rows.splice(target, 0, rows.splice(index, 1)[0])
    render()
  }

  // The text field is as high as its text (at least its CSS minimum).
  function grow(area) {
    area.style.height = 'auto'
    area.style.height = `${area.scrollHeight}px`
  }

  async function save() {
    try {
      await service.saveSpeakTest({ text, language, pause_ms: pauseMs, order: rows.map(place), use, units, voices })
      show(t('saved'))
    } catch (error) { fail(error) }
  }

  async function run() {
    const chosen = rows.filter((row) => use[place(row)])
    if (chosen.length === 0) { fail(new Error(t('nothingChosen'))); return }
    const head = {
      text, language, paragraph_pause_ms: pauseMs,
      engines: chosen.map((row) => ({ engine: row.engine, unit: units[place(row)], host: row.host })),
      voices: Object.fromEntries(chosen.filter((row) => voices[place(row)]).map((row) => [place(row), { voice: voices[place(row)] }])),
    }
    const controller = new AbortController()
    speaking = controller
    summary = null
    render()
    try {
      const response = await service.speak(head, controller.signal)
      summary = await player.play(response, controller.signal)
    } catch (error) { fail(error) }
    speaking = null
    render()
  }

  function cancel() { speaking?.abort(); player.cancel() }

  function traceTable() {
    if (summary === null) return null
    return h('div', {}, h('h3', {}, t('trace')),
      h('table', {}, h('thead', {}, h('tr', {}, ...['entry', 'outcome', 'reason', 'detail'].map((key) => h('th', {}, t(key))))),
        h('tbody', {}, summary.trace.map((step) => h('tr', {},
          h('td', {}, h('code', {}, step.entry)),
          h('td', { class: `outcome-${step.outcome}` }, t({ spoke: 'outcomeSpoke', failed: 'outcomeFailed', skipped: 'outcomeSkipped' }[step.outcome])),
          h('td', {}, step.reason ?? ''),
          h('td', {}, step.detail ? h('span', { class: 'hint' }, step.detail) : ''))))),
      summary.switch ? h('p', { class: 'muted' }, t('switched', { reason: summary.switch })) : null)
  }

  function render() {
    replaceChildren(root, 
      h('h2', {}, t('tabSpeak')),
      h('p', { class: 'muted' }, t('speakHint')),
      cardTable(['engine', 'use', 'order', 'host', 'unit', 'voice'].map((key) => t(key)), rows.map((row, index) => [
        h('code', {}, row.engine),
        h('input', { type: 'checkbox', checked: use[place(row)], onchange: (event) => (use[place(row)] = event.target.checked) }),
        h('div', { class: 'row' }, h('button', { class: 'small', onclick: () => move(index, -1) }, '↑'), h('button', { class: 'small', onclick: () => move(index, 1) }, '↓')),
        hostLabel(row),
        h('select', { onchange: (event) => (units[place(row)] = event.target.value) },
          UNITS.map((unit) => h('option', { value: unit, selected: units[place(row)] === unit }, t(UNIT_KEYS[unit])))),
        (voiceLists[row.engine]?.length ?? 0) === 0 ? '' : h('select', { onchange: (event) => (voices[place(row)] = event.target.value) },
          voiceLists[row.engine].map((voice) => h('option', { value: voice.name, selected: voices[place(row)] === voice.name }, voice.label)))])),
      h('div', { class: 'row' }, h('label', {}, t('language2') + ' ',
        h('select', { onchange: (event) => (language = event.target.value) }, ['de', 'en'].map((code) => h('option', { value: code, selected: code === language }, code)))),
        h('label', {}, t('pause') + ' ',
          h('input', { type: 'number', min: '0', step: '50', value: String(pauseMs), oninput: (event) => (pauseMs = Math.max(0, Number(event.target.value) || 0)) }))),
      h('textarea', { oninput: (event) => { text = event.target.value; grow(event.target) } }, text),
      h('div', { class: 'actions' },
        h('button', { onclick: save }, t('save')),
        h('button', { class: 'primary', onclick: run, disabled: speaking !== null }, speaking === null ? t('speak') : t('speaking')),
        speaking === null ? null : h('button', { onclick: cancel }, t('stopPlaying'))),
      traceTable(),
    )
    const area = root.querySelector('textarea')
    if (area) grow(area)
  }

  return { root, load, render }
}
