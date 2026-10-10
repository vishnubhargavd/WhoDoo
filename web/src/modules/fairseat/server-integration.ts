import 'server-only';
import type { EventReadPort } from './view-models/booking-view';
import type { QuoteReadPort, ReservationPort } from './checkout';
import type { BackendApi } from '../../lib/backend-contract';
import { readEvents, readQuote, reserveBackend } from './fastapi-adapter';

/** Approval gates, deliberately disconnected. No endpoint, database or identity mapping is guessed.
 * Future adapters must be per verified user, fetch no-store, map real contracts,
 * strip private fields and issue authenticated quotes through MemberCore.
 * Do not return tokens, app-user IDs, roles or raw backend objects to client components.
 */
export function getFairSeatIntegration(api: BackendApi | null): {
  events: EventReadPort | null;
  reservation: ReservationPort | null;
  quote: QuoteReadPort | null;
} {
  if (!api) return { events: null, reservation: null, quote: null };
  return { events: { load: () => readEvents(api) }, reservation: { reserve: command => reserveBackend(api, command) },
    quote: { load: selection => readQuote(api, selection) } };
}
