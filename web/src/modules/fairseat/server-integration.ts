import 'server-only';
import type { EventReadPort } from './view-models/booking-view';
import type { BookingApiConfig } from './booking-api';
import type { QuoteReadPort } from './checkout';

/** Approval gates, deliberately disconnected. No endpoint, database or identity mapping is guessed.
 * Future adapters must be per verified user, fetch no-store, map real contracts,
 * strip private fields and issue authenticated quotes through MemberCore.
 * Do not return tokens, app-user IDs, roles or raw backend objects to client components.
 */
export function getFairSeatIntegration(): {
  events: EventReadPort | null;
  reservation: BookingApiConfig | null;
  quote: QuoteReadPort | null;
} {
  return { events: null, reservation: null, quote: null };
}
