import type { DiscountResult } from '../membercore/ticket-discount.ts';
import type { BookingEventSnapshot } from './booking-rules.ts';

/** FairSeat-local brands. They do not define shared database ID contracts. */
type Brand<T, Name extends string> = T & { readonly __fairseatBrand: Name };
export type EventId = Brand<string, 'EventId'>;
export type ClubId = Brand<string, 'ClubId'>;
export type TicketCategoryId = Brand<string, 'TicketCategoryId'>;
export type SupabaseAuthUserId = Brand<string, 'SupabaseAuthUserId'>;
export type ApplicationUserId = Brand<string, 'ApplicationUserId'>;
export type BookingId = Brand<string, 'BookingId'>;
export type WaitlistEntryId = Brand<string, 'WaitlistEntryId'>;
export type BookingQuoteId = Brand<string, 'BookingQuoteId'>;

/** Browser input is deliberately limited to selectors and an idempotency key. */
export interface BookingRequest {
  readonly eventId: EventId;
  readonly ticketCategoryId: TicketCategoryId;
  readonly quantity: number;
  /** Client-generated retry key; scope it to the verified purchaser on the server. */
  readonly idempotencyKey: string;
}

export type BookingRequestError = 'invalid_shape' | 'unexpected_field' | 'invalid_event_id'
  | 'invalid_ticket_category_id' | 'invalid_quantity' | 'invalid_idempotency_key';
export type BookingRequestParse =
  | { readonly ok: true; readonly value: BookingRequest }
  | { readonly ok: false; readonly error: BookingRequestError };

const REQUEST_FIELDS = new Set(['eventId', 'ticketCategoryId', 'quantity', 'idempotencyKey']);
const IDEMPOTENCY_KEY = /^[A-Za-z0-9_-]{16,128}$/;

function validIdentifier(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 200
    && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value);
}

/**
 * Parse only browser-controlled booking selectors. Unknown/trusted-looking fields
 * are rejected rather than copied into a command. This does not verify that IDs
 * exist or authorize access; the server must resolve them from trusted data.
 */
export function parseBookingRequest(input: unknown): BookingRequestParse {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return { ok: false, error: 'invalid_shape' };
  const record = input as Record<string, unknown>;
  if (Object.keys(record).some((key) => !REQUEST_FIELDS.has(key))) return { ok: false, error: 'unexpected_field' };
  if (!validIdentifier(record.eventId)) return { ok: false, error: 'invalid_event_id' };
  if (!validIdentifier(record.ticketCategoryId)) return { ok: false, error: 'invalid_ticket_category_id' };
  if (!Number.isSafeInteger(record.quantity) || (record.quantity as number) <= 0) {
    return { ok: false, error: 'invalid_quantity' };
  }
  if (typeof record.idempotencyKey !== 'string' || !IDEMPOTENCY_KEY.test(record.idempotencyKey)) {
    return { ok: false, error: 'invalid_idempotency_key' };
  }
  return {
    ok: true,
    value: {
      eventId: record.eventId as EventId,
      ticketCategoryId: record.ticketCategoryId as TicketCategoryId,
      quantity: record.quantity as number,
      idempotencyKey: record.idempotencyKey
    }
  };
}

/** Identity must be verified server-side and mapped only under an approved contract. */
export interface AuthenticatedPurchaser {
  readonly supabaseAuthUserId: SupabaseAuthUserId;
  /** Existing application users.id; never substitute the Supabase Auth UUID. */
  readonly applicationUserId: ApplicationUserId;
}

/** A local event view, not a proposal for columns on the shared events table. */
export type TrustedBookingEvent = BookingEventSnapshot & { readonly clubId: ClubId };

/** Local resolved category view; use Member 2's actual contract when approved. */
export interface TicketCategorySnapshot {
  readonly ticketCategoryId: TicketCategoryId;
  readonly eventId: EventId;
  readonly clubId: ClubId;
  readonly basePricePaise: number;
  readonly active: boolean;
}

/** Capacity is trusted server data. No browser availability value belongs here. */
export interface BookingCapacitySnapshot {
  readonly eventId: EventId;
  readonly ticketCategoryId: TicketCategoryId;
  readonly capacity: number;
  /** Confirmed seats plus unexpired holds, read consistently by the transaction. */
  readonly occupied: number;
  readonly capturedAtMs: number;
}

/** Immutable server-computed quote; `memberPricing` is MemberCore's result verbatim. */
export interface BookingPriceQuote {
  readonly quoteId: BookingQuoteId;
  readonly eventId: EventId;
  readonly ticketCategoryId: TicketCategoryId;
  readonly quantity: number;
  readonly baseUnitPricePaise: number;
  readonly unitPricePaise: number;
  readonly totalPricePaise: number;
  readonly memberPricing: Readonly<DiscountResult>;
  readonly issuedAtMs: number;
  readonly expiresAtMs: number;
}

export type QuoteAcceptance = 'accepted' | 'price_changed' | 'quote_expired' | 'invalid_quote';

function safePaise(value: number): boolean {
  return Number.isSafeInteger(value) && value >= 0;
}

function validTimestamp(value: number): boolean {
  return Number.isSafeInteger(value) && Number.isFinite(new Date(value).getTime());
}

function validPricing(basePaise: number, pricing: DiscountResult): boolean {
  return !!pricing && typeof pricing === 'object'
    && safePaise(pricing.pricePaise) && safePaise(pricing.savingsPaise)
    && pricing.pricePaise <= basePaise
    && pricing.pricePaise + pricing.savingsPaise === basePaise
    && Number.isSafeInteger(pricing.appliedDiscountBps) && pricing.appliedDiscountBps >= 0
    && pricing.appliedDiscountBps <= 10000
    && typeof pricing.eligible === 'boolean' && typeof pricing.discountApplied === 'boolean'
    && pricing.discountApplied === (pricing.eligible && pricing.appliedDiscountBps > 0)
    && (pricing.eligible || pricing.appliedDiscountBps === 0)
    && (pricing.appliedDiscountBps !== 0 || pricing.pricePaise === basePaise)
    && typeof pricing.reason === 'string' && typeof pricing.explanation === 'string';
}

function validQuote(quote: BookingPriceQuote): boolean {
  if (!quote || typeof quote !== 'object' || !validIdentifier(quote.quoteId)
      || !validIdentifier(quote.eventId) || !validIdentifier(quote.ticketCategoryId)
      || !Number.isSafeInteger(quote.quantity) || quote.quantity <= 0
      || !safePaise(quote.baseUnitPricePaise) || !safePaise(quote.unitPricePaise)
      || !safePaise(quote.totalPricePaise) || !validPricing(quote.baseUnitPricePaise, quote.memberPricing)
      || quote.unitPricePaise !== quote.memberPricing.pricePaise
      || !validTimestamp(quote.issuedAtMs) || !validTimestamp(quote.expiresAtMs)
      || quote.expiresAtMs <= quote.issuedAtMs) return false;
  const total = BigInt(quote.unitPricePaise) * BigInt(quote.quantity);
  return total <= BigInt(Number.MAX_SAFE_INTEGER) && Number(quote.totalPricePaise) === Number(total);
}

function samePricing(a: DiscountResult, b: DiscountResult): boolean {
  return a.eligible === b.eligible && a.discountApplied === b.discountApplied
    && a.reason === b.reason && a.appliedDiscountBps === b.appliedDiscountBps
    && a.pricePaise === b.pricePaise && a.savingsPaise === b.savingsPaise
    && a.explanation === b.explanation;
}

/**
 * Compare two server-loaded quotes. Never call this with a browser-supplied
 * "current" price. The transaction must lock/re-read authoritative pricing and
 * require renewed purchaser acceptance when the value is `price_changed`.
 */
export function compareBookingQuotes(
  accepted: BookingPriceQuote,
  current: BookingPriceQuote,
  nowMs: number
): QuoteAcceptance {
  if (!validTimestamp(nowMs) || !validQuote(accepted) || !validQuote(current)) return 'invalid_quote';
  if (nowMs >= accepted.expiresAtMs || nowMs >= current.expiresAtMs) return 'quote_expired';
  if (accepted.eventId !== current.eventId || accepted.ticketCategoryId !== current.ticketCategoryId
      || accepted.quantity !== current.quantity || accepted.baseUnitPricePaise !== current.baseUnitPricePaise
      || accepted.unitPricePaise !== current.unitPricePaise || accepted.totalPricePaise !== current.totalPricePaise
      || !samePricing(accepted.memberPricing, current.memberPricing)) return 'price_changed';
  return 'accepted';
}

export type BookingStatus = 'pending_payment' | 'confirmed' | 'cancelled' | 'expired';
export type WaitlistStatus = 'waiting' | 'offered' | 'promoted' | 'withdrawn' | 'expired';
export type PaymentStatus = 'not_required' | 'pending' | 'paid' | 'failed' | 'refunded';

export interface WaitlistPosition {
  readonly entryId: WaitlistEntryId;
  readonly status: WaitlistStatus;
  /** Position is a server snapshot and may change after promotion/withdrawal. */
  readonly position: number | null;
}

export interface BookingSnapshot {
  readonly bookingId: BookingId;
  readonly eventId: EventId;
  readonly clubId: ClubId;
  readonly ticketCategoryId: TicketCategoryId;
  readonly purchaserApplicationUserId: ApplicationUserId;
  readonly quantity: number;
  readonly priceQuote: BookingPriceQuote;
  readonly status: BookingStatus;
  readonly paymentStatus: PaymentStatus;
  readonly holdExpiresAtMs: number | null;
}

export interface WaitlistSnapshot {
  readonly waitlist: WaitlistPosition;
  readonly eventId: EventId;
  readonly clubId: ClubId;
  readonly ticketCategoryId: TicketCategoryId;
  readonly purchaserApplicationUserId: ApplicationUserId;
  readonly quantity: number;
}

export type ReservationResult =
  | { readonly kind: 'reserved'; readonly booking: BookingSnapshot; readonly replayed: boolean }
  | { readonly kind: 'waitlisted'; readonly entry: WaitlistSnapshot; readonly replayed: boolean }
  | { readonly kind: 'price_changed'; readonly currentQuote: BookingPriceQuote }
  | { readonly kind: 'rejected'; readonly reason: 'closed' | 'sold_out' | 'invalid' | 'not_authorized' };

export interface TrustedReservationCommand {
  readonly request: BookingRequest;
  readonly purchaser: AuthenticatedPurchaser;
  readonly event: TrustedBookingEvent;
  readonly category: TicketCategorySnapshot;
  readonly capacity: BookingCapacitySnapshot;
  readonly quote: BookingPriceQuote;
  readonly nowMs: number;
}

/** Caller verifies provider signatures before passing events to this port. */
export interface VerifiedPaymentEvent {
  readonly providerEventId: string;
  readonly bookingId: BookingId;
  readonly outcome: 'paid' | 'failed' | 'refunded';
  readonly occurredAtMs: number;
}

export interface CancellationCommand {
  readonly bookingId: BookingId;
  readonly purchaser: AuthenticatedPurchaser;
  readonly requestedAtMs: number;
}

export interface WaitlistPromotionCommand {
  readonly eventId: EventId;
  readonly clubId: ClubId;
  readonly ticketCategoryId: TicketCategoryId;
  readonly nowMs: number;
}

/**
 * Future persistence boundary only. A PostgreSQL adapter must implement these
 * operations in transactions; this interface and its tests do not provide a
 * capacity or duplicate-prevention guarantee.
 */
export interface BookingTransactionPort {
  reserve(command: TrustedReservationCommand): Promise<ReservationResult>;
  applyVerifiedPayment(event: VerifiedPaymentEvent): Promise<'applied' | 'duplicate' | 'ignored'>;
  cancel(command: CancellationCommand): Promise<'cancelled' | 'already_terminal' | 'not_found'>;
  promoteNextWaitlisted(command: WaitlistPromotionCommand): Promise<'offered' | 'queue_empty' | 'not_available'>;
}

/** GateSync may issue only after the booking and payment reached terminal approval. */
export function isTicketIssuanceEligible(booking: Pick<BookingSnapshot, 'status' | 'paymentStatus'>): boolean {
  return booking.status === 'confirmed'
    && (booking.paymentStatus === 'paid' || booking.paymentStatus === 'not_required');
}
