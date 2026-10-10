import { QRCodeSVG } from 'qrcode.react';
import { buildTicketDisplayView, formatTicketDate } from '../ticket-display.ts';

export function StudentTicketList({ tickets }: { tickets: readonly unknown[] | null }) {
  return <section aria-labelledby="my-tickets-title" className="mt-10 space-y-5">
    <header><p className="text-sm font-semibold uppercase tracking-wide text-teal-700">GateSync</p>
      <h2 id="my-tickets-title" className="mt-1 text-2xl font-semibold">My tickets</h2>
      <p className="mt-2 text-slate-600">Your private ticket credentials appear here after they are issued by the ticket service.</p>
    </header>
    {tickets === null ? <div role="status" className="rounded-2xl border border-dashed border-slate-300 bg-white p-6 sm:p-8">
      <h3 className="font-semibold">Ticket service not connected</h3>
      <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">We cannot load your ticket records yet. A confirmed booking does not mean that a QR credential has been issued.</p>
    </div> : tickets.length === 0 ? <div role="status" className="rounded-2xl border border-slate-200 bg-white p-6">
      <h3 className="font-semibold">No tickets available</h3><p className="mt-2 text-sm text-slate-600">Issued tickets will appear here.</p>
    </div> : <div className="grid gap-5 lg:grid-cols-2">{tickets.map((ticket, index) =>
      <TicketCard key={index} record={ticket} />)}</div>}
  </section>;
}

export function TicketCard({ record }: { record: unknown }) {
  const ticket = buildTicketDisplayView(record);
  const issued = ticket.state === 'issued' && ticket.credential !== null;
  return <article className="break-inside-avoid rounded-2xl border border-slate-200 bg-white p-5 shadow-sm print:break-inside-avoid print:shadow-none sm:p-6">
    <div className="flex items-start justify-between gap-4">
      <div><p className="text-xs font-semibold uppercase tracking-wide text-teal-700">{ticket.label}</p>
        <h3 className="mt-2 text-xl font-semibold">{ticket.eventName}</h3></div>
      <span aria-label={ticket.label} className={`shrink-0 rounded-full px-3 py-1 text-xs font-semibold ${issued ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-100 text-slate-700'}`}>{ticket.state.replaceAll('_', ' ')}</span>
    </div>
    <dl className="mt-5 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
      <dt className="text-slate-500">When</dt><dd><time dateTime={ticket.startsAt}>{ticket.startsAt === 'Not available' ? ticket.startsAt : formatTicketDate(ticket.startsAt)}</time></dd>
      <dt className="text-slate-500">Venue</dt><dd>{ticket.venue}</dd>
      <dt className="text-slate-500">Ticket</dt><dd>{ticket.category}</dd>
      <dt className="text-slate-500">Holder</dt><dd>{ticket.holderName}</dd>
    </dl>
    <p role="status" className="mt-4 text-sm leading-6 text-slate-600">{ticket.message}</p>
    {issued && <div className="mt-5 flex flex-col items-center rounded-xl border border-slate-200 bg-white p-4 print:border-black">
      <QRCodeSVG value={ticket.credential!} size={208} level="H" title={`Private ticket QR for ${ticket.eventName}`} />
      <p className="mt-3 text-center text-xs text-slate-500">Keep this code private. Event staff verify it at the entrance.</p>
    </div>}
  </article>;
}
