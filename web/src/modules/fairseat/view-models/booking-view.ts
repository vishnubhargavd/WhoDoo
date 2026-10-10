import { compareBookingQuotes } from '../booking-contracts.ts';
import type { BookingPriceQuote, BookingStatus, PaymentStatus } from '../booking-contracts.ts';

/** Presentation adapter only: these names do not define shared event-table columns. */
export interface EventView {
  eventId: string; title: string; description: string; venue: string | null;
  startsAt: string; endsAt: string;
  status: 'draft' | 'published' | 'cancelled' | 'completed';
  saleState: 'open' | 'closed' | 'sold_out' | 'waitlist';
  categories: CategoryView[];
}
export interface CategoryView {
  categoryId: string; name: string; basePricePaise: number;
  maxQuantity: number; availability: number | null;
}
export type EventListing = { state: 'ready'; events: EventView[] }
  | { state: 'not_connected' | 'unavailable'; events: [] };
export interface EventReadPort { load(): Promise<unknown> }
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const text = (v: unknown, max = 200): v is string => typeof v === 'string' && v.trim().length > 0 && v.length <= max && !/[\u0000-\u001f\u007f]/.test(v);
const paise = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0;
function date(v: unknown): v is string {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(v)) return false;
  const ms = Date.parse(v);
  return Number.isFinite(ms) && new Date(ms).toISOString() === v;
}
/** Fail the whole listing closed on malformed adapter data; never render raw HTML. */
export function parseEvents(value: unknown): EventView[] | null {
  if (!Array.isArray(value) || value.length > 500) return null;
  const events: EventView[] = [];
  for (const e of value) {
    if (!object(e) || !text(e.eventId) || !text(e.title) || !text(e.description, 4000)
      || !(e.venue === null || text(e.venue)) || !date(e.startsAt) || !date(e.endsAt)
      || Date.parse(e.endsAt) <= Date.parse(e.startsAt)
      || !['draft', 'published', 'cancelled', 'completed'].includes(String(e.status))
      || !['open', 'closed', 'sold_out', 'waitlist'].includes(String(e.saleState))
      || !Array.isArray(e.categories) || e.categories.length > 50) return null;
    const categories: CategoryView[] = [];
    for (const c of e.categories) {
      if (!object(c) || !text(c.categoryId) || !text(c.name) || !paise(c.basePricePaise)
        || !Number.isSafeInteger(c.maxQuantity) || (c.maxQuantity as number) < 1 || (c.maxQuantity as number) > 100
        || !(c.availability === null || paise(c.availability))) return null;
      categories.push({ categoryId: c.categoryId, name: c.name, basePricePaise: c.basePricePaise,
        maxQuantity: c.maxQuantity as number, availability: c.availability as number | null });
    }
    if (new Set(categories.map(c => c.categoryId)).size !== categories.length) return null;
    events.push({ eventId: e.eventId, title: e.title, description: e.description, venue: e.venue,
      startsAt: e.startsAt, endsAt: e.endsAt, status: e.status as EventView['status'],
      saleState: e.saleState as EventView['saleState'], categories });
  }
  return new Set(events.map(e => e.eventId)).size === events.length ? events : null;
}
export async function loadEvents(port: EventReadPort | null): Promise<EventListing> {
  if (!port) return { state: 'not_connected', events: [] };
  try {
    const events = parseEvents(await port.load());
    return events ? { state: 'ready', events } : { state: 'unavailable', events: [] };
  } catch { return { state: 'unavailable', events: [] }; }
}
/** Format integer paise without floating-point conversion, even at the safe integer limit. */
export function formatPaise(value: number): string {
  if (!paise(value)) throw new RangeError('Price must be nonnegative integer paise.');
  const amount = BigInt(value);
  return `₹${new Intl.NumberFormat('en-IN').format(amount / 100n)}.${String(amount % 100n).padStart(2, '0')}`;
}
export function selectionError(event: EventView, categoryId: string, quantity: number): string | null {
  const category = event.categories.find(c => c.categoryId === categoryId);
  if (!category) return 'Choose an available ticket category.';
  if (!Number.isSafeInteger(quantity) || quantity < 1 || quantity > category.maxQuantity) return `Choose between 1 and ${category.maxQuantity} tickets.`;
  // Availability is informational. Atomic capacity validation belongs to the backend.
  return null;
}
export function priceView(event: EventView, categoryId: string, quantity: number,
  quote: BookingPriceQuote | null, nowMs: number) {
  if (selectionError(event, categoryId, quantity)) return null;
  const category = event.categories.find(c => c.categoryId === categoryId)!;
  if (quote && compareBookingQuotes(quote, quote, nowMs) === 'accepted' && quote.issuedAtMs <= nowMs
    && quote.eventId === event.eventId && quote.ticketCategoryId === categoryId && quote.quantity === quantity) {
    return { kind: 'confirmed' as const, total: formatPaise(quote.totalPricePaise), unit: formatPaise(quote.unitPricePaise),
      base: formatPaise(quote.baseUnitPricePaise), savings: formatPaise(quote.memberPricing.savingsPaise),
      explanation: quote.memberPricing.explanation, quoteId: quote.quoteId };
  }
  const total = BigInt(category.basePricePaise) * BigInt(quantity);
  if (total > BigInt(Number.MAX_SAFE_INTEGER)) return null;
  return { kind: 'estimated' as const, total: formatPaise(Number(total)), unit: formatPaise(category.basePricePaise),
    base: formatPaise(category.basePricePaise), savings: null,
    explanation: 'Member pricing is not verified. A fresh server quote is required before booking.', quoteId: null };
}
export function filterEvents(events: EventView[], search: string, status: string, day: string): EventView[] {
  return events.filter(e => e.title.toLocaleLowerCase('en-IN').includes(search.trim().toLocaleLowerCase('en-IN'))
    && (!status || e.status === status) && (!day || e.startsAt.slice(0, 10) === day));
}
export function bookingStatusLabel(status: BookingStatus, payment: PaymentStatus): string {
  if (status === 'cancelled') return payment === 'refunded' ? 'Cancelled · Refunded' : 'Cancelled';
  if (status === 'expired') return 'Expired';
  if (payment === 'failed') return 'Payment failed';
  if (payment === 'refunded') return 'Refunded';
  return status === 'pending_payment' ? 'Held · Awaiting payment' : 'Confirmed';
}
