<script setup lang="ts">
/** Brief and ask share one workbench: request on top, core log left, result right. */
import { computed, ref } from 'vue'
import { useRun } from '../lib/run'
import type { AskDone, BriefDone, Meta } from '../types'
import CoreLog from './CoreLog.vue'
import MarkdownView from './MarkdownView.vue'

const props = defineProps<{ mode: 'brief' | 'ask'; meta: Meta | null }>()

const isBrief = computed(() => props.mode === 'brief')
const run = useRun<BriefDone | AskDone>(isBrief.value ? '/api/brief' : '/api/ask')
const text = ref(isBrief.value ? (props.meta?.examples.brief[0] ?? '') : '')
const useLlm = ref(true)

const examples = computed(() =>
  isBrief.value ? (props.meta?.examples.brief ?? []) : (props.meta?.examples.ask ?? []),
)
const needsLlm = computed(() => !isBrief.value && props.meta !== null && !props.meta.llm)
const canRun = computed(() => !run.running.value && text.value.trim().length > 0 && !needsLlm.value)
const footnote = computed(() => {
  const r = run.result.value
  if (!r) return null
  if ('generator' in r) return `生成方式：${r.generator}`
  const unverified = r.unverified_urls.length ? ` · ${r.unverified_urls.length} 个链接未验证` : ''
  return `${r.model} · ${r.calls.length} 次工具调用${unverified}`
})

function submit(): void {
  if (!canRun.value) return
  const body = isBrief.value
    ? { query: text.value.trim(), use_llm: useLlm.value }
    : { question: text.value.trim() }
  void run.start(body)
}

function pick(example: string): void {
  text.value = example
  submit()
}
</script>

<template>
  <div class="workbench">
    <form class="request" @submit.prevent="submit">
      <label :for="`q-${mode}`" class="eyebrow">
        {{ isBrief ? '日报请求 · 固定流程：代码决定调用哪些工具' : '提问 · 由 LLM 自己决定调用哪些工具' }}
      </label>
      <div class="row">
        <textarea
          :id="`q-${mode}`"
          v-model="text"
          class="field"
          rows="2"
          :placeholder="isBrief ? '例如：给我生成一份关于 Pilbara 锂矿的今日简报' : '例如：Pilgangoora 的储量和最近锂价对项目有什么影响？'"
          @keydown.enter.exact.prevent="submit"
        />
        <div class="actions">
          <button v-if="!run.running.value" class="btn" type="submit" :disabled="!canRun">
            {{ isBrief ? '生成日报' : '提问' }}
          </button>
          <button v-else class="btn ghost" type="button" @click="run.stop()">停止</button>
          <label v-if="isBrief" class="toggle" :class="{ off: !meta?.llm }">
            <input v-model="useLlm" type="checkbox" :disabled="!meta?.llm" />
            {{ meta?.llm ? 'LLM 撰写摘要' : '未配置 LLM：确定性模式' }}
          </label>
        </div>
      </div>
      <div class="chips">
        <button v-for="e in examples" :key="e" type="button" class="chip" @click="pick(e)">{{ e }}</button>
      </div>
      <p v-if="needsLlm" class="error-box">
        问答模式需要 LLM。在项目根目录的 <code>.env</code> 中配置 <code>ANTHROPIC_API_KEY</code> 或
        <code>MB_OPENAI_*</code>，然后重启服务。
      </p>
    </form>

    <div class="split">
      <aside class="log">
        <CoreLog :entries="run.entries.value" :running="run.running.value" :elapsed="run.elapsed.value" />
      </aside>
      <main class="result">
        <p v-if="run.error.value" class="error-box">{{ run.error.value }}</p>
        <MarkdownView v-if="run.result.value" :source="run.result.value.markdown" />
        <div v-else-if="run.running.value" class="placeholder">
          <span class="eyebrow">取样中</span>
          <p>{{ isBrief ? '正在并发调用新闻、报告和价格三个 server…' : 'LLM 正在决定下一步要调用的工具…' }}</p>
        </div>
        <div v-else-if="!run.error.value" class="placeholder">
          <span class="eyebrow">{{ isBrief ? '日报' : '回答' }}</span>
          <p>
            {{
              isBrief
                ? '日报包含新闻摘要、资源量表、价格走势和风险提示，每条事实都带编号来源。'
                : '回答中的数字只取自工具结果；未出现在工具结果里的链接会被标为未验证。'
            }}
          </p>
        </div>
        <p v-if="footnote" class="footnote">{{ footnote }}</p>
      </main>
    </div>
  </div>
</template>

<style scoped>
.request {
  display: grid;
  gap: 10px;
  margin-bottom: 24px;
}

.row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 12px;
  align-items: start;
}

.row textarea {
  resize: vertical;
  min-height: 52px;
  font-size: 16px;
}

.actions {
  display: grid;
  gap: 8px;
  justify-items: start;
}

.toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--ink-2);
  white-space: nowrap;
}

.toggle.off {
  color: var(--ink-3);
}

.split {
  display: grid;
  grid-template-columns: minmax(280px, 360px) 1fr;
  gap: clamp(20px, 3vw, 40px);
  align-items: start;
}

.result {
  min-width: 0;
}

.placeholder {
  padding: 28px 0;
  color: var(--ink-2);
  max-width: 52ch;
}

.placeholder p {
  margin: 8px 0 0;
}

.footnote {
  margin-top: 24px;
  font: 12px var(--font-data);
  color: var(--ink-3);
}

@media (max-width: 820px) {
  .row {
    grid-template-columns: 1fr;
  }

  .actions {
    grid-auto-flow: column;
    justify-content: start;
    align-items: center;
    gap: 16px;
  }

  .split {
    grid-template-columns: 1fr;
  }

  .log :deep(.core-log) {
    position: static;
  }
}
</style>
