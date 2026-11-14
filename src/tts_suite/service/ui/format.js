import { currentLanguage } from './i18n.js'

// Numbers as the reader of the page writes them: a comma in German, a dot in English. No thousands
// separator, so that 6350 MiB is never read as 6.350.
export function formatNumber(value) {
  return new Intl.NumberFormat(currentLanguage(), { maximumFractionDigits: 3, useGrouping: false }).format(value)
}

// Minutes and seconds past a minute, plain seconds below it.
export function duration(seconds) {
  if (seconds < 60) return `${seconds} s`
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} min`
}
