/**
 * Server-Sent Events over a POST response.
 *
 * EventSource only supports GET, so the UI POSTs with fetch() and parses the
 * `event:` / `data:` blocks from the body stream itself.
 */

export interface SseMessage {
  event: string
  data: unknown
}

export function parseBlock(block: string): SseMessage | null {
  let event = 'message'
  const data: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
  }
  if (data.length === 0) return null
  return { event, data: JSON.parse(data.join('\n')) }
}

/** Incremental parser: feed arbitrary chunks, get whole messages back. */
export function createSseParser(onMessage: (message: SseMessage) => void) {
  let buffer = ''
  return {
    push(chunk: string): void {
      buffer += chunk.replace(/\r\n/g, '\n')
      let end = buffer.indexOf('\n\n')
      while (end >= 0) {
        const message = parseBlock(buffer.slice(0, end))
        buffer = buffer.slice(end + 2)
        if (message) onMessage(message)
        end = buffer.indexOf('\n\n')
      }
    },
  }
}

export class ApiError extends Error {}

/** POST JSON and stream SSE messages until the server closes the response. */
export async function postSse(
  url: string,
  body: unknown,
  onMessage: (message: SseMessage) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok || !response.body) {
    throw new ApiError(await errorText(response))
  }
  const parser = createSseParser(onMessage)
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    parser.push(value)
  }
}

export async function errorText(response: Response): Promise<string> {
  const text = await response.text()
  try {
    const parsed: unknown = JSON.parse(text)
    if (parsed && typeof parsed === 'object' && 'error' in parsed) return String(parsed.error)
  } catch {
    // not JSON; fall through to the raw text
  }
  return text || `HTTP ${response.status}`
}
