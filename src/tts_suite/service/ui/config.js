import { service } from './api.js'
import { cardTable, h, replaceChildren } from './dom.js'
import { factorInput } from './factor.js'
import { place, unitSelect } from './entries.js'
import { formatNumber } from './format.js'
import { currentLanguage, hostLabel, t } from './i18n.js'

// Everything /config knows: the voices folder, the auto list, the machines, the GPU rules, the margin, the
// defaults per engine and the pronunciations. Fields the page does not edit (a voice default of an engine) are kept.
// Speed and pitch of 1 leave the audio as the engine made it.
const NEUTRAL_FACTOR = 1

export function configView({ show, fail }) {
  const root = h('div')
  let config = null
  let loaded = null // the configuration as the service sent it: what Save compares with
  let catalog = []
  let voiceLists = {} // engine -> [{name, label}], for the voice default of each engine

  async function load() {
    try {
      ;[loaded, catalog] = await Promise.all([service.config(), service.engines()])
      config = structuredClone(loaded)
      joinCatalog()
      const engines = [...new Set(catalog.filter((row) => !row.browser).map((row) => row.engine))]
      voiceLists = Object.fromEntries(await Promise.all(engines.map(async (engine) => [engine, await service.voices(engine)])))
      render()
    } catch (error) { fail(error) }
  }

  const numberOrNull = (value) => (value === '' ? null : Number(value))

  // A group of settings with its title and what it is for, as on the STT-Suite's page.
  const group = (title, hint, ...content) => h('fieldset', {}, h('legend', {}, title), hint ? h('p', { class: 'muted' }, hint) : null, ...content)

  function textInput(value, onchange, placeholder = '') {
    return h('input', { type: 'text', autocomplete: 'off', value: value ?? '', placeholder, onchange: (event) => onchange(event.target.value) })
  }

  function numberInput(value, onchange, placeholder = '', step = 1) {
    return h('input', { type: 'number', min: 0, step, autocomplete: 'off', value: value ?? '', placeholder, onchange: (event) => onchange(numberOrNull(event.target.value)) })
  }

  function move(list, index, delta) {
    const target = index + delta
    if (target < 0 || target >= list.length) return
    list.splice(target, 0, list.splice(index, 1)[0])
    render()
  }

  // The catalog's row of an entry, while the entry can still be named: a machine removed or renamed in
  // this form takes its entries out at once (the catalog itself only knows it after Save).
  const catalogRow = (entry) => (entry.host === null || config.hosts.some((host) => host.name === entry.host)
    ? catalog.find((row) => place(row) === place(entry)) : undefined)

  // Every engine of the catalog has its row in the auto list: one the list does not have yet joins at the
  // end, switched off (an engine may cost money). The browser's voice never speaks in the suite.
  function joinCatalog() {
    const listed = new Set(config.auto_engines.map(place))
    for (const row of catalog) {
      if (!row.browser && !listed.has(place(row))) config.auto_engines.push({ engine: row.engine, unit: row.unit_default, host: row.host, enabled: false })
    }
  }

  // Where an entry runs and what it needs, at the right of its row.
  function needs(row) {
    if (row.needed_mib !== null) return t('needsFree', { mib: formatNumber(row.needed_mib) })
    if (row.cloud) return t('cloud')
    return row.needs_gpu ? t('needsGpu') : t('cpuAlways')
  }

  // The list a request with `"engines": "auto"` goes through from the top: each entry can be switched off,
  // moved and given its unit. An entry that is no longer offered (its machine removed) can only be removed.
  function autoList() {
    return group(t('groupAuto'), t('groupAutoHint'),
      config.auto_engines.map((entry, index) => {
        const row = catalogRow(entry)
        return h('div', { class: `row chain ${entry.enabled ? '' : 'off'}` },
          h('button', { class: 'small', title: t('up'), onclick: () => move(config.auto_engines, index, -1) }, '↑'),
          h('button', { class: 'small', title: t('down'), onclick: () => move(config.auto_engines, index, 1) }, '↓'),
          h('label', { class: 'switch' }, h('input', { type: 'checkbox', checked: entry.enabled, onchange: (event) => { entry.enabled = event.target.checked; render() } }), h('span')),
          h('span', { class: 'name' }, row ? (row.label[currentLanguage()] ?? row.label.en) : h('code', {}, entry.engine), ' · ', row ? hostLabel(row) : (entry.host ?? t('thisMachine'))),
          unitSelect(entry.unit, (unit) => (entry.unit = unit)),
          row ? h('span', { class: 'muted' }, needs(row))
            : h('span', { class: 'muted' }, t('notOffered'), ' ', h('button', { class: 'small', onclick: () => { config.auto_engines.splice(index, 1); render() } }, t('remove'))))
      }))
  }

  function hostsTable() {
    return group(t('hosts'), t('hostsHint'),
      cardTable(['hostName', 'hostAddress', 'sshUser', 'sshPort', 'hostGpuOrc', 'hostGpuOrcToken', ''].map((key) => (key ? t(key) : '')), config.hosts.map((host, index) => [
        textInput(host.name, (value) => { host.name = value; render() }),
        textInput(host.address, (value) => (host.address = value)),
        textInput(host.ssh_user, (value) => (host.ssh_user = value)),
        // Empty = the service's default port (it fills the field on Save).
        numberInput(host.ssh_port, (value) => { if (value === null) delete host.ssh_port; else host.ssh_port = value }),
        // Empty = that machine has no GPU-Orc (no traffic light before a start there).
        textInput(host.gpu_orc_url ?? '', (value) => (host.gpu_orc_url = value || null)),
        textInput(host.gpu_orc_token_file ?? '', (value) => (host.gpu_orc_token_file = value || null)),
        h('button', { class: 'small', onclick: () => { config.hosts.splice(index, 1); render() } }, t('remove'))])),
      h('div', { class: 'actions' }, h('button', { class: 'small', onclick: () => { config.hosts.push({ name: '', address: '', ssh_user: '', gpu_orc_url: null, gpu_orc_token_file: null }); render() } }, t('add'))))
  }

  function gpusTable() {
    const mode = h('select', { onchange: (event) => { config.gpu_mode = event.target.value; render() } },
      ['auto', 'list'].map((value) => h('option', { value, selected: config.gpu_mode === value }, t(value === 'auto' ? 'gpuModeAuto' : 'gpuModeList'))))
    // The list is only used in the mode "list"; it stays in the configuration while the mode is "auto".
    const list = config.gpu_mode === 'auto' ? [] : [
      cardTable(['gpu', 'reserve', ''].map((key) => (key ? t(key) : '')), config.gpus.map((rule, index) => [
        textInput(rule.gpu, (value) => (rule.gpu = value)),
        numberInput(rule.reserve_mib, (value) => (rule.reserve_mib = value ?? 0)),
        h('div', { class: 'row' },
          h('button', { class: 'small', onclick: () => move(config.gpus, index, -1) }, '↑'),
          h('button', { class: 'small', onclick: () => move(config.gpus, index, 1) }, '↓'),
          h('button', { class: 'small', onclick: () => { config.gpus.splice(index, 1); render() } }, t('remove')))])),
      h('div', { class: 'actions' }, h('button', { class: 'small', onclick: () => { config.gpus.push({ gpu: '', reserve_mib: 0 }); render() } }, t('add'))),
    ]
    return group(t('gpus'), t('gpuModeHint'), mode, list)
  }

  // What a request that does not say otherwise gets from an engine: its voice, speed and pitch (the
  // request's own values win), and the limits of the engine.
  function defaultsTable() {
    const keys = [...new Set(catalog.filter((row) => !row.browser).map((row) => row.engine))]
    const defaultsOf = (engine) => (config.engines[engine] ??= {})
    const voiceOf = (engine) => (defaultsOf(engine).voice ??= { voice: null, speed: null, pitch: null })
    // What an empty field means is its placeholder, in grey: the engine's own value (the catalog has them).
    const own = (engine) => catalog.find((row) => row.engine === engine)
    const limit = (engine, name, ownValue) => numberInput(config.engines[engine]?.[name], (value) => (defaultsOf(engine)[name] = value), String(ownValue ?? '–'))
    const setting = (engine, name) => factorInput(config.engines[engine]?.voice?.[name], (value) => (voiceOf(engine)[name] = value), NEUTRAL_FACTOR, fail)
    function voiceSelect(engine) {
      const chosen = config.engines[engine]?.voice?.voice ?? ''
      const names = (voiceLists[engine] ?? []).map((voice) => voice.name)
      // A voice set earlier that the engine no longer lists stays selectable: nothing is dropped unseen.
      const all = chosen && !names.includes(chosen) ? [{ name: chosen, label: chosen }, ...voiceLists[engine]] : (voiceLists[engine] ?? [])
      return h('select', { onchange: (event) => (voiceOf(engine).voice = event.target.value || null) },
        h('option', { value: '', selected: chosen === '' }, t('emptyMeansDefault')),
        all.map((voice) => h('option', { value: voice.name, selected: voice.name === chosen }, voice.label)))
    }
    return group(t('engineDefaults'), t('engineDefaultsHint'),
      cardTable(['engine', 'defaultVoice', 'defaultSpeed', 'defaultPitch', 'vramBudget', 'maxParallel', 'maxQueue'].map((key) => t(key)), keys.map((engine) => [
        h('code', {}, engine), voiceSelect(engine), setting(engine, 'speed'), setting(engine, 'pitch'),
        limit(engine, 'vram_budget_mib', own(engine)?.default_vram_mib), limit(engine, 'max_parallel', own(engine)?.default_max_parallel),
        limit(engine, 'max_queue', own(engine)?.default_max_queue)])))
  }

  // One line per word: the word, what is spoken instead, a button to remove it.
  function pronunciationsTable() {
    const words = Object.entries(config.pronunciations)
    return group(t('pronunciations'), t('pronunciationsHint'),
      h('div', { class: 'pairs' },
        h('span', { class: 'muted' }, t('word')), h('span', { class: 'muted' }, t('spoken')), h('span'),
        words.flatMap(([word, spoken]) => [
          textInput(word, (value) => { delete config.pronunciations[word]; if (value) config.pronunciations[value] = spoken; render() }),
          textInput(spoken, (value) => (config.pronunciations[word] = value)),
          h('button', { class: 'small', title: t('remove'), 'aria-label': t('remove'), onclick: () => { delete config.pronunciations[word]; render() } }, '✕')])),
      h('div', { class: 'actions' }, h('button', { class: 'small', onclick: () => { config.pronunciations[''] = ''; render() } }, t('add'))))
  }

  async function save() {
    try {
      // Empty defaults are left out, so the engine's own apply.
      for (const [engine, defaults] of Object.entries(config.engines)) {
        for (const key of ['vram_budget_mib', 'max_parallel', 'max_queue']) if (defaults[key] === null) delete defaults[key]
        if (defaults.voice) {
          for (const key of ['voice', 'speed', 'pitch']) if (defaults.voice[key] === null) delete defaults.voice[key]
          if (Object.keys(defaults.voice).length === 0) delete defaults.voice
        }
        if (Object.keys(defaults).length === 0) delete config.engines[engine]
      }
      delete config.pronunciations['']
      const kept = await service.saveConfig(config)
      // What changed, by the names of the settings, as the service keeps them (as on the STT-Suite's page).
      const changed = Object.keys(kept).filter((key) => JSON.stringify(kept[key]) !== JSON.stringify(loaded[key]))
      show(changed.length === 0 ? t('nothingChanged') : t('savedChanged', { changed: changed.join(', ') }))
      await load()
    } catch (error) { fail(error) }
  }

  function render() {
    if (config === null) return
    replaceChildren(root, 
      h('h2', {}, t('tabConfig')),
      h('p', { class: 'muted' }, t('configHint')),
      autoList(), hostsTable(), gpusTable(), defaultsTable(), pronunciationsTable(),
      group(t('groupGeneral'), '', h('div', { class: 'fields' },
        h('label', {}, t('voicesDir')), textInput(config.voices_dir, (value) => (config.voices_dir = value)),
        h('label', {}, t('headroom')), numberInput(config.headroom_mib, (value) => (config.headroom_mib = value ?? 0)))),
      h('div', { class: 'actions' }, h('button', { class: 'primary', onclick: save }, t('save')), h('button', { onclick: load }, t('reload'))),
    )
  }

  return { root, load, render }
}
