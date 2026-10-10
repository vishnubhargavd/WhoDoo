import { getPublicSupabaseConfiguration } from '../lib/supabase/config';

export function SetupStatus() {
  const config = getPublicSupabaseConfiguration();
  return (
    <aside aria-label="Supabase setup status" className="mt-8 rounded-xl border border-slate-200 bg-white p-5 text-sm text-slate-600">
      <p className="font-semibold text-slate-900">{config.status === 'ready' ? 'Configuration present' : 'Supabase setup required'}</p>
      <p className="mt-2">{config.status === 'ready' ? 'Supabase connection and sign-in have not been verified. Organization data is not connected yet.' : config.message}</p>
    </aside>
  );
}
