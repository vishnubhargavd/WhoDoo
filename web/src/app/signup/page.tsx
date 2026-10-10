import { AuthForm } from '../../components/auth-form';
import { getPublicSupabaseConfiguration } from '../../lib/supabase/config';
import { safeDestination } from '../../modules/membercore/auth-flow';

export default async function SignupPage({ searchParams }: { searchParams: Promise<{ next?: string }> }) {
  const query = await searchParams;
  const config = getPublicSupabaseConfiguration();
  return <section className="mx-auto max-w-md space-y-6">
    <h1 className="text-3xl font-semibold">Create an auth account</h1>
    <p>Confirm your email using the link we send. This creates a Supabase Auth identity; club membership and application profiles are not connected yet.</p>
    {config.status === 'ready' ? <AuthForm mode="signup" destination={safeDestination(query.next)} /> : <p role="alert">{config.message}</p>}
  </section>;
}
