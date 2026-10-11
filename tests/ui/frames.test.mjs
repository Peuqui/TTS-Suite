import assert from 'node:assert/strict'
import { test } from 'node:test'
import { FrameReader } from '../../src/tts_suite/service/ui/frames.js'

const frame = (header, body = []) => {
  const head = new TextEncoder().encode(JSON.stringify({ ...header, bytes: body.length }))
  return [0, 0, 0, head.length, ...head, ...body]
}

test('frames in one chunk come out with header and body', () => {
  const data = new Uint8Array([...frame({ kind: 'audio', engine: 'xtts' }, [1, 2, 3]), ...frame({ kind: 'audio', keepalive: true }), ...frame({ kind: 'summary' }, [123, 125])])
  const frames = new FrameReader().push(data)
  assert.deepEqual(frames.map((each) => each.header.kind), ['audio', 'audio', 'summary'])
  assert.deepEqual([...frames[0].body], [1, 2, 3])
  assert.equal(frames[1].body.length, 0)
})

test('a frame cut at any place is completed by the next chunk', () => {
  const data = new Uint8Array([...frame({ kind: 'audio' }, [5, 6, 7, 8]), ...frame({ kind: 'summary' })])
  for (let cut = 1; cut < data.length; cut++) {
    const reader = new FrameReader()
    const frames = [...reader.push(data.slice(0, cut)), ...reader.push(data.slice(cut))]
    assert.deepEqual(frames.map((each) => each.header.kind), ['audio', 'summary'], `cut at ${cut}`)
    assert.equal(reader.incomplete, false)
  }
})

test('a stream that ends inside a frame is told', () => {
  const reader = new FrameReader()
  assert.deepEqual(reader.push(new Uint8Array(frame({ kind: 'audio' }, [1, 2, 3])).slice(0, 9)), [])
  assert.equal(reader.incomplete, true)
})
