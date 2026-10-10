import { backendMessage, errorCode, iso, label, object, paise, uuid, type BackendApi, type BackendReply } from '../../lib/backend-contract.ts';
import { parseBookingApiResponse, type BookingApiResult } from './booking-api.ts';
import { compareBookingQuotes, parseBookingRequest, type BookingPriceQuote, type BookingStatus, type PaymentStatus } from './booking-contracts.ts';
import type { DiscountReason } from '../membercore/ticket-discount.ts';
import { parseEvents, type EventView } from './view-models/booking-view.ts';
import type { QuoteSelection } from './checkout.ts';

const discountReasons = new Set(['missing_membership','inactive_membership','different_club','not_started','expired','zero_discount','discount_applied']);
/** Preserve the backend's tested calculator result; this only adapts its wire representation. */
export function mapQuote(value: unknown): BookingPriceQuote | null {
  if (!object(value) || !uuid(value.id) || !uuid(value.event_id) || !uuid(value.ticket_category_id)
    || !paise(value.base_unit_price_paise) || !paise(value.unit_price_paise) || value.unit_price_paise > value.base_unit_price_paise
    || !label(value.discount_explanation, 1000) || !discountReasons.has(String(value.discount_reason))) return null;
  const issued = iso(value.issued_at), expires = iso(value.expires_at);
  if (!issued || !expires) return null;
  const quote = { quoteId: value.id, eventId: value.event_id, ticketCategoryId: value.ticket_category_id,
    quantity: value.quantity, baseUnitPricePaise: value.base_unit_price_paise, unitPricePaise: value.unit_price_paise,
    totalPricePaise: value.total_price_paise, issuedAtMs: Date.parse(issued), expiresAtMs: Date.parse(expires),
    memberPricing: { eligible: value.member_eligible, discountApplied: value.discount_applied,
      reason: value.discount_reason as DiscountReason, explanation: value.discount_explanation,
      appliedDiscountBps: value.applied_discount_bps, pricePaise: value.unit_price_paise,
      savingsPaise: value.base_unit_price_paise - value.unit_price_paise } } as BookingPriceQuote;
  return compareBookingQuotes(quote, quote, quote.issuedAtMs) === 'accepted' ? quote : null;
}

export function mapReservation(reply: BackendReply): BookingApiResult {
  if (reply.status >= 200 && reply.status < 300 && object(reply.body)) {
    const r = reply.body;
    if (!uuid(r.booking_id) || r.kind !== 'reserved') return invalidResponse;
    const hold = r.hold_expires_at === null ? null : iso(r.hold_expires_at);
    if (r.hold_expires_at !== null && hold === null) return invalidResponse;
    const value = parseBookingApiResponse({ kind: 'reserved', bookingId: r.booking_id, status: r.status,
      paymentStatus: r.payment_status, totalPricePaise: r.total_price_paise, holdExpiresAt: hold });
    return value ? { ok: true, value } : invalidResponse;
  }
  const code = errorCode(reply);
  if (['quote_stale','quote_expired','invalid_quote'].includes(code ?? '')) return { ok: true, value: { kind: 'price_changed', message: 'Request a fresh quote and review the price before reserving.' } };
  if (reply.status === 401) return { ok: false, error: 'not_authenticated', message: backendMessage(reply) };
  const reason = reply.status === 403 ? 'not_authorized' : ['sold_out','insufficient_capacity','category_sold_out'].includes(code ?? '') ? 'sold_out' : ['event_not_published','event_started','sales_closed','sales_not_open','booking_closed'].includes(code ?? '') ? 'closed' : 'invalid';
  return { ok: true, value: { kind: 'rejected', reason, message: reason === 'sold_out' ? 'There are not enough seats for this request.' : reason === 'closed' ? 'Booking is not open for this event.' : backendMessage(reply) } };
}
const invalidResponse: BookingApiResult = { ok: false, error: 'invalid_response', message: 'The booking service returned an unsupported response.' };

export async function reserveBackend(api: BackendApi, input: unknown): Promise<BookingApiResult> {
  if (!object(input) || Object.keys(input).some(k => !['request','acceptedQuoteId'].includes(k)) || !uuid(input.acceptedQuoteId)) return { ok: false, error: 'invalid_request', message: 'A valid accepted quote is required.' };
  const parsed = parseBookingRequest(input.request);
  if (!parsed.ok || !uuid(parsed.value.eventId) || !uuid(parsed.value.ticketCategoryId) || parsed.value.quantity > 1000) return { ok: false, error: 'invalid_request', message: 'Choose a valid event, category and quantity.' };
  const r = parsed.value;
  try { return mapReservation(await api.request('/api/v1/ticket-bookings', 'POST', {
    event_id: r.eventId, ticket_category_id: r.ticketCategoryId, quantity: r.quantity,
    idempotency_key: r.idempotencyKey, accepted_quote_id: input.acceptedQuoteId
  })); } catch { return { ok: false, error: 'network_error', message: 'The booking service could not be reached. Retry the same request.' }; }
}

export async function readQuote(api: BackendApi, selection: QuoteSelection): Promise<BookingPriceQuote | null> {
  const reply = await api.request('/api/v1/ticket-bookings/quotes', 'POST', {
    event_id: selection.eventId, ticket_category_id: selection.ticketCategoryId, quantity: selection.quantity
  });
  return reply.status >= 200 && reply.status < 300 ? mapQuote(reply.body) : null;
}

export function mapEvent(value: unknown, categories: unknown): EventView | null {
  if (!object(value) || !uuid(value.id) || !label(value.name) || !Array.isArray(categories)) return null;
  const starts = iso(value.starts_at), ends = iso(value.ends_at);
  if (!starts || !ends) return null;
  const mapped = [];
  let onSale = false;
  let soldOut = false;
  for (const c of categories) {
    if (!object(c) || !uuid(c.id) || c.event_id !== value.id || typeof c.on_sale !== 'boolean' || typeof c.active !== 'boolean' || !paise(c.available)) return null;
    if (!c.active) continue;
    onSale ||= c.on_sale && c.available > 0;
    soldOut ||= c.on_sale && c.available === 0;
    mapped.push({ categoryId: c.id, name: c.name, basePricePaise: c.base_price_paise,
      maxQuantity: 100, availability: c.available });
  }
  return parseEvents([{ eventId: value.id, title: value.name, description: value.description ?? 'Event details will be shared by the organizer.',
    venue: value.venue, startsAt: starts, endsAt: ends, status: value.status,
    saleState: onSale ? 'open' : soldOut ? 'sold_out' : 'closed', categories: mapped }])?.[0] ?? null;
}
export async function readEvents(api: BackendApi): Promise<EventView[]> {
  const reply = await api.request('/api/v1/events?scope=all');
  if (reply.status !== 200 || !Array.isArray(reply.body) || reply.body.length > 500) throw new Error('Event source unavailable');
  const events: EventView[] = [];
  // Bounded batches avoid one connection per event at the same time.
  for (let offset = 0; offset < reply.body.length; offset += 8) {
    const batch = await Promise.all(reply.body.slice(offset, offset + 8).map(async raw => {
      if (!object(raw) || !uuid(raw.id)) throw new Error('Invalid event');
      if (raw.starts_at === null || raw.ends_at === null) return null; // Unscheduled shared legacy events cannot be booked or calendared.
      const categories = await api.request(`/api/v1/ticket-categories?event_id=${raw.id}`);
      // A club may have scheduled events without published ticket settings yet.
      if (categories.status !== 200 && categories.status !== 404) throw new Error('Ticket categories unavailable');
      const event = mapEvent(raw, categories.status === 404 ? [] : categories.body);
      if (!event) throw new Error('Invalid event response');
      return event;
    }));
    events.push(...batch.filter((e): e is EventView => e !== null));
  }
  return events;
}

export type MyBooking = { bookingId: string; eventId: string; status: BookingStatus; paymentStatus: PaymentStatus; quantity: number; totalPricePaise: number; holdExpiresAt: string | null };
export function mapMyBookings(value: unknown): MyBooking[] | null {
  if (!Array.isArray(value) || value.length > 1000) return null;
  const items: MyBooking[] = [];
  for (const r of value) {
    if (!object(r) || !uuid(r.booking_id) || !uuid(r.event_id) || !['pending_payment','confirmed','cancelled','expired'].includes(String(r.status))
      || !['pending','failed','paid','not_required','refunded'].includes(String(r.payment_status)) || !paise(r.total_price_paise)
      || !Number.isSafeInteger(r.quantity) || (r.quantity as number) < 1) return null;
    const hold = r.hold_expires_at === null ? null : iso(r.hold_expires_at);
    if (r.hold_expires_at !== null && hold === null) return null;
    items.push({ bookingId: r.booking_id, eventId: r.event_id, status: r.status as BookingStatus,
      paymentStatus: r.payment_status as PaymentStatus, quantity: r.quantity as number, totalPricePaise: r.total_price_paise, holdExpiresAt: hold });
  }
  return items;
}
