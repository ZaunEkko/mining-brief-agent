import { describe, expect, it } from 'vitest'
import { createLatest } from './latest'

describe('createLatest', () => {
  it('lets only the newest request write its result', () => {
    const latest = createLatest()
    const priceCall = latest.next()
    latest.invalidate() // user selects another tool while the call is in flight
    expect(latest.isCurrent(priceCall)).toBe(false)

    const pdfCall = latest.next()
    expect(latest.isCurrent(pdfCall)).toBe(true)
  })
})
