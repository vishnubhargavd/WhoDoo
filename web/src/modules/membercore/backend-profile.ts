import { backendMessage, iso, label, object, uuid, type BackendApi } from '../../lib/backend-contract.ts';
import { buildMembershipView, type MembershipDashboardView } from './membership-view.ts';

export type ApplicationProfile = { fullName: string; role: string; membership: MembershipDashboardView };
export type ProfileRead = { state: 'ready'; profile: ApplicationProfile } | { state: 'unavailable'; message: string };
/** Server DTO validation never treats Auth metadata as an application profile. */
export function mapApplicationProfile(value: unknown, nowMs: number): ApplicationProfile | null {
  if (!object(value) || value.status !== 'mapped' || !object(value.identity)) return null;
  const identity = value.identity;
  if (!uuid(identity.application_user_id) || !uuid(identity.club_id) || !label(identity.full_name)
    || !['president','treasurer','event_lead','volunteer','member'].includes(String(identity.role))) return null;
  let membership = buildMembershipView(null, nowMs);
  if (value.membership === null) {
    membership = { ...membership, heading: 'No membership yet', description: 'Your application account is linked. No membership record is registered for your club.', status: 'No membership', discount: 'Not currently eligible', renewal: 'Contact your club administrator about joining.' };
  } else {
    const m = value.membership;
    if (!object(m) || !uuid(m.id) || !label(m.plan_name, 120) || !label(m.club_name, 120)
      || !Number.isSafeInteger(m.discount_bps_snapshot) || (m.discount_bps_snapshot as number) < 0 || (m.discount_bps_snapshot as number) > 10000
      || typeof m.eligible !== 'boolean' || !Number.isSafeInteger(m.applied_discount_bps)) return null;
    const starts = iso(m.starts_at), expires = iso(m.expires_at);
    if (!starts || !expires || expires <= starts) return null;
    if (m.status === 'pending') {
      if (m.eligible || m.applied_discount_bps !== 0) return null;
      membership = { ...membership, state: 'record', heading: 'Membership pending', description: 'Activation is awaiting verified settlement or an authorized waiver.', status: 'Pending',
        plan: m.plan_name, club: m.club_name, startsAt: starts.slice(0, 10), expiresAt: expires.slice(0, 10), discount: 'Not currently eligible', renewal: 'Pending memberships do not grant ticket discounts.' };
    } else {
      if (!['active','cancelled','expired'].includes(String(m.status))) return null;
      membership = buildMembershipView({ planName: m.plan_name, clubName: m.club_name, membership: { status: m.status as 'active'|'cancelled'|'expired', clubId: identity.club_id,
        startsAt: starts, expiresAt: expires, discountBps: m.discount_bps_snapshot as number } }, nowMs);
      if (membership.state === 'invalid') return null;
      // An upstream denial always suppresses benefits, even if a presentation calculation is eligible.
      if (!m.eligible) membership = { ...membership, discount: 'Not currently eligible' };
    }
  }
  return { fullName: identity.full_name, role: String(identity.role).replaceAll('_',' '), membership };
}
export async function readApplicationProfile(api: BackendApi | null, nowMs: number): Promise<ProfileRead> {
  if (!api) return { state: 'unavailable', message: 'Application data is unavailable. Check the backend connection and sign in again.' };
  try {
    const response = await api.request('/api/v1/membercore/me');
    if (response.status !== 200) return { state: 'unavailable', message: backendMessage(response) };
    const profile = mapApplicationProfile(response.body, nowMs);
    return profile ? { state: 'ready', profile } : { state: 'unavailable', message: 'Membership details could not be verified. No benefits are confirmed.' };
  } catch { return { state: 'unavailable', message: 'Membership service is temporarily unavailable.' }; }
}
