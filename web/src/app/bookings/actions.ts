'use server';
import { createClient } from '../../lib/supabase/server';
import { reserveForAccount, quoteForAccount } from '../../modules/fairseat/checkout';
import type { QuoteReadResult } from '../../modules/fairseat/checkout';
import { getFairSeatIntegration } from '../../modules/fairseat/server-integration';
import type { BookingApiResult } from '../../modules/fairseat/booking-api';
import { getBackend } from '../../lib/backend';
import { issueTicket } from '../../modules/gatesync/backend-tickets';

export async function loadTicketCredential(ticketId: string) { return issueTicket(await getBackend(), ticketId); }

export async function reserveTickets(command: unknown): Promise<BookingApiResult> {
  try {
    const client = await createClient();
    return await reserveForAccount(client?.auth ?? null, getFairSeatIntegration(await getBackend()).reservation, command);
  } catch { return { ok: false, error: 'network_error', message: 'Booking is temporarily unavailable. Please try again later.' }; }
}

export async function requestTicketQuote(selection: unknown): Promise<QuoteReadResult> {
  try {
    const client = await createClient();
    return await quoteForAccount(client?.auth ?? null, getFairSeatIntegration(await getBackend()).quote, selection, Date.now());
  } catch { return { state: 'unavailable' }; }
}
