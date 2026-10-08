<script setup lang="ts">
/**
 * The run drawn as a drill-core log. Every timed step is a core segment:
 * MCP calls take the texture of the server that answered, LLM rounds are grey
 * overburden. Segment length follows duration on a log scale so a 30 s LLM round
 * and a 14 ms cache hit both stay readable. Calls started together are marked
 * as parallel; planner notes and warnings sit in the margin, in order.
 */
import { computed } from 'vue'
import type { LogEntry } from '../types'

const props = defineProps<{ entries: LogEntry[]; running: boolean; elapsed: number }>()

const SERVER_LABEL = { news: '新闻', pdf: '报告', price: '价格' } as const
const MIN_PX = 30
const MAX_PX = 150
const PARALLEL_WINDOW_MS = 150

function duration(entry: LogEntry): number {
  if (entry.kind === 'note') return 0
  return entry.ms ?? Math.max(0, props.elapsed - entry.at)
}

const longest = computed(() => Math.max(1, ...props.entries.map(duration)))

/** Calls whose start is within PARALLEL_WINDOW_MS of the previous call: same batch. */
const parallel = computed(() => {
  const flags: boolean[] = []
  let previousCallAt: number | null = null
  for (const entry of props.entries) {
    const isCall = entry.kind === 'call'
    flags.push(isCall && previousCallAt !== null && entry.at - previousCallAt <= PARALLEL_WINDOW_MS)
    if (isCall) previousCallAt = entry.at
    else if (entry.kind === 'llm') previousCallAt = null
  }
  return flags
})

const llmShare = computed(() => {
  const llm = props.entries.reduce((sum, e) => sum + (e.kind === 'llm' ? duration(e) : 0), 0)
  return props.elapsed > 0 && llm > 0 ? Math.round((llm / props.elapsed) * 100) : null
})

function height(entry: LogEntry): string {
  const ratio = Math.log1p(duration(entry)) / Math.log1p(longest.value)
  return `${Math.round(MIN_PX + ratio * (MAX_PX - MIN_PX))}px`
}

function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`
}

function took(ms: number | undefined): string {
  if (ms === undefined) return '进行中'
  return ms >= 1000 ? `耗时 ${seconds(ms)}` : `耗时 ${ms} ms`
}

function llmLabel(entry: Extract<LogEntry, { kind: 'llm' }>): string {
  if (entry.phase === 'compose') return entry.status === 'running' ? '撰写摘要与风险…' : '撰写摘要与风险'
  if (entry.status === 'running') return '思考下一步…'
  return entry.calls ? `决定调用 ${entry.calls} 个工具` : '撰写最终回答'
}

function args(value: Record<string, unknown>): string {
  return Object.entries(value)
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(' · ')
}
</script>

<template>
  <section class="core-log" aria-live="polite">
    <header class="log-head">
      <span class="eyebrow">钻孔日志 · 运行过程</span>
      <span class="clock" :class="{ live: running }">
        总耗时 {{ seconds(elapsed) }}<template v-if="llmShare !== null && !running"> · LLM 占 {{ llmShare }}%</template>
      </span>
    </header>

    <p v-if="!entries.length" class="empty">
      {{ running ? '正在连接 MCP server…' : '运行后，每一步（LLM 思考、MCP 工具调用）会在这里按时间顺序成层。' }}
    </p>

    <ol class="column">
      <li v-for="(entry, i) in entries" :key="i" :class="entry.kind">
        <div class="depth">
          <template v-if="parallel[i]"><span class="par" title="与上一个调用同时开始">∥</span></template>
          <template v-else-if="entry.kind !== 'note'">+{{ seconds(entry.at) }}</template>
        </div>

        <template v-if="entry.kind === 'call'">
          <div
            class="core"
            :class="[`server-${entry.server}`, entry.status]"
            :style="{ height: height(entry) }"
            :title="`${entry.server}.${entry.tool}`"
          />
          <div class="desc">
            <div class="title">
              <span class="lith-tag" :class="`server-${entry.server}`">{{ SERVER_LABEL[entry.server] }}</span>
              <code>{{ entry.tool }}</code>
              <span v-if="parallel[i]" class="par-label">并行</span>
              <span class="ms">{{ took(entry.ms) }}</span>
            </div>
            <div class="args">{{ args(entry.args) }}</div>
            <div v-if="entry.summary" class="summary" :class="entry.status">{{ entry.summary }}</div>
          </div>
        </template>

        <template v-else-if="entry.kind === 'llm'">
          <div class="core overburden" :class="entry.status" :style="{ height: height(entry) }" />
          <div class="desc">
            <div class="title">
              <span class="lith-tag llm-tag">LLM</span>
              <span class="llm-label">{{ llmLabel(entry) }}</span>
              <span class="ms">{{ took(entry.ms) }}</span>
            </div>
            <div class="args">{{ entry.model }}</div>
          </div>
        </template>

        <template v-else>
          <div class="note-mark" :class="entry.tone" />
          <p class="note-text" :class="entry.tone">{{ entry.text }}</p>
        </template>
      </li>
    </ol>

    <footer v-if="entries.length" class="legend">
      <span class="llm-key"><i />LLM</span>
      <span class="server-news"><i />新闻</span>
      <span class="server-pdf"><i />报告</span>
      <span class="server-price"><i />价格</span>
      <span class="failed"><i />失败</span>
      <span class="hint">左侧为开始时刻；∥ 表示与上一个调用同时开始</span>
    </footer>
  </section>
</template>

<style scoped>
.core-log {
  position: sticky;
  top: 16px;
  padding: 16px 16px 12px;
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--surface);
}

.log-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}

.clock {
  font: 500 12px var(--font-data);
  font-variant-numeric: tabular-nums;
  color: var(--ink-3);
  text-align: right;
}

.clock.live {
  color: var(--ink);
}

.empty {
  margin: 8px 0 4px;
  color: var(--ink-3);
  font-size: 13px;
}

.column {
  list-style: none;
  margin: 0;
  padding: 0;
}

.column li {
  display: grid;
  grid-template-columns: 44px 22px 1fr;
  column-gap: 10px;
}

.column li > .core,
.column li > .note-mark {
  justify-self: center;
  width: 22px;
}

.depth {
  font: 11px/1 var(--font-data);
  color: var(--ink-3);
  padding-top: 3px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.par {
  font-size: 14px;
  color: var(--ink-2);
}

.core {
  margin: 1px 0;
  border-radius: 3px;
  background-color: var(--lith);
  transition: height 200ms ease-out;
}

/* Lithology textures. */
.server-news.core {
  background-image: repeating-linear-gradient(0deg, rgb(255 255 255 / 0.22) 0 1px, transparent 1px 5px);
}

.server-pdf.core {
  background-image: radial-gradient(rgb(255 255 255 / 0.35) 1px, transparent 1.2px);
  background-size: 5px 5px;
}

.server-price.core {
  background-image:
    linear-gradient(60deg, rgb(255 255 255 / 0.18) 1px, transparent 1px),
    linear-gradient(-60deg, rgb(255 255 255 / 0.18) 1px, transparent 1px);
  background-size: 8px 8px;
}

/* LLM rounds: unconsolidated overburden, grey with wavy bedding. */
.core.overburden {
  background-color: var(--ink-3);
  background-image: repeating-radial-gradient(
    ellipse 9px 3px at 50% 0,
    rgb(255 255 255 / 0.18) 0 1px,
    transparent 1px 4px
  );
  background-size: 22px 7px;
}

.core.running {
  opacity: 0.75;
  animation: drilling 900ms linear infinite;
}

.core.error {
  background-color: var(--hematite);
  background-image: repeating-linear-gradient(-45deg, rgb(255 255 255 / 0.3) 0 3px, transparent 3px 7px);
}

@keyframes drilling {
  from {
    background-position: 0 0;
  }
  to {
    background-position: 0 10px;
  }
}

.desc {
  padding: 1px 0 10px;
  min-width: 0;
}

.title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.title code {
  font: 600 13px var(--font-data);
}

.lith-tag {
  font: 600 10px/1 var(--font-data);
  letter-spacing: 0.08em;
  padding: 3px 5px;
  border-radius: 3px;
  color: var(--paper);
  background: var(--lith);
}

.llm-tag {
  background: var(--ink-3);
}

.llm-label {
  font-size: 13px;
  font-weight: 600;
}

.par-label {
  font: 10px var(--font-data);
  color: var(--ink-3);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 1px 4px;
}

.ms {
  margin-left: auto;
  font: 11px var(--font-data);
  color: var(--ink-3);
}

.args {
  font: 11px/1.4 var(--font-data);
  color: var(--ink-3);
  overflow-wrap: anywhere;
}

.summary {
  margin-top: 2px;
  font-size: 13px;
  color: var(--ink-2);
}

.summary.error {
  color: var(--hematite);
}

.note-mark {
  width: 2px !important;
  background: var(--rule);
}

.note-mark.warning {
  background: var(--hematite);
}

.note-text {
  margin: 0;
  padding: 0 0 10px;
  font-size: 12.5px;
  font-style: italic;
  color: var(--ink-2);
}

.note-text.warning {
  color: var(--hematite);
  font-style: normal;
}

.note-text.plan {
  font-style: normal;
  color: var(--ink);
}

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  margin-top: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--rule);
  font-size: 12px;
  color: var(--ink-3);
}

.legend i {
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-right: 5px;
  border-radius: 2px;
  background: var(--lith);
  vertical-align: -1px;
}

.legend .llm-key i {
  background: var(--ink-3);
}

.legend .failed i {
  background: var(--hematite);
}

.legend .hint {
  flex-basis: 100%;
  font-size: 11px;
}
</style>
