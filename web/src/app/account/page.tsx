import { redirect } from 'next/navigation';
import { createClient } from '../../lib/supabase/server';
import { verifyProfile } from '../../modules/membercore/auth-flow';
import { LogoutForm } from '../../components/logout-form';
import { MembershipDashboard } from '../../components/membership-dashboard';
import { buildMembershipView } from '../../modules/membercore/membership-view';
import { getBackend } from '../../lib/backend';
import { readApplicationProfile } from '../../modules/membercore/backend-profile';

export const dynamic = 'force-dynamic';

export default async function AccountPage() {
  const client = await createClient();
  const account = await verifyProfile(client?.auth ?? null);
  if (account.status === 'login') redirect(account.destination);
  if (account.status === 'unavailable') return <section><h1 className="text-3xl font-semibold">Account unavailable</h1><p role="alert" className="mt-4">{account.message}</p></section>;
  const emailStatus = { verified: 'Verified', unverified: 'Not verified', unavailable: 'Not available' }[account.emailVerification];
  const application = await readApplicationProfile(await getBackend(), Date.now());
  return <div className="space-y-8">
    <div><p className="mb-2 text-sm font-medium text-teal-700">Your WhoDoo workspace</p><h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Profile & memberships</h1><p className="mt-3 text-slate-600">Your sign-in details and organization membership overview.</p></div>
    <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
      <section aria-labelledby="profile-title" className="rounded-2xl border border-slate-200 bg-white p-6 sm:p-8">
        <div aria-hidden="true" className="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-teal-50 text-xl font-semibold text-teal-800">W</div>
        <h2 id="profile-title" className="text-xl font-semibold">Your profile</h2>
        <dl className="my-6 space-y-5">
          <div><dt className="text-sm text-slate-500">Authenticated email</dt><dd className="mt-1 break-all font-medium">{account.email ?? 'Not available'}</dd></div>
          <div><dt className="text-sm text-slate-500">Account identity</dt><dd className="mt-1 font-medium text-teal-800">Authenticated with Supabase</dd></div>
          <div><dt className="text-sm text-slate-500">Email verification</dt><dd className="mt-1 font-medium">{emailStatus}</dd></div>
        </dl>
        {application.state === 'ready' ? <dl className="mb-6 space-y-3"><div><dt className="text-sm text-slate-500">Name</dt><dd>{application.profile.fullName}</dd></div><div><dt className="text-sm text-slate-500">Club role</dt><dd className="capitalize">{application.profile.role}</dd></div></dl>
          : <p role="status" className="mb-6 text-sm leading-6 text-slate-600">{application.message}</p>}
        <LogoutForm />
      </section>
      <MembershipDashboard view={application.state === 'ready' ? application.profile.membership : buildMembershipView(null, Date.now())} />
    </div>
  </div>;
}
