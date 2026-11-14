import { service } from './api.js'
import { cardTable, h, replaceChildren } from './dom.js'
import { place } from './entries.js'
import { duration, formatNumber } from './format.js'
import { hostLabel, t } from './i18n.js'
import { BURN_IN_POLL_MS, REFRESH_MS } from './settings.js'

// The catalog: every engine on every machine, with its state and a start and stop for each row, and for a
// GPU engine of this machine a burn-in that measures what it takes and makes that its budget.
export function catalogView({ show, fail }) {
  const root = h('div')
  let rows = []
  let burnIn = { running: false }
  let config = null
  // Engines of this machine a release left alone because a reading holds them: offered once more with force.
  let forceable = []
  // The rows with a command under way: place -> the command. Its buttons stay off until it is done, so a
  // slow start is not pressed twice.
  const busy = new Map()

  async function load() {
    try {
      ;[rows, burnIn, config] = await Promise.all([service.engines(), service.burnIn(), service.config()])
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
      else show(t('burnInDone', { engine: burnIn.engine, peak: formatNumber(burnIn.peak_mib) }))
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
  // holds one keeps it, unless `force` ends the reading; the answer says what became of each. This
  // machine's engines go through the common GPU release API, the hosts' through /release (no force there).
  function release(targets, force = false) {
    const affected = targets === 'all' ? rows.filter((row) => !row.browser && !row.cloud) : rows.filter((row) => targets.some((target) => target.engine === row.engine && (target.host ?? null) === (row.host ?? null)))
    const local = targets === 'all' ? 'all' : targets.filter((target) => target.host == null).map((target) => target.engine)
    const remote = targets === 'all' ? 'all' : targets.filter((target) => target.host != null)
    const describe = (engine, host, outcome, detail) =>
      `${engine}${host ? `@${host}` : ''}: ${t(`release${outcome[0].toUpperCase()}${outcome.slice(1)}`)}${detail ? ` (${detail})` : ''}`
    return working('release', affected, async () => {
      const lines = []
      forceable = []
      if (local === 'all' || local.length > 0) {
        const results = await service.gpuRelease(local, force)
        forceable = results.filter((result) => result.outcome === 'busy').map((result) => result.item)
        lines.push(...results.map((result) => describe(result.item, null, result.outcome, result.detail)))
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
      { key: 'status', cell: stateOf },
      { key: 'installed', cell: (row) => (row.installed ? t('yes') : t('no')) },
      // Likewise the memory: only when some row holds any.
      ...(visible.some((row) => memory(row.vram_mib) !== '') ? [{ key: 'gpuMemory', cell: (row) => memory(row.vram_mib) }] : []),
      // The column of the hints only when some row has one.
      ...(hasHint ? [{ key: 'hint', cell: (row) => (row.start_hint ? h('span', { class: 'hint' }, row.start_hint) : '') }] : []),
      { key: 'memoryNeeded', cell: (row) => (row.needed_mib === null ? '' : formatNumber(row.needed_mib)) },
      // Whether the need was measured on this machine (a burn-in wrote the budget) or is the engine's estimate.
      ...(visible.some((row) => row.needed_mib !== null) ? [{ key: 'source', cell: source }] : []),
      { key: '', cell: (row) => (row.cloud ? '' : h('div', { class: 'buttons' },
        h('button', { class: 'small', onclick: () => control('start', row), disabled: busy.has(place(row)) || row.status === 'running' }, t('start')),
        h('button', { class: 'small', onclick: () => control('stop', row), disabled: busy.has(place(row)) || row.status === 'stopped' }, t('stop')),
        h('button', { class: 'small', onclick: () => release([{ engine: row.engine, host: row.host }]), disabled: busy.has(place(row)) || row.status === 'stopped' }, t('release')),
        row.needed_mib !== null && row.installed ? h('button', { class: 'small', onclick: () => startBurnIn(row), disabled: burnIn.running || busy.has(place(row)) }, t('burnIn')) : null)) },
    ]
  }

  // The state of a row, and under it how long a running container stays before it ends itself for being idle.
  function stateOf(row) {
    if (busy.has(place(row))) return h('span', { class: 'status starting' }, t({ start: 'busyStart', stop: 'busyStop', release: 'busyRelease' }[busy.get(place(row))]))
    const name = `${row.status[0].toUpperCase()}${row.status.slice(1)}`
    return [
      h('span', { class: `status ${row.status}`, title: t(`state${name}`) }, t(`status${name}`)),
      row.idle_remaining_s === null ? null : h('div', { class: 'muted' }, t('idleLeft', { time: duration(Math.round(row.idle_remaining_s)) })),
    ]
  }

  function source(row) {
    if (row.needed_mib === null) return ''
    const measured = config?.engines[row.engine]?.vram_budget_mib != null
    return h('span', { class: measured ? 'status running' : 'muted' }, t(measured ? 'measured' : 'estimate'))
  }

  function render() {
    const visible = rows.filter((row) => !row.browser)
    const shown = columns(visible)
    replaceChildren(root,
      h('h2', {}, t('tabCatalog')),
      h('p', { class: 'muted' }, t('catalogHint')),
      h('div', { class: 'row' },
        h('button', { onclick: load }, t('reload')),
        h('button', { onclick: () => release('all'), disabled: busy.size > 0 }, t('releaseAll')),
        forceable.length === 0 ? null : h('button', { class: 'primary', disabled: busy.size > 0,
          onclick: () => release(forceable.map((engine) => ({ engine, host: null })), true) }, t('force'))),
      burnIn.running ? h('p', { class: 'status starting' }, burnInText()) : null,
      cardTable(shown.map((column) => (column.key ? t(column.key) : '')), visible.map((row) => shown.map((column) => column.cell(row)))),
    )
  }

  function burnInText() {
    if (burnIn.round === 0) return t('burnInStarting', { engine: burnIn.engine })
    return t('burnInRunning', { engine: burnIn.engine, round: burnIn.round, rounds: burnIn.rounds, peak: formatNumber(burnIn.peak_mib) })
  }

  // What an engine holds: the sum over its cards; nothing when it holds none or the measurement failed.
  function memory(vram) {
    if (vram === null || vram === undefined) return '?'
    const total = Object.values(vram).reduce((sum, mib) => sum + mib, 0)
    return total === 0 ? '' : formatNumber(total)
  }

  setInterval(() => {
    if (!root.closest('[hidden]') && !document.hidden && busy.size === 0) load()
  }, REFRESH_MS)

  return { root, load, render }
}
