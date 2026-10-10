import type { BookingApiCommand, BookingApiResult, BookingApiConfig } from './booking-api.ts';
import { createBookingApi, parseBookingApiResponse } from './booking-api.ts';
import { verifyAccount } from '../membercore/auth-flow.ts';
import type { AuthPort } from '../membercore/auth-flow.ts';
import { compareBookingQuotes, parseBookingRequest } from './booking-contracts.ts';
import type { BookingPriceQuote, BookingRequest } from './booking-contracts.ts';

export type QuoteSelection = Omit<BookingRequest, 'idempotencyKey'>;
export interface QuoteReadPort { load(selection: QuoteSelection): Promise<BookingPriceQuote | null> }
export type QuoteReadResult = { state: 'ready'; quote: BookingPriceQuote }
  | { state: 'not_connected' | 'not_authenticated' | 'unavailable' };
/** The read adapter must compute membership eligibility from the current mapped purchaser. */
export async function quoteForAccount(auth: AuthPort | null, port: QuoteReadPort | null,
  input: unknown, nowMs: number): Promise<QuoteReadResult> {
  const account = await verifyAccount(auth);
  if (account.status !== 'authenticated') return { state: 'not_authenticated' };
  if (!port) return { state: 'not_connected' };
  if (!input || typeof input !== 'object' || Array.isArray(input)
    || Object.keys(input).some(key => !['eventId', 'ticketCategoryId', 'quantity'].includes(key))) return { state: 'unavailable' };
  const parsed = parseBookingRequest({ ...input, idempotencyKey: 'quote-validation-only' });
  if (!parsed.ok) return { state: 'unavailable' };
  const { eventId, ticketCategoryId, quantity } = parsed.value;
  try {
    const quote = await port.load({ eventId, ticketCategoryId, quantity });
    if (!quote || compareBookingQuotes(quote, quote, nowMs) !== 'accepted' || quote.issuedAtMs > nowMs
      || quote.eventId !== eventId || quote.ticketCategoryId !== ticketCategoryId || quote.quantity !== quantity) return { state: 'unavailable' };
    // Copy only the defined public quote fields, never raw adapter objects or private metadata.
    const p = quote.memberPricing;
    return { state: 'ready', quote: { quoteId: quote.quoteId, eventId, ticketCategoryId, quantity,
      baseUnitPricePaise: quote.baseUnitPricePaise, unitPricePaise: quote.unitPricePaise, totalPricePaise: quote.totalPricePaise,
      issuedAtMs: quote.issuedAtMs, expiresAtMs: quote.expiresAtMs,
      memberPricing: { eligible: p.eligible, discountApplied: p.discountApplied, reason: p.reason,
        explanation: p.explanation, appliedDiscountBps: p.appliedDiscountBps, pricePaise: p.pricePaise, savingsPaise: p.savingsPaise } } };
  } catch { return { state: 'unavailable' }; }
}

export const disconnectedResult: BookingApiResult = { ok: false, error: 'not_configured', message: 'Ticket reservations are not connected yet.' };
/** Server boundary: verified Auth identity is necessary but does not replace the app-user bridge. */
export interface ReservationPort { reserve(command: unknown): Promise<BookingApiResult> }
export async function reserveForAccount(auth: AuthPort | null, config: BookingApiConfig | ReservationPort | null, command: unknown): Promise<BookingApiResult> {
  const account = await verifyAccount(auth);
  if (account.status !== 'authenticated') return { ok: false, error: 'not_authenticated', message: 'Sign in again before booking.' };
  if (!config) return disconnectedResult;
  return 'reserve' in config ? config.reserve(command) : createBookingApi(config).reserve(command);
}
export function bookingLoginDestination(): string { return '/login?next=%2Fbookings'; }
/** UI convenience only. Backend must enforce purchaser-scoped idempotency atomically. */
export function createSubmissionController(keyFactory: () => string) {
  let busy = false;
  const operations = new Map<string, { key: string; result?: BookingApiResult }>();
  return {
    async submit(selection: Omit<BookingApiCommand['request'], 'idempotencyKey'> & { acceptedQuoteId: string },
      reserve: (command: BookingApiCommand) => Promise<BookingApiResult>): Promise<BookingApiResult | null> {
      if (busy) return null;
      const fingerprint = JSON.stringify([selection.eventId, selection.ticketCategoryId, selection.quantity, selection.acceptedQuoteId]);
      let operation = operations.get(fingerprint);
      if (!operation) { operation = { key: keyFactory() }; operations.set(fingerprint, operation); }
      if (operation.result) return operation.result;
      busy = true;
      try {
        const result = await reserve({ request: { eventId: selection.eventId, ticketCategoryId: selection.ticketCategoryId,
          quantity: selection.quantity, idempotencyKey: operation.key }, acceptedQuoteId: selection.acceptedQuoteId });
        if (result.ok) {
          const parsed = parseBookingApiResponse(result.value);
          if (!parsed) return { ok: false, error: 'invalid_response', message: 'The booking service returned an unsupported response.' };
          const safe: BookingApiResult = { ok: true, value: parsed };
          operation.result = safe;
          return safe;
        }
        return result;
      } catch { return { ok: false, error: 'network_error', message: 'The request could not be completed. Retry the same selection; its retry key will be reused.' }; }
      finally { busy = false; }
    }
  };
}
