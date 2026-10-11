import { service } from './api.js'
import { h, replaceChildren } from './dom.js'
import { t } from './i18n.js'

// Everything /config knows: the voices folder, the machines, the GPU rules, the margin, the defaults per
// engine and the pronunciations. Fields the page does not edit (a voice default of an engine) are kept.
export function configView({ show, fail }) {
  const root = h('div')
  let config = null
  let catalog = []

  async function load() {
    try {
      ;[config, catalog] = await Promise.all([service.config(), service.engines()])
      render()
    } catch (error) { fail(error) }
  }

  const numberOrNull = (value) => (value === '' ? null : Number(value))

  function field(label, input) { return h('label', { class: 'row' }, h('span', { class: 'muted' }, label), input) }

  function textInput(value, onchange, placeholder = '') {
    return h('input', { type: 'text', value: value ?? '', placeholder, onchange: (event) => onchange(event.target.value) })
  }

  function numberInput(value, onchange, placeholder = '') {
    return h('input', { type: 'number', min: 0, value: value ?? '', placeholder, onchange: (event) => onchange(numberOrNull(event.target.value)) })
  }

  function move(list, index, delta) {
    const target = index + delta
    if (target < 0 || target >= list.length) return
    list.splice(target, 0, list.splice(index, 1)[0])
    render()
  }

  function hostsTable() {
    return h('div', {}, h('h3', {}, t('hosts')),
      h('table', {}, h('thead', {}, h('tr', {}, ...['hostName', 'hostAddress', 'sshUser', 'sshPort', ''].map((key) => h('th', {}, key ? t(key) : '')))),
        h('tbody', {}, config.hosts.map((host, index) => h('tr', {},
          h('td', {}, textInput(host.name, (value) => (host.name = value))),
          h('td', {}, textInput(host.address, (value) => (host.address = value))),
          h('td', {}, textInput(host.ssh_user, (value) => (host.ssh_user = value))),
          h('td', {}, numberInput(host.ssh_port, (value) => (host.ssh_port = value ?? 22))),
          h('td', {}, h('button', { class: 'small', onclick: () => { config.hosts.splice(index, 1); render() } }, t('remove'))))))),
      h('div', { class: 'actions' }, h('button', { class: 'small', onclick: () => { config.hosts.push({ name: '', address: '', ssh_port: 22, ssh_user: '' }); render() } }, t('add'))))
  }

  function gpusTable() {
    return h('div', {}, h('h3', {}, t('gpus')),
      h('table', {}, h('thead', {}, h('tr', {}, ...['gpu', 'reserve', ''].map((key) => h('th', {}, key ? t(key) : '')))),
        h('tbody', {}, config.gpus.map((rule, index) => h('tr', {},
          h('td', {}, textInput(rule.gpu, (value) => (rule.gpu = value))),
          h('td', {}, numberInput(rule.reserve_mib, (value) => (rule.reserve_mib = value ?? 0))),
          h('td', {}, h('div', { class: 'row' },
            h('button', { class: 'small', onclick: () => move(config.gpus, index, -1) }, '↑'),
            h('button', { class: 'small', onclick: () => move(config.gpus, index, 1) }, '↓'),
            h('button', { class: 'small', onclick: () => { config.gpus.splice(index, 1); render() } }, t('remove'))))))),
      ),
      h('div', { class: 'actions' }, h('button', { class: 'small', onclick: () => { config.gpus.push({ gpu: '', reserve_mib: 0 }); render() } }, t('add'))))
  }

  function defaultsTable() {
    const keys = [...new Set(catalog.filter((row) => !row.browser).map((row) => row.engine))]
    const defaultsOf = (engine) => (config.engines[engine] ??= {})
    return h('div', {}, h('h3', {}, t('engineDefaults')),
      h('table', {}, h('thead', {}, h('tr', {}, ...['engine', 'vramBudget', 'maxParallel', 'maxQueue'].map((key) => h('th', {}, t(key))))),
        h('tbody', {}, keys.map((engine) => h('tr', {},
          h('td', {}, h('code', {}, engine)),
          h('td', {}, numberInput(config.engines[engine]?.vram_budget_mib, (value) => (defaultsOf(engine).vram_budget_mib = value), t('emptyMeansDefault'))),
          h('td', {}, numberInput(config.engines[engine]?.max_parallel, (value) => (defaultsOf(engine).max_parallel = value), t('emptyMeansDefault'))),
          h('td', {}, numberInput(config.engines[engine]?.max_queue, (value) => (defaultsOf(engine).max_queue = value), t('emptyMeansDefault'))))))))
  }

  function pronunciationsTable() {
    const words = Object.entries(config.pronunciations)
    return h('div', {}, h('h3', {}, t('pronunciations')),
      h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, t('word')), h('th', {}, t('spoken')), h('th', {}))),
        h('tbody', {}, words.map(([word, spoken]) => h('tr', {},
          h('td', {}, textInput(word, (value) => { delete config.pronunciations[word]; if (value) config.pronunciations[value] = spoken; render() })),
          h('td', {}, textInput(spoken, (value) => (config.pronunciations[word] = value))),
          h('td', {}, h('button', { class: 'small', onclick: () => { delete config.pronunciations[word]; render() } }, t('remove'))))))),
      h('div', { class: 'actions' }, h('button', { class: 'small', onclick: () => { config.pronunciations[''] = ''; render() } }, t('add'))))
  }

  async function save() {
    try {
      // Empty defaults are left out, so the engine's own apply.
      for (const [engine, defaults] of Object.entries(config.engines)) {
        for (const key of ['vram_budget_mib', 'max_parallel', 'max_queue']) if (defaults[key] === null) delete defaults[key]
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
