import type { BookingRequest, BookingRequestParse } from './booking-contracts.ts';
import { parseBookingRequest } from './booking-contracts.ts';

/** Response shape proposed for the future FastAPI reservation endpoint. */
export type BookingApiResponse =
  | {
      readonly kind: 'reserved';
      readonly bookingId: string;
      readonly status: 'pending_payment' | 'confirmed';
      readonly paymentStatus: 'pending' | 'paid' | 'not_required';
      readonly totalPricePaise: number;
      readonly holdExpiresAt: string | null;
    }
  | {
      readonly kind: 'waitlisted';
      readonly entryId: string;
      readonly status: 'waiting';
      readonly position: number | null;
    }
  | { readonly kind: 'price_changed'; readonly message: string }
  | {
      readonly kind: 'rejected';
      readonly reason: 'closed' | 'sold_out' | 'invalid' | 'not_authorized';
      readonly message: string;
    };

export interface BookingApiCommand {
  /** Only browser-controlled selectors are accepted; trusted identity and price are omitted. */
  readonly request: BookingRequest;
  /** Server-issued quote reference; never a price supplied by the browser. */
  readonly acceptedQuoteId: string;
}

export type BookingApiErrorCode =
  | 'not_configured'
  | 'invalid_configuration'
  | 'invalid_request'
  | 'not_authenticated'
  | 'network_error'
  | 'http_error'
  | 'invalid_response';

export class BookingApiError extends Error {
  readonly code: BookingApiErrorCode;

  constructor(code: BookingApiErrorCode, message: string) {
    super(message);
    this.code = code;
    this.name = 'BookingApiError';
  }
}

export type BookingApiResult =
  | { readonly ok: true; readonly value: BookingApiResponse }
  | { readonly ok: false; readonly error: BookingApiErrorCode; readonly message: string };

export interface BookingApiConfig {
  /** Explicit full endpoint URL from approved server-side configuration; no default route is guessed. */
  readonly reservationEndpoint: string | undefined;
  /** Must obtain authorization through an approved server-side identity bridge. */
  readonly getAuthorizationHeader: () => Promise<string | null>;
  /** Injection point for deterministic tests; defaults to the platform fetch. */
  readonly fetcher?: (input: string, init: RequestInit) => Promise<Response>;
}

function validIdentifier(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 200
    && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value);
}

function hasOnlyKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).every((key) => keys.includes(key));
}

function validPrice(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}

function validDateOrNull(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && value.length > 0
    && Number.isFinite(Date.parse(value)));
}

/** Strictly parse the proposed API response; unknown shapes never look successful. */
export function parseBookingApiResponse(input: unknown): BookingApiResponse | null {
  if (input === null || typeof input !== 'object' || Array.isArray(input)) return null;
  const value = input as Record<string, unknown>;

  if (value.kind === 'reserved' && hasOnlyKeys(value,
      ['kind', 'bookingId', 'status', 'paymentStatus', 'totalPricePaise', 'holdExpiresAt'])
      && validIdentifier(value.bookingId)
      && (value.status === 'pending_payment' || value.status === 'confirmed')
      && (value.paymentStatus === 'pending' || value.paymentStatus === 'paid' || value.paymentStatus === 'not_required')
      && validPrice(value.totalPricePaise)
      && validDateOrNull(value.holdExpiresAt)) {
    if ((value.status === 'pending_payment') !== (value.paymentStatus === 'pending')) return null;
    if (value.status === 'confirmed' && value.holdExpiresAt !== null) return null;
    return {
      kind: 'reserved', bookingId: value.bookingId, status: value.status,
      paymentStatus: value.paymentStatus, totalPricePaise: value.totalPricePaise,
      holdExpiresAt: value.holdExpiresAt
    };
  }

  if (value.kind === 'waitlisted' && hasOnlyKeys(value, ['kind', 'entryId', 'status', 'position'])
      && validIdentifier(value.entryId) && value.status === 'waiting'
      && (value.position === null || (Number.isSafeInteger(value.position) && (value.position as number) > 0))) {
    return { kind: 'waitlisted', entryId: value.entryId, status: 'waiting', position: value.position as number | null };
  }

  if (value.kind === 'price_changed' && hasOnlyKeys(value, ['kind', 'message'])
      && typeof value.message === 'string' && value.message.length <= 500) {
    return { kind: 'price_changed', message: value.message };
  }

  const rejectionReasons = ['closed', 'sold_out', 'invalid', 'not_authorized'] as const;
  if (value.kind === 'rejected' && hasOnlyKeys(value, ['kind', 'reason', 'message'])
      && rejectionReasons.includes(value.reason as typeof rejectionReasons[number])
      && typeof value.message === 'string' && value.message.length <= 500) {
    return {
      kind: 'rejected', reason: value.reason as 'closed' | 'sold_out' | 'invalid' | 'not_authorized',
      message: value.message
    };
  }
  return null;
}

function approvedEndpoint(value: string | undefined): URL {
  if (!value?.trim()) throw new BookingApiError('not_configured', 'FairSeat booking API is not connected.');
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new BookingApiError('invalid_configuration', 'FairSeat booking API endpoint must be an absolute URL.');
  }
  const localHttp = url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if ((!localHttp && url.protocol !== 'https:') || url.username || url.password || url.hash || url.search) {
    throw new BookingApiError('invalid_configuration', 'FairSeat booking API endpoint must use HTTPS and contain no credentials, query or fragment.');
  }
  return url;
}

function parseCommand(input: unknown): BookingApiCommand {
  if (input === null || typeof input !== 'object' || Array.isArray(input)) {
    throw new BookingApiError('invalid_request', 'Booking command must be an object.');
  }
  const value = input as Record<string, unknown>;
  if (Object.keys(value).some((key) => key !== 'request' && key !== 'acceptedQuoteId')) {
    throw new BookingApiError('invalid_request', 'Booking command contains an unsupported field.');
  }
  const parsed: BookingRequestParse = parseBookingRequest(value.request);
  if (!parsed.ok) throw new BookingApiError('invalid_request', `Invalid booking request: ${parsed.error}.`);
  if (!validIdentifier(value.acceptedQuoteId)) {
    throw new BookingApiError('invalid_request', 'A valid server-issued quote reference is required.');
  }
  return { request: parsed.value, acceptedQuoteId: value.acceptedQuoteId };
}

/**
 * An HTTP boundary only. It does not authenticate users, authorize events, price tickets,
 * reserve seats, or make concurrency guarantees; those remain the approved FastAPI service's job.
 */
export function createBookingApi(config: BookingApiConfig) {
  return {
    async reserve(input: unknown): Promise<BookingApiResult> {
      let command: BookingApiCommand;
      let endpoint: URL;
      try {
        command = parseCommand(input);
        endpoint = approvedEndpoint(config.reservationEndpoint);
      } catch (error) {
        if (error instanceof BookingApiError) {
          return { ok: false, error: error.code, message: error.message };
        }
        throw error;
      }

      let authorization: string | null;
      try {
        authorization = await config.getAuthorizationHeader();
      } catch {
        return { ok: false, error: 'not_authenticated', message: 'Sign in again before booking.' };
      }
      if (!authorization || !/^Bearer\s+\S+$/i.test(authorization)) {
        return { ok: false, error: 'not_authenticated', message: 'Sign in again before booking.' };
      }

      let response: Response;
      try {
        response = await (config.fetcher ?? fetch)(endpoint.toString(), {
          method: 'POST',
          headers: { 'content-type': 'application/json', authorization },
          body: JSON.stringify({ ...command.request, acceptedQuoteId: command.acceptedQuoteId }),
          cache: 'no-store',
          redirect: 'error'
        });
      } catch {
        return { ok: false, error: 'network_error', message: 'The booking service could not be reached.' };
      }

      let body: unknown;
      try {
        body = await response.json();
      } catch {
        return { ok: false, error: 'invalid_response', message: 'The booking service returned an unreadable response.' };
      }
      const parsed = parseBookingApiResponse(body);
      if (!parsed) return { ok: false, error: 'invalid_response', message: 'The booking service returned an unsupported response.' };
      if (!response.ok && parsed.kind !== 'rejected' && parsed.kind !== 'price_changed') {
        return { ok: false, error: 'http_error', message: 'The booking service could not complete this request.' };
      }
      return { ok: true, value: parsed };
    }
  };
}
