import { AuthForm } from '../../components/auth-form';
import { getPublicSupabaseConfiguration } from '../../lib/supabase/config';
import { safeDestination } from '../../modules/membercore/auth-flow';

const notices: Record<string, string> = {
  session: 'Sign in to continue. Your session may have expired.',
  confirmation: 'The confirmation link could not be completed. It may be expired, already used or opened in a different browser. If your email is confirmed, sign in here; otherwise try signup again.',
  setup: 'Authentication setup is unavailable.',
  'signed-out': 'You have signed out of this browser.'
};

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string; notice?: string }> }) {
  const query = await searchParams;
  const config = getPublicSupabaseConfiguration();
  const notice = query.notice && Object.hasOwn(notices, query.notice) ? notices[query.notice] : undefined;
  return <section className="mx-auto max-w-md space-y-6">
    <h1 className="text-3xl font-semibold">Sign in</h1>
    {notice && <p role="status">{notice}</p>}
    {config.status === 'ready' ? <AuthForm mode="login" destination={safeDestination(query.next)} /> : <p role="alert">{config.message}</p>}
  </section>;
}
