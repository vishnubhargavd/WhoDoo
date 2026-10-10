import { createServerClient } from '@supabase/ssr';
import { NextResponse, type NextRequest } from 'next/server';
import { getPublicSupabaseConfiguration } from './config';

export async function updateSession(request: NextRequest) {
  let response = NextResponse.next({ request });
  const config = getPublicSupabaseConfiguration();
  if (config.status !== 'ready') return response;
  const supabase = createServerClient(config.url, config.publishableKey, {
    cookies: {
      getAll() { return request.cookies.getAll(); },
      setAll(cookiesToSet, cacheHeaders) {
        const previousCookies = response.cookies.getAll();
        const previousHeaders = new Headers(response.headers);
        cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
        response = NextResponse.next({ request });
        previousCookies.forEach((cookie) => response.cookies.set(cookie));
        for (const name of ['cache-control', 'expires', 'pragma']) {
          const value = previousHeaders.get(name);
          if (value) response.headers.set(name, value);
        }
        cookiesToSet.forEach(({ name, value, options }) => response.cookies.set(name, value, options));
        Object.entries(cacheHeaders ?? {}).forEach(([name, value]) => response.headers.set(name, value));
      }
    }
  });
  // Verifies and refreshes an existing token; getSession alone would not verify.
  // Account protection is enforced separately with getUser on the server.
  // Never authorize using metadata or an unverified session.
  try { await supabase.auth.getClaims(); }
  catch { /* Public auth forms remain reachable; account verification fails closed. */ }
  response.headers.set('Cache-Control', 'private, no-store');
  return response;
}
