import { redirect } from 'next/navigation';
import { createClient } from '../../lib/supabase/server';
import { verifyAccount } from '../../modules/membercore/auth-flow';
import { bookingLoginDestination } from '../../modules/fairseat/checkout';
import { loadEvents, bookingStatusLabel, formatPaise } from '../../modules/fairseat/view-models/booking-view';
import { getFairSeatIntegration } from '../../modules/fairseat/server-integration';
import { BookingPortal } from '../../modules/fairseat/components/booking-portal';
import { reserveTickets, requestTicketQuote, loadTicketCredential } from './actions';
import { TicketWallet } from '../../modules/gatesync/components/ticket-wallet';
import { getBackend } from '../../lib/backend';
import { mapMyBookings } from '../../modules/fairseat/fastapi-adapter';
import { readTickets } from '../../modules/gatesync/backend-tickets';

export const dynamic = 'force-dynamic';
export default async function BookingsPage() {
  const client = await createClient();
  const access = await verifyAccount(client?.auth ?? null);
  if (access.status === 'login') redirect(bookingLoginDestination());
  if (access.status === 'unavailable') return <section><h1>Ticket booking unavailable</h1><p role="alert">{access.message}</p></section>;
  // Public event reads have not been approved: protect this route before loading any events.
  const api = await getBackend();
  const integration = getFairSeatIntegration(api);
  const [listing, tickets, bookings] = await Promise.all([loadEvents(integration.events), readTickets(api),
    api?.request('/api/v1/ticket-bookings').then(r => r.status === 200 ? mapMyBookings(r.body) : null).catch(() => null) ?? null]);
  return <>
    <BookingPortal listing={listing} quoteConnected={integration.quote !== null}
      requestQuote={requestTicketQuote} checkoutConnected={integration.reservation !== null} reserve={reserveTickets} />
    <section className="mt-10 space-y-4"><h2 className="text-2xl font-semibold">My reservations</h2>
      {bookings === null ? <p role="status">Reservation history is unavailable. Your account may need linking or the booking service may be offline.</p>
        : bookings.length === 0 ? <p>No reservations yet.</p> : <ul className="space-y-3">{bookings.map(b => <li key={b.bookingId} className="rounded-xl border border-slate-200 p-4"><p className="font-semibold">{listing.events.find(e => e.eventId === b.eventId)?.title ?? 'Event reservation'}</p><p>{bookingStatusLabel(b.status,b.paymentStatus)} · {b.quantity} ticket(s) · {formatPaise(b.totalPricePaise)}</p><p className="break-all text-xs text-slate-500">{b.bookingId}</p></li>)}</ul>}
    </section>
    {tickets.state === 'ready' ? <TicketWallet tickets={tickets.tickets} issue={loadTicketCredential} /> : <section className="mt-10"><h2 className="text-2xl font-semibold">My tickets</h2><p role="status" className="mt-3">{tickets.message}</p></section>}
  </>;
}
