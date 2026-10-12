import { ApiError, token } from './api.js'
import { catalogView } from './catalog.js'
import { configView } from './config.js'
import { h } from './dom.js'
import { currentLanguage, onLanguageChange, t, toggleLanguage } from './i18n.js'
import { speakView } from './speak.js'
import { voicesView } from './voices.js'

// The page: four views behind tabs, the token for the commands that change something, the language and
// the colours. Light or dark follows the system until one is chosen.
const message = document.getElementById('message')
// A message fades after a few seconds (an error stays longer) and goes at a tap.
const NOTICE_MS = { ok: 4000, error: 10000 }
let hideTimer
function say(text, kind) {
  clearTimeout(hideTimer)
  message.textContent = text
  message.className = kind
  message.hidden = false
  hideTimer = setTimeout(() => { message.hidden = true }, NOTICE_MS[kind])
}
message.addEventListener('click', () => { message.hidden = true })
const show = (text) => say(text, 'ok')
const fail = (error) => say(error instanceof ApiError && error.status === 401 ? `${t('needsToken')} (${error.message})` : error.message, 'error')

const views = {
  catalog: catalogView({ show, fail }),
  config: configView({ show, fail }),
  voices: voicesView({ show, fail }),
  speak: speakView({ show, fail }),
}
const TAB_KEYS = { catalog: 'tabCatalog', config: 'tabConfig', voices: 'tabVoices', speak: 'tabSpeak' }
let current = 'catalog'

for (const [name, view] of Object.entries(views)) document.getElementById(`view-${name}`).append(view.root)

function select(name) {
  current = name
  // The tab is in the address: a reload or a bookmark comes back to it.
  history.replaceState(null, '', `#${name}`)
  message.hidden = true
  for (const key of Object.keys(views)) document.getElementById(`view-${key}`).hidden = key !== name
  drawTabs()
  views[name].load()
}

function drawTabs() {
  document.getElementById('tabs').replaceChildren(...Object.keys(views).map((name) =>
    h('button', { type: 'button', role: 'tab', 'aria-selected': name === current ? 'true' : 'false', onclick: () => select(name) }, t(TAB_KEYS[name]))))
}

const THEMES = ['auto', 'light', 'dark']
const THEME_KEYS = { auto: 'themeAuto', light: 'themeLight', dark: 'themeDark' }
function applyTheme() {
  const theme = localStorage.getItem('tts-suite-theme') ?? 'auto'
  if (theme === 'auto') document.documentElement.removeAttribute('data-theme')
  else document.documentElement.dataset.theme = theme
  document.getElementById('theme').textContent = t(THEME_KEYS[theme])
}

function translateStatic() {
  document.documentElement.lang = currentLanguage()
  document.querySelectorAll('[data-i18n]').forEach((element) => (element.textContent = t(element.dataset.i18n)))
  document.getElementById('language').textContent = t('language')
  applyTheme()
  drawTabs()
  views[current].render()
}

document.getElementById('language').addEventListener('click', toggleLanguage)
document.getElementById('theme').addEventListener('click', () => {
  const now = localStorage.getItem('tts-suite-theme') ?? 'auto'
  localStorage.setItem('tts-suite-theme', THEMES[(THEMES.indexOf(now) + 1) % THEMES.length])
  applyTheme()
})
// The token form is never sent: the field only keeps the token (below).
document.getElementById('token-form').addEventListener('submit', (event) => event.preventDefault())
const tokenField = document.getElementById('token')
tokenField.value = token.get()
tokenField.addEventListener('change', () => {
  token.set(tokenField.value.trim())
  show(t(token.get() ? 'tokenKept' : 'tokenCleared'))
})
onLanguageChange(translateStatic)
translateStatic()
select(location.hash.slice(1) in views ? location.hash.slice(1) : 'catalog')
