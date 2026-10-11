import { service } from './api.js'
import { h, replaceChildren } from './dom.js'
import { t } from './i18n.js'

// The voices of an engine, and the sync that uploads the shared folder's voices to a cloud engine.
export function voicesView({ show, fail }) {
  const root = h('div')
  let engines = []
  let chosen = null
  let voices = []

  async function load() {
    try {
      const rows = await service.engines()
      engines = [...new Set(rows.filter((row) => !row.browser).map((row) => row.engine))]
      chosen = engines.includes(chosen) ? chosen : (engines[0] ?? null)
      await loadVoices()
    } catch (error) { fail(error) }
  }

  async function loadVoices() {
    voices = chosen === null ? [] : await service.voices(chosen)
    render()
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
        h('div', { class: 'table-scroll' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, t('voice')))),
          h('tbody', {}, voices.map((voice) => h('tr', {}, h('td', {}, voice.label === voice.name ? voice.name : `${voice.label}  (${voice.name})`)))))),
    )
  }

  return { root, load, render }
}
