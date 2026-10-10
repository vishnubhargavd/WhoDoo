'use client'

import Image from 'next/image'
import type { CSSProperties } from 'react'
import { motion, useTransform } from 'motion/react'
import type { MotionValue } from 'motion/react'
import { cn } from '@/lib/utils'

type FloatingStickerProps = {
  src: string
  alt: string
  className?: string
  delay: number
  rotate: number
  depth: number
  bob?: number
  pointerX: MotionValue<number>
  pointerY: MotionValue<number>
}

export function FloatingSticker({
  src,
  alt,
  className,
  delay,
  rotate,
  depth,
  bob = 5,
  pointerX,
  pointerY,
}: FloatingStickerProps) {
  const x = useTransform(pointerX, (v) => v * depth)
  const y = useTransform(pointerY, (v) => v * depth)
  const tilt = useTransform(pointerX, (v) => v * depth * 0.08)

  return (
    <motion.div className={cn('pointer-events-none absolute z-20', className)} style={{ x, y, rotate: tilt }}>
      <div className="sticker-pop" style={{ '--d': `${delay}s`, '--rot': `${rotate}deg` } as CSSProperties}>
        <div className="sticker-bob" style={{ '--d': `${delay}s`, '--bob': `${bob}s` } as CSSProperties}>
          <Image
            src={src || '/placeholder.svg'}
            alt={alt}
            width={512}
            height={512}
            priority
            className="h-auto w-full drop-shadow-[0_18px_22px_rgba(80,30,10,0.28)]"
          />
        </div>
      </div>
    </motion.div>
  )
}
