'use client'

import dynamic from 'next/dynamic'
import { useRef, useState } from 'react'
import type { CSSProperties, PointerEvent } from 'react'
import {
  MotionConfig,
  motion,
  useInView,
  useMotionValue,
  useReducedMotion,
  useScroll,
  useSpring,
  useTransform,
} from 'motion/react'
import { ArrowUpRight } from 'lucide-react'
import { APP_URL } from '@/lib/whodoo'
import { FloatingNavigation } from './floating-navigation'
import { FloatingSticker } from './floating-sticker'
import { HeroTypography } from './hero-typography'
import { MagneticPillButton } from './magnetic-pill-button'

const HeroScene = dynamic(() => import('./scene/hero-scene').then((m) => m.HeroScene), { ssr: false })

const SUBTITLE = ['Who', 'does', 'what?', 'WhoDoo', 'knows.']

const STICKERS = [
  {
    src: '/images/rocket.png',
    alt: 'Rocket sticker',
    className: '-top-[34%] -left-[8%] w-[14%]',
    delay: 1.0,
    rotate: -8,
    depth: -26,
    bob: 5.5,
  },
  {
    src: '/images/smiley.png',
    alt: 'Winking smiley sticker',
    className: 'top-[10%] left-[55%] w-[11%]',
    delay: 1.15,
    rotate: 8,
    depth: 20,
    bob: 4.2,
  },
  {
    src: '/images/coin-w.png',
    alt: 'Mint WhoDoo membership coin sticker',
    className: '-bottom-[30%] -left-[5%] w-[12%]',
    delay: 1.3,
    rotate: -10,
    depth: 32,
    bob: 6.4,
  },
  {
    src: '/images/wallet.png',
    alt: 'Blue wallet sticker',
    className: 'top-[42%] -right-[8%] w-[15%]',
    delay: 1.45,
    rotate: -14,
    depth: -18,
    bob: 4.8,
  },
]

export function Hero() {
  const sectionRef = useRef<HTMLElement>(null)
  const [run, setRun] = useState(0)
  const reduceMotion = useReducedMotion() ?? false
  const inView = useInView(sectionRef)

  const pointerX = useMotionValue(0)
  const pointerY = useMotionValue(0)
  const springX = useSpring(pointerX, { stiffness: 70, damping: 20, mass: 0.6 })
  const springY = useSpring(pointerY, { stiffness: 70, damping: 20, mass: 0.6 })

  const { scrollYProgress } = useScroll({ target: sectionRef, offset: ['start start', 'end start'] })
  const contentY = useTransform(scrollYProgress, [0, 1], [0, reduceMotion ? 0 : 140])
  const sceneScale = useTransform(scrollYProgress, [0, 1], [1, reduceMotion ? 1 : 1.08])

  const handlePointerMove = (e: PointerEvent<HTMLElement>) => {
    if (reduceMotion || e.pointerType !== 'mouse') return
    const rect = e.currentTarget.getBoundingClientRect()
    pointerX.set(((e.clientX - rect.left) / rect.width - 0.5) * 2)
    pointerY.set(((e.clientY - rect.top) / rect.height - 0.5) * 2)
  }

  const handlePointerLeave = () => {
    pointerX.set(0)
    pointerY.set(0)
  }

  const replay = () => {
    window.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' })
    setRun((r) => r + 1)
  }

  return (
    <MotionConfig reducedMotion="user">
      <section
        id="top"
        ref={sectionRef}
        onPointerMove={handlePointerMove}
        onPointerLeave={handlePointerLeave}
        className="relative isolate flex min-h-[calc(100svh-2.75rem)] flex-col overflow-hidden rounded-[1.75rem] bg-cream sm:min-h-[calc(100svh-3.25rem)] sm:rounded-[2.5rem]"
      >
        <motion.div aria-hidden="true" className="pointer-events-none absolute inset-0 -z-10" style={{ scale: sceneScale }}>
          <HeroScene animate={!reduceMotion} active={inView} pointerX={pointerX} pointerY={pointerY} />
        </motion.div>

        <FloatingNavigation onBrandClick={replay} />

        <motion.div
          key={run}
          style={{ y: contentY }}
          className="flex flex-1 flex-col items-center justify-center px-4 pt-24 pb-10 sm:pt-28"
        >
          <div className="relative">
            <div className="text-[clamp(4.5rem,22.5vw,min(30rem,48svh))]">
              <HeroTypography text="WhoDoo" />
            </div>
            {STICKERS.map((sticker) => (
              <FloatingSticker key={sticker.src} {...sticker} pointerX={springX} pointerY={springY} />
            ))}
          </div>

          <p className="mt-8 flex max-w-5xl flex-wrap justify-center gap-x-[0.25em] text-center text-[clamp(1.9rem,5.4vw,4.5rem)] leading-[1.05] font-semibold tracking-[-0.045em] text-ink sm:mt-10">
            {SUBTITLE.map((word, i) => (
              <span key={`${word}-${i}`} className="inline-block overflow-hidden pb-[0.08em]">
                <span className="rise inline-block" style={{ '--d': `${1.1 + i * 0.08}s` } as CSSProperties}>
                  {word}
                </span>
              </span>
            ))}
          </p>

          <motion.div
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, delay: 1.45, ease: [0.22, 1, 0.36, 1] }}
            className="mt-6 flex flex-col items-center gap-3 sm:mt-8 sm:flex-row sm:gap-4"
          >
            <MagneticPillButton href={APP_URL}>Enter WhoDoo</MagneticPillButton>
            <MagneticPillButton href="#features" className="group">
              Explore features
              <ArrowUpRight
                aria-hidden="true"
                className="size-4 transition-transform duration-200 group-hover:translate-x-0.5 group-hover:-translate-y-0.5"
              />
            </MagneticPillButton>
          </motion.div>
        </motion.div>
      </section>
    </MotionConfig>
  )
}
