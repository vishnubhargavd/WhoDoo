import { verifyAccount, type AuthPort } from '../membercore/auth-flow.ts';

export type ScannerAccess =
  | { readonly status: 'allow-authenticated'; readonly email: string | null }
  | { readonly status: 'redirect'; readonly destination: string }
  | { readonly status: 'unavailable'; readonly message: string };

/** Sign-in gate only. It does not grant event-staff or club permissions. */
export async function getScannerAccess(auth: AuthPort | null): Promise<ScannerAccess> {
  const account = await verifyAccount(auth);
  if (account.status === 'login') return { status: 'redirect', destination: '/login?next=%2Fcheck-in' };
  if (account.status === 'unavailable') return { status: 'unavailable', message: account.message };
  return { status: 'allow-authenticated', email: account.email };
}
