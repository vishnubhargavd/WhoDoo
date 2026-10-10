const PREFIX = 'whodoo.ticket.v1.';
// 32 bytes encode to 43 base64url chars; the final 4 padding bits must be zero.
const TOKEN = /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/;

/** Browser-safe format check only; it never proves that a credential was issued. */
export function isOpaqueCredentialFormat(value: unknown): value is string {
  if (typeof value !== 'string' || value.length !== PREFIX.length + 43 || !value.startsWith(PREFIX)) return false;
  return TOKEN.test(value.slice(PREFIX.length));
}

export function credentialFormatPrefix(): string { return PREFIX; }
