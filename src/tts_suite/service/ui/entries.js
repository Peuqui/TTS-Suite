import { h } from './dom.js'
import { t } from './i18n.js'

// What the pages share about an entry of a list or a row of the catalog.

// "engine@host", or "engine@" for this machine: the key of an entry here and in a request's `voices`.
export const place = (row) => `${row.engine}@${row.host ?? ''}`

const UNIT_KEYS = { sentence: 'unitSentence', paragraph: 'unitParagraph', whole: 'unitWhole' }

// The choice of how much text an entry takes at once.
export function unitSelect(value, onchange) {
  return h('select', { onchange: (event) => onchange(event.target.value) },
    Object.entries(UNIT_KEYS).map(([unit, key]) => h('option', { value: unit, selected: value === unit }, t(key))))
}
