import { computed, ref, shallowRef } from 'vue'
import type { CallEntry, LogEntry, ServerName } from '../types'
import { postSse } from './sse'

type Payload = Record<string, unknown>

/**
 * State for one streamed run (brief or ask): the core-log entries, the final
 * payload and the error, plus `start` / `stop` controls.
 */
export function useRun<Done>(url: string) {
  const entries = ref<LogEntry[]>([])
  const result = shallowRef<Done | null>(null)
  const error = ref<string | null>(null)
  const running = ref(false)
  const elapsed = ref(0)
  let controller: AbortController | null = null
  let started = 0
  let timer: ReturnType<typeof setInterval> | undefined

  const calls = computed(() => entries.value.filter((e): e is CallEntry => e.kind === 'call'))

  function now(): number {
    return Math.round(performance.now() - started)
  }

  function note(tone: 'plan' | 'text' | 'warning', text: string): void {
    entries.value.push({ kind: 'note', tone, text, at: now() })
  }

  function handle(event: string, data: Payload): void {
    switch (event) {
      case 'plan':
        note('plan', describePlan(data))
        break
      case 'llm':
        entries.value.push({
          kind: 'llm',
          id: String(data.id),
          model: String(data.model),
          phase: data.phase === 'compose' ? 'compose' : 'decide',
          status: 'running',
          at: now(),
        })
        break
      case 'llm_done': {
        const entry = entries.value.find((e) => e.kind === 'llm' && e.id === data.id)
        if (entry?.kind === 'llm') {
          entry.status = data.ok === false ? 'error' : 'ok'
          entry.ms = Number(data.ms)
          if (typeof data.calls === 'number') entry.calls = data.calls
        }
        break
      }
      case 'text':
        note('text', String(data.text))
        break
      case 'warning':
        note('warning', String(data.message))
        break
      case 'tool_call':
        entries.value.push({
          kind: 'call',
          id: String(data.id),
          server: data.server as ServerName,
          tool: String(data.tool),
          args: (data.arguments ?? {}) as Record<string, unknown>,
          status: 'running',
          at: now(),
        })
        break
      case 'tool_result': {
        const entry = calls.value.find((c) => c.id === data.id)
        if (entry) {
          entry.status = data.ok ? 'ok' : 'error'
          entry.ms = Number(data.ms)
          entry.summary = String(data.summary)
        }
        break
      }
      case 'done':
        result.value = data as Done
        break
      case 'error':
        error.value = String(data.message)
        break
    }
  }

  async function start(body: Payload): Promise<void> {
    stop()
    entries.value = []
    result.value = null
    error.value = null
    running.value = true
    started = performance.now()
    elapsed.value = 0
    timer = setInterval(() => (elapsed.value = now()), 100)
    controller = new AbortController()
    try {
      await postSse(url, body, (m) => handle(m.event, m.data as Payload), controller.signal)
      if (!result.value && !error.value) error.value = '连接在完成前中断，请重试。'
    } catch (e) {
      if (!(e instanceof DOMException && e.name === 'AbortError')) {
        error.value = e instanceof Error ? e.message : String(e)
      }
    } finally {
      running.value = false
      elapsed.value = now()
      clearInterval(timer)
    }
  }

  function stop(): void {
    controller?.abort()
    controller = null
  }

  return { entries, calls, result, error, running, elapsed, start, stop }
}

function describePlan(data: Payload): string {
  const parts = [`对象：${String(data.subject)}`]
  if (data.commodity) parts.push(`品种：${String(data.commodity)}`)
  parts.push(`新闻检索「${String(data.news_query)}」近 ${String(data.news_days)} 天`)
  if (data.report) parts.push('附资源量报告')
  return parts.join(' · ')
}
