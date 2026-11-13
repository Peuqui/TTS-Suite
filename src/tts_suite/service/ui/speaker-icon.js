import { h } from './dom.js'

// The speaker button's symbol of the page: the speaker and its small arrow from speaker/ (the one source for the shape and
// the movement while it is read, also for the programs that read aloud; see speaker/speaker.css).
let shapes = null

// The shapes are files of their own, which have to sit inside the page to be coloured and moved by speaker.css.
export async function loadSpeaker() {
  if (shapes !== null) return
  const parse = async (name) => {
    const response = await fetch(`speaker/${name}`)
    if (!response.ok) throw new Error(`speaker/${name}: ${response.status}`)
    return new DOMParser().parseFromString(await response.text(), 'image/svg+xml').documentElement
  }
  const [icon, from] = await Promise.all([parse('speaker.svg'), parse('speaker-from.svg')])
  shapes = { icon, from }
}

// `playing`: while it is read (the bars rise and fall); `from`: the arrow for "this and what comes after it".
export function speakerIcon({ playing = false, from = false } = {}) {
  return h('span', { class: playing ? 'speaker playing' : 'speaker' }, shapes.icon.cloneNode(true), from ? shapes.from.cloneNode(true) : null)
}
