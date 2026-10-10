import { backendMessage, iso, label, object, uuid, type BackendApi, type BackendReply } from '../../lib/backend-contract.ts';
import { isOpaqueCredentialFormat } from './credential-format.ts';
import { buildTicketDisplayView, type TicketDisplayRecord } from './ticket-display.ts';
import type { ScanFeedback } from './scanner-state.ts';

export type StudentTicket = TicketDisplayRecord & { readonly eventId: string };
export type ScannerEvent = { readonly eventId: string; readonly name: string; readonly startsAt: string | null };
export function mapTickets(value: unknown): StudentTicket[] | null {
  if (!Array.isArray(value) || value.length > 1000) return null;
  const tickets: StudentTicket[] = [];
  for (const r of value) {
    if (!object(r) || !uuid(r.ticket_id) || !uuid(r.event_id) || !['pending_issue','issued','revoked','checked_in'].includes(String(r.ticket_status))) return null;
    const starts = iso(r.starts_at);
    if (!starts) return null;
    const ticket = { ticketId: r.ticket_id, eventId: r.event_id, eventName: r.event_name, venue: r.venue,
      startsAt: starts, category: r.category, holderName: r.holder_name, bookingStatus: r.booking_status, paymentStatus: r.payment_status,
      credentialState: r.ticket_status === 'pending_issue' ? null : r.ticket_status, credential: null } as StudentTicket;
    if (buildTicketDisplayView(ticket).state === 'invalid') return null;
    tickets.push(ticket);
  }
  return new Set(tickets.map(t => t.ticketId)).size === tickets.length ? tickets : null;
}
export function mapScannerEvents(value: unknown): ScannerEvent[] | null {
  if (!object(value) || !Array.isArray(value.events) || value.events.length > 500) return null;
  const events: ScannerEvent[] = [];
  for (const r of value.events) {
    if (!object(r) || !uuid(r.event_id) || !label(r.event_name,160) || !uuid(r.club_id)) return null;
    const startsAt = r.starts_at === null ? null : iso(r.starts_at);
    if (r.starts_at !== null && startsAt === null) return null;
    events.push({ eventId: r.event_id, name: r.event_name, startsAt });
  }
  return new Set(events.map(e => e.eventId)).size === events.length ? events : null;
}
export type TicketRead = { state: 'ready'; tickets: StudentTicket[] } | { state: 'unavailable'; message: string };
export async function readTickets(api: BackendApi | null): Promise<TicketRead> {
  if (!api) return { state: 'unavailable', message: 'Ticket service not connected. Sign in and check the backend configuration.' };
  try {
    const reply = await api.request('/api/v1/gatesync/tickets');
    if (reply.status !== 200) return { state: 'unavailable', message: backendMessage(reply) };
    const tickets = mapTickets(reply.body);
    return tickets ? { state: 'ready', tickets } : { state: 'unavailable', message: 'Ticket details could not be verified.' };
  } catch { return { state: 'unavailable', message: 'Ticket service is temporarily unavailable.' }; }
}
export type IssuedTicket = { ok: true; credential: string } | { ok: false; message: string };
export async function issueTicket(api: BackendApi | null, ticketId: unknown): Promise<IssuedTicket> {
  if (!api || !uuid(ticketId)) return { ok: false, message: 'Sign in again and choose an issued ticket.' };
  try {
    const reply = await api.request(`/api/v1/gatesync/tickets/${ticketId}/credential`, 'POST');
    if (reply.status !== 200) return { ok: false, message: backendMessage(reply) };
    const r = reply.body;
    if (!object(r) || r.ticket_id !== ticketId || !iso(r.issued_at) || !isOpaqueCredentialFormat(r.credential)) return { ok: false, message: 'Ticket credential could not be verified.' };
    return { ok: true, credential: r.credential };
  } catch { return { ok: false, message: 'Ticket service is temporarily unavailable.' }; }
}
/** A check-in success is created only from a successful authenticated backend response. */
export function mapRedemption(reply: BackendReply): ScanFeedback {
  if (reply.status === 401 || reply.status === 403) return { state: 'authorization_denied', message: backendMessage(reply) };
  if (reply.status !== 200 || !object(reply.body)) return { state: 'network_unavailable', message: 'Check-in verification is unavailable. Attendance was not confirmed.' };
  const r = reply.body;
  if (r.outcome === 'checked_in' && uuid(r.ticket_id) && iso(r.checked_in_at)) return { state: 'redeemed', message: 'Checked in successfully. Attendance was confirmed by the server.' };
  if (r.outcome === 'already_checked_in') return { state: 'already_checked_in', message: 'This ticket was already checked in.' };
  if (r.outcome === 'wrong_event') return { state: 'wrong_event', message: 'This ticket belongs to a different event.' };
  if (r.outcome === 'not_eligible') return { state: 'expired_or_revoked', message: 'This ticket is invalid, revoked, expired or awaiting confirmation.' };
  return { state: 'network_unavailable', message: 'The check-in response could not be verified.' };
}
export async function redeemBackend(api: BackendApi | null, eventId: unknown, credential: unknown): Promise<ScanFeedback> {
  if (!api) return { state: 'authorization_denied', message: 'Sign in again before scanning tickets.' };
  if (!uuid(eventId) || !isOpaqueCredentialFormat(credential)) return { state: 'invalid_qr', message: 'Choose an event and scan a valid WhoDoo ticket.' };
  try { return mapRedemption(await api.request('/api/v1/gatesync/redeem', 'POST', { event_id: eventId, credential })); }
  catch { return { state: 'network_unavailable', message: 'Check-in could not be confirmed. Retry to verify the ticket state.' }; }
}
