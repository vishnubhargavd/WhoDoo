// The door: scan tickets, check people in from the guest list, and keep the count in step.
// Uses api() and showMessage() from app.js.
//
// Reading the QR code: the browser's own BarcodeDetector where it exists (Chrome, Edge, Android),
// otherwise the ZXing library, loaded only then (Safari, Firefox). Either way a frame is read a
// few times a second and a code is sent once, however long it is held in front of the camera.

(() => {
  const door = document.querySelector("[data-door]");
  if (!door) return;
  const eventId = door.dataset.door;
  const $ = (id) => document.getElementById(id);
  const video = $("scan-video"), start = $("scan-start"), stop = $("scan-stop"), note = $("scan-note");
  const result = $("scan-result"), frame = $("scan-frame");
  const counted = $("door-count"), total = $("door-total"), bar = $("door-bar");

  const OUTCOMES = {
    redeemed: ["ok", "Checked in"],
    already_checked_in: ["warn", "Already checked in"],
    wrong_event: ["bad", "Ticket is for a different event"],
    revoked: ["bad", "Ticket is cancelled"],
    expired: ["bad", "This event is not admitting"],
    unrecognized: ["bad", "Not a ticket we issued"],
    invalid_credential: ["bad", "Not a WhoDoo ticket code"],
  };

  // --- Feedback -------------------------------------------------------------------------

  let audio = null;
  function tone(kind) {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const osc = audio.createOscillator(), gain = audio.createGain();
      osc.frequency.value = kind === "ok" ? 880 : kind === "warn" ? 440 : 220;
      gain.gain.setValueAtTime(0.15, audio.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + (kind === "ok" ? 0.15 : 0.4));
      osc.connect(gain).connect(audio.destination);
      osc.start();
      osc.stop(audio.currentTime + (kind === "ok" ? 0.15 : 0.4));
    } catch (error) { /* no sound is fine */ }
    if (navigator.vibrate) navigator.vibrate(kind === "ok" ? 80 : [120, 60, 120]);
  }

  function setCount(inside) {
    counted.textContent = String(inside);
    const all = Number(total.textContent) || 0;
    bar.style.width = all ? `${Math.round((inside * 100) / all)}%` : "0%";
  }

  let clearTimer = null;
  function show(outcome) {
    const [kind, text] = OUTCOMES[outcome.status] || ["bad", "Could not check this ticket"];
    result.className = "scan-result " + kind;
    result.hidden = false;
    result.querySelector("strong").textContent = text;
    let who = "";
    if (outcome.holderName && outcome.studentId) who = `${outcome.holderName} · student ID ${outcome.studentId} · ${outcome.participantCode}`;
    else if (outcome.holderName) who = `${outcome.holderName} · ${outcome.categoryName} · seat ${outcome.seatNumber} of ${outcome.partySize}`;
    if (outcome.status === "already_checked_in") who += ` · at ${new Date(outcome.checkedInAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
    result.querySelector("span").textContent = who;
    tone(kind);
    if (outcome.status === "redeemed") markArrived(outcome.redemptionId, outcome.checkedInAt);
    clearTimeout(clearTimer);
    clearTimer = setTimeout(() => { result.hidden = true; }, 6000);   // ready for the next person
  }

  // --- The guest list -------------------------------------------------------------------

  const rows = [...document.querySelectorAll("[data-guest]")];
  const search = $("guest-search"), empty = $("guest-none");
  let filter = "all";

  function markArrived(id, when) {
    const row = rows.find((candidate) => candidate.dataset.guest === id);
    if (!row || row.dataset.status === "checked_in") return;
    row.dataset.status = "checked_in";
    const badge = row.querySelector(".badge");
    badge.textContent = "Checked in";
    badge.className = "badge paid";
    row.querySelector(".guest-when").textContent = new Date(when).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    row.querySelector("button")?.remove();
    setCount(rows.filter((candidate) => candidate.dataset.status === "checked_in").length);
    applyFilter();
  }

  function applyFilter() {
    const words = (search?.value || "").trim().toLowerCase();
    let shown = 0;
    for (const row of rows) {
      const matches = (!words || row.dataset.search.includes(words))
        && (filter === "all" || (filter === "in") === (row.dataset.status === "checked_in"));
      row.hidden = !matches;
      if (!matches && openBox && openBox.button.closest("[data-guest]") === row) closeConfirm();
      if (matches) shown += 1;
    }
    if (empty) empty.hidden = shown > 0 || rows.length === 0;
  }
  search?.addEventListener("input", applyFilter);
  document.querySelectorAll("[data-guest-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      filter = button.dataset.guestFilter;
      document.querySelectorAll("[data-guest-filter]").forEach((other) => other.setAttribute("aria-pressed", String(other === button)));
      applyFilter();
    });
  });

  // Checking someone in by name asks first, in a box under their row (never a browser pop-up):
  // with no code to prove who they are, staff confirm against the student ID, and are warned
  // when someone else on the list has the same name.
  let openBox = null;
  function closeConfirm() {
    if (!openBox) return;
    openBox.row.remove();
    openBox.button.disabled = false;
    openBox = null;
  }

  function askConfirm(button) {
    closeConfirm();
    const row = button.closest("[data-guest]");
    const name = row.dataset.name;
    const twins = rows.filter((other) => other.dataset.name.trim().toLowerCase() === name.trim().toLowerCase()).length;

    const holder = document.createElement("tr");
    holder.className = "guest-confirm";
    const cell = document.createElement("td");
    cell.colSpan = row.children.length;
    const box = document.createElement("div");
    box.className = "confirm-box";
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", `Confirm check-in for ${name}`);
    const add = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; node.textContent = text; box.append(node); return node; };
    add("h3", "", `Check in ${name}?`);
    add("p", "confirm-who", row.dataset.detail);
    if (twins > 1) add("p", "confirm-warn", `${twins} people on this list are called ${name}. Go by the student ID, not the name.`);
    add("p", "", "Check this against their college ID. It cannot be undone from here.");
    const actions = document.createElement("div");
    actions.className = "actions";
    const yes = document.createElement("button"); yes.type = "button"; yes.textContent = "Yes, check in";
    const no = document.createElement("button"); no.type = "button"; no.className = "secondary"; no.textContent = "Cancel";
    actions.append(yes, no);
    box.append(actions);
    cell.append(box);
    holder.append(cell);
    row.after(holder);
    button.disabled = true;
    openBox = { row: holder, button };
    yes.focus();

    no.addEventListener("click", () => { closeConfirm(); button.focus(); });
    yes.addEventListener("click", async () => {
      yes.disabled = no.disabled = true;
      const who = button.dataset.admitRegistration ? { registration_id: button.dataset.admitRegistration } : { ticket_id: button.dataset.admit };
      try {
        const outcome = await api("POST", "/api/v1/check-in/manual", { event_id: eventId, ...who });
        closeConfirm();
        show(outcome);
      } catch (error) {
        showMessage(error.message);
        yes.disabled = no.disabled = false;
      }
    });
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-admit], [data-admit-registration]");
    if (button) askConfirm(button);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && openBox) { const button = openBox.button; closeConfirm(); button.focus(); }
  });

  // --- Sending a code -------------------------------------------------------------------

  let busy = false;
  async function submit(value) {
    if (busy) return;
    busy = true;
    try { show(await api("POST", "/api/v1/check-in/scan", { event_id: eventId, credential: value })); }
    catch (error) { showMessage(error.message); }
    busy = false;
  }

  $("scan-manual").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = event.target.querySelector("input");
    if (field.value.trim()) submit(field.value.trim());
    field.value = "";
    field.focus();
  });

  // --- The camera -----------------------------------------------------------------------

  // Browsers hand the camera only to pages on https:// or on localhost.
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    start.hidden = true;
    note.textContent = window.isSecureContext
      ? "This browser cannot use the camera here. Type or paste the code below, use a handheld scanner, or check people in from the guest list."
      : "The camera only works on a secure (https) address or on this computer itself. Here, type or paste the code below, use a handheld scanner, or check people in from the guest list.";
    return;
  }

  function loadZxing() {
    return new Promise((resolve, reject) => {
      if (window.ZXing) { resolve(); return; }
      const script = document.createElement("script");
      script.src = "/static/vendor/zxing.min.js";
      script.onload = resolve;
      script.onerror = () => reject(new Error("The scanner could not be loaded."));
      document.head.append(script);
    });
  }

  // Returns a function that reads one frame and resolves to the code's text, or "".
  async function makeReader() {
    if ("BarcodeDetector" in window) {
      try {
        if ((await BarcodeDetector.getSupportedFormats()).includes("qr_code")) {
          const detector = new BarcodeDetector({ formats: ["qr_code"] });
          return async () => { const found = await detector.detect(video); return found.length ? found[0].rawValue : ""; };
        }
      } catch (error) { /* fall through to ZXing */ }
    }
    await loadZxing();
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { willReadFrequently: true });
    const reader = new ZXing.QRCodeReader();
    return async () => {
      if (!video.videoWidth) return "";
      // A centred square is enough and keeps each read quick on a phone.
      const side = Math.min(video.videoWidth, video.videoHeight, 720);
      canvas.width = canvas.height = side;
      context.drawImage(video, (video.videoWidth - side) / 2, (video.videoHeight - side) / 2, side, side, 0, 0, side, side);
      try {
        const bitmap = new ZXing.BinaryBitmap(new ZXing.HybridBinarizer(new ZXing.HTMLCanvasElementLuminanceSource(canvas)));
        return reader.decode(bitmap).getText();
      } catch (error) { return ""; }          // no code in this frame
    };
  }

  let stream = null, last = "", lastAt = 0;
  async function scanLoop(read) {
    if (!stream) return;
    try {
      const value = await read();
      // The same code held in front of the camera is sent once, not on every frame.
      if (value && (value !== last || Date.now() - lastAt > 5000)) {
        last = value; lastAt = Date.now();
        await submit(value);
      }
    } catch (error) { /* a frame that could not be read; try the next */ }
    setTimeout(() => scanLoop(read), 200);
  }

  function halt() {
    stream?.getTracks().forEach((track) => track.stop());
    stream = null;
    video.srcObject = null;
    frame.hidden = true; stop.hidden = true; start.hidden = false;
    note.textContent = "Camera off. Start it again to keep scanning.";
  }

  start.addEventListener("click", async () => {
    start.disabled = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
      video.srcObject = stream;
      await video.play();
      frame.hidden = false; stop.hidden = false; start.hidden = true;
      note.textContent = "Hold the QR code inside the square.";
      scanLoop(await makeReader());
    } catch (error) {
      halt();
      note.textContent = error.name === "NotAllowedError"
        ? "Camera access was refused. Allow it in the browser's site settings, or type the code below."
        : "The camera could not be started. Type the code below, or check people in from the guest list.";
    }
    start.disabled = false;
  });
  stop.addEventListener("click", halt);
  document.addEventListener("visibilitychange", () => { if (document.hidden && stream) halt(); });   // do not keep the camera on in the background
})();
