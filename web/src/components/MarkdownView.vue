<script setup lang="ts">
import { computed } from 'vue'
import { createMarkdown } from '../lib/markdown'

const props = defineProps<{ source: string }>()
const md = createMarkdown()
const html = computed(() => md.render(props.source))
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -- sanitised: markdown-it with html disabled -->
  <article class="markdown" v-html="html" />
</template>

<style scoped>
.markdown {
  max-width: 78ch;
  overflow-wrap: anywhere;
}

.markdown :deep(h1) {
  margin: 0 0 12px;
  font: 700 clamp(26px, 3.4vw, 36px) / 1.15 var(--font-display);
  font-stretch: 72%;
  letter-spacing: -0.01em;
}

.markdown :deep(h2) {
  margin: 32px 0 10px;
  padding-top: 14px;
  border-top: 1px solid var(--rule);
  font: 700 19px/1.3 var(--font-display);
  font-stretch: 80%;
}

.markdown :deep(blockquote) {
  margin: 12px 0;
  padding: 8px 14px;
  border-left: 3px solid var(--rule);
  color: var(--ink-2);
  background: var(--surface);
}

.markdown :deep(table) {
  width: 100%;
  margin: 10px 0;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
  font-size: 14px;
  display: block;
  overflow-x: auto;
}

.markdown :deep(th),
.markdown :deep(td) {
  padding: 7px 10px;
  border-bottom: 1px solid var(--rule);
  text-align: left;
  white-space: nowrap;
}

.markdown :deep(th) {
  font: 600 12px var(--font-data);
  letter-spacing: 0.04em;
  color: var(--ink-3);
}

.markdown :deep(code) {
  font: 13px var(--font-data);
}

.markdown :deep(a) {
  color: var(--shale);
  text-underline-offset: 2px;
}

.markdown :deep(li) {
  margin: 6px 0;
}

.markdown :deep(hr) {
  margin: 28px 0 16px;
  border: none;
  border-top: 1px dashed var(--rule);
}
</style>
