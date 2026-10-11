// The frames the service answers a reading with: the length of the header (uint32, big endian), the
// header as UTF-8 JSON (`kind`, `bytes`, ...), then the body. They arrive in chunks of any size.
const LENGTH_BYTES = 4

export class FrameReader {
  constructor() { this.buffer = new Uint8Array(0) }

  /** The frames the data so far completes. */
  push(chunk) {
    const joined = new Uint8Array(this.buffer.length + chunk.length)
    joined.set(this.buffer)
    joined.set(chunk, this.buffer.length)
    this.buffer = joined
    const frames = []
    for (let frame = this.take(); frame !== null; frame = this.take()) frames.push(frame)
    return frames
  }

  /** Whether the stream ended inside a frame. */
  get incomplete() { return this.buffer.length > 0 }

  take() {
    if (this.buffer.length < LENGTH_BYTES) return null
    const view = new DataView(this.buffer.buffer, this.buffer.byteOffset, this.buffer.byteLength)
    const bodyStart = LENGTH_BYTES + view.getUint32(0)
    if (this.buffer.length < bodyStart) return null
    const header = JSON.parse(new TextDecoder().decode(this.buffer.subarray(LENGTH_BYTES, bodyStart)))
    const end = bodyStart + header.bytes
    if (this.buffer.length < end) return null
    const body = this.buffer.slice(bodyStart, end)
    this.buffer = this.buffer.slice(end)
    return { header, body }
  }
}
