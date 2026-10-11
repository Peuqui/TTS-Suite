// Plays what a reading sends, piece after piece as the pieces arrive (WebAudio), and reports the
// course of the reading (the last frame).
import { FrameReader } from './frames.js'

export class Player {
  constructor() { this.context = null; this.sources = new Set() }

  /** Reads the frames of the response and plays the audio; resolves with the summary, or with null when
   * `signal` (that of the request) was aborted. A failure after the first piece is thrown. */
  async play(response, signal) {
    this.cancel()
    const context = (this.context ??= new AudioContext())
    await context.resume()
    const reader = response.body.getReader()
    const frames = new FrameReader()
    let startAt = context.currentTime
    let lastEnded = Promise.resolve()
    let summary = null
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        for (const { header, body } of frames.push(value)) {
          if (header.kind === 'error') {
            const failure = JSON.parse(new TextDecoder().decode(body))
            throw new Error(`${failure.error}: ${failure.detail}`)
          }
          if (header.kind === 'summary') summary = JSON.parse(new TextDecoder().decode(body))
          if (header.kind !== 'audio' || header.keepalive || body.length === 0) continue
          const buffer = await context.decodeAudioData(body.buffer)
          const source = context.createBufferSource()
          source.buffer = buffer
          source.connect(context.destination)
          startAt = Math.max(startAt, context.currentTime)
          source.start(startAt)
          startAt += buffer.duration
          this.sources.add(source)
          lastEnded = new Promise((resolve) => { source.onended = resolve })
        }
      }
      await lastEnded
    } catch (error) {
      if (signal.aborted) return null
      throw error
    }
    return summary
  }

  cancel() {
    for (const source of this.sources) source.stop()
    this.sources = new Set()
  }
}
