'use client'

import Link from 'next/link'
import { AnimatePresence, motion } from 'motion/react'
import { APP_URL, MENU_LINKS } from '@/lib/whodoo'

const EASE_OUT = [0.22, 1, 0.36, 1] as const

const listVariants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.025, delayChildren: 0.06 } },
}

const itemVariants = {
  hidden: { opacity: 0, y: -8, scale: 0.96 },
  show: { opacity: 1, y: 0, scale: 1, transition: { duration: 0.32, ease: EASE_OUT } },
}

export function NavigationMenu({ open, onNavigate }: { open: boolean; onNavigate: () => void }) {
  return (
    <AnimatePresence>
      {open && (
        <motion.nav
          id="site-menu"
          aria-label="Main"
          initial={{ opacity: 0, y: -12, scale: 0.96 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -8, scale: 0.97, transition: { duration: 0.2, ease: EASE_OUT } }}
          transition={{ duration: 0.4, ease: EASE_OUT }}
          className="absolute top-full right-0 mt-3 w-[min(34rem,calc(100vw-2rem))] origin-top-right overflow-hidden rounded-[1.75rem] border border-ink/60 bg-white/40 p-3 shadow-[0_30px_60px_-20px_rgba(40,10,0,0.4)] backdrop-blur-2xl backdrop-saturate-150 sm:p-4"
        >
          <div aria-hidden="true" className="pointer-events-none absolute inset-0">
            <div className="absolute -top-10 -right-6 size-48 rounded-full bg-coral/35 blur-3xl" />
            <div className="absolute top-1/3 -left-10 size-40 rounded-full bg-mint/40 blur-3xl" />
            <div className="absolute right-10 bottom-6 size-36 rounded-full bg-cobalt/25 blur-3xl" />
            <div className="absolute bottom-0 left-1/3 size-32 rounded-full bg-butter/40 blur-3xl" />
          </div>

          <motion.ul
            variants={listVariants}
            initial="hidden"
            animate="show"
            className="relative grid grid-cols-2 gap-2 sm:gap-2.5"
          >
            {MENU_LINKS.map((link) => (
              <motion.li key={link.label} variants={itemVariants}>
                <Link
                  href={link.href}
                  onClick={onNavigate}
                  className="flex h-11 items-center justify-center rounded-full border border-ink/80 bg-white/75 px-2 text-center text-xs font-semibold tracking-tight text-ink uppercase transition-colors duration-200 hover:bg-ink hover:text-paper focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink sm:h-13 sm:text-sm"
                >
                  {link.label}
                </Link>
              </motion.li>
            ))}
            <motion.li variants={itemVariants} className="col-span-2">
              <Link
                href={APP_URL}
                onClick={onNavigate}
                className="flex h-11 items-center justify-center rounded-full bg-ink text-xs font-semibold tracking-tight text-paper uppercase transition-transform duration-200 hover:scale-[1.015] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink sm:h-13 sm:text-sm"
              >
                Enter WhoDoo
              </Link>
            </motion.li>
          </motion.ul>
        </motion.nav>
      )}
    </AnimatePresence>
  )
}
