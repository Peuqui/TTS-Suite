import { service } from './api.js'
import { cardTable, factorText, h, replaceChildren } from './dom.js'
import { t } from './i18n.js'

// Everything /config knows: the voices folder, the machines, the GPU rules, the margin, the defaults per
// engine and the pronunciations. Fields the page does not edit (a voice default of an engine) are kept.
// Speed and pitch of 1 leave the audio as the engine made it.
const NEUTRAL_FACTOR = 1

export function configView({ show, fail }) {
  const root = h('div')
  let config = null
  let catalog = []
  let voiceLists = {} // engine -> [{name, label}], for the voice default of each engine

  async function load() {
    try {
      ;[config, catalog] = await Promise.all([service.config(), service.engines()])
      const engines = [...new Set(catalog.filter((row) => !row.browser).map((row) => row.engine))]
      voiceLists = Object.fromEntries(await Promise.all(engines.map(async (engine) => [engine, await service.voices(engine)])))
      render()
    } catch (error) { fail(error) }
  }

  const numberOrNull = (value) => (value === '' ? null : Number(value))

  function field(label, input) { return h('label', { class: 'row' }, h('span', { class: 'muted' }, label), input) }

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

  function hostsTable() {
    return h('div', {}, h('h3', {}, t('hosts')),
      cardTable(['hostName', 'hostAddress', 'sshUser', 'sshPort', ''].map((key) => (key ? t(key) : '')), config.hosts.map((host, index) => [
        textInput(host.name, (value) => (host.name = value)),
        textInput(host.address, (value) => (host.address = value)),
        textInput(host.ssh_user, (value) => (host.ssh_user = value)),
        numberInput(host.ssh_port, (value) => (host.ssh_port = value ?? 22)),
        h('button', { class: 'small', onclick: () => { config.hosts.splice(index, 1); render() } }, t('remove'))])),
      h('div', { class: 'actions' }, h('button', { class: 'small', onclick: () => { config.hosts.push({ name: '', address: '', ssh_port: 22, ssh_user: '' }); render() } }, t('add'))))
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
    return h('div', {}, h('h3', {}, t('gpus')), h('p', { class: 'muted' }, t('gpuModeHint')), mode, list)
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
    const setting = (engine, name) => numberInput(config.engines[engine]?.voice?.[name], (value) => (voiceOf(engine)[name] = value), factorText(NEUTRAL_FACTOR), 0.05)
    function voiceSelect(engine) {
      const chosen = config.engines[engine]?.voice?.voice ?? ''
      const names = (voiceLists[engine] ?? []).map((voice) => voice.name)
      // A voice set earlier that the engine no longer lists stays selectable: nothing is dropped unseen.
      const all = chosen && !names.includes(chosen) ? [{ name: chosen, label: chosen }, ...voiceLists[engine]] : (voiceLists[engine] ?? [])
      return h('select', { onchange: (event) => (voiceOf(engine).voice = event.target.value || null) },
        h('option', { value: '', selected: chosen === '' }, t('emptyMeansDefault')),
        all.map((voice) => h('option', { value: voice.name, selected: voice.name === chosen }, voice.label)))
    }
    return h('div', {}, h('h3', {}, t('engineDefaults')), h('p', { class: 'muted' }, t('engineDefaultsHint')),
      cardTable(['engine', 'defaultVoice', 'defaultSpeed', 'defaultPitch', 'vramBudget', 'maxParallel', 'maxQueue'].map((key) => t(key)), keys.map((engine) => [
        h('code', {}, engine), voiceSelect(engine), setting(engine, 'speed'), setting(engine, 'pitch'),
        limit(engine, 'vram_budget_mib', own(engine)?.default_vram_mib), limit(engine, 'max_parallel', own(engine)?.default_max_parallel),
        limit(engine, 'max_queue', own(engine)?.default_max_queue)])))
  }

  // One line per word: the word, what is spoken instead, a button to remove it.
  function pronunciationsTable() {
    const words = Object.entries(config.pronunciations)
    return h('div', {}, h('h3', {}, t('pronunciations')),
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
      config = await service.saveConfig(config)
      show(t('saved'))
      render()
    } catch (error) { fail(error) }
  }

  function render() {
    if (config === null) return
    replaceChildren(root, 
      h('h2', {}, t('tabConfig')),
      field(t('voicesDir'), textInput(config.voices_dir, (value) => (config.voices_dir = value))),
      field(t('headroom'), numberInput(config.headroom_mib, (value) => (config.headroom_mib = value ?? 0))),
      hostsTable(), gpusTable(), defaultsTable(), pronunciationsTable(),
      h('div', { class: 'actions' }, h('button', { class: 'primary', onclick: save }, t('save')), h('button', { onclick: load }, t('reload'))),
    )
  }

  return { root, load, render }
}
