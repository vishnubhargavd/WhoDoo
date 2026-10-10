import type { SupabaseClient } from '@supabase/supabase-js';

// Auth identity only. No application user, club or role mapping is established.
export type AuthPort = Pick<SupabaseClient['auth'],
  'signUp' | 'signInWithPassword' | 'getUser' | 'exchangeCodeForSession' | 'signOut'>;
export type AuthResult = { ok: true; destination: string } | { ok: false; message: string };
const destinations = new Set(['/', '/account', '/memberships', '/bookings', '/check-in']);

/** Deliberately allow only known internal pages; reject queries, encodings and hosts. */
export function safeDestination(value: unknown): string {
  return typeof value === 'string' && destinations.has(value) ? value : '/account';
}

export const setupMessage = 'Authentication setup is unavailable. Configure Supabase and restart the server.';
export const confirmationMessage = 'If signup is available for this address, check your email to confirm it. Open the link in the same browser used here. Already registered? Sign in instead.';

function errorMessage(code: string | undefined, mode: 'login' | 'signup'): string {
  if (code === 'over_request_rate_limit' || code === 'over_email_send_rate_limit') return 'Too many attempts. Please wait before trying again.';
  if (code === 'email_not_confirmed') return 'Confirm your email before signing in.';
  if (code === 'weak_password') return 'Choose a stronger password that meets the project password policy.';
  return mode === 'login' ? 'Unable to sign in. Check your email and password, then try again.' : 'Unable to sign up. Check your details and try again later.';
}

export async function submitEmailAuth(
  auth: AuthPort | null, mode: 'login' | 'signup', email: string, password: string,
  destination: unknown, origin: string
): Promise<AuthResult | { ok: true; message: string }> {
  if (!auth) return { ok: false, message: setupMessage };
  if (email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())
      || !password || password.length > 1024 || (mode === 'signup' && password.length < 8)) {
    return { ok: false, message: 'Enter a valid email and password. New passwords must contain at least 8 characters.' };
  }
  const next = safeDestination(destination);
  try {
    if (mode === 'signup') {
      const callback = new URL('/auth/callback', origin);
      callback.searchParams.set('next', next);
      const { data, error } = await auth.signUp({ email: email.trim(), password, options: { emailRedirectTo: callback.toString() } });
      // Duplicate signups may deliberately return an obfuscated user; never claim creation.
      if (error?.code === 'user_already_exists' || error?.code === 'email_exists') return { ok: true, message: confirmationMessage };
      if (error) return { ok: false, message: errorMessage(error.code, mode) };
      if (data.session) return { ok: true, destination: next };
      return { ok: true, message: confirmationMessage };
    }
    const { error } = await auth.signInWithPassword({ email: email.trim(), password });
    return error ? { ok: false, message: errorMessage(error.code, mode) } : { ok: true, destination: next };
  } catch { return { ok: false, message: 'Authentication service is unavailable. Please try again later.' }; }
}

export type AccountAccess =
  | { status: 'authenticated'; authUserId: string; email: string | null }
  | { status: 'login'; destination: string }
  | { status: 'unavailable'; message: string };

export type ProfileAccess = Exclude<AccountAccess, { status: 'authenticated' }>
  | { status: 'authenticated'; authUserId: string; email: string | null;
      emailVerification: 'verified' | 'unverified' | 'unavailable' };

/** Only getUser's server-verified response can grant account access. */
export async function verifyAccount(auth: AuthPort | null): Promise<AccountAccess> {
  const profile = await verifyProfile(auth);
  if (profile.status !== 'authenticated') return profile;
  return { status: 'authenticated', authUserId: profile.authUserId, email: profile.email };
}

/** Read only fields returned by getUser, never confirmation claims in metadata. */
export async function verifyProfile(auth: AuthPort | null): Promise<ProfileAccess> {
  if (!auth) return { status: 'unavailable', message: setupMessage };
  try {
    const { data, error } = await auth.getUser();
    if (error || !data.user) return { status: 'login', destination: '/login?next=%2Faccount&notice=session' };
    const confirmedAt = data.user.email_confirmed_at;
    const emailVerification = !data.user.email || confirmedAt === undefined ? 'unavailable'
      : confirmedAt === null ? 'unverified'
      : typeof confirmedAt === 'string' && Number.isFinite(Date.parse(confirmedAt)) ? 'verified' : 'unavailable';
    return { status: 'authenticated', authUserId: data.user.id, email: data.user.email ?? null, emailVerification };
  } catch { return { status: 'unavailable', message: 'Account verification is unavailable. Please try again later.' }; }
}

export async function confirmEmail(auth: AuthPort | null, code: string | null, destination: unknown): Promise<string> {
  if (!auth) return '/login?notice=setup';
  if (!code || code.length > 2048) return '/login?notice=confirmation';
  try {
    const { error } = await auth.exchangeCodeForSession(code);
    if (error) return '/login?notice=confirmation';
    const account = await verifyAccount(auth);
    return account.status === 'authenticated' ? safeDestination(destination) : '/login?notice=confirmation';
  } catch { return '/login?notice=confirmation'; }
}

export async function logout(auth: AuthPort | null): Promise<AuthResult> {
  if (!auth) return { ok: false, message: setupMessage };
  try {
    const { error } = await auth.signOut({ scope: 'local' });
    return error ? { ok: false, message: 'Unable to sign out. Please try again.' } : { ok: true, destination: '/login?notice=signed-out' };
  } catch { return { ok: false, message: 'Unable to sign out. Please try again.' }; }
}
