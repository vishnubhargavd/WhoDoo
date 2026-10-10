export type PublicSupabaseConfiguration =
  | { status: 'ready'; url: string; publishableKey: string }
  | { status: 'missing' | 'invalid'; message: string };

/** Shape validation only: configuration never proves connectivity or identity. */
export function validatePublicSupabaseConfiguration(
  rawUrl: string | undefined,
  rawKey: string | undefined
): PublicSupabaseConfiguration {
  const url = rawUrl?.trim();
  const publishableKey = rawKey?.trim();
  if (!url || !publishableKey) {
    return { status: 'missing', message: 'Supabase setup required. Add NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY to web/.env.local, then restart the server.' };
  }
  try {
    const parsed = new URL(url);
    const localHttp = parsed.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname);
    if ((parsed.protocol !== 'https:' && !localHttp) || parsed.username || parsed.password
        || parsed.search || parsed.hash || parsed.pathname !== '/') throw new Error('Invalid URL');
  } catch {
    return { status: 'invalid', message: 'Supabase configuration is invalid. Use the project HTTPS URL (or a localhost development URL).' };
  }
  if (!/^sb_publishable_[A-Za-z0-9_-]+$/.test(publishableKey)) {
    return { status: 'invalid', message: 'Supabase configuration is invalid. Use a publishable key; secret and service-role keys are not supported.' };
  }
  return { status: 'ready', url, publishableKey };
}

export function getPublicSupabaseConfiguration(): PublicSupabaseConfiguration {
  // Direct references let Next.js inline these public variables in browser code.
  return validatePublicSupabaseConfiguration(
    process.env.NEXT_PUBLIC_SUPABASE_URL,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY
  );
}
