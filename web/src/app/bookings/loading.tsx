export default function BookingLoading() {
  return <section aria-busy="true" aria-label="Loading event tickets" className="space-y-4"><h1 className="text-3xl font-semibold">Event tickets</h1><p role="status">Loading your ticket workspace…</p><div aria-hidden="true" className="h-40 rounded-2xl bg-slate-100 motion-safe:animate-pulse" /></section>;
}
