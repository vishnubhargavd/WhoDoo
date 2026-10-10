'use client';
import { useEffect, useRef, useState } from 'react';
import type { BookingPriceQuote, EventId, TicketCategoryId } from '../booking-contracts';
import type { BookingApiCommand, BookingApiResult } from '../booking-api';
import { createSubmissionController } from '../checkout';
import type { QuoteReadResult, QuoteSelection } from '../checkout';
import { bookingStatusLabel, filterEvents, formatPaise, priceView, selectionError } from '../view-models/booking-view';
import type { EventListing, EventView } from '../view-models/booking-view';
import styles from './booking-portal.module.css';

function eventTime(value: string): string {
  return new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
}
export function ReservationNotice({ result }: { result: BookingApiResult | null }) {
  if (!result) return null;
  if (!result.ok) return <p role="alert">{result.message}{result.error === 'not_authenticated' && <> <a href="/login?next=%2Fbookings">Sign in again</a></>}</p>;
  const value = result.value;
  if (value.kind === 'rejected' || value.kind === 'price_changed') return <p role="alert">{value.kind === 'price_changed' ? 'Price changed. Reload to review a fresh quote. ' : 'Reservation not completed. '}{value.message}</p>;
  if (value.kind === 'waitlisted') return <div><h3>Waitlisted</h3><p>Reference: {value.entryId}</p><p>{value.position === null ? 'Position not available.' : `Current position: ${value.position}`}</p><p>A waitlist entry does not confirm a seat.</p></div>;
  return <div><h3>{bookingStatusLabel(value.status, value.paymentStatus)}</h3><p>Reservation: {value.bookingId}</p><p>Server total: {formatPaise(value.totalPricePaise)}</p>{value.holdExpiresAt && <p>Hold expires: {eventTime(value.holdExpiresAt)} IST</p>}<p>{value.status === 'confirmed' ? 'Payment and ticket actions are not connected. No QR ticket has been issued.' : 'A hold is not a paid ticket. Payment is not connected yet.'}</p></div>;
}

export function BookingPortal({ listing, quoteConnected, requestQuote, checkoutConnected, reserve }: {
  listing: EventListing; quoteConnected: boolean; checkoutConnected: boolean;
  requestQuote: (selection: QuoteSelection) => Promise<QuoteReadResult>;
  reserve: (command: BookingApiCommand) => Promise<BookingApiResult>;
}) {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [day, setDay] = useState('');
  const [selected, setSelected] = useState<EventView | null>(null);
  const [categoryId, setCategoryId] = useState('');
  const [quantity, setQuantity] = useState(1);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BookingApiResult | null>(null);
  const [now, setNow] = useState<number | null>(null);
  const [quote, setQuote] = useState<BookingPriceQuote | null>(null);
  const [quoteLoading, setQuoteLoading] = useState(false);
  const [quoteNotice, setQuoteNotice] = useState('');
  const detailHeading = useRef<HTMLHeadingElement>(null);
  const notice = useRef<HTMLDivElement>(null);
  const controller = useRef<ReturnType<typeof createSubmissionController> | null>(null);
  if (!controller.current) controller.current = createSubmissionController(() => crypto.randomUUID());
  useEffect(() => { setNow(Date.now()); const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => { if (selected) detailHeading.current?.focus(); }, [selected]);
  useEffect(() => { if (result) notice.current?.focus(); }, [result]);
  useEffect(() => {
    let cancelled = false;
    setQuote(null); setQuoteNotice('');
    if (!selected || !quoteConnected || selectionError(selected, categoryId, quantity)) { setQuoteLoading(false); return; }
    setQuoteLoading(true);
    const timer = setTimeout(() => {
      requestQuote({ eventId: selected.eventId as EventId, ticketCategoryId: categoryId as TicketCategoryId, quantity })
        .then(response => {
          if (cancelled) return;
          if (response.state === 'ready') setQuote(response.quote);
          else setQuoteNotice(response.state === 'not_authenticated' ? 'Sign in again to verify your price.' : 'A server quote could not be verified. Reload to try again.');
        }).catch(() => { if (!cancelled) setQuoteNotice('Pricing service unavailable. Reload to try again.'); })
        .finally(() => { if (!cancelled) setQuoteLoading(false); });
    }, 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [selected, categoryId, quantity, quoteConnected, requestQuote]);
  const price = selected && now !== null ? priceView(selected, categoryId, quantity, quote, now) : null;
  const category = selected?.categories.find(c => c.categoryId === categoryId);
  const error = selected ? selectionError(selected, categoryId, quantity) : null;
  const saleOpen = selected?.status === 'published' && ['open', 'waitlist'].includes(selected.saleState) && now !== null && now < Date.parse(selected.startsAt);
  const disabled = busy || quoteLoading || !checkoutConnected || !saleOpen || !!error || price?.kind !== 'confirmed';
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled || !selected || !price?.quoteId) return;
    setBusy(true);
    try {
      const response = await controller.current!.submit({ eventId: selected.eventId as EventId,
        ticketCategoryId: categoryId as TicketCategoryId, quantity, acceptedQuoteId: price.quoteId }, reserve);
      if (response) setResult(response);
    } catch { setResult({ ok: false, error: 'network_error', message: 'Unable to submit. Please try again later.' }); }
    finally { setBusy(false); }
  }
  const events = filterEvents(listing.events, search, status, day);
  return <div className={styles.portal}>
    <header className={styles.hero}><p className={styles.eyebrow}>FairSeat · Your campus, your next experience</p><h1>Find your next event</h1><p>Explore organization events, review your ticket options, and reserve with a verified price.</p><div className={styles.chips}><span>Server-verified pricing</span><span>Clear reservation status</span></div></header>
    {!checkoutConnected && <aside className={styles.banner} role="status"><strong>Ticket reservations are not connected yet.</strong><p>Event access, member quotes and the approved reservation service must be connected before checkout is available.</p></aside>}
    <section aria-labelledby="events-heading"><div className={styles.sectionHeader}><h2 id="events-heading">Discover events</h2><span>Dates shown in IST</span></div>
      {listing.state !== 'ready' ? <div className={styles.empty}><span aria-hidden="true" className={styles.emptyIcon}>↗</span><h3>{listing.state === 'not_connected' ? 'Event listings not connected yet' : 'Events temporarily unavailable'}</h3><p>{listing.state === 'not_connected' ? 'The approved event-read API is not available. Real events and ticket options will appear here once the backend is connected.' : 'Event information could not be verified. Please reload to try again.'}</p><a href="/account">View your account</a></div> : <>
        <div className={styles.filters}><label>Search events<input type="search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search by event name" /></label><label>Status<select value={status} onChange={e => setStatus(e.target.value)}><option value="">All statuses</option>{['published', 'cancelled', 'completed', 'draft'].map(s => <option key={s} value={s}>{s}</option>)}</select></label><label>Date (UTC)<input type="date" value={day} onChange={e => setDay(e.target.value)} /></label></div>
        <p role="status" className={styles.count}>{events.length} {events.length === 1 ? 'event' : 'events'}</p>
        {events.length === 0 ? <div className={styles.empty}><h3>{listing.events.length === 0 ? 'No events available yet' : 'No matching events'}</h3><p>{listing.events.length === 0 ? 'There are no events in the connected listing.' : 'Try a different search or clear your filters.'}</p></div> : <div className={styles.grid}>{events.map(e => <article className={styles.card} key={e.eventId}><span className={styles.badge}>{e.status} · {e.saleState.replaceAll('_', ' ')}</span><h3>{e.title}</h3><p><time dateTime={e.startsAt}>{eventTime(e.startsAt)}</time> IST</p><p>{e.venue ?? 'Venue not available'}</p><button type="button" disabled={busy} onClick={() => { setSelected(e); setCategoryId(e.categories[0]?.categoryId ?? ''); setQuantity(1); setResult(null); }}>View ticket options<span className="sr-only"> for {e.title}</span></button></article>)}</div>}
      </>}
    </section>
    {selected && <section className={styles.details} aria-labelledby="event-details"><div><span className={styles.badge}>{selected.status} · {selected.saleState.replaceAll('_', ' ')}</span><h2 id="event-details" tabIndex={-1} ref={detailHeading}>{selected.title}</h2><p>{selected.description}</p><dl><dt>Starts</dt><dd>{eventTime(selected.startsAt)} IST</dd><dt>Ends</dt><dd>{eventTime(selected.endsAt)} IST</dd><dt>Venue</dt><dd>{selected.venue ?? 'Not available'}</dd></dl><p>Availability is a server snapshot. Seats are confirmed only by the reservation service.</p></div>
      <form onSubmit={submit} aria-busy={busy}><h3>Choose your tickets</h3><label>Ticket category<select value={categoryId} disabled={busy} onChange={e => { setCategoryId(e.target.value); setResult(null); }}>{selected.categories.length === 0 && <option value="">No ticket categories available</option>}{selected.categories.map(c => <option key={c.categoryId} value={c.categoryId}>{c.name} · {formatPaise(c.basePricePaise)}</option>)}</select></label><label>Quantity<input type="number" min={1} max={category?.maxQuantity ?? 1} step={1} value={Number.isNaN(quantity) ? '' : quantity} disabled={busy} aria-invalid={!!error} aria-describedby="selection-help" onChange={e => { setQuantity(e.target.valueAsNumber); setResult(null); }} /></label><p id="selection-help">{error ?? (category?.availability === null ? 'Availability not supplied.' : `${category?.availability ?? 0} seats in the last server snapshot.`)}</p>
        <div className={styles.price} aria-busy={quoteLoading}><h4>{price?.kind === 'confirmed' ? 'Server-confirmed quote' : 'Estimated price'}</h4>{quoteLoading && <p role="status">Verifying your ticket price…</p>}{quoteNotice && <p role="alert">{quoteNotice}</p>}{price ? <><dl><dt>Base / ticket</dt><dd>{price.base}</dd><dt>Discount / ticket</dt><dd>{price.savings ?? 'Not verified'}</dd><dt>Price / ticket</dt><dd>{price.unit}</dd><dt>{price.kind === 'confirmed' ? 'Quoted total' : 'Provisional total'}</dt><dd><strong>{price.total}</strong></dd></dl><p>{price.explanation}</p></> : <p>Select a valid category and quantity to review pricing.</p>}<p>The server revalidates price and availability when you reserve.</p></div>
        {!checkoutConnected ? <p>Checkout is unavailable until the reservation API is connected.</p> : !saleOpen ? <p>Booking is not open for this event.</p> : price?.kind !== 'confirmed' ? <p>A fresh authenticated server quote is required. Reload after quotes become available.</p> : null}
        <button disabled={disabled} type="submit">{busy ? 'Submitting reservation…' : price?.kind === 'confirmed' ? `Reserve at ${price.total}` : 'Reserve tickets'}</button>
        <div ref={notice} tabIndex={-1} role="status" aria-live="polite" className={styles.notice}><ReservationNotice result={result} /></div>
      </form>
    </section>}
  </div>;
}
