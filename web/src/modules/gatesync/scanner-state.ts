import { isOpaqueCredentialFormat } from './credential-format.ts';

export type ScanUiState = 'ready' | 'scanning' | 'invalid_qr' | 'credential_captured'
  | 'unrecognized' | 'wrong_event' | 'expired_or_revoked' | 'already_checked_in'
  | 'network_unavailable' | 'authorization_denied' | 'redeemed';

export interface ScanFeedback { readonly state: ScanUiState; readonly message: string; }

/** A local parse is format feedback only and can never yield a redeemed state. */
export function parseScannedCredential(value: unknown): ScanFeedback {
  return isOpaqueCredentialFormat(value)
    ? { state: 'credential_captured', message: 'Credential captured — server verification unavailable. Attendance was not changed.' }
    : { state: 'invalid_qr', message: 'This QR code is not a WhoDoo ticket credential.' };
}

export type CameraFailure = 'denied' | 'unavailable' | 'initialization';
export function cameraFailure(error: unknown): CameraFailure {
  const name = error && typeof error === 'object' && 'name' in error ? String((error as { name?: unknown }).name) : '';
  if (name === 'NotAllowedError' || name === 'SecurityError') return 'denied';
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError' || name === 'OverconstrainedError') return 'unavailable';
  return 'initialization';
}

export function createDuplicateScanGuard(cooldownMs = 1800) {
  let lastAcceptedAt = Number.NEGATIVE_INFINITY;
  return {
    accept(nowMs: number): boolean {
      if (!Number.isSafeInteger(nowMs) || nowMs < lastAcceptedAt || nowMs - lastAcceptedAt < cooldownMs) return false;
      lastAcceptedAt = nowMs;
      return true;
    },
    reset() { lastAcceptedAt = Number.NEGATIVE_INFINITY; }
  };
}

export interface ScannerControlsLike { stop(): void; }
export interface MediaTrackLike { stop(): void; }
export interface MediaStreamLike { getTracks(): readonly MediaTrackLike[]; }

/** Idempotent cleanup for ZXing controls and acquired media tracks. */
export function createScannerLifecycle() {
  let controls: ScannerControlsLike | null = null;
  let stream: MediaStreamLike | null = null;
  return {
    attach(nextControls: ScannerControlsLike, nextStream: MediaStreamLike | null) {
      controls?.stop();
      stream?.getTracks().forEach(track => track.stop());
      controls = nextControls;
      stream = nextStream;
    },
    stop() {
      controls?.stop();
      stream?.getTracks().forEach(track => track.stop());
      controls = null;
      stream = null;
    }
  };
}

export type RedemptionPayload =
  | { readonly status: 'redeemed'; readonly redemptionId: string; readonly checkedInAt: string; readonly eventId: string; readonly eventName: string }
  | { readonly status: 'invalid_credential' | 'unrecognized' }
  | { readonly status: 'wrong_event' }
  | { readonly status: 'expired' | 'revoked' }
  | { readonly status: 'already_checked_in' }
  | { readonly status: 'authorization_denied' }
  | { readonly status: 'unavailable' };

function validTimestamp(value: unknown): value is string {
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) return false;
  return new Date(value).toISOString() === value;
}

/** Only call with a parsed response from the authenticated redemption server. */
export function redemptionResultToFeedback(value: unknown, expectedEventId?: string): ScanFeedback {
  if (!value || typeof value !== 'object') return { state: 'network_unavailable', message: 'Check-in verification is unavailable. Please try again.' };
  const r = value as Record<string, unknown>;
  if (r.status === 'redeemed' && typeof r.redemptionId === 'string' && r.redemptionId.length > 0
      && validTimestamp(r.checkedInAt) && typeof r.eventId === 'string' && r.eventId.trim()
      && typeof r.eventName === 'string' && r.eventName.trim()) {
    if (expectedEventId && expectedEventId !== r.eventId) return { state: 'wrong_event', message: 'The server result does not match the selected event.' };
    return { state: 'redeemed', message: `Checked in successfully for ${r.eventName}.` };
  }
  if (r.status === 'authorization_denied') return { state: 'authorization_denied', message: 'You are not authorized to check in tickets for this event.' };
  if (r.status === 'wrong_event') return { state: 'wrong_event', message: 'This ticket belongs to a different event.' };
  if (r.status === 'expired' || r.status === 'revoked') return { state: 'expired_or_revoked', message: 'This ticket has expired or was revoked.' };
  if (r.status === 'already_checked_in') return { state: 'already_checked_in', message: 'This ticket was already checked in.' };
  if (r.status === 'invalid_credential' || r.status === 'unrecognized') return { state: 'unrecognized', message: 'The credential could not be verified.' };
  return { state: 'network_unavailable', message: 'Check-in verification is unavailable. Please try again.' };
}
