import { api, ApiError, seatStream, type Booking, type EventInfo, type Id, type Snapshot, type User } from "./api";
import { date, label, money } from "./format";
import { IntentUnavailable, MutationIntent, retryDelay, retryLabel } from "./intent";
import { SeatReconnect } from "./reconnect";
import { SeatCanvas } from "./seat-canvas";
import { AVAILABLE, SeatModel } from "./seat-model";
import { button, el, errorText, field, formValue, heading, interval, link,
  notice, panel, salesReport, select, show, sleep } from "./view";

export function eventsPage(root: HTMLElement, signal: AbortSignal): void {
  const cards = el("div", { class: "event-grid" });
  const status = notice();
  const more = button("Load more events", "secondary");
  const tail = el("div", { class: "list-tail" }, more, status);
  const search = el("form", { class: "filters" },
    field("Search", el("input", { type: "search", name: "q", placeholder: "Event title" })),
    field("Category", el("input", { name: "category", placeholder: "Any category" })),
    field("Sort", el("select", { name: "sort" }, el("option", { value: "" }, "Default order"),
      el("option", { value: "sales_start_at" }, "Earliest sale"),
      el("option", { value: "-created" }, "Recently added"), el("option", { value: "price_min" }, "Lowest price"))),
    button("Apply filters", "secondary", "submit"),
  );
  root.append(heading("Events", "Browse events and find available tickets."), panel(search), cards, tail);
  let cursor: string | null = null;
  let done = false;
  let busy = false;
  let failed = false;
  let restart = false;
  let count = 0;
  let generation = 0;
  let filters = { q: "", category: "", sort: "" };

  const observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting) && !failed) void load();
  }, { rootMargin: "160px" });
  signal.addEventListener("abort", () => observer.disconnect(), { once: true });

  function reset(): void {
    generation++;
    cards.replaceChildren();
    count = 0;
    cursor = null;
    done = false;
    busy = false;
    restart = false;
    void load();
  }

  async function load(): Promise<void> {
    if (signal.aborted || busy || done) return;
    const run = generation;
    busy = true;
    failed = false;
    observer.unobserve(more);
    more.disabled = true;
    more.hidden = false;
    more.textContent = "Load more events";
    show(status, "Loading events…");
    try {
      const page = await api.events({ limit: 24, cursor, ...filters }, signal);
      if (signal.aborted || run !== generation) return;
      for (const event of page.items) {
        if (event.unavailable) {
          cards.append(el("article", { class: "event-card unavailable" }, el("h2", {}, "Event unavailable")));
        } else {
          cards.append(el("article", { class: "event-card" },
            el("div", { class: "split" }, el("span", { class: "tag" }, event.category),
              el("span", { class: "muted" }, label(event.status))),
            el("h2", {}, link(event.title, `#/events/${encodeURIComponent(event.id)}`)),
            el("p", {}, event.venue), el("p", { class: "muted" }, `Sale opens ${date(event.sales_start_at)}`)));
        }
        count++;
      }
      cursor = page.next_cursor;
      done = cursor === null;
      show(status, count === 0 ? "No events match these filters." : "");
    } catch (error) {
      if (signal.aborted || run !== generation) return;
      show(status, errorText(error), true);
      failed = true;
      restart = error instanceof ApiError && (error.status === 410 || error.code === "cursor-invalidated");
      more.textContent = restart ? "Refresh results" : "Retry loading events";
    } finally {
      if (!signal.aborted && run === generation) {
        busy = false;
        more.disabled = false;
        more.hidden = done;
        if (!done && !failed) observer.observe(more);
      }
    }
  }

  more.onclick = () => restart ? reset() : void load();
  search.onsubmit = event => {
    event.preventDefault();
    filters = { q: formValue(search, "q"), category: formValue(search, "category"), sort: formValue(search, "sort") };
    reset();
  };
  void load();
}

function saleOpen(event: EventInfo): boolean {
  const now = Date.now();
  return event.status === "published" && now >= Date.parse(event.sales_start_at) && now < Date.parse(event.sales_end_at);
}

export async function eventPage(root: HTMLElement, id: Id, user: User, signal: AbortSignal): Promise<void> {
  const event = await api.event(id, signal);
  if (signal.aborted) return;
  const manage = user.role === "admin" || (user.role === "user" && String(event.owner_id ?? "") === String(user.id));
  const buyer = user.role !== "viewer";
  const bookingStatus = notice();
  const stale = el("p", { class: "stale", role: "status", hidden: true },
    "Seat data may be stale. The server checks availability when you book.");
  const connection = el("span", { class: "connection" }, "Connecting");
  const inventory = el("p", { class: "muted" }, "Loading seats…");
  const seatArea = el("div");
  const controls = el("aside", { class: "panel booking-panel" });
  const selection = el("ul", { class: "chosen" });
  const summary = el("p", { class: "muted" });
  const manual = button("Hold selected seats");
  const zone = select([]);
  const quantity = select([["1", "1 ticket"], ["2", "2 tickets"], ["3", "3 tickets"], ["4", "4 tickets"]]);
  const auto = button("Find seats", "secondary", "submit");
  const retry = button("Retry last request", "secondary");
  const expired = button("Clear expired request", "quiet");
  const pendingText = el("p");
  const pending = el("div", { class: "pending-confirmation", hidden: true },
    pendingText, retry, expired, link("My orders", "#/orders", "text-link"));
  const allocation = el("form", { class: "allocation" }, field("Zone", zone), field("Quantity", quantity), auto);
  if (buyer) controls.append(el("h2", {}, "Tickets"), summary, selection, manual, el("hr"),
    el("h3", {}, "Automatic allocation"), allocation, pending, bookingStatus);
  else controls.append(el("h2", {}, "Zones and prices"), summary, selection);
  if (manage || user.role === "viewer") {
    controls.append(el("hr"), salesReport(id));
  }
  if (manage) controls.append(link("Manage event", `#/organizer/${encodeURIComponent(id)}`, "text-link"));
  const legend = el("ul", { class: "legend", "aria-label": "Seat status legend" },
    ...["Available", "Held", "Sold", ...(buyer ? ["Selected"] : [])].map(state =>
      el("li", {}, el("span", { class: `swatch ${state.toLowerCase()}`, "aria-hidden": true }), state)));
  const map = panel(el("div", { class: "split map-title" }, el("h2", {}, "Seat map"), connection),
    inventory, stale, legend, seatArea);
  root.append(heading(event.title, event.venue), el("div", { class: "event-meta" },
    el("span", { class: "tag" }, event.category), el("span", {}, label(event.status)),
    el("span", {}, `Sale ${date(event.sales_start_at)} – ${date(event.sales_end_at)}`)),
  event.description ? el("p", { class: "description" }, event.description) : el("div"),
  el("div", { class: "seat-layout" }, map, controls));

  let model: SeatModel | undefined;
  let canvas: SeatCanvas | undefined;
  let snapshot: Snapshot | undefined;
  const selected = new Set<number>();
  const intent = new MutationIntent<Booking>(user.id, { operation: "booking", event: id });
  let booking = false;
  let lastFrame = performance.now();
  let lastHead = 0;
  let lagSince: number | undefined;
  let outboxAge = 0;
  let disconnected = false;
  let received = false;
  let unavailable = false;

  function renderBooking(): void {
    const open = saleOpen(event);
    summary.textContent = buyer
      ? open ? `${selected.size} of 4 seats selected` : "Ticket sales are currently closed."
      : "Browse the seating plan and published zone prices.";
    const waiting = Boolean(intent.current);
    const elapsed = intent.expired;
    const seconds = intent.seconds;
    manual.disabled = !open || !selected.size || booking || waiting;
    auto.disabled = !open || !snapshot?.zones.length || booking || waiting;
    zone.disabled = quantity.disabled = booking || waiting;
    pending.hidden = !waiting;
    expired.hidden = !elapsed;
    expired.disabled = booking;
    retry.hidden = elapsed;
    retry.disabled = booking || seconds > 0;
    retry.textContent = retryLabel("Retry last request", seconds);
    pendingText.textContent = elapsed
      ? "Pending confirmation. The retry window has ended. Check My orders before clearing this request."
      : "Pending confirmation. Retry to recover the result of the original booking.";
    const now = performance.now();
    const late = now - lastFrame >= 5000 || outboxAge > 2000
      || (lagSince !== undefined && now - lagSince >= 3000);
    const text = unavailable ? "Unavailable" : disconnected ? "Reconnecting" : late ? "Stale" : received ? "Live" : "Connecting";
    if (connection.textContent !== text) connection.textContent = text;
    stale.hidden = !(disconnected || late);
  }

  function renderSelection(): void {
    if (!buyer) {
      selection.replaceChildren(...(snapshot?.zones ?? []).map(item =>
        el("li", {}, `${item.name} · ${money(item.price_twd)}`)));
      return;
    }
    const active = document.activeElement instanceof HTMLElement ? document.activeElement.dataset.seatPosition : undefined;
    selection.replaceChildren(...[...selected].map(position => {
      const seat = model!.seats[position]!;
      const name = `${seat.zone} · Row ${seat.row}, seat ${seat.number}`;
      const remove = button("Remove", "quiet");
      remove.dataset.seatPosition = String(position);
      remove.setAttribute("aria-label", `Remove ${name}`);
      remove.onclick = () => {
        toggle(position);
        (selection.querySelector("button") ?? seatArea.querySelector("canvas"))?.focus();
      };
      const price = snapshot?.zones.find(item => item.code === seat.zone)?.price_twd;
      return el("li", {}, el("span", {}, name, price === undefined ? "" : ` · ${money(price)}`), remove);
    }));
    if (active !== undefined) {
      (selection.querySelector<HTMLElement>(`[data-seat-position="${active}"]`)
        ?? selection.querySelector("button") ?? seatArea.querySelector("canvas"))?.focus();
    }
    renderBooking();
  }

  function toggle(position: number): void {
    if (selected.delete(position)) canvas?.select(position, false);
    else if (!saleOpen(event)) show(bookingStatus, "Ticket sales are currently closed.", true);
    else if (model?.states[position] !== AVAILABLE) show(bookingStatus, "Choose a seat marked available.", true);
    else if (selected.size >= 4) show(bookingStatus, "Remove a seat before selecting more than four.", true);
    else {
      selected.add(position);
      canvas?.select(position, true);
      if (!intent.current) show(bookingStatus, "");
    }
    renderSelection();
  }

  function setSnapshot(newSnapshot: Snapshot): void {
    const prior = new Set([...selected].map(position => model?.seats[position]?.id));
    const previousZone = zone.value;
    snapshot = newSnapshot;
    model = new SeatModel(newSnapshot.seats, newSnapshot.last_seq, model);
    lastHead = model.applied;
    lagSince = undefined;
    outboxAge = 0;
    selected.clear();
    const restoreFocus = document.activeElement === seatArea.querySelector("canvas");
    canvas?.destroy();
    canvas = newSnapshot.seats.length ? new SeatCanvas(model, toggle, buyer) : undefined;
    seatArea.replaceChildren(...(canvas ? [canvas.viewport, canvas.announcement,
      el("p", { class: "map-help" }, "Scroll to explore. Use arrow keys to move between seats.",
        buyer ? " Press Enter or Space to select." : "")]
      : [el("p", { class: "muted" }, "No seats have been added to this layout.")]));
    if (restoreFocus) seatArea.querySelector("canvas")?.focus();
    newSnapshot.seats.forEach((seat, position) => {
      if (prior.has(seat.id) && model?.states[position] === AVAILABLE) {
        selected.add(position);
        canvas?.select(position, true);
      }
    });
    zone.replaceChildren(...newSnapshot.zones.map(item =>
      el("option", { value: item.code }, `${item.name} · ${money(item.price_twd)}`)));
    if (newSnapshot.zones.some(item => item.code === previousZone)) zone.value = previousZone;
    inventory.textContent = `${newSnapshot.seats.length.toLocaleString()} seats across ${newSnapshot.zones.length} zones`;
    renderSelection();
    renderBooking();
  }

  function trackLag(): void {
    if (model && lastHead > model.applied) lagSince ??= performance.now();
    else lagSince = undefined;
  }

  async function subscribe(): Promise<void> {
    let snapshotNeeded = true;
    let minLayout: number | undefined;
    const reconnect = new SeatReconnect();
    while (!signal.aborted) {
      let retryAfter = 0;
      try {
        if (snapshotNeeded) {
          const next = await api.seats(id, minLayout, signal);
          if (signal.aborted) return;
          if (next.layout_ver < (minLayout ?? 0) || next.last_seq < (model?.applied ?? 0)) {
            await sleep(reconnect.delay(), signal);
            continue;
          }
          setSnapshot(next);
          snapshotNeeded = false;
          minLayout = undefined;
        }
        if (!model) return;
        const connectionAbort = new AbortController();
        let seen = performance.now();
        const watchdog = setInterval(() => {
          const now = performance.now();
          if (now - seen >= 20_000 || (lagSince !== undefined && now - lagSince >= 10_000)) {
            connectionAbort.abort();
          }
        }, 1000);
        try {
          const streamSignal = AbortSignal.any([signal, connectionAbort.signal]);
          for await (const frame of seatStream(id, model.applied, streamSignal, () => { seen = performance.now(); })) {
            if (signal.aborted) return;
            seen = performance.now();
            if (frame.event === "heartbeat") continue;
            if (frame.event === "resync" || frame.event === "layout") {
              snapshotNeeded = true;
              minLayout = Math.max(snapshot?.layout_ver ?? 0, frame.event === "layout" ? frame.data.layout_ver : 0);
              break;
            }
            if (frame.event === "progress") {
              lastHead = frame.data.head;
              outboxAge = frame.data.oldest_unassigned_age_ms;
            }
            if (frame.event === "delta") {
              let removed = false;
              const result = model.apply(frame.data, position => {
                canvas?.mark(position);
                if (model?.states[position] !== AVAILABLE && selected.delete(position)) {
                  canvas?.select(position, false);
                  removed = true;
                }
              });
              if (result === "gap" || result === "layout-mismatch") {
                snapshotNeeded = result === "layout-mismatch";
                if (snapshotNeeded) minLayout = snapshot?.layout_ver;
                break;
              }
              canvas?.describe();
              if (removed) {
                show(bookingStatus, "Some selected seats became unavailable and were removed.", true);
                renderSelection();
              }
            }
            lastFrame = seen;
            disconnected = false;
            received = true;
            trackLag();
            if (lagSince === undefined && outboxAge <= 2000) {
              if (frame.event === "progress") reconnect.caughtUp(seen);
            } else reconnect.lagging();
            renderBooking();
          }
        } finally {
          clearInterval(watchdog);
          connectionAbort.abort();
        }
      } catch (error) {
        if (signal.aborted) return;
        if (error instanceof ApiError && [403, 404].includes(error.status)) {
          inventory.textContent = errorText(error);
          unavailable = true;
          disconnected = true;
          renderBooking();
          return;
        }
        retryAfter = retryDelay(error);
        if (!model) inventory.textContent = "Seat data is temporarily unavailable. Retrying…";
      }
      disconnected = true;
      received = false;
      renderBooking();
      lagSince = undefined;
      await sleep(Math.max(reconnect.delay(), retryAfter), signal);
    }
  }

  async function book(body?: Booking): Promise<void> {
    if (booking || signal.aborted || intent.seconds || intent.expired) return;
    if (body && (intent.current || !saleOpen(event))) return;
    booking = true;
    renderBooking();
    show(bookingStatus, "Requesting tickets…");
    try {
      const order = await intent.send(body, (saved, key) => api.book(id, saved, key, signal));
      if (!signal.aborted) location.hash = `#/orders/${encodeURIComponent(order.id)}`;
    } catch (error) {
      if (!signal.aborted) {
        show(bookingStatus, error instanceof IntentUnavailable ? error.message : errorText(error), true);
      }
    } finally {
      booking = false;
      renderBooking();
    }
  }

  manual.onclick = () => void book({ seat_ids: [...selected].map(position => model!.seats[position]!.id) });
  allocation.onsubmit = event => {
    event.preventDefault();
    void book({ zone_code: zone.value, quantity: Number(quantity.value) });
  };
  retry.onclick = () => void book();
  expired.onclick = async () => {
    if (!booking) await intent.dismissExpired();
    renderBooking();
  };
  signal.addEventListener("abort", () => canvas?.destroy(), { once: true });
  interval(renderBooking, 250, signal);
  renderBooking();
  void subscribe();
}
