import type { CSSProperties } from 'react'

const SLICES = 9

// Neighbouring strips fly in from opposite sides at different distances,
// producing the shredded look mid-animation seen in the reference.
const OFFSETS = [-38, 26, -14, 44, -30, 18, -46, 34, -22]

export function HeroTypography({ text, delay = 0.25 }: { text: string; delay?: number }) {
  const step = 100 / SLICES

  return (
    <h1 className="relative select-none font-display leading-[0.8] tracking-[-0.015em] whitespace-nowrap text-ink uppercase">
      <span className="sr-only">{text}</span>
      <span aria-hidden="true" className="title-settle block">
        {text}
      </span>
      {Array.from({ length: SLICES }, (_, i) => {
        const top = i * step
        const bottom = 100 - (i + 1) * step
        const style = {
          clipPath: `inset(${Math.max(top - 0.6, 0)}% -2% ${Math.max(bottom - 0.6, 0)}% -2%)`,
          '--sx': `${OFFSETS[i % OFFSETS.length]}%`,
          '--sy': `${i % 2 === 0 ? -6 : 6}%`,
          '--d': `${delay + (i % 3) * 0.07 + i * 0.035}s`,
        } as CSSProperties
        return (
          <span key={i} aria-hidden="true" className="slice absolute inset-0 block will-change-transform" style={style}>
            {text}
          </span>
        )
      })}
    </h1>
  )
}
