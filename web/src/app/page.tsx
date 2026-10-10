import Link from 'next/link';
import { SetupStatus } from '../components/setup-status';

const modules = [
  { name: 'Memberships', href: '/memberships', description: 'Your membership, renewals and member benefits.' },
  { name: 'Bookings', href: '/bookings', description: 'Tickets, member pricing and event waitlists.' },
  { name: 'Check-in', href: '/check-in', description: 'QR tickets and authorized event check-in.' }
];

export default function HomePage() {
  return (
    <>
      <p className="text-sm font-semibold uppercase tracking-widest text-teal-700">Your organization, together</p>
      <h1 className="mt-4 text-4xl font-bold tracking-tight sm:text-5xl">Welcome to WhoDoo.</h1>
      <p className="mt-4 max-w-2xl leading-7 text-slate-600">A workspace for your student organization. Explore the areas below as we connect memberships, bookings and check-in.</p>
      <div className="mt-10 grid gap-4 sm:grid-cols-3">
        {modules.map(module => (
          <Link key={module.href} href={module.href} className="rounded-xl border border-slate-200 bg-white p-6 transition hover:border-teal-600">
            <span className="text-xs font-medium text-slate-500">Not connected yet</span>
            <h2 className="mt-3 text-lg font-semibold">{module.name} <span aria-hidden="true">→</span></h2>
            <p className="mt-3 text-sm leading-6 text-slate-600">{module.description}</p>
          </Link>
        ))}
      </div>
      <SetupStatus />
    </>
  );
}
