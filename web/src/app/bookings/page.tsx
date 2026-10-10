import { redirect } from 'next/navigation';
import { createClient } from '../../lib/supabase/server';
import { verifyAccount } from '../../modules/membercore/auth-flow';
import { bookingLoginDestination } from '../../modules/fairseat/checkout';
import { loadEvents } from '../../modules/fairseat/view-models/booking-view';
import { getFairSeatIntegration } from '../../modules/fairseat/server-integration';
import { BookingPortal } from '../../modules/fairseat/components/booking-portal';
import { reserveTickets, requestTicketQuote } from './actions';
import { StudentTicketList } from '../../modules/gatesync/components/student-ticket-list';

export const dynamic = 'force-dynamic';
export default async function BookingsPage() {
  const client = await createClient();
  const access = await verifyAccount(client?.auth ?? null);
  if (access.status === 'login') redirect(bookingLoginDestination());
  if (access.status === 'unavailable') return <section><h1>Ticket booking unavailable</h1><p role="alert">{access.message}</p></section>;
  // Public event reads have not been approved: protect this route before loading any events.
  const integration = getFairSeatIntegration();
  const listing = await loadEvents(integration.events);
  return <>
    <BookingPortal listing={listing} quoteConnected={integration.quote !== null}
      requestQuote={requestTicketQuote} checkoutConnected={integration.reservation !== null} reserve={reserveTickets} />
    <StudentTicketList tickets={null} />
  </>;
}
