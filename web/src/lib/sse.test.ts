import { describe, expect, it } from 'vitest'
import { createSseParser, parseBlock, type SseMessage } from './sse'

describe('parseBlock', () => {
  it('reads event name and JSON data', () => {
    expect(parseBlock('event: tool_call\ndata: {"tool":"search"}')).toEqual({
      event: 'tool_call',
      data: { tool: 'search' },
    })
  })

  it('defaults the event name and ignores comment-only blocks', () => {
    expect(parseBlock('data: 1')).toEqual({ event: 'message', data: 1 })
    expect(parseBlock(': keep-alive')).toBeNull()
  })
})

describe('createSseParser', () => {
  it('reassembles messages split across chunks and CRLF line endings', () => {
    const seen: SseMessage[] = []
    const parser = createSseParser((m) => seen.push(m))

    parser.push('event: plan\r\ndata: {"sub')
    parser.push('ject":"PLS"}\r\n\r\nevent: done\ndata: {"markdown":"# 日报"}\n')
    expect(seen).toHaveLength(1)
    parser.push('\n')

    expect(seen).toEqual([
      { event: 'plan', data: { subject: 'PLS' } },
      { event: 'done', data: { markdown: '# 日报' } },
    ])
  })
})
