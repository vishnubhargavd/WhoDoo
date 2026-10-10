'use client'

import Link from 'next/link'
import { MotionConfig, motion } from 'motion/react'
import { ArrowUp } from 'lucide-react'
import { APP_URL, FEATURES } from '@/lib/whodoo'
import { cn } from '@/lib/utils'
import { MagneticPillButton } from './magnetic-pill-button'

const ACCENTS = ['bg-coral', 'bg-mint', 'bg-butter', 'bg-cobalt']
const EASE_OUT = [0.22, 1, 0.36, 1] as const

export function ProductOverview() {
  return (
    <MotionConfig reducedMotion="user">
      <section
        id="features"
        aria-labelledby="features-title"
        className="scroll-mt-4 rounded-[1.75rem] bg-cream px-5 py-16 sm:rounded-[2.5rem] sm:px-10 sm:py-24 lg:px-14"
      >
        <div className="mx-auto max-w-7xl">
          <div className="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
            <motion.h2
              id="features-title"
              initial={{ opacity: 0, y: 40 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: '-80px' }}
              transition={{ duration: 0.9, ease: EASE_OUT }}
              className="font-display text-[clamp(3.5rem,11vw,9.5rem)] leading-[0.85] tracking-[-0.01em] text-ink uppercase"
            >
              Twelve tools.
              <br />
              One campus.
            </motion.h2>
            <p className="max-w-sm text-lg leading-relaxed text-ink/75 text-pretty">
              Memberships, tickets, attendance, volunteers, money and more, all run from one place for every student
              organization.
            </p>
          </div>

          <ul className="mt-12 grid grid-cols-1 gap-4 sm:mt-16 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map((feature, i) => (
              <motion.li
                key={feature.id}
                id={feature.id}
                initial={{ opacity: 0, y: 32 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true, margin: '-60px' }}
                transition={{ duration: 0.7, delay: (i % 3) * 0.08, ease: EASE_OUT }}
                className="scroll-mt-6"
              >
                <article className="group flex h-full flex-col gap-8 rounded-[1.75rem] border-[1.5px] border-ink bg-paper p-6 transition-[transform,box-shadow] duration-300 ease-out hover:-translate-y-1 hover:shadow-[6px_6px_0_#080808] sm:p-7">
                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-2.5">
                      <span aria-hidden="true" className={cn('size-3 rounded-full border border-ink', ACCENTS[i % ACCENTS.length])} />
                      <span className="font-display text-lg text-ink">{String(i + 1).padStart(2, '0')}</span>
                    </span>
                    <span className="rounded-full border border-ink px-3 py-1 text-[11px] font-semibold tracking-wide text-ink uppercase">
                      {feature.category}
                    </span>
                  </div>
                  <div className="mt-auto">
                    <h3 className="font-display text-4xl leading-[0.95] text-ink uppercase">{feature.name}</h3>
                    <p className="mt-3 leading-relaxed text-ink/75">{feature.description}</p>
                  </div>
                </article>
              </motion.li>
            ))}
          </ul>

          <motion.div
            initial={{ opacity: 0, y: 40 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: '-80px' }}
            transition={{ duration: 0.9, ease: EASE_OUT }}
            className="mt-16 flex flex-col items-start gap-8 rounded-[2rem] border-[1.5px] border-ink bg-coral p-8 sm:mt-24 sm:p-12 lg:flex-row lg:items-end lg:justify-between"
          >
            <p className="font-display text-[clamp(2.75rem,7vw,6rem)] leading-[0.9] text-ink uppercase">
              Who does what?
              <br />
              WhoDoo knows.
            </p>
            <div className="flex flex-col gap-3 sm:flex-row">
              <MagneticPillButton href={APP_URL}>Enter WhoDoo</MagneticPillButton>
              <Link
                href="#top"
                className="inline-flex items-center justify-center gap-2 rounded-full px-6 py-3.5 text-sm font-semibold tracking-tight text-ink uppercase underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink sm:text-base"
              >
                Back to top
                <ArrowUp aria-hidden="true" className="size-4" />
              </Link>
            </div>
          </motion.div>
        </div>
      </section>
    </MotionConfig>
  )
}
