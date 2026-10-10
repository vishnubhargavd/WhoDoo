import { calculateTicketDiscount, type DiscountResult } from './ticket-discount.ts';
import {
  applicationUserId,
  type ReadOnlyMembershipRepository,
  type TrustedMemberContext
} from './membership-repository.ts';

export class MembershipScopeError extends Error {
  constructor() {
    super('Trusted member context does not match the event club');
    this.name = 'MembershipScopeError';
  }
}

export class MembershipRepositoryError extends Error {
  readonly code: 'lookup_failed' | 'invalid_record';
  constructor(code: 'lookup_failed' | 'invalid_record') {
    super(code === 'lookup_failed' ? 'Membership lookup failed' : 'Membership repository returned an invalid or out-of-scope record');
    this.name = 'MembershipRepositoryError';
    this.code = code;
  }
}

export interface TrustedTicketQuote {
  readonly basePricePaise: number;
  readonly eventClubId: string;
  readonly nowMs: number;
}

/**
 * An orchestration service, not an auth verifier. Inputs must be supplied by
 * trusted server code, never directly populated from browser identity flags.
 * All eligibility, price validation and rounding stay in the existing calculator.
 */
export async function quoteMemberTicket(
  repository: ReadOnlyMembershipRepository,
  context: TrustedMemberContext,
  ticket: TrustedTicketQuote
): Promise<DiscountResult> {
  if (!context || typeof context !== 'object') throw new TypeError('Trusted member context is required');
  const userId = applicationUserId(context.applicationUserId);
  if (context.clubId !== ticket.eventClubId) throw new MembershipScopeError();
  // Validate the trusted ticket before I/O using the calculator's existing rules.
  const fullPrice = calculateTicketDiscount({ ...ticket, membership: null });
  let record;
  try {
    record = await repository.findForUserAndClub(userId, context.clubId);
  } catch {
    // Preserve a stable error without leaking storage details. Never fall back
    // to a guest quote when the membership store is unavailable.
    throw new MembershipRepositoryError('lookup_failed');
  }
  if (record === null) return fullPrice;
  if (!record || record.applicationUserId !== userId || !record.membership
      || record.membership.clubId !== context.clubId) {
    throw new MembershipRepositoryError('invalid_record');
  }
  return calculateTicketDiscount({ ...ticket, membership: record.membership });
}
