'use client'

import Link from 'next/link'
import type { PointerEvent, ReactNode } from 'react'
import { motion, useMotionValue, useReducedMotion, useSpring } from 'motion/react'
import { cn } from '@/lib/utils'

const MotionLink = motion.create(Link)

type MagneticPillButtonProps = {
  href: string
  children: ReactNode
  variant?: 'outline' | 'solid'
  className?: string
  onClick?: () => void
}

const MAGNET_STRENGTH = 0.22
const MAGNET_LIMIT = 14

export function MagneticPillButton({ href, children, variant = 'outline', className, onClick }: MagneticPillButtonProps) {
  const reduceMotion = useReducedMotion()
  const x = useMotionValue(0)
  const y = useMotionValue(0)
  const springX = useSpring(x, { stiffness: 220, damping: 18, mass: 0.4 })
  const springY = useSpring(y, { stiffness: 220, damping: 18, mass: 0.4 })

  const handlePointerMove = (e: PointerEvent<HTMLAnchorElement>) => {
    if (reduceMotion || e.pointerType !== 'mouse') return
    const rect = e.currentTarget.getBoundingClientRect()
    const clamp = (v: number) => Math.max(-MAGNET_LIMIT, Math.min(MAGNET_LIMIT, v))
    x.set(clamp((e.clientX - (rect.left + rect.width / 2)) * MAGNET_STRENGTH))
    y.set(clamp((e.clientY - (rect.top + rect.height / 2)) * MAGNET_STRENGTH))
  }

  const reset = () => {
    x.set(0)
    y.set(0)
  }

  return (
    <MotionLink
      href={href}
      onClick={onClick}
      onPointerMove={handlePointerMove}
      onPointerLeave={reset}
      style={{ x: springX, y: springY }}
      whileHover={{ scale: 1.03 }}
      whileTap={{ scale: 0.95 }}
      transition={{ type: 'spring', stiffness: 420, damping: 24 }}
      className={cn(
        'inline-flex items-center justify-center gap-3 rounded-full px-8 py-3.5 text-sm font-semibold tracking-tight uppercase transition-colors duration-200 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink sm:px-11 sm:py-4 sm:text-base',
        variant === 'outline'
          ? 'border-[1.5px] border-ink bg-paper text-ink hover:bg-white'
          : 'border-[1.5px] border-ink bg-ink text-paper hover:bg-ink/85',
        className,
      )}
    >
      {children}
    </MotionLink>
  )
}
