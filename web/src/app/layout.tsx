import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import Link from 'next/link';
import './globals.css';

export const metadata: Metadata = { title: 'WhoDoo', description: 'Your student organization workspace.' };
export const dynamic = 'force-dynamic';

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:z-10 focus:bg-white focus:p-4">Skip to content</a>
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-5xl flex-col gap-4 px-6 py-5 sm:flex-row sm:items-center sm:justify-between">
            <Link href="/" className="text-xl font-bold tracking-tight">WhoDoo<span className="ml-2 text-sm font-normal text-slate-500">Workspace</span></Link>
            <nav aria-label="Primary navigation" className="flex flex-wrap gap-x-5 gap-y-2 text-sm text-slate-600">
              <Link href="/memberships" className="hover:text-teal-700">Memberships</Link>
              <Link href="/bookings" className="hover:text-teal-700">Bookings</Link>
              <Link href="/check-in" className="hover:text-teal-700">Check-in</Link>
              <Link href="/account" className="hover:text-teal-700">Account</Link>
              <Link href="/login" className="hover:text-teal-700">Sign in</Link>
            </nav>
          </div>
        </header>
        <main id="main" className="mx-auto min-h-[70vh] max-w-5xl px-6 py-10 sm:py-14">{children}</main>
        <footer className="mx-auto max-w-5xl px-6 py-6 text-sm text-slate-500">WhoDoo · Student organization workspace</footer>
      </body>
    </html>
  );
}
