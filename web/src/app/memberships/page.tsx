import { redirect } from 'next/navigation';
import { createClient } from '../../lib/supabase/server';
import { verifyProfile } from '../../modules/membercore/auth-flow';
import { buildMembershipView } from '../../modules/membercore/membership-view';
import { MembershipDashboard } from '../../components/membership-dashboard';

export const dynamic = 'force-dynamic';

export default async function MembershipsPage() {
  const client = await createClient();
  const profile = await verifyProfile(client?.auth ?? null);
  if (profile.status === 'login') redirect('/login?next=%2Fmemberships&notice=session');
  if (profile.status === 'unavailable') return <section><h1 className="text-3xl font-semibold">Memberships unavailable</h1><p role="alert" className="mt-4">{profile.message}</p></section>;
  return <div className="mx-auto max-w-3xl space-y-6"><h1 className="text-3xl font-semibold tracking-tight">Your memberships</h1><MembershipDashboard view={buildMembershipView(null, Date.now())} /></div>;
}
