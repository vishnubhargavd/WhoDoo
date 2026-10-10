'use client';

import { useActionState } from 'react';
import { signOut } from '../app/account/actions';

export function LogoutForm() {
  const [state, action, pending] = useActionState(signOut, { message: '' });
  return <form action={action} aria-busy={pending} className="space-y-3">
    <button disabled={pending} className="rounded bg-teal-700 px-5 py-3 font-medium text-white disabled:opacity-60">{pending ? 'Signing out…' : 'Sign out'}</button>
    {state.message && <p role="alert">{state.message}</p>}
  </form>;
}
