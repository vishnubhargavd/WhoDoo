import type { Metadata } from 'next'
import Link from 'next/link'
import { ArrowLeft } from 'lucide-react'

export const metadata: Metadata = {
  title: 'Sign in — WhoDoo',
  description: 'Sign in to WhoDoo, the platform for student organizations.',
}

export default function LoginPage() {
  return (
    <main className="flex min-h-svh flex-col bg-ink p-2 sm:p-3">
      <div className="flex flex-1 flex-col items-center justify-center gap-8 rounded-[1.75rem] bg-cream px-6 py-16 text-center sm:rounded-[2.5rem]">
        <span
          aria-hidden="true"
          className="grid size-14 place-items-center rounded-full border-[1.5px] border-ink bg-paper font-display text-3xl -skew-x-12"
        >
          W
        </span>
        <h1 className="font-display text-[clamp(3.5rem,12vw,8rem)] leading-[0.85] text-ink uppercase">
          Enter WhoDoo
        </h1>
        <p className="max-w-md text-lg leading-relaxed text-ink/75 text-pretty">
          This is where WhoDoo sign-in lives. Your organization&apos;s account access will connect here.
        </p>
        <Link
          href="/"
          className="inline-flex items-center gap-2 rounded-full border-[1.5px] border-ink bg-paper px-8 py-3.5 text-sm font-semibold tracking-tight text-ink uppercase transition-colors hover:bg-white focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink"
        >
          <ArrowLeft aria-hidden="true" className="size-4" />
          Back home
        </Link>
      </div>
    </main>
  )
}
