import Link from 'next/link';
import { SetupStatus } from '../components/setup-status';
import { EventCalendar, type CalendarEvent } from '../components/event-calendar';
import { getBackend } from '../lib/backend';

export const dynamic = 'force-dynamic';

const modules = [
  { name: 'Memberships', href: '/memberships', description: 'Your membership, renewals and member benefits.' },
  { name: 'Bookings', href: '/bookings', description: 'Tickets, member pricing and event waitlists.' },
  { name: 'Check-in', href: '/check-in', description: 'QR tickets and authorized event check-in.' }
];

export default async function HomePage() {
  let calendarEvents: CalendarEvent[] = [];

  try {
    const api = await getBackend();
    if (api) {
      const res = await api.request('/api/v1/events?scope=all');
      if (res.status === 200 && Array.isArray(res.body)) {
        calendarEvents = res.body
          .filter((e: any) => e.starts_at && e.ends_at)
          .map((e: any) => ({
            id: String(e.id),
            name: String(e.name),
            description: e.description ?? null,
            venue: e.venue ?? null,
            startsAt: String(e.starts_at),
            endsAt: String(e.ends_at),
            status: String(e.status ?? 'published')
          }));
      }
    }
  } catch {
    // If backend is not available or auth is unlinked, calendar operates gracefully
  }

  return (
    <>
      <p className="text-sm font-semibold uppercase tracking-widest text-teal-700">Your organization, together</p>
      <h1 className="mt-4 text-4xl font-bold tracking-tight sm:text-5xl">Welcome to WhoDoo.</h1>
      <p className="mt-4 max-w-2xl leading-7 text-slate-600">
        A workspace for your student organization. Explore the areas below as we connect memberships, bookings, check-in, and campus schedules.
      </p>

      {/* Interactive Campus Event Calendar */}
      <EventCalendar initialEvents={calendarEvents} />

      <div className="mt-10 grid gap-4 sm:grid-cols-3">
        {modules.map(module => (
          <Link key={module.href} href={module.href} className="rounded-xl border border-slate-200 bg-white p-6 transition hover:border-teal-600 hover:shadow-sm">
            <h2 className="mt-2 text-lg font-semibold text-slate-900">{module.name} <span aria-hidden="true">→</span></h2>
            <p className="mt-2 text-sm leading-6 text-slate-600">{module.description}</p>
          </Link>
        ))}
      </div>
      <SetupStatus />
    </>
  );
}
