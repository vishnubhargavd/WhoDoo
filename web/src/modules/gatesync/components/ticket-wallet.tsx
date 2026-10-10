'use client';
import { useState } from 'react';
import type { IssuedTicket, StudentTicket } from '../backend-tickets';
import { TicketCard } from './student-ticket-list';

export function TicketWallet({ tickets, issue }: { tickets: StudentTicket[]; issue: (ticketId: string) => Promise<IssuedTicket> }) {
  const [credentials, setCredentials] = useState<Record<string,string>>({});
  const [pending, setPending] = useState<string | null>(null);
  const [message, setMessage] = useState('');
  async function show(ticketId: string) {
    if (pending) return;
    setPending(ticketId); setMessage('');
    try {
      const result = await issue(ticketId);
      if (result.ok) setCredentials(previous => ({ ...previous, [ticketId]: result.credential }));
      else setMessage(result.message);
    } catch { setMessage('Your ticket could not be loaded. Try again.'); }
    finally { setPending(null); }
  }
  return <section aria-labelledby="wallet-title" className="mt-10 space-y-5"><h2 id="wallet-title" className="text-2xl font-semibold">My tickets</h2>
    <p className="text-sm text-slate-600">Keep your QR private. Show it only to event staff. Use your browser’s print option to save a personal copy.</p>
    {message && <p role="alert">{message}</p>}{tickets.length === 0 && <p role="status">No tickets available yet. Your confirmed reservations will appear here.</p>}
    <div className="grid gap-5 lg:grid-cols-2">{tickets.map(ticket => {
      const eligible = ticket.bookingStatus === 'confirmed' && ['paid','not_required'].includes(ticket.paymentStatus) && !['revoked','checked_in','cancelled'].includes(ticket.credentialState ?? '');
      const credential = credentials[ticket.ticketId] ?? null;
      return <div key={ticket.ticketId}><TicketCard record={{ ...ticket, credential, credentialState: credential ? 'issued' : ticket.credentialState }} />
        {eligible && <button type="button" disabled={!!pending} onClick={() => void show(ticket.ticketId)} className="my-3 min-h-11 rounded-lg bg-teal-700 px-5 font-semibold text-white disabled:opacity-60 print:hidden">{pending === ticket.ticketId ? 'Loading ticket…' : credential ? 'Refresh ticket' : 'Show private QR'}</button>}
      </div>;
    })}</div>
  </section>;
}
