import { api, ApiError, type EventInfo, type EventInput, type Id, type ImportJob,
  type ImportMode, type User, type Zone } from "./api";
import { label, money } from "./format";
import { button, el, errorText, field, formValue, heading, link, notice, panel, reportButton, salesReport, select, show, sleep } from "./view";

const eventFields = ["title", "description", "category", "venue", "sales_start_at", "sales_end_at"] as const;
const timeFields = new Set<string>(["sales_start_at", "sales_end_at"]);

function localTime(value: string): string {
  const time = new Date(value);
  return Number.isFinite(time.valueOf())
    ? new Date(time.valueOf() - time.getTimezoneOffset() * 60_000).toISOString().slice(0, 19) : "";
}

function eventForm(current?: EventInfo) {
  const fields = [
    field("Event title", el("input", { name: "title", required: true })),
    field("Category", el("input", { name: "category", required: true })),
    field("Venue", el("input", { name: "venue", required: true })),
    field("Description", el("textarea", { name: "description", rows: 3 })),
    field("Sales start (local time)", el("input", { type: "datetime-local", name: "sales_start_at", step: 1, required: true })),
    field("Sales end (local time)", el("input", { type: "datetime-local", name: "sales_end_at", step: 1, required: true })),
  ];
  const form = el("form", { class: "edit-form" }, el("div", { class: "form-grid" }, ...fields));
  if (current) {
    for (const name of eventFields) {
      const input = form.elements.namedItem(name) as HTMLInputElement | HTMLTextAreaElement;
      input.value = timeFields.has(name) ? localTime(current[name]) : current[name] ?? "";
      if (timeFields.has(name) && current.status === "published") input.disabled = true;
    }
  }
  return form;
}

function eventDetails(form: HTMLFormElement): Omit<EventInput, "sales_start_at" | "sales_end_at"> {
  return {
    title: formValue(form, "title"), description: formValue(form, "description"),
    category: formValue(form, "category"), venue: formValue(form, "venue"),
  };
}

function eventData(form: HTMLFormElement): EventInput {
  const start = new Date(formValue(form, "sales_start_at"));
  const end = new Date(formValue(form, "sales_end_at"));
  if (end <= start) throw new RangeError("Sales end must be later than sales start.");
  return {
    ...eventDetails(form),
    sales_start_at: start.toISOString(), sales_end_at: end.toISOString(),
  };
}

function createForm(signal: AbortSignal): HTMLElement {
  const form = eventForm();
  const zones = el("div", { class: "stack" });
  const add = button("Add zone", "secondary");
  const submit = button("Create draft", "primary", "submit");
  const feedback = notice();

  function appendZone(): void {
    const code = el("input", { "aria-label": "Zone code", placeholder: "A", required: true });
    const name = el("input", { "aria-label": "Zone name", placeholder: "Main Floor", required: true });
    const price = el("input", { type: "number", min: 0, step: 1, "aria-label": "Price in TWD", placeholder: "1200", required: true });
    const remove = button("Remove zone", "quiet");
    const row = el("div", { class: "zone-row" }, field("Code", code), field("Name", name), field("Price (TWD)", price), remove);
    remove.onclick = () => {
      row.remove();
      const first = zones.querySelector<HTMLButtonElement>("button");
      if (first && zones.childElementCount === 1) first.disabled = true;
      add.focus();
    };
    zones.append(row);
    for (const control of zones.querySelectorAll("button")) control.disabled = zones.childElementCount === 1;
    if (zones.childElementCount > 1) code.focus();
  }

  appendZone();
  add.onclick = appendZone;
  form.append(el("fieldset", {}, el("legend", {}, "Ticket zones"), zones, add), submit, feedback);
  form.onsubmit = async event => {
    event.preventDefault();
    submit.disabled = true;
    show(feedback, "Creating draft…");
    try {
      const entries: Zone[] = Array.from(zones.children, zone => {
        const [code, name, price] = zone.querySelectorAll("input");
        return { code: code!.value.trim(), name: name!.value.trim(), price_twd: Number(price!.value) };
      });
      const created = await api.createEvent({ ...eventData(form), zones: entries }, signal);
      if (!signal.aborted) location.hash = `#/organizer/${encodeURIComponent(created.id)}`;
    } catch (error) {
      show(feedback, error instanceof RangeError ? error.message : errorText(error), true);
    } finally {
      submit.disabled = false;
    }
  };
  return panel(el("h2", {}, "Create an event"), form);
}

export function organizerPage(root: HTMLElement, user: User, signal: AbortSignal): void {
  const results = el("ul", { class: "record-list" });
  const feedback = notice();
  const more = button("Load more", "secondary");
  root.append(heading("Organizer", "Create events, import seats and publish ticket sales."),
    panel(el("h2", {}, user.role === "admin" ? "Manage events" : "Your events"), results, feedback, more), createForm(signal));
  let cursor: string | null = null;
  let visible = 0;
  let restart = false;

  async function load(): Promise<void> {
    if (restart) {
      results.replaceChildren();
      cursor = null;
      visible = 0;
      restart = false;
    }
    more.disabled = true;
    show(feedback, "Loading events…");
    try {
      const page = await api.events({ limit: 40, cursor }, signal);
      if (signal.aborted) return;
      for (const event of page.items) {
        if (event.unavailable || (user.role !== "admin" && event.owner_id !== undefined
          && String(event.owner_id) !== String(user.id))) continue;
        results.append(el("li", {}, link(event.title, `#/organizer/${encodeURIComponent(event.id)}`),
          el("span", { class: "muted" }, label(event.status))));
        visible++;
      }
      cursor = page.next_cursor;
      more.hidden = cursor === null;
      more.textContent = "Load more";
      show(feedback, !visible && !cursor ? "No events yet." : "");
    } catch (error) {
      restart = error instanceof ApiError && (error.status === 410 || error.code === "cursor-invalidated");
      more.textContent = restart ? "Refresh events" : "Retry loading events";
      show(feedback, errorText(error), true);
    } finally {
      more.disabled = false;
    }
  }
  more.onclick = () => void load();
  void load();
}

function importPanel(event: EventInfo, user: User, signal: AbortSignal): HTMLElement {
  const feedback = notice();
  const details = el("p", { class: "muted" });
  const progress = el("progress", { max: 100, "aria-label": "Seat import progress", hidden: true });
  const errors = el("div");
  const upload = button("Upload seats", "primary", "submit");
  const file = el("input", { type: "file", name: "file", accept: ".csv,.json,text/csv,application/json", required: true });
  const mode = select([["all_or_nothing", "All rows must be valid"], ["skip_invalid", "Skip invalid rows"]]);
  const form = el("form", { class: "edit-form" }, field("CSV or JSON file", file), field("Validation mode", mode), upload);
  const storage = `ticketing:import:${user.id}:${event.id}`;
  let active = false;

  async function watch(jobId: string, initial?: ImportJob): Promise<void> {
    if (active) return;
    active = true;
    upload.disabled = true;
    errors.replaceChildren();
    progress.hidden = false;
    progress.removeAttribute("value");
    let job = initial;
    let delay = 500;
    try {
      while (!signal.aborted) {
        try {
          job ??= await api.importJob(jobId, signal);
          if (signal.aborted) return;
          const processed = job.processed_rows ?? 0;
          const total = job.total_rows ?? null;
          const percent = total && total > 0 ? processed / total * 100
            : typeof job.progress === "number" ? job.progress * (job.progress <= 1 ? 100 : 1) : undefined;
          if (percent !== undefined) progress.value = Math.max(0, Math.min(100, percent));
          else progress.removeAttribute("value");
          const invalid = job.error_rows ?? 0;
          details.textContent = `${processed.toLocaleString()}${total === null ? "" : ` of ${total.toLocaleString()}`} processed · ${job.valid_rows ?? "—"} valid · ${invalid} errors`;
          const working = job.status === "queued" || job.status === "running";
          const failed = /failed|cancelled/.test(job.status);
          show(feedback, `${label(job.status)}${job.failure_reason ? ` · ${label(job.failure_reason)}` : ""}`, failed);
          if (!working) {
            if (!failed) progress.value = 100;
            if (invalid || job.status === "validation_failed") {
              errors.append(reportButton(() => api.importErrors(jobId, signal), "Download errors CSV", feedback));
            }
            return;
          }
          delay = 650;
          job = undefined;
        } catch (error) {
          if (signal.aborted) return;
          if (error instanceof ApiError && [403, 404].includes(error.status)) {
            sessionStorage.removeItem(storage);
            show(feedback, errorText(error), true);
            return;
          }
          show(feedback, "Import status is temporarily unavailable. Reconnecting…", true);
          delay = Math.min(delay * 2, 5000);
        }
        await sleep(delay, signal);
      }
    } finally {
      active = false;
      upload.disabled = false;
    }
  }

  form.onsubmit = async action => {
    action.preventDefault();
    const chosen = file.files?.[0];
    if (!chosen || active) return;
    if (chosen.size > 20 * 1024 * 1024) {
      show(feedback, "Choose a file up to 20 MB.", true);
      return;
    }
    upload.disabled = true;
    show(feedback, "Uploading seats…");
    try {
      const job = await api.upload(event.id, chosen, mode.value as ImportMode, signal);
      if (!signal.aborted) {
        sessionStorage.setItem(storage, job.id);
        void watch(job.id, job);
      }
    } catch (error) {
      show(feedback, errorText(error), true);
    } finally {
      upload.disabled = active;
    }
  };
  const prior = sessionStorage.getItem(storage);
  if (prior) void watch(prior);
  return panel(el("h2", {}, "Import seats"), el("p", { class: "muted" },
    "Up to 20 MB and 100,000 rows. Include zone_code, row_label and seat_number."),
    event.status === "draft" ? form : undefined, progress, details, feedback, errors);
}

export async function managePage(root: HTMLElement, id: Id, user: User, signal: AbortSignal): Promise<void> {
  const { data: event, etag } = await api.eventRevision(id, signal);
  if (signal.aborted) return;
  if (user.role !== "admin" && event.owner_id !== undefined && String(event.owner_id) !== String(user.id)) {
    root.append(heading("Manage event"), panel(el("p", {}, "You can manage events you created.")));
    return;
  }

  const feedback = notice();
  const form = eventForm(event);
  const save = button("Save changes", "primary", "submit");
  const publish = button("Publish event", "secondary");
  const remove = button("Delete event", "danger");
  const reload = button("Reload saved version", "secondary");
  const report = salesReport(id);
  reload.hidden = true;
  form.append(el("div", { class: "actions" }, save, event.status === "draft" ? publish : undefined, reload), feedback);
  const detail = panel(el("div", { class: "split" }, el("h2", {}, "Event details"), el("span", { class: "tag" }, label(event.status))),
    form, el("hr"), el("div", { class: "actions" }, report, remove));
  const pricing = el("p", { class: "muted", hidden: true });
  detail.append(pricing);
  function showZones(zones: Zone[]): void {
    if (signal.aborted || !zones.length) return;
    pricing.textContent = zones.map(zone => `${zone.name} · ${money(zone.price_twd)}`).join(" / ");
    pricing.hidden = false;
  }
  if (event.zones) showZones(event.zones);
  else void api.zones(id, signal).then(showZones).catch(() => undefined);
  root.append(heading(event.title, "Manage event"), el("div", { class: "actions page-actions" },
    link("All organizer events", "#/organizer"), link("View seat map", `#/events/${encodeURIComponent(id)}`)),
  el("div", { class: "organizer-layout" }, detail, importPanel(event, user, signal)));

  let busy = false;
  let changed = false;
  let conflict = false;
  function updateButtons(): void {
    save.disabled = busy || conflict;
    publish.disabled = busy || conflict || changed;
    remove.disabled = busy;
    reload.hidden = !conflict;
  }
  async function action(work: () => Promise<unknown>, next?: string): Promise<void> {
    if (busy) return;
    busy = true;
    updateButtons();
    show(feedback, "Saving…");
    try {
      await work();
      if (signal.aborted) return;
      if (next) location.hash = next;
      else {
        root.dispatchEvent(new CustomEvent("refresh-route", { bubbles: true }));
      }
    } catch (error) {
      conflict = error instanceof ApiError && error.status === 412;
      show(feedback, error instanceof RangeError ? error.message : errorText(error), true);
    } finally {
      busy = false;
      updateButtons();
    }
  }

  form.oninput = () => {
    changed = true;
    updateButtons();
    if (!conflict && event.status === "draft") show(feedback, "Save changes before publishing.");
  };
  form.onsubmit = submit => {
    submit.preventDefault();
    void action(() => api.updateEvent(id, event.status === "published" ? eventDetails(form) : eventData(form), etag, signal));
  };
  publish.onclick = () => void action(() => api.publish(id, etag, signal));
  reload.onclick = () => {
    if (!changed || window.confirm("Discard unsaved edits and load the saved version?")) {
      root.dispatchEvent(new CustomEvent("refresh-route", { bubbles: true }));
    }
  };
  remove.onclick = () => {
    if (window.confirm(`Delete “${event.title}”?`)) {
      void action(() => api.deleteEvent(id, signal), "#/organizer");
    }
  };
}
