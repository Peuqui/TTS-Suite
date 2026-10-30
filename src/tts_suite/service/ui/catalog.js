import { service } from './api.js'
import { cardTable, h, replaceChildren } from './dom.js'
import { hostLabel, t } from './i18n.js'

// How often the page asks for the progress of a running burn-in.
const BURN_IN_POLL_MS = 1500

// The catalog: every engine on every machine, with its state and a start and stop for each row, and for a
// GPU engine of this machine a burn-in that measures what it takes and makes that its budget.
export function catalogView({ show, fail }) {
  const root = h('div')
  let rows = []
  let burnIn = { running: false }
  // The rows with a command under way: place -> the command. Its buttons stay off until it is done, so a
  // slow start is not pressed twice.
  const busy = new Map()
  const place = (row) => `${row.engine}@${row.host ?? ''}`

  async function load() {
    try {
      rows = await service.engines()
      burnIn = await service.burnIn()
      render()
      if (burnIn.running) followBurnIn()
    } catch (error) { fail(error) }
  }

  // Asks until the burn-in has ended, also after a reload of the page; then the new budget is in the catalog.
  let following = false
  async function followBurnIn() {
    if (following) return
    following = true
    try {
      while (burnIn.running) {
        await new Promise((resolve) => setTimeout(resolve, BURN_IN_POLL_MS))
        burnIn = await service.burnIn()
        render()
      }
      if (burnIn.error) fail(new Error(burnIn.error))
      else show(t('burnInDone', { engine: burnIn.engine, peak: burnIn.peak_mib }))
      await load()
    } catch (error) { fail(error) } finally { following = false }
  }

  async function startBurnIn(row) {
    if (!window.confirm(t('burnInConfirm', { engine: row.engine }))) return
    try {
      burnIn = await service.startBurnIn(row.engine)
      render()
      followBurnIn()
    } catch (error) { fail(error) }
  }

  // Runs one command for some rows with their buttons off meanwhile; the page shows what is going on.
  async function working(command, targets, run) {
    for (const row of targets) busy.set(place(row), command)
    render()
    try { await run() } catch (error) { fail(error) }
    for (const row of targets) busy.delete(place(row))
    await load()
  }

  const control = (action, row) => working(action, [row], async () => {
    await service[action](row.engine, row.host)
    show(t(action === 'start' ? 'started' : 'stopped'))
  })

  // Takes the containers down so their GPU memory is free: the targets, or "all" rows. A reading that
  // holds one keeps it; the answer says what became of each. This machine's engines go through the
  // common GPU release API, the hosts' through /release.
  function release(targets) {
    const affected = targets === 'all' ? rows.filter((row) => !row.browser && !row.cloud) : rows.filter((row) => targets.some((target) => target.engine === row.engine && (target.host ?? null) === (row.host ?? null)))
    const local = targets === 'all' ? 'all' : targets.filter((target) => target.host == null).map((target) => target.engine)
    const remote = targets === 'all' ? 'all' : targets.filter((target) => target.host != null)
    const describe = (engine, host, outcome, detail) =>
      `${engine}${host ? `@${host}` : ''}: ${t(`release${outcome[0].toUpperCase()}${outcome.slice(1)}`)}${detail ? ` (${detail})` : ''}`
    return working('release', affected, async () => {
      const lines = []
      if (local === 'all' || local.length > 0) {
        lines.push(...(await service.gpuRelease(local)).map((result) => describe(result.item, null, result.outcome, result.detail)))
      }
      if (remote === 'all' || remote.length > 0) {
        lines.push(...(await service.release(remote)).map((result) => describe(result.engine, result.host, result.outcome, result.detail)))
      }
      show(lines.join(' · '))
    })
  }

  function columns(visible) {
    const hasHint = visible.some((row) => row.start_hint)
    return [
      { key: 'engine', cell: (row) => [row.label[document.documentElement.lang] ?? row.label.en,
        row.cloud ? h('span', { class: 'tag' }, t('cloud')) : null,
        row.needs_gpu ? h('span', { class: 'tag' }, t('needsGpu')) : null,
        h('div', { class: 'muted' }, h('code', {}, row.engine))] },
      { key: 'host', cell: hostLabel },
      { key: 'status', cell: (row) => busy.has(place(row)) ? h('span', { class: 'status starting' }, t({ start: 'busyStart', stop: 'busyStop', release: 'busyRelease' }[busy.get(place(row))])) : h('span', { class: `status ${row.status}`, title: t(`state${row.status[0].toUpperCase()}${row.status.slice(1)}`) }, t(`status${row.status[0].toUpperCase()}${row.status.slice(1)}`)) },
      { key: 'installed', cell: (row) => (row.installed ? t('yes') : t('no')) },
      // Likewise the memory: only when some row holds any.
      ...(visible.some((row) => memory(row.vram_mib) !== '') ? [{ key: 'gpuMemory', cell: (row) => memory(row.vram_mib) }] : []),
      // The column of the hints only when some row has one.
      ...(hasHint ? [{ key: 'hint', cell: (row) => (row.start_hint ? h('span', { class: 'hint' }, row.start_hint) : '') }] : []),
      { key: 'memoryNeeded', cell: (row) => row.reserve_mib ?? '' },
      { key: '', cell: (row) => (row.cloud ? '' : h('div', { class: 'buttons' },
        h('button', { class: 'small', onclick: () => control('start', row), disabled: busy.has(place(row)) || row.status === 'running' }, t('start')),
        h('button', { class: 'small', onclick: () => control('stop', row), disabled: busy.has(place(row)) || row.status === 'stopped' }, t('stop')),
        h('button', { class: 'small', onclick: () => release([{ engine: row.engine, host: row.host }]), disabled: busy.has(place(row)) || row.status === 'stopped' }, t('release')),
        row.reserve_mib !== null && row.installed ? h('button', { class: 'small', onclick: () => startBurnIn(row), disabled: burnIn.running || busy.has(place(row)) }, t('burnIn')) : null)) },
    ]
  }

  function render() {
    const visible = rows.filter((row) => !row.browser)
    const shown = columns(visible)
    replaceChildren(root,
      h('h2', {}, t('tabCatalog')),
      h('p', { class: 'muted' }, t('catalogHint')),
      h('div', { class: 'row' }, h('button', { onclick: load }, t('reload')), h('button', { onclick: () => release('all'), disabled: busy.size > 0 }, t('releaseAll'))),
      burnIn.running ? h('p', { class: 'status starting' }, burnInText()) : null,
      cardTable(shown.map((column) => (column.key ? t(column.key) : '')), visible.map((row) => shown.map((column) => column.cell(row)))),
    )
  }

  function burnInText() {
    if (burnIn.round === 0) return t('burnInStarting', { engine: burnIn.engine })
    return t('burnInRunning', { engine: burnIn.engine, round: burnIn.round, rounds: burnIn.rounds, peak: burnIn.peak_mib })
  }

  // What an engine holds: the sum over its cards; nothing when it holds none or the measurement failed.
  function memory(vram) {
    if (vram === null || vram === undefined) return '?'
    const total = Object.values(vram).reduce((sum, mib) => sum + mib, 0)
    return total === 0 ? '' : String(total)
  }

  return { root, load, render }
}
