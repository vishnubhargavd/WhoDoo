import Link from 'next/link';
import { getBackend } from '../../lib/backend';

export const dynamic = 'force-dynamic';

interface Announcement {
  id: string;
  club_id: string;
  author_id: string;
  author_name: string;
  title: string;
  content: string;
  target_audience: string;
  is_pinned: boolean;
  created_at: string;
}

export default async function AnnouncementsPage() {
  let announcements: Announcement[] = [];
  let errorMsg: string | null = null;

  try {
    const api = await getBackend();
    if (api) {
      const res = await api.request('/api/v1/announcements');
      if (res.status === 200 && Array.isArray(res.body)) {
        announcements = res.body;
      } else if (res.status === 401 || res.status === 403) {
        errorMsg = 'Please sign in to view club announcements.';
      }
    }
  } catch (err: any) {
    errorMsg = err?.message ?? 'Could not load announcements.';
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <div className="border-b border-slate-200 pb-5">
        <p className="text-xs font-semibold uppercase tracking-widest text-teal-700">WhoDoo Connect</p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-slate-900">Club Announcements</h1>
        <p className="mt-2 text-sm text-slate-600">
          Official notifications, updates, and broadcasts from your club leadership.
        </p>
      </div>

      {errorMsg && (
        <div className="rounded-lg bg-amber-50 p-4 text-sm text-amber-800">
          {errorMsg}{' '}
          <Link href="/login?next=%2Fannouncements" className="font-semibold underline">
            Sign in
          </Link>
        </div>
      )}

      {announcements.length === 0 && !errorMsg ? (
        <div className="rounded-xl border border-dashed border-slate-300 p-12 text-center">
          <p className="text-slate-500">No announcements posted yet. Check back soon for club updates!</p>
        </div>
      ) : (
        <div className="space-y-4">
          {announcements.map((a) => (
            <article
              key={a.id}
              className={`rounded-xl border p-6 transition ${
                a.is_pinned
                  ? 'border-amber-300 bg-amber-50/40 shadow-sm'
                  : 'border-slate-200 bg-white hover:border-slate-300'
              }`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  {a.is_pinned && (
                    <span className="rounded-md bg-amber-100 px-2 py-0.5 text-xs font-bold text-amber-800">
                      📌 PINNED
                    </span>
                  )}
                  <span className="rounded-md bg-slate-100 px-2 py-0.5 text-xs font-medium uppercase text-slate-700">
                    {a.target_audience}
                  </span>
                  <h2 className="text-lg font-semibold text-slate-900">{a.title}</h2>
                </div>
                <time className="text-xs text-slate-500">
                  {new Date(a.created_at).toLocaleDateString('en-IN', {
                    day: 'numeric',
                    month: 'short',
                    year: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </time>
              </div>

              <p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-slate-700">{a.content}</p>

              <div className="mt-4 border-t border-slate-100 pt-3 text-xs text-slate-500">
                Posted by <strong className="font-medium text-slate-700">{a.author_name}</strong>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
