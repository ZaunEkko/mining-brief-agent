import { describe, expect, it } from 'vitest'
import { createMarkdown } from './markdown'

const md = createMarkdown()

describe('createMarkdown', () => {
  it('closes emphasis between CJK punctuation and CJK letters', () => {
    const html = md.render('**资源量为 4.46 亿吨，这不是“储量”。**最近一个月锂价下跌。')
    expect(html).toContain('<strong>资源量为 4.46 亿吨，这不是“储量”。</strong>最近')
  })

  it('never renders raw HTML from generated text', () => {
    expect(md.render('<img src=x onerror=alert(1)>')).not.toContain('<img')
  })

  it('opens links in a new tab without referrer', () => {
    expect(md.render('[来源](https://example.com)')).toContain('target="_blank" rel="noopener noreferrer"')
  })
})
