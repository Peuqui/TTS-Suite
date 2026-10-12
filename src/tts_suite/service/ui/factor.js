import { h } from './dom.js'
import { currentLanguage, t } from './i18n.js'

// Speed and pitch are decimal numbers. A field of type "number" takes a comma or not as the browser
// pleases, so these are text fields with the decimal keyboard that understand both, and show the number
// the way the language writes it (1,15 in German, 1.15 in English).

/** The way a factor is shown: 1 as "1,0" (it is a decimal number), 1.15 as "1,15". */
export function formatFactor(value) {
  const text = Number.isInteger(value) ? value.toFixed(1) : String(value)
  return currentLanguage() === 'de' ? text.replace('.', ',') : text
}

/** What was typed, as a number; empty is null. A comma counts as the decimal point. */
export function parseFactor(typed) {
  const text = typed.trim().replace(',', '.')
  if (text === '') return null
  const value = Number(text)
  if (!Number.isFinite(value) || value <= 0) throw new Error(t('notAFactor'))
  return value
}

/** A field for a factor: `value` null shows the grey `placeholder`; what is typed goes to `onchange` once the field is left. */
export function factorInput(value, onchange, placeholder, fail) {
  const shown = (current) => (current === null || current === undefined ? '' : formatFactor(current))
  return h('input', {
    type: 'text', inputmode: 'decimal', autocomplete: 'off', value: shown(value), placeholder: formatFactor(placeholder),
    onchange: (event) => {
      try { onchange(parseFactor(event.target.value)) } catch (error) { fail(error); event.target.value = shown(value) }
    },
  })
}
