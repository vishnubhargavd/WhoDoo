import 'server-only';
import { createClient } from './supabase/server';
import { backendOrigin, verifiedAccessToken, type BackendApi } from './backend-contract';

/** Request-scoped; no global clients, cached tokens, browser-provided IDs or service keys. */
export async function getBackend(): Promise<BackendApi | null> {
  const origin = backendOrigin(process.env.WHODOO_API_URL);
  if (!origin) return null;
  const supabase = await createClient();
  if (!supabase) return null;
  const token = await verifiedAccessToken(supabase.auth);
  if (!token) return null;
  return {
    async request(path, method = 'GET', body) {
      if (!path.startsWith('/api/v1/') || /[\\\r\n#]/.test(path)) throw new Error('Invalid API path');
      const url = new URL(path, origin);
      if (url.origin !== origin) throw new Error('Invalid API origin');
      const response = await fetch(url, {
        method, headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(15000)
      });
      return { status: response.status, body: await response.json() };
    }
  };
}
