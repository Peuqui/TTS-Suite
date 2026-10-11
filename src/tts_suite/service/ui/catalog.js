import { service } from './api.js'
import { h, replaceChildren } from './dom.js'
import { t } from './i18n.js'

// The catalog: every engine on every machine, with its state and a start and stop for each row.
export function catalogView({ show, fail }) {
  const root = h('div')
  let rows = []

  async function load() {
    try {
      rows = await service.engines()
      render()
    } catch (error) { fail(error) }
  }

  async function control(action, row) {
    try {
      await service[action](row.engine, row.host)
      show(t(action === 'start' ? 'started' : 'stopped'))
      await load()
    } catch (error) { fail(error) }
  }

  // Takes the containers down so their GPU memory is free: the targets, or "all" rows. A reading that
  // holds one keeps it; the answer says what became of each.
  async function release(targets) {
    try {
      const results = await service.release(targets)
      show(results.map((result) => `${result.engine}${result.host ? `@${result.host}` : ''}: ${t(`release${result.outcome[0].toUpperCase()}${result.outcome.slice(1)}`)}${result.detail ? ` (${result.detail})` : ''}`).join(' · '))
      await load()
    } catch (error) { fail(error) }
  }

  function render() {
    replaceChildren(root, 
      h('h2', {}, t('tabCatalog')),
      h('p', { class: 'muted' }, t('catalogHint')),
      h('div', { class: 'row' }, h('button', { onclick: load }, t('reload')), h('button', { onclick: () => release('all') }, t('releaseAll'))),
      h('div', { class: 'table-scroll' }, h('table', {},
        h('thead', {}, h('tr', {}, ...['engine', 'host', 'status', 'installed', 'gpuMemory', 'hint', ''].map((key) => h('th', {}, key ? t(key) : '')))),
        h('tbody', {}, rows.filter((row) => !row.browser).map((row) => h('tr', {},
          h('td', {}, row.label[document.documentElement.lang] ?? row.label.en,
            row.cloud ? h('span', { class: 'tag' }, t('cloud')) : null,
            row.needs_gpu ? h('span', { class: 'tag' }, t('needsGpu')) : null,
            h('div', { class: 'muted' }, h('code', {}, row.engine))),
          h('td', {}, row.host ?? t('thisMachine')),
          h('td', {}, h('span', { class: `status ${row.status}`, title: t(`state${row.status[0].toUpperCase()}${row.status.slice(1)}`) }, row.status)),
          h('td', {}, row.installed ? t('yes') : t('no')),
          h('td', {}, memory(row.vram_mib)),
          h('td', {}, row.start_hint ? h('span', { class: 'hint' }, row.start_hint) : ''),
          h('td', {}, row.cloud ? '' : h('div', { class: 'row' },
            h('button', { class: 'small', onclick: () => control('start', row), disabled: row.status === 'running' }, t('start')),
            h('button', { class: 'small', onclick: () => control('stop', row), disabled: row.status === 'stopped' }, t('stop')),
            h('button', { class: 'small', onclick: () => release([{ engine: row.engine, host: row.host }]), disabled: row.status === 'stopped' }, t('release'))))))),
      )),
    )
  }

  // What an engine holds: the sum over its cards; nothing when it holds none or the measurement failed.
  function memory(vram) {
    if (vram === null || vram === undefined) return '?'
    const total = Object.values(vram).reduce((sum, mib) => sum + mib, 0)
    return total === 0 ? '' : String(total)
  }

  return { root, load, render }
}
