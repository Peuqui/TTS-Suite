import { ApiError, service } from './api.js'
import { cardTable, h, replaceChildren } from './dom.js'
import { place, unitSelect } from './entries.js'
import { hostLabel, t } from './i18n.js'
import { Player } from './player.js'
import { TEST_LANGUAGE, TEST_LANGUAGES, TEST_PAUSE_MS, TEST_PAUSE_STEP_MS, TEST_TEXT } from './settings.js'
import { loadSpeaker, speakerIcon } from './speaker-icon.js'

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
  let text = TEST_TEXT
  let language = TEST_LANGUAGE
  let pauseMs = TEST_PAUSE_MS
  // Who speaks: the ticked rows one after the other (a comparison), or one reading by the auto list.
  let mode = 'compare'
  let savedOrder = [] // the order kept on the server: it counts the first time the list is built
  let restored = false
  let speaking = null // the AbortController of a reading in progress
  let summary = null

  // What Save keeps on the server (the same on every device): the first time this tab is opened it is
  // read; after that what is on the page counts until Save.
  async function restore() {
    const saved = await service.speakTest()
    restored = true
    if (saved.text === undefined) return
    ;({ text, language } = saved)
    pauseMs = saved.pause_ms
    mode = saved.mode
    use = saved.use
    units = saved.units
    voices = saved.voices
    savedOrder = saved.order
  }

  async function load() {
    try {
      await loadSpeaker()
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
      await service.saveSpeakTest({ text, language, pause_ms: pauseMs, mode, order: rows.map(place), use, units, voices })
      show(t('saved'))
    } catch (error) { fail(error) }
  }

  // The voice chosen on this page for each of these rows, as a request's `voices`.
  const voiceSettings = (chosen) => Object.fromEntries(chosen.filter((row) => voices[place(row)]).map((row) => [place(row), { voice: voices[place(row)] }]))

  // One reading per list, one after the other; the course of all of them goes into one table.
  async function readAll(lists) {
    const controller = new AbortController()
    speaking = controller
    summary = { trace: [], switch: null }
    render()
    for (const list of lists) {
      const head = { text, language, paragraph_pause_ms: pauseMs, ...list }
      try {
        const part = await player.play(await service.speak(head, controller.signal), controller.signal)
        if (part === null) break
        summary.trace.push(...part.trace)
        summary.switch = part.switch ?? summary.switch
      } catch (error) {
        if (controller.signal.aborted) break
        // No engine could speak: the course says why, and with the auto list the page says where to look.
        if (error instanceof ApiError) summary.trace.push(...error.trace)
        fail(error instanceof ApiError && error.code === 'no_engine' && list.engines === 'auto' ? new Error(`${error.message} — ${t('noEngineAuto')}`) : error)
      }
      render()
    }
    speaking = null
    render()
  }

  // Compare: every ticked row speaks, one after the other in the order of the list, each in a reading of its
  // own, so one that fails or is not up does not stop the rest (it shows in the course). Auto: one reading by
  // the suite's own list (Configuration, Automatic), the way a program that says "auto" gets it; the voices
  // are this page's.
  function run() {
    if (mode === 'auto') { readAll([{ engines: 'auto', voices: voiceSettings(rows) }]); return }
    const chosen = rows.filter((row) => use[place(row)])
    if (chosen.length === 0) { fail(new Error(t('nothingChosen'))); return }
    readAll(chosen.map((row) => ({ engines: [{ engine: row.engine, unit: units[place(row)], host: row.host }], voices: voiceSettings([row]) })))
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
    // With the auto list the order, the switch and the unit are the list's (Configuration); only the voice counts here.
    const auto = mode === 'auto'
    replaceChildren(root,
      h('h2', {}, t('tabSpeak')),
      h('p', { class: 'muted' }, t('speakHint')),
      h('div', { class: 'fields' },
        h('label', {}, t('modeField')),
        h('select', { onchange: (event) => { mode = event.target.value; render() } },
          ['compare', 'auto'].map((value) => h('option', { value, selected: value === mode }, t(value === 'auto' ? 'modeAuto' : 'modeCompare'))))),
      cardTable(['engine', 'use', 'order', 'host', 'unit', 'voice'].map((key) => t(key)), rows.map((row, index) => [
        h('code', {}, row.engine),
        h('input', { type: 'checkbox', disabled: auto, checked: use[place(row)], onchange: (event) => (use[place(row)] = event.target.checked) }),
        h('div', { class: 'row' }, h('button', { class: 'small', disabled: auto, onclick: () => move(index, -1) }, '↑'), h('button', { class: 'small', disabled: auto, onclick: () => move(index, 1) }, '↓')),
        hostLabel(row),
        unitSelect(units[place(row)], (unit) => (units[place(row)] = unit), auto),
        (voiceLists[row.engine]?.length ?? 0) === 0 ? '' : h('select', { onchange: (event) => (voices[place(row)] = event.target.value) },
          voiceLists[row.engine].map((voice) => h('option', { value: voice.name, selected: voices[place(row)] === voice.name }, voice.label)))])),
      h('div', { class: 'fields' },
        h('label', {}, t('language2')),
        h('select', { onchange: (event) => (language = event.target.value) }, TEST_LANGUAGES.map((code) => h('option', { value: code, selected: code === language }, code))),
        h('label', {}, t('pause')),
        h('input', { type: 'number', min: '0', step: String(TEST_PAUSE_STEP_MS), value: String(pauseMs), oninput: (event) => (pauseMs = Math.max(0, Number(event.target.value) || 0)) }),
        h('label', {}, t('text')),
        h('textarea', { oninput: (event) => { text = event.target.value; grow(event.target) } }, text)),
      h('div', { class: 'actions' },
        h('button', { onclick: save }, t('save')),
        h('button', { class: 'primary', onclick: run, disabled: speaking !== null }, speakerIcon({ playing: speaking !== null }), speaking === null ? t('speak') : t('speaking')),
        speaking === null ? null : h('button', { onclick: cancel }, t('stopPlaying'))),
      traceTable(),
    )
    const area = root.querySelector('textarea')
    if (area) grow(area)
  }

  return { root, load, render }
}
