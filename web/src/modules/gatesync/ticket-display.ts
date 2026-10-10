import { isOpaqueCredentialFormat } from './credential-format.ts';

export type TicketDisplayState = 'pending' | 'confirmed' | 'credential_unavailable'
  | 'cancelled' | 'revoked' | 'checked_in' | 'invalid' | 'issued';

export interface TicketDisplayRecord {
  readonly ticketId: string;
  readonly eventName: string;
  readonly venue: string | null;
  readonly startsAt: string;
  readonly category: string;
  readonly holderName: string | null;
  readonly bookingStatus: 'pending_payment' | 'confirmed' | 'cancelled' | 'expired';
  readonly paymentStatus: 'pending' | 'paid' | 'not_required' | 'failed' | 'refunded';
  readonly credentialState: 'issued' | 'revoked' | 'cancelled' | 'checked_in' | null;
  /** Only a trusted server ticket source may provide the opaque bearer value. */
  readonly credential: string | null;
}

export interface TicketDisplayView {
  readonly state: TicketDisplayState;
  readonly label: string;
  readonly eventName: string;
  readonly venue: string;
  readonly startsAt: string;
  readonly category: string;
  readonly holderName: string;
  readonly credential: string | null;
  readonly message: string;
}

function safeLabel(value: unknown, optional = false): string | null {
  if (optional && value === null) return null;
  if (typeof value !== 'string' || !value.trim() || value.length > 160
      || /[\u0000-\u001f\u007f]/.test(value)) return null;
  return value.trim();
}

function validUtcDate(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value)) return false;
  const date = Date.parse(value);
  const canonical = value.includes('.') ? value : value.replace('Z', '.000Z');
  return Number.isFinite(date) && new Date(date).toISOString() === canonical;
}

const invalid: TicketDisplayView = {
  state: 'invalid', label: 'Ticket details unavailable', eventName: 'Ticket unavailable',
  venue: 'Not available', startsAt: 'Not available', category: 'Not available',
  holderName: 'Not available', credential: null,
  message: 'Ticket details could not be verified. No QR credential is displayed.'
};

/** Presentation validation only; trusted server sourcing remains mandatory. */
export function buildTicketDisplayView(value: unknown): TicketDisplayView {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid;
  const r = value as Record<string, unknown>;
  const eventName = safeLabel(r.eventName);
  const venue = safeLabel(r.venue, true);
  const category = safeLabel(r.category);
  const holderName = safeLabel(r.holderName, true);
  if (!safeLabel(r.ticketId) || !eventName || venue === null && r.venue !== null
      || !category || holderName === null && r.holderName !== null || !validUtcDate(r.startsAt)) return invalid;
  if (!['pending_payment', 'confirmed', 'cancelled', 'expired'].includes(String(r.bookingStatus))
      || !['pending', 'paid', 'not_required', 'failed', 'refunded'].includes(String(r.paymentStatus))) return invalid;

  const base = { eventName, venue: venue ?? 'Venue not available', startsAt: r.startsAt,
    category, holderName: holderName ?? 'Ticket holder' };
  const view = (state: TicketDisplayState, label: string, message: string, credential: string | null = null): TicketDisplayView =>
    ({ ...base, state, label, message, credential });

  if (r.bookingStatus === 'cancelled' || r.bookingStatus === 'expired'
      || r.paymentStatus === 'failed' || r.paymentStatus === 'refunded'
      || r.credentialState === 'cancelled') {
    return view('cancelled', 'Ticket unavailable', 'This booking or ticket is no longer valid.');
  }
  if (r.bookingStatus === 'pending_payment' || r.paymentStatus === 'pending') {
    return view('pending', 'Awaiting confirmation', 'A pending booking is not a ticket. No QR credential is available.');
  }
  if (r.bookingStatus !== 'confirmed' || !['paid', 'not_required'].includes(String(r.paymentStatus))) return invalid;
  if (r.credentialState === 'revoked') return view('revoked', 'Ticket revoked', 'This ticket credential has been revoked.');
  if (r.credentialState === 'checked_in') return view('checked_in', 'Already checked in', 'This ticket has already been checked in.');
  if (r.credentialState === null && r.credential === null) {
    return view('confirmed', 'Booking confirmed · credential not issued', 'Your booking is confirmed. GateSync has not issued a ticket credential yet.');
  }
  if (r.credentialState !== 'issued' || !isOpaqueCredentialFormat(r.credential)) {
    return view('credential_unavailable', 'Credential unavailable', 'Your booking is confirmed, but the issued GateSync credential could not be verified.');
  }
  return view('issued', 'QR credential issued', 'Show this private QR code to event staff.', r.credential);
}

export function formatTicketDate(value: string): string {
  return new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(value)) + ' IST';
}
