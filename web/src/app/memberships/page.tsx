import { redirect } from 'next/navigation';
import { createClient } from '../../lib/supabase/server';
import { verifyProfile } from '../../modules/membercore/auth-flow';
import { buildMembershipView } from '../../modules/membercore/membership-view';
import { MembershipDashboard } from '../../components/membership-dashboard';
import { getBackend } from '../../lib/backend';
import { readApplicationProfile } from '../../modules/membercore/backend-profile';

export const dynamic = 'force-dynamic';

export default async function MembershipsPage() {
  const client = await createClient();
  const profile = await verifyProfile(client?.auth ?? null);
  if (profile.status === 'login') redirect('/login?next=%2Fmemberships&notice=session');
  if (profile.status === 'unavailable') return <section><h1 className="text-3xl font-semibold">Memberships unavailable</h1><p role="alert" className="mt-4">{profile.message}</p></section>;
  const application = await readApplicationProfile(await getBackend(), Date.now());
  return <div className="mx-auto max-w-3xl space-y-6"><h1 className="text-3xl font-semibold tracking-tight">Your memberships</h1>{application.state === 'unavailable' && <p role="status">{application.message}</p>}<MembershipDashboard view={application.state === 'ready' ? application.profile.membership : buildMembershipView(null, Date.now())} /></div>;
}
