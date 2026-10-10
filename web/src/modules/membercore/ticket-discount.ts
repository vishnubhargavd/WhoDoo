export type Membership = {
  status: 'active' | 'cancelled' | 'expired';
  clubId: string;
  /** UTC ISO timestamp: YYYY-MM-DDTHH:mm:ss[.sss]Z. */
  startsAt: string;
  expiresAt: string;
  discountBps: number;
};

export type DiscountInput = {
  basePricePaise: number;
  eventClubId: string;
  membership: Membership | null;
  /** Explicit Unix time in milliseconds, supplied by a trusted server clock. */
  nowMs: number;
};

export type DiscountReason = 'missing_membership' | 'inactive_membership'
  | 'different_club' | 'not_started' | 'expired' | 'zero_discount' | 'discount_applied';

export type DiscountResult = {
  eligible: boolean;
  discountApplied: boolean;
  reason: DiscountReason;
  explanation: string;
  appliedDiscountBps: number;
  pricePaise: number;
  savingsPaise: number;
};

const explanations: Record<DiscountReason, string> = {
  missing_membership: 'No membership was provided.',
  inactive_membership: 'Membership status is not active.',
  different_club: 'Membership belongs to a different club.',
  not_started: 'Membership validity has not started.',
  expired: 'Membership validity has ended.',
  zero_discount: 'Membership is eligible but provides a zero discount.',
  discount_applied: 'Active membership for this club is valid; its discount was applied.'
};

function integer(value: number, name: string, maximum: number): void {
  if (!Number.isSafeInteger(value) || value < 0 || value > maximum) {
    throw new RangeError(`${name} must be a nonnegative safe integer <= ${maximum}`);
  }
}

function club(value: string, name: string): void {
  if (typeof value !== 'string' || value.length === 0 || value.trim() !== value) {
    throw new TypeError(`${name} must be a nonempty string without surrounding whitespace`);
  }
}

function utcDate(value: string, name: string): number {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value)) {
    throw new TypeError(`${name} must be a UTC ISO timestamp`);
  }
  const milliseconds = Date.parse(value);
  const canonical = value.includes('.') ? value : value.replace('Z', '.000Z');
  if (!Number.isFinite(milliseconds) || new Date(milliseconds).toISOString() !== canonical) {
    throw new RangeError(`${name} must be a real calendar date`);
  }
  return milliseconds;
}

/**
 * Pure quote calculation, not authorization. Membership must come from trusted
 * server data; never trust a browser's active-member flag. Booking transactions
 * must independently revalidate membership and price before committing.
 * Validity: startsAt <= now < expiresAt. Rounding: nearest paise, ties upward.
 * All supplied membership fields are validated, even when it is ineligible.
 */
export function calculateTicketDiscount(input: DiscountInput): DiscountResult {
  if (!input || typeof input !== 'object') throw new TypeError('Input is required');
  integer(input.basePricePaise, 'basePricePaise', Number.MAX_SAFE_INTEGER);
  club(input.eventClubId, 'eventClubId');
  if (!Number.isSafeInteger(input.nowMs) || !Number.isFinite(new Date(input.nowMs).getTime())) {
    throw new RangeError('nowMs must be a safe integer representing a valid timestamp');
  }

  let reason: DiscountReason = 'missing_membership';
  let eligible = false;
  let appliedDiscountBps = 0;
  if (input.membership !== null) {
    const member = input.membership;
    if (!member || typeof member !== 'object') throw new TypeError('membership must be an object or null');
    if (!['active', 'cancelled', 'expired'].includes(member.status)) throw new TypeError('Invalid membership status');
    club(member.clubId, 'membership.clubId');
    integer(member.discountBps, 'discountBps', 10000);
    const start = utcDate(member.startsAt, 'startsAt');
    const expiry = utcDate(member.expiresAt, 'expiresAt');
    if (expiry <= start) throw new RangeError('expiresAt must be later than startsAt');
    if (member.status !== 'active') reason = 'inactive_membership';
    else if (member.clubId !== input.eventClubId) reason = 'different_club';
    else if (input.nowMs < start) reason = 'not_started';
    else if (input.nowMs >= expiry) reason = 'expired';
    else {
      eligible = true;
      appliedDiscountBps = member.discountBps;
      reason = appliedDiscountBps === 0 ? 'zero_discount' : 'discount_applied';
    }
  }

  // BigInt avoids overflow in the intermediate multiplication. Output cannot
  // exceed the validated input price, so conversion back to number is exact.
  const pricePaise = Number((BigInt(input.basePricePaise) * BigInt(10000 - appliedDiscountBps) + 5000n) / 10000n);
  return {
    eligible,
    discountApplied: eligible && appliedDiscountBps > 0,
    reason,
    explanation: explanations[reason],
    appliedDiscountBps,
    pricePaise,
    savingsPaise: input.basePricePaise - pricePaise
  };
}
