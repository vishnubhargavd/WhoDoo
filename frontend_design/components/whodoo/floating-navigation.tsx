'use client'

import { useEffect, useRef, useState } from 'react'
import { motion } from 'motion/react'
import { NavigationMenu } from './navigation-menu'

const controlClass =
  'relative grid size-12 place-items-center rounded-full border-[1.5px] border-ink bg-paper text-ink focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink'

export function FloatingNavigation({ onBrandClick }: { onBrandClick: () => void }) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const toggleRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setOpen(false)
      toggleRef.current?.focus()
    }
    const onDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('pointerdown', onDown)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('pointerdown', onDown)
    }
  }, [open])

  return (
    <header className="absolute inset-x-0 top-0 z-40 flex items-start justify-between p-4 sm:p-6">
      <motion.button
        type="button"
        onClick={onBrandClick}
        aria-label="WhoDoo home, replay intro"
        initial={{ opacity: 0, scale: 0.6 }}
        animate={{ opacity: 1, scale: 1 }}
        whileHover={{ scale: 1.06 }}
        whileTap={{ scale: 0.94 }}
        transition={{ type: 'spring', stiffness: 380, damping: 22 }}
        className={controlClass}
      >
        <span aria-hidden="true" className="font-display text-2xl leading-none -skew-x-12">
          W
        </span>
      </motion.button>

      <div ref={wrapRef} className="relative flex flex-col items-end">
        <motion.button
          ref={toggleRef}
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls="site-menu"
          aria-label={open ? 'Close menu' : 'Open menu'}
          initial={{ opacity: 0, scale: 0.6 }}
          animate={{ opacity: 1, scale: 1 }}
          whileHover={{ scale: 1.06 }}
          whileTap={{ scale: 0.94 }}
          transition={{ type: 'spring', stiffness: 380, damping: 22 }}
          className={controlClass}
        >
          {[-1, 0, 1].map((n) => (
            <span
              key={n}
              aria-hidden="true"
              className="absolute h-[2px] w-5 rounded-full bg-ink transition-all duration-300 ease-out"
              style={{
                transform: open ? (n === 0 ? 'scaleX(0)' : `rotate(${n * 45}deg)`) : `translateY(${n * 6}px)`,
                opacity: open && n === 0 ? 0 : 1,
              }}
            />
          ))}
        </motion.button>

        <NavigationMenu open={open} onNavigate={() => setOpen(false)} />
      </div>
    </header>
  )
}
