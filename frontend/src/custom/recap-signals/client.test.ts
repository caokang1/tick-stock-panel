import { describe, expect, it } from 'vitest'
import { signalText } from './client'

describe('signalText', () => {
  it('说清方向和价位', () => {
    expect(signalText({ direction: 'below', price: 26.35 })).toBe('跌破 26.35')
    expect(signalText({ direction: 'above', price: 32.99 })).toBe('突破 32.99')
  })
})
