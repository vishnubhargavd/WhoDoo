import type { MembershipDashboardView } from '../modules/membercore/membership-view';

export function MembershipDashboard({ view }: { view: MembershipDashboardView }) {
  const fields = [
    ['Membership status', view.status], ['Membership plan', view.plan],
    ['Club', view.club], ['Member discounts', view.discount],
    ['Start date (UTC)', view.startsAt], ['Expiry date (UTC)', view.expiresAt]
  ];
  return <section aria-labelledby="membership-title" className="rounded-2xl border border-slate-200 bg-white p-6 sm:p-8">
    <div className="mb-6 border-b border-slate-100 pb-6">
      <p className="mb-2 text-xs font-semibold uppercase tracking-widest text-teal-700">MemberCore</p>
      <h2 id="membership-title" className="text-xl font-semibold text-slate-900">{view.heading}</h2>
      <p className="mt-2 max-w-xl text-sm leading-6 text-slate-600">{view.description}</p>
    </div>
    <dl className="grid gap-x-8 gap-y-6 sm:grid-cols-2">
      {fields.map(([name, value]) => <div key={name}><dt className="text-sm text-slate-500">{name}</dt><dd className="mt-1 break-words font-medium text-slate-900">{value}</dd></div>)}
    </dl>
    <div className="mt-7 rounded-xl bg-slate-50 p-4">
      <h3 className="text-sm font-semibold">Renewal reminder</h3><p className="mt-1 text-sm leading-6 text-slate-600">{view.renewal}</p>
    </div>
    <p className="mt-5 text-xs leading-5 text-slate-500">Sign-in does not establish club membership, a club role, payment of dues or discount eligibility.</p>
  </section>;
}
