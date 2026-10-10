/** Transport for the existing FastAPI API. Credentials never belong in returned DTOs. */
export type BackendReply = { status: number; body: unknown };
export interface BackendApi { request(path: string, method?: 'GET' | 'POST', body?: unknown): Promise<BackendReply> }
export const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
export const uuid = (v: unknown): v is string => typeof v === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v);
export const label = (v: unknown, max = 200): v is string => typeof v === 'string' && !!v.trim() && v.length <= max && !/[\u0000-\u001f\u007f]/.test(v);
export const paise = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0;
/** FastAPI serializes UTC with Z or an offset and may include microseconds. */
export function iso(value: unknown): string | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return null;
  // Reject normalized invalid calendar dates before accepting the timezone conversion.
  const day = value.slice(0, 10);
  if (new Date(day + 'T00:00:00.000Z').toISOString().slice(0, 10) !== day) return null;
  return new Date(ms).toISOString();
}
export function backendOrigin(value: unknown): string | null {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const url = new URL(value);
    const local = url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    if ((!local && url.protocol !== 'https:') || url.username || url.password || url.search || url.hash || url.pathname !== '/') return null;
    return url.origin;
  } catch { return null; }
}
export function errorCode(reply: BackendReply): string | null {
  return object(reply.body) && object(reply.body.error) && typeof reply.body.error.code === 'string' ? reply.body.error.code : null;
}
export function backendMessage(reply: BackendReply): string {
  if (errorCode(reply) === 'account_not_linked') return 'Your sign-in is verified. Ask a club administrator to link your existing application account.';
  if (reply.status === 401) return 'Your session has expired. Sign in again.';
  if (reply.status === 403) return 'You do not have permission for this action.';
  if (reply.status === 404 || reply.status === 503) return 'This service is not available yet. Please contact your club administrator.';
  return 'The service could not complete this request. Please try again.';
}

export interface TokenAuth {
  getUser(): Promise<{ data: { user: { id: string } | null }; error: unknown }>;
  getSession(): Promise<{ data: { session: { access_token: string; user: { id: string } } | null }; error: unknown }>;
}
/** getSession only retrieves the token after getUser has independently verified identity. */
export async function verifiedAccessToken(auth: TokenAuth): Promise<string | null> {
  try {
    const user = await auth.getUser();
    if (user.error || !user.data.user) return null;
    const session = await auth.getSession();
    if (session.error || !session.data.session || session.data.session.user.id !== user.data.user.id) return null;
    const token = session.data.session.access_token;
    return typeof token === 'string' && token.length > 0 && !/[\r\n]/.test(token) ? token : null;
  } catch { return null; }
}
