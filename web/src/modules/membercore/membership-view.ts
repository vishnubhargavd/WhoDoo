import { calculateTicketDiscount, type Membership } from './ticket-discount.ts';

/** Local presentation contract, not a shared database schema or authorization proof. */
export type MembershipViewRecord = {
  membership: Membership;
  planName: string | null;
  clubName: string | null;
};
export type MembershipDashboardView = {
  state: 'not-connected' | 'invalid' | 'record';
  heading: string;
  description: string;
  status: string;
  plan: string;
  club: string;
  startsAt: string;
  expiresAt: string;
  renewal: string;
  discount: string;
};

function label(value: unknown): string {
  if (value === null) return 'Not available';
  if (typeof value !== 'string' || !value.trim() || value.length > 120 || /[\u0000-\u001f]/.test(value)) throw new TypeError('Invalid display label');
  return value.trim();
}

/** Only a future trusted server adapter may supply records; validation does not establish trust.
 * Current routes always pass null. Never call this with Auth metadata or client membership flags.
 */
export function buildMembershipView(record: MembershipViewRecord | null, nowMs: number): MembershipDashboardView {
  const empty: MembershipDashboardView = {
    state: 'not-connected', heading: 'Membership data not connected',
    description: 'Your sign-in is connected. Club membership records are not available yet.',
    status: 'Not available', plan: 'Not available', club: 'Not available',
    startsAt: 'Not available', expiresAt: 'Not available',
    renewal: 'Renewal details will appear when membership data is connected.', discount: 'Not available'
  };
  if (record === null) return empty;
  try {
    // Reuse all date/status/BPS/clock validation and eligibility; no ticket-price display.
    const quote = calculateTicketDiscount({ basePricePaise: 0, eventClubId: record.membership.clubId, membership: record.membership, nowMs });
    const plan = label(record.planName);
    const club = label(record.clubName);
    const status = record.membership.status === 'cancelled' ? 'Cancelled'
      : record.membership.status === 'expired' || quote.reason === 'expired' ? 'Expired'
      : quote.reason === 'not_started' ? 'Upcoming' : 'Active';
    const remaining = Date.parse(record.membership.expiresAt) - nowMs;
    const renewal = status === 'Expired' ? 'Membership has expired. Renewal options are not connected.'
      : status === 'Cancelled' ? 'Membership is cancelled. Renewal options are not connected.'
      : quote.eligible && remaining <= 30 * 24 * 60 * 60 * 1000 ? 'Expires within 30 days. Renewal options are not connected.'
      : 'Renewal options are not connected.';
    return { state: 'record', heading: 'Membership overview', description: 'Details supplied by MemberCore server data.',
      status, plan, club, startsAt: record.membership.startsAt.slice(0, 10), expiresAt: record.membership.expiresAt.slice(0, 10), renewal,
      discount: quote.eligible ? `${quote.appliedDiscountBps / 100}% member ticket discount` : 'Not currently eligible'
    };
  } catch {
    return { ...empty, state: 'invalid', heading: 'Membership details unavailable',
      description: 'Membership data could not be validated. No status or benefits are confirmed.' };
  }
}
