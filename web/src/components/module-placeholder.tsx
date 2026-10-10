import Link from 'next/link';
import { SetupStatus } from './setup-status';

export function ModulePlaceholder({ title, description }: { title: string; description: string }) {
  return (
    <>
      <Link href="/" className="text-sm text-teal-700 hover:underline">← Workspace</Link>
      <h1 className="mt-6 text-3xl font-bold tracking-tight">{title}</h1>
      <p className="mt-3 max-w-2xl leading-7 text-slate-600">{description}</p>
      <section className="mt-8 rounded-xl border border-dashed border-slate-300 bg-white p-8">
        <h2 className="font-semibold">Not connected yet</h2>
        <p className="mt-2 text-sm leading-6 text-slate-600">This feature is being prepared. No organization records are displayed and no actions are available yet.</p>
      </section>
      <SetupStatus />
    </>
  );
}
