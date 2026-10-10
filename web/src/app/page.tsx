import Link from 'next/link';
import { SetupStatus } from '../components/setup-status';
import { EventCalendar, type CalendarEvent } from '../components/event-calendar';
import { getBackend } from '../lib/backend';

export const dynamic = 'force-dynamic';

const modules = [
  { name: 'Memberships', href: '/memberships', description: 'Your membership, renewals and member benefits.' },
  { name: 'Bookings', href: '/bookings', description: 'Tickets, member pricing and event waitlists.' },
  { name: 'Check-in', href: '/check-in', description: 'QR tickets and authorized event check-in.' },
  { name: 'Announcements', href: '/announcements', description: 'WhoDoo Connect: Official club broadcasts and updates.' }
];

export default async function HomePage() {
  let calendarEvents: CalendarEvent[] = [];
  let latestAnnouncement: { title: string; content: string } | null = null;

  try {
    const api = await getBackend();
    if (api) {
      const [res, annRes] = await Promise.all([
        api.request('/api/v1/events?scope=all').catch(() => null),
        api.request('/api/v1/announcements').catch(() => null)
      ]);
      if (res && res.status === 200 && Array.isArray(res.body)) {
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
      if (annRes && annRes.status === 200 && Array.isArray(annRes.body) && annRes.body.length > 0) {
        latestAnnouncement = {
          title: String(annRes.body[0].title),
          content: String(annRes.body[0].content)
        };
      }
    }
  } catch {
    // If backend is not available or auth is unlinked, calendar and announcements operate gracefully
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

      {/* WhoDoo Connect: Latest Announcement Banner */}
      {latestAnnouncement && (
        <div className="mt-8 rounded-xl border border-amber-200 bg-amber-50/80 p-5 shadow-sm">
          <div className="flex items-center justify-between">
            <span className="inline-flex items-center gap-1.5 text-xs font-bold uppercase tracking-wider text-amber-800">
              <span>📢</span> WhoDoo Connect Announcement
            </span>
            <Link href="/announcements" className="text-xs font-semibold text-amber-900 underline hover:text-amber-700">
              View all &rarr;
            </Link>
          </div>
          <h3 className="mt-2 text-base font-semibold text-slate-900">{latestAnnouncement.title}</h3>
          <p className="mt-1 line-clamp-2 text-sm text-slate-700">{latestAnnouncement.content}</p>
        </div>
      )}

      <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
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
