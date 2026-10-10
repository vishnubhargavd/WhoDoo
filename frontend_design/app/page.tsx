import { AnnouncementTicker } from '@/components/whodoo/announcement-ticker'
import { Hero } from '@/components/whodoo/hero'
import { ProductOverview } from '@/components/whodoo/product-overview'

export default function Page() {
  return (
    <main className="min-h-svh overflow-x-clip bg-ink">
      <AnnouncementTicker />
      <div className="flex flex-col gap-2 p-2 sm:gap-3 sm:p-3">
        <Hero />
        <ProductOverview />
      </div>
    </main>
  )
}
