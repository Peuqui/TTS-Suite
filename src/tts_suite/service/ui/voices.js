import { service } from './api.js'
import { cardTable, h, replaceChildren } from './dom.js'
import { factorInput } from './factor.js'
import { t } from './i18n.js'

// Speed and pitch of 1 leave the audio as the engine made it.
const NEUTRAL_FACTOR = 1

// The voices of an engine, the speed and pitch each one is tuned to (a click on a voice opens them), and the
// sync that uploads the shared folder's voices to a cloud engine.
export function voicesView({ show, fail }) {
  const root = h('div')
  let engines = []
  let chosen = null
  let voices = []
  let config = null
  let selected = null // the name of the voice whose tuning is open

  const tuningOf = (voice) => config?.engines[chosen]?.voice_tuning?.[voice] ?? {}
  const show2 = (value) => (value === null || value === undefined ? '–' : String(value))

  async function load() {
    try {
      config = await service.config()
      const rows = await service.engines()
      engines = [...new Set(rows.filter((row) => !row.browser).map((row) => row.engine))]
      chosen = engines.includes(chosen) ? chosen : (engines[0] ?? null)
      await loadVoices()
    } catch (error) { fail(error) }
  }

  async function loadVoices() {
    voices = chosen === null ? [] : await service.voices(chosen)
    selected = null
    render()
  }

  // Keeps the tuning of the open voice in the configuration; empty fields mean "the default of the engine".
  async function saveTuning(speed, pitch) {
    try {
      const engine = (config.engines[chosen] ??= {})
      const tuning = (engine.voice_tuning ??= {})
      if (speed === null && pitch === null) delete tuning[selected]
      else tuning[selected] = { speed, pitch }
      config = await service.saveConfig(config)
      show(t('saved'))
      render()
    } catch (error) { fail(error) }
  }

  function tuningEditor() {
    const current = tuningOf(selected)
    let speed = current.speed ?? null
    let pitch = current.pitch ?? null
    // What an empty field means is shown in it, in grey: the default of the engine, else 1 (unchanged).
    const engineDefault = (name) => config?.engines[chosen]?.voice?.[name] ?? NEUTRAL_FACTOR
    const input = (value, onchange, name) => factorInput(value, onchange, engineDefault(name), fail)
    return h('div', { class: 'editor' },
      h('h3', {}, t('tuningOf', { voice: selected })),
      h('p', { class: 'muted' }, t('tuningHint')),
      h('div', { class: 'row' }, h('label', {}, t('defaultSpeed') + ' ', input(speed, (value) => (speed = value), 'speed')),
        h('label', {}, t('defaultPitch') + ' ', input(pitch, (value) => (pitch = value), 'pitch'))),
      h('div', { class: 'actions' },
        h('button', { class: 'primary', onclick: () => saveTuning(speed, pitch) }, t('save')),
        h('button', { onclick: () => saveTuning(null, null) }, t('reset')),
        h('button', { onclick: () => { selected = null; render() } }, t('close'))))
  }

  async function sync() {
    try {
      voices = await service.syncVoices(chosen)
      show(t('synced', { count: voices.length }))
      render()
    } catch (error) { fail(error) }
  }

  function render() {
    replaceChildren(root, 
      h('h2', {}, t('tabVoices')),
      h('p', { class: 'muted' }, t('voicesHint')),
      h('div', { class: 'row' },
        h('select', { onchange: async (event) => { chosen = event.target.value; try { await loadVoices() } catch (error) { fail(error) } } },
          engines.map((engine) => h('option', { value: engine, selected: engine === chosen }, engine))),
        h('button', { onclick: loadVoices }, t('reload')),
        h('button', { onclick: sync, disabled: chosen === null }, t('sync'))),
      voices.length === 0 ? h('p', { class: 'muted' }, t('noVoices')) :
        cardTable([t('voice'), t('defaultSpeed'), t('defaultPitch')], voices.map((voice) => [
          h('button', { class: 'link', 'aria-pressed': selected === voice.name ? 'true' : 'false', onclick: () => { selected = voice.name; render() } },
            voice.label === voice.name ? voice.name : `${voice.label}  (${voice.name})`),
          show2(tuningOf(voice.name).speed), show2(tuningOf(voice.name).pitch)])),
      selected === null ? null : tuningEditor(),
    )
    // The list can be long: the settings of the voice just opened come into view.
    root.querySelector('.editor')?.scrollIntoView({ block: 'nearest' })
  }

  return { root, load, render }
}
