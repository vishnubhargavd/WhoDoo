import { GATESYNC_CREDENTIAL_VERSION, type CredentialResolution, type TicketCredentialRecord } from './credential.ts';

export interface CheckInContext {
  readonly eventId: string;
  readonly clubId: string;
  readonly opensAt: string;
  readonly closesAt: string;
  readonly cancelled: boolean;
  /** Trusted server clock, Unix milliseconds. */
  readonly nowMs: number;
}

export type CheckInOutcome = 'ALLOW' | 'INVALID_CREDENTIAL' | 'WRONG_EVENT' | 'WRONG_CLUB'
  | 'NOT_YET_VALID' | 'EXPIRED' | 'REVOKED' | 'CANCELLED'
  | 'ALREADY_CHECKED_IN' | 'EVENT_NOT_OPEN';
export interface CheckInDecision {
  readonly outcome: CheckInOutcome;
  readonly reason: string;
  readonly allowed: boolean;
}

const reasons: Record<CheckInOutcome, string> = {
  ALLOW: 'Ticket is valid for this event and may be checked in.',
  INVALID_CREDENTIAL: 'Ticket credential is invalid or unknown.',
  WRONG_EVENT: 'Ticket belongs to a different event.',
  WRONG_CLUB: 'Ticket belongs to a different club.',
  NOT_YET_VALID: 'Ticket is not valid yet.',
  EXPIRED: 'Ticket has expired.',
  REVOKED: 'Ticket has been revoked.',
  CANCELLED: 'Ticket has been cancelled.',
  ALREADY_CHECKED_IN: 'Ticket was already checked in.',
  EVENT_NOT_OPEN: 'Event check-in is not open.'
};

function result(outcome: CheckInOutcome): CheckInDecision {
  return { outcome, reason: reasons[outcome], allowed: outcome === 'ALLOW' };
}
function id(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 200 && value.trim() === value && !/[\u0000-\u001f]/.test(value);
}
function timestamp(value: unknown): number | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value)) return null;
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return null;
  const canonical = value.includes('.') ? value : value.replace('Z', '.000Z');
  return new Date(parsed).toISOString() === canonical ? parsed : null;
}
function validRecord(value: TicketCredentialRecord): boolean {
  if (!value || typeof value !== 'object' || value.version !== GATESYNC_CREDENTIAL_VERSION
      || !id(value.ticketId) || !id(value.eventId) || !id(value.clubId)
      || !/^[a-f0-9]{64}$/.test(value.credentialId)
      || !['issued', 'checked_in', 'revoked', 'cancelled'].includes(value.state)
      || !(value.checkedInAt === null || timestamp(value.checkedInAt) !== null)) return false;
  const issued = timestamp(value.issuedAt);
  const expiry = timestamp(value.expiresAt);
  if (issued === null || expiry === null || expiry <= issued) return false;
  if (value.state === 'checked_in') return value.checkedInAt !== null;
  return value.checkedInAt === null;
}

/**
 * Deterministic ticket eligibility only. Inputs must come from a verified
 * credential lookup and trusted server event/clock. No scanner auth or redemption
 * occurs here; simultaneous calls remain simultaneous eligible decisions.
 */
export function decideCheckIn(credential: Exclude<CredentialResolution, { status: 'unavailable' }>, context: CheckInContext): CheckInDecision {
  if (!credential || typeof credential !== 'object' || !context || typeof context !== 'object') return result('INVALID_CREDENTIAL');
  if (credential.status !== 'resolved' || !validRecord(credential.record)) return result('INVALID_CREDENTIAL');
  const ticket = credential.record;
  if (!id(context.eventId) || !id(context.clubId) || typeof context.cancelled !== 'boolean'
      || !Number.isSafeInteger(context.nowMs) || !Number.isFinite(new Date(context.nowMs).getTime())) return result('INVALID_CREDENTIAL');
  const opens = timestamp(context.opensAt);
  const closes = timestamp(context.closesAt);
  if (opens === null || closes === null || closes <= opens) return result('EVENT_NOT_OPEN');
  if (ticket.eventId !== context.eventId) return result('WRONG_EVENT');
  if (ticket.clubId !== context.clubId) return result('WRONG_CLUB');
  if (ticket.state === 'revoked') return result('REVOKED');
  if (ticket.state === 'cancelled') return result('CANCELLED');
  if (ticket.state === 'checked_in' || ticket.checkedInAt !== null) return result('ALREADY_CHECKED_IN');
  const issued = timestamp(ticket.issuedAt)!;
  const expiry = timestamp(ticket.expiresAt)!;
  if (context.nowMs < issued) return result('NOT_YET_VALID');
  if (context.nowMs >= expiry) return result('EXPIRED');
  if (context.cancelled || context.nowMs < opens || context.nowMs >= closes) return result('EVENT_NOT_OPEN');
  return result('ALLOW');
}
