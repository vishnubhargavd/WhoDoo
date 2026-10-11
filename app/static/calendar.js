// The events calendar: a month grid, and a panel listing everything on the chosen day.
// Days and times are the college's (IST) whatever the visitor's own clock says.
// Everything is written with textContent, so an event's name can never inject markup.

(() => {
  const ZONE = "Asia/Kolkata";
  const dayKey = new Intl.DateTimeFormat("en-CA", { timeZone: ZONE, year: "numeric", month: "2-digit", day: "2-digit" });
  const clock = new Intl.DateTimeFormat("en-IN", { timeZone: ZONE, hour: "numeric", minute: "2-digit", hour12: true });
  const longDay = new Intl.DateTimeFormat("en-IN", { timeZone: "UTC", weekday: "long", day: "numeric", month: "long", year: "numeric" });
  const monthName = new Intl.DateTimeFormat("en-IN", { timeZone: "UTC", month: "long", year: "numeric" });

  const key = (date) => dayKey.format(date);                       // "2026-10-23", in IST
  const keyOf = (y, m, d) => `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
  const rupees = (paise) => "₹" + (paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function kind(event) {
    if (event.registration_state === "closed" || new Date(event.ends_at) < new Date()) return "past";
    return { free: "free", paid: "paid" }[event.registration_mode] || "plain";
  }

  document.querySelectorAll("[data-calendar]").forEach((root) => {
    const events = JSON.parse(root.querySelector(".cal-data").textContent)
      .map((event) => ({ ...event, start: new Date(event.starts_at), end: new Date(event.ends_at) }))
      .sort((a, b) => a.start - b.start);

    // An event appears on every day it touches (a two-day fest shows on both).
    const byDay = new Map();
    for (const event of events) {
      const last = key(new Date(event.end.getTime() - 1));
      for (let at = new Date(event.start), guard = 0; guard < 10; at = new Date(at.getTime() + 86400000), guard += 1) {
        const day = key(at);
        if (!byDay.has(day)) byDay.set(day, []);
        byDay.get(day).push(event);
        if (day >= last) break;
      }
    }

    const today = key(new Date());
    const [ty, tm] = today.split("-").map(Number);
    let year = ty, month = tm - 1;                                  // the month on show
    // Start on today if something is on; otherwise the next day that has an event.
    let selected = byDay.has(today) ? today : ([...byDay.keys()].sort().find((day) => day >= today) || today);
    if (selected !== today) { year = Number(selected.slice(0, 4)); month = Number(selected.slice(5, 7)) - 1; }

    const grid = root.querySelector(".cal-grid");
    const title = root.querySelector(".cal-month");
    const dayTitle = root.querySelector(".cal-day-title");
    const dayCount = root.querySelector(".cal-day-count");
    const dayList = root.querySelector(".cal-day-list");

    function drawDay() {
      const [y, m, d] = selected.split("-").map(Number);
      const list = byDay.get(selected) || [];
      dayTitle.textContent = longDay.format(new Date(Date.UTC(y, m - 1, d)));
      dayCount.textContent = list.length ? `${list.length} event${list.length === 1 ? "" : "s"}` : "Nothing scheduled.";
      dayList.replaceChildren();
      for (const event of list) {
        const card = el("article", "cal-event " + kind(event));
        card.append(el("h4", "", event.name));
        const starts = key(event.start) === selected ? clock.format(event.start) : "continues";
        card.append(el("p", "cal-when", `${starts} – ${clock.format(event.end)} · ${event.venue || "Venue to be announced"}`));
        card.append(el("p", "muted", event.club_name));
        const foot = el("div", "cal-foot");
        const state = event.registration_state;
        let note = "";
        if (state === "open") note = `${event.registration_mode === "paid" ? rupees(event.price_paise) : "Free"} · ${event.places_left} of ${event.max_participants} places left`;
        else if (state === "full") note = "Fully booked";
        else if (kind(event) === "past") note = "This event is over";
        else if (state === "closed") note = "Registration closed";
        else note = "No online registration";
        foot.append(el("span", "cal-note", note));
        const link = el("a", "button small" + (state === "open" ? "" : " secondary"),
          state === "open" ? (event.registration_mode === "paid" ? "Book a place" : "Register") : "Details");
        link.href = "/e/" + event.id;
        foot.append(link);
        card.append(foot);
        dayList.append(card);
      }
    }

    function drawMonth() {
      title.textContent = monthName.format(new Date(Date.UTC(year, month, 1)));
      grid.replaceChildren();
      const lead = new Date(Date.UTC(year, month, 1)).getUTCDay();          // blanks before the 1st
      const days = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
      for (let i = 0; i < lead; i += 1) grid.append(el("span", "cal-blank"));
      for (let d = 1; d <= days; d += 1) {
        const day = keyOf(year, month, d);
        const list = byDay.get(day) || [];
        const cell = el("button", "cal-cell" + (day === today ? " today" : "") + (day === selected ? " selected" : "")
          + (list.length ? " has-events" : ""));
        cell.type = "button";
        cell.setAttribute("role", "gridcell");
        cell.setAttribute("aria-pressed", String(day === selected));
        cell.setAttribute("aria-label", `${longDay.format(new Date(Date.UTC(year, month, d)))}, ${list.length} event${list.length === 1 ? "" : "s"}`);
        cell.append(el("span", "cal-num", String(d)));
        const chips = el("span", "cal-chips");
        for (const event of list.slice(0, 3)) {
          const starts = key(event.start) === day ? clock.format(event.start).replace(":00", "").replace(" ", "") + " " : "";
          chips.append(el("span", "cal-chip " + kind(event), starts + event.name));
        }
        if (list.length > 3) chips.append(el("span", "cal-more", `+${list.length - 3} more`));
        cell.append(chips);
        // Narrow screens have no room for names: one dot per event instead.
        const dots = el("span", "cal-dots");
        for (const event of list.slice(0, 4)) dots.append(el("i", kind(event)));
        cell.append(dots);
        cell.addEventListener("click", () => { selected = day; drawMonth(); drawDay(); });
        grid.append(cell);
      }
    }

    root.querySelector("[data-cal=prev]").addEventListener("click", () => { month -= 1; if (month < 0) { month = 11; year -= 1; } drawMonth(); });
    root.querySelector("[data-cal=next]").addEventListener("click", () => { month += 1; if (month > 11) { month = 0; year += 1; } drawMonth(); });
    root.querySelector("[data-cal=today]").addEventListener("click", () => { year = ty; month = tm - 1; selected = today; drawMonth(); drawDay(); });
    drawMonth();
    drawDay();
  });
})();
