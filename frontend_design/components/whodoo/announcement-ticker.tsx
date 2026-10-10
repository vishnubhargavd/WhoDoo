const MESSAGES = ['WhoDoo is live', 'Your campus. One platform.']
const ITEMS = Array.from({ length: 6 }, () => MESSAGES).flat()

export function AnnouncementTicker() {
  return (
    <div
      className="relative overflow-hidden bg-butter py-1 text-ink"
      role="marquee"
      aria-label="WhoDoo is live. Your campus. One platform."
    >
      <div className="animate-marquee flex w-max" aria-hidden="true">
        {[0, 1].map((copy) => (
          <ul key={copy} className="flex shrink-0 items-center">
            {ITEMS.map((item, i) => (
              <li
                key={i}
                className="flex items-center gap-5 pr-5 font-display text-[13px] leading-5 tracking-[0.02em] uppercase sm:gap-6 sm:pr-6 sm:text-[15px]"
              >
                {item}
                <span className="text-[0.7em]">{'●'}</span>
              </li>
            ))}
          </ul>
        ))}
      </div>
    </div>
  )
}
