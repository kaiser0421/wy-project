import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { accessStatusClasses, api, ApiError, logServices, type AdminUser, type Alert, type HttpMetrics, type Metric, type Role, type User } from "./api";
import { date, label } from "./format";
import { incomplete, metricSecond, MetricWindow, WINDOW_SECONDS, type Sample } from "./metrics";
import { onThemeChange } from "./theme";
import { button, el, errorText, field, formValue, heading, interval, link, notice, panel, select, show, sleep, table } from "./view";

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const seriesColors = ["--chart-blue", "--chart-teal", "--chart-amber"] as const;
type Reading = (sample: Metric) => number | null | undefined;
interface Series { label: string; read: Reading; color: typeof seriesColors[number]; dash?: number[] }

function chartColors() {
  const styles = getComputedStyle(document.documentElement);
  const color = (token: string) => styles.getPropertyValue(token).trim();
  return {
    axis: color("--muted"),
    grid: color("--chart-grid"),
    incomplete: color("--chart-incomplete"),
    "--chart-blue": color("--chart-blue"),
    "--chart-teal": color("--chart-teal"),
    "--chart-amber": color("--chart-amber"),
  };
}

function httpSeries(entries: (keyof Omit<HttpMetrics, "ts">)[], labels: string[], multiplier = 1): Series[] {
  return entries.flatMap((key, index) => (["edge", "app"] as const).map(source => ({
    label: `${source === "edge" ? "Edge" : "App"}${labels[index] ? ` ${labels[index]}` : ""}`,
    color: seriesColors[index]!, dash: source === "app" ? [5, 4] : undefined,
    read: (sample: Metric) => {
      const value = sample[source]?.[key];
      return value == null ? null : value * multiplier;
    },
  })));
}

function chart(title: string, unit: string, definitions: Series[], signal: AbortSignal) {
  const host = el("div", { class: "chart-host", role: "img", "aria-label": `${title}, last five minutes` });
  const current = el("p", { class: "chart-summary" }, "Waiting for data");
  const box = panel(el("h2", {}, title), current, host);
  box.classList.add("chart-panel");
  let graph: uPlot | undefined;
  let shaded: boolean[] = [];
  let colors = chartColors();
  onThemeChange(() => {
    colors = chartColors();
    graph?.redraw(false);
  }, signal);

  const resize = new ResizeObserver(() => {
    const width = Math.floor(host.clientWidth);
    if (width > 0 && graph && graph.width !== width) graph.setSize({ width, height: 182 });
  });
  resize.observe(host);
  signal.addEventListener("abort", () => {
    resize.disconnect();
    graph?.destroy();
  }, { once: true });

  function update(seconds: number[], samples: Sample[]): void {
    shaded = samples.map(incomplete);
    const data: uPlot.AlignedData = [seconds, ...definitions.map(series => samples.map(sample =>
      sample ? series.read(sample) ?? null : null))];
    if (samples.some(sample => sample !== null)) {
      current.textContent = definitions.map(series => {
        const index = samples.findLastIndex(sample => sample !== null && series.read(sample) != null);
        const value = index < 0 ? null : series.read(samples[index]!);
        const time = index < 0 ? "" : ` at ${new Date(seconds[index]! * 1000).toLocaleTimeString()}`;
        return `${series.label} ${value == null ? "—" : `${number.format(value)}${unit ? ` ${unit}` : ""}${time}`}`;
      }).join("  ·  ");
    } else current.textContent = "Waiting for data";
    if (graph) {
      graph.setData(data);
      return;
    }
    const axis = {
      stroke: () => colors.axis,
      grid: { stroke: () => colors.grid },
      ticks: { stroke: () => colors.grid },
      font: "12px system-ui",
    };
    // uPlot sets these DOM colours as inline styles. CSS variables keep them live.
    const markerColor = (_plot: uPlot, index: number) => `var(${definitions[index - 1]!.color})`;
    graph = new uPlot({
      width: Math.max(250, host.clientWidth), height: 182,
      cursor: { drag: { setScale: false }, points: { size: 5, fill: markerColor, stroke: markerColor } },
      legend: { live: false, markers: { stroke: markerColor } },
      axes: [axis, { ...axis, size: 48 }],
      scales: {
        x: { time: true, range: plot => [plot.data[0][0]! - 0.5, plot.data[0].at(-1)! + 0.5] },
        y: { auto: true, range: (_plot, _min, max) => [0, max > 0 && Number.isFinite(max) ? max * 1.1 : 1] },
      },
      series: [{}, ...definitions.map(series => ({
        label: series.label, stroke: () => colors[series.color], dash: series.dash, width: 2,
        points: { show: false }, spanGaps: false,
      }))],
      hooks: { drawClear: [plot => {
        const { ctx, bbox } = plot;
        const x = plot.scales.x!;
        const step = bbox.width / ((x.max ?? 1) - (x.min ?? 0));
        ctx.save();
        ctx.fillStyle = colors.incomplete;
        shaded.forEach((missing, index) => {
          if (!missing) return;
          const second = plot.data[0][index]!;
          const position = plot.valToPos(second - 0.5, "x", true);
          ctx.fillRect(position, bbox.top, step + 0.25, bbox.height);
        });
        ctx.restore();
      }] },
    }, data, host);
  }
  return { box, update };
}

function metricsPanel(signal: AbortSignal): HTMLElement {
  const connection = el("span", { class: "connection" }, "Connecting");
  const status = notice();
  const charts = [
    chart("Request rate", "req/s", httpSeries(["rps"], [""]), signal),
    chart("Request latency", "ms", httpSeries(["p50_ms", "p95_ms", "p99_ms"], ["p50", "p95", "p99"]), signal),
    chart("Error rate", "%", httpSeries(["error_rate"], [""], 100), signal),
    chart("Active connections", "", [
      { label: "nginx active TCP connections", color: "--chart-blue", read: metric => metric.connections?.active },
      { label: "Seat SSE", color: "--chart-teal", read: metric => metric.connections?.sse },
    ], signal),
  ];
  const funnel = el("dl", { class: "metric-strip" });
  const business = [["Holds", "hold_ok"], ["Hold conflicts", "hold_conflict"], ["Sold out", "hold_soldout"],
    ["Payments", "pay_ok"]] as const;
  const values = business.map(([title]) => {
    const value = el("dd", {}, "—");
    funnel.append(el("div", {}, el("dt", {}, title), value));
    return value;
  });
  const section = el("section", { class: "dashboard-section" },
    el("div", { class: "split" }, el("h2", {}, "Live metrics"), connection),
    el("p", { class: "muted" }, "Last 5 minutes · Shaded seconds have incomplete or missing data."),
    status, el("div", { class: "chart-grid" }, ...charts.map(item => item.box)),
    panel(el("h2", {}, "Sales activity"), el("p", { class: "muted" }, "Most recent reporting second"), funnel));
  const samples = new MetricWindow();
  let delay = 750;

  function draw(): void {
    const { seconds, samples: window } = samples.window(Math.floor(Date.now() / 1000));
    charts.forEach(item => item.update(seconds, window));
    const lastBusiness = window.findLast(sample => sample?.business)?.business;
    business.forEach(([, key], index) => { values[index]!.textContent = lastBusiness?.[key]?.toLocaleString() ?? "—"; });
  }

  async function backfill(): Promise<void> {
    const to = Math.floor(Date.now() / 1000) * 1000;
    try {
      const rows = await api.metrics(new Date(to - WINDOW_SECONDS * 1000).toISOString(), new Date(to).toISOString(), signal);
      if (signal.aborted) return;
      for (const row of rows) samples.add(row, false);
      draw();
    } catch {
      if (!signal.aborted && connection.textContent !== "Live") show(status, "Metric history is unavailable.", true);
    }
  }

  async function connect(): Promise<void> {
    while (!signal.aborted) {
      void backfill();
      const watchdog = new AbortController();
      let lastFrame = performance.now();
      const timer = setInterval(() => {
        if (performance.now() - lastFrame >= 10_000) watchdog.abort();
      }, 1000);
      try {
        for await (const metric of api.metricsStream(AbortSignal.any([signal, watchdog.signal]))) {
          if (signal.aborted) return;
          if (!Number.isFinite(metricSecond(metric.ts))) continue;
          lastFrame = performance.now();
          samples.add(metric);
          delay = 750;
          connection.textContent = "Live";
          show(status, "");
          draw();
        }
      } catch (error) {
        if (error instanceof ApiError && error.status === 403) {
          connection.textContent = "Unavailable";
          show(status, errorText(error), true);
          return;
        }
      } finally {
        clearInterval(timer);
        watchdog.abort();
      }
      if (signal.aborted) return;
      connection.textContent = "Reconnecting";
      show(status, "Live metrics disconnected. Reconnecting and fetching missed seconds.", true);
      await sleep(delay, signal);
      delay = Math.min(6000, delay * 2);
    }
  }

  requestAnimationFrame(() => {
    if (signal.aborted) return;
    draw();
    void connect();
    interval(() => { if (connection.textContent !== "Live") draw(); }, 1000, signal);
  });
  return section;
}

function alertsPanel(signal: AbortSignal): HTMLElement {
  const feedback = notice();
  const list = el("ul", { class: "record-list alerts-list", "aria-label": "Firing alerts" });
  const cards = new Map<string, { alert: Alert; card: HTMLLIElement }>();
  const acknowledged = new Set<string>();

  // Resolution and firing may be distinct event rows for the same rule.
  const alertKey = (alert: Alert) => JSON.stringify([alert.rule_id, alert.scope ?? "all"]);

  function emptyMessage(): void {
    if (list.childElementCount === 0) list.append(el("li", { class: "muted empty-alerts" }, "No firing alerts."));
    else if (cards.size) list.querySelector(".empty-alerts")?.remove();
  }

  function update(alert: Alert): void {
    const key = alertKey(alert);
    const id = String(alert.id);
    const prior = cards.get(key);
    const previous = prior?.card;
    if (prior && Date.parse(prior.alert.ts) > Date.parse(alert.ts)) return;
    if (alert.state !== "firing") {
      previous?.remove();
      cards.delete(key);
      emptyMessage();
      return;
    }
    const ack = alert.ack_at != null || alert.ack_by != null || acknowledged.has(id);
    const fingerprint = JSON.stringify([alert, ack]);
    if (previous?.dataset.fingerprint === fingerprint) return;
    const action = button("Acknowledge", "secondary");
    action.onclick = async () => {
      action.disabled = true;
      try {
        await api.ack(alert.id, signal);
        acknowledged.add(id);
        const result = cards.get(key);
        if (result && String(result.alert.id) === id) {
          const current = result.card;
          current.querySelector("button")?.replaceWith(el("span", { class: "muted" }, "Acknowledged"));
          if (document.activeElement === action || document.activeElement === document.body) current.focus();
        }
        show(feedback, "Alert acknowledged.");
      } catch (error) {
        show(feedback, errorText(error), true);
        action.disabled = false;
      }
    };
    const card = el("li", { tabindex: -1 }, el("div", {},
      el("strong", {}, label(alert.rule_id.toLowerCase())),
      el("p", { class: "muted" }, [alert.scope, alert.ts && date(alert.ts),
        alert.value != null ? `Value ${number.format(alert.value)}` : "",
        alert.threshold != null ? `Threshold ${number.format(alert.threshold)}` : ""].filter(Boolean).join(" · "))),
    ack ? el("span", { class: "muted" }, "Acknowledged") : action);
    card.dataset.fingerprint = fingerprint;
    const focused = previous?.contains(document.activeElement);
    if (previous) previous.replaceWith(card);
    else list.prepend(card);
    cards.set(key, { alert, card });
    if (focused) (ack ? card : action).focus();
    emptyMessage();
  }

  function replaceAlerts(snapshot: Alert[]): void {
    const firing = snapshot.filter(alert => alert.state === "firing");
    const existing = new Set(firing.map(alertKey));
    for (const [key, { card }] of cards) {
      if (!existing.has(key)) {
        card.remove();
        cards.delete(key);
      }
    }
    firing.forEach(update);
    emptyMessage();
  }

  async function connect(): Promise<void> {
    let delay = 750;
    while (!signal.aborted) {
      try {
        const initial = await api.alerts(signal);
        if (signal.aborted) return;
        replaceAlerts(initial);
        show(feedback, "");
        for await (const frame of api.alertsStream(signal)) {
          if (signal.aborted) return;
          if ("snapshot" in frame) replaceAlerts(frame.snapshot);
          else update(frame.alert);
          delay = 750;
        }
      } catch (error) {
        if (signal.aborted) return;
        if (error instanceof ApiError && error.status === 403) {
          show(feedback, errorText(error), true);
          return;
        }
      }
      if (signal.aborted) return;
      show(feedback, "Alert feed disconnected. Reconnecting…", true);
      await sleep(delay, signal);
      delay = Math.min(6000, delay * 2);
    }
  }

  void connect();
  return panel(el("h2", {}, "Firing alerts"), feedback, list);
}

function traceAction(id: string | null, openTrace: (id: string) => void): HTMLButtonElement | string {
  if (!id) return "—";
  const action = button(id, "quiet log-request");
  action.onclick = () => openTrace(id);
  return action;
}

function recentLogsPanel(signal: AbortSignal, openTrace: (id: string) => void): HTMLElement {
  const title = "Recent warnings and errors";
  const filter = select([["", "All services"], ...logServices.map(service => [service, service] as const)]);
  const status = notice();
  const { body, container } = table(["Time", "Service", "Level", "Event", "Request ID"]);
  container.setAttribute("aria-label", title);
  container.classList.add("recent-logs-scroll");
  container.hidden = true;
  let pending: AbortController | undefined;
  let timer: ReturnType<typeof setInterval> | undefined;
  let snapshot = "";

  async function refresh(): Promise<void> {
    if (pending || signal.aborted || document.visibilityState !== "visible") return;
    const request = new AbortController();
    pending = request;
    try {
      const service = logServices.find(name => name === filter.value);
      const entries = await api.recentLogs({ service }, request.signal);
      if (request.signal.aborted) return;
      const next = JSON.stringify(entries);
      if (snapshot !== next) {
        snapshot = next;
        const active = document.activeElement;
        const focusedId = active instanceof HTMLButtonElement && body.contains(active) ? active.textContent : null;
        body.replaceChildren(...entries.map(entry =>
          el("tr", {}, el("td", {}, el("time", { datetime: entry.ts }, date(entry.ts))),
            el("td", {}, entry.service), el("td", {}, el("span", { class: `log-level ${entry.level}` }, entry.level)),
            el("td", {}, entry.event ?? "—"), el("td", {}, traceAction(entry.request_id, openTrace)))));
        if (focusedId) {
          const nextAction = [...body.querySelectorAll("button")].find(action => action.textContent === focusedId);
          (nextAction ?? filter).focus();
        }
      }
      container.hidden = entries.length === 0;
      const message = entries.length ? "" : "No recent warnings or errors.";
      if (status.textContent !== message || status.classList.contains("problem")) show(status, message);
    } catch (error) {
      if (!request.signal.aborted) show(status, errorText(error), true);
    } finally {
      if (pending === request) pending = undefined;
    }
  }

  function stop(): void {
    clearInterval(timer);
    pending?.abort();
    pending = undefined;
  }

  function restart(): void {
    stop();
    if (signal.aborted || document.visibilityState !== "visible") return;
    void refresh();
    timer = setInterval(() => void refresh(), 5_000);
  }

  filter.onchange = () => {
    snapshot = "";
    body.replaceChildren();
    container.hidden = true;
    show(status, "Loading logs…");
    restart();
  };
  document.addEventListener("visibilitychange", restart, { signal });
  signal.addEventListener("abort", stop, { once: true });
  show(status, "Loading logs…");
  restart();
  return panel(el("h2", {}, title), el("div", { class: "filters" }, field("Service", filter)), status, container);
}

function accessLogPanel(signal: AbortSignal, openTrace: (id: string) => void): HTMLElement {
  const title = "Access log";
  const statusClass = select([["", "All statuses"], ...accessStatusClasses.map(status => [status, status] as const)]);
  const form = el("form", { class: "filters" },
    field("Window (minutes)", el("input", { name: "since_minutes", type: "number", min: 1, max: 60, value: 15, required: true })),
    field("Limit", el("input", { name: "limit", type: "number", min: 1, max: 200, value: 50, required: true })),
    field("Status", statusClass),
    field("Method", el("input", { name: "method", maxlength: 32, pattern: "[A-Za-z]{1,32}", placeholder: "GET" })),
    field("Path prefix", el("input", { name: "path_prefix", maxlength: 512, placeholder: "/api/v1/" })),
    field("Request ID", el("input", { name: "request_id", maxlength: 128 })),
    button("Apply filters", "secondary", "submit"));
  const status = notice();
  const { body, container } = table(["Time", "Method", "Path", "Status", "Duration", "Request ID"]);
  container.setAttribute("aria-label", title);
  container.classList.add("recent-logs-scroll");
  let pending: AbortController | undefined;

  async function load(): Promise<void> {
    pending?.abort();
    pending = new AbortController();
    const lookupSignal = AbortSignal.any([signal, pending.signal]);
    container.hidden = true;
    show(status, "Loading access logs…");
    try {
      const entries = await api.accessLogs({
        since_minutes: Number(formValue(form, "since_minutes")),
        limit: Number(formValue(form, "limit")),
        status_class: accessStatusClasses.find(value => value === statusClass.value),
        method: formValue(form, "method") || undefined,
        path_prefix: formValue(form, "path_prefix") || undefined,
        request_id: formValue(form, "request_id") || undefined,
      }, lookupSignal);
      if (lookupSignal.aborted) return;
      body.replaceChildren(...entries.map(entry =>
        el("tr", {}, el("td", {}, el("time", { datetime: entry.ts }, date(entry.ts))),
          el("td", {}, entry.method ?? "—"), el("td", {}, entry.path ?? "—"),
          el("td", {}, entry.status ?? "—"), el("td", {}, entry.req_ms == null ? "—" : `${number.format(entry.req_ms)} ms`),
          el("td", {}, traceAction(entry.request_id, openTrace)))));
      container.hidden = entries.length === 0;
      show(status, entries.length ? "" : "No access logs match these filters.");
    } catch (error) {
      if (!lookupSignal.aborted) show(status, errorText(error), true);
    }
  }

  form.onsubmit = event => {
    event.preventDefault();
    void load();
  };
  void load();
  return panel(el("h2", {}, title), form, status, container);
}

function tracePanel(signal: AbortSignal) {
  const lookup = button("Find request", "primary", "submit");
  const input = el("input", { name: "request_id", required: true, maxLength: 160, placeholder: "Paste a request ID" });
  const form = el("form", { class: "filters" }, field("Request ID", input), lookup);
  const status = notice();
  const { body, container } = table(["Time", "Service", "Level", "Event / request", "Status", "Details"]);
  container.hidden = true;
  let pending: AbortController | undefined;
  form.onsubmit = async event => {
    event.preventDefault();
    const id = formValue(form, "request_id");
    if (!id) return;
    pending?.abort();
    pending = new AbortController();
    const lookupSignal = AbortSignal.any([signal, pending.signal]);
    lookup.disabled = true;
    show(status, "Looking up request…");
    container.hidden = true;
    try {
      const entries = await api.trace(id, lookupSignal);
      if (lookupSignal.aborted) return;
      body.replaceChildren(...entries.map(entry => {
        const details = el("details", {}, el("summary", {}, "View"), el("pre", {}, JSON.stringify(entry.attrs, null, 2)));
        return el("tr", {}, el("td", {}, entry.ts ? date(entry.ts) : "—"),
          el("td", {}, entry.service ?? "—"), el("td", {}, entry.level ?? "—"),
          el("td", {}, [entry.event, entry.method, entry.route ?? entry.path].filter(Boolean).join(" ") || "—"),
          el("td", {}, entry.status ?? "—"), el("td", {}, entry.req_ms == null ? undefined : `${entry.req_ms} ms`,
            entry.attrs ? details : undefined));
      }));
      container.hidden = !entries.length;
      show(status, !entries.length ? "No log entries found for this request." : `${entries.length} entries`);
    } catch (error) {
      if (!lookupSignal.aborted) show(status, errorText(error), true);
    } finally {
      if (!lookupSignal.aborted) {
        lookup.disabled = false;
        pending = undefined;
      }
    }
  };
  return {
    box: panel(el("h2", {}, "Request trace"), form, status, container),
    open: (id: string): void => {
      input.value = id;
      input.focus();
      form.requestSubmit();
    },
  };
}

export function adminPage(root: HTMLElement, signal: AbortSignal): void {
  const trace = tracePanel(signal);
  root.append(heading("Monitoring", "Watch traffic, ticketing activity and operational alerts."),
    el("div", { class: "page-actions" }, link("Manage users", "#/admin/users")), metricsPanel(signal),
    el("div", { class: "admin-lower" }, alertsPanel(signal), trace.box,
      recentLogsPanel(signal, trace.open), accessLogPanel(signal, trace.open)));
}

export function usersPage(root: HTMLElement, principal: User, signal: AbortSignal): void {
  const { body, container } = table(["Email", "Role", "Enabled", "Action"]);
  const edit = el("div", { class: "user-editor" });
  const feedback = notice();
  const more = button("Load more users", "secondary");
  let cursor: string | null = null;
  let selected: string | undefined;
  let revising = false;
  const accounts = new Map<string, AdminUser>();
  root.append(heading("Users", "Manage account roles and access."),
    el("div", { class: "page-actions" }, link("Back to monitoring", "#/admin")), panel(container, feedback, more), edit);

  function render(): void {
    body.replaceChildren(...Array.from(accounts.values(), account => {
      const mine = String(principal.id) === String(account.id);
      const change = button("Edit", "secondary");
      change.setAttribute("aria-label", `Edit ${account.email}`);
      change.disabled = revising;
      change.onclick = () => void loadUser(account);
      return el("tr", {}, el("td", {}, account.email, mine ? " (you)" : ""),
        el("td", {}, label(account.role)), el("td", {}, account.is_active ? "Yes" : "No"),
        el("td", {}, mine ? "—" : change));
    }));
  }

  async function load(): Promise<void> {
    more.disabled = true;
    show(feedback, "Loading users…");
    try {
      const page = await api.users(cursor, signal);
      if (signal.aborted) return;
      page.items.forEach(account => accounts.set(String(account.id), account));
      render();
      cursor = page.next_cursor;
      more.hidden = cursor === null;
      show(feedback, accounts.size ? "" : "No users found.");
    } catch (error) {
      show(feedback, errorText(error), true);
      more.textContent = "Retry loading users";
    } finally {
      more.disabled = false;
    }
  }

  async function loadUser(account: AdminUser): Promise<void> {
    if (revising) return;
    const id = String(account.id);
    selected = id;
    const status = notice();
    show(status, "Loading account…");
    edit.replaceChildren(panel(status));
    try {
      const { data, etag } = await api.user(account.id, signal);
      if (signal.aborted || selected !== id) return;
      const role = select([["admin", "Admin"], ["user", "User"], ["viewer", "Viewer"]]);
      role.setAttribute("aria-label", "Role");
      role.value = data.role;
      const active = el("input", { type: "checkbox", checked: data.is_active });
      const save = button("Save account", "primary", "submit");
      const reload = button("Reload account", "secondary");
      reload.hidden = true;
      const form = el("form", { class: "edit-form" }, field("Role", role), field("Account enabled", active),
        el("div", { class: "actions" }, save, reload), status);
      edit.replaceChildren(panel(el("h2", {}, data.email), form));
      show(status, "");
      role.focus();
      reload.onclick = () => void loadUser(data);
      form.onsubmit = async event => {
        event.preventDefault();
        if (revising) return;
        const body: Partial<Pick<AdminUser, "role" | "is_active">> = {};
        if (role.value !== data.role) body.role = role.value as Role;
        if (active.checked !== data.is_active) body.is_active = active.checked;
        if (!Object.keys(body).length) {
          show(status, "No changes to save.");
          return;
        }
        revising = true;
        render();
        save.disabled = true;
        try {
          const updated = await api.updateUser(data.id, body, etag, signal);
          if (signal.aborted) return;
          accounts.set(id, updated);
          edit.replaceChildren();
          show(feedback, `Saved changes to ${updated.email}.`);
        } catch (error) {
          show(status, errorText(error), true);
          reload.hidden = !(error instanceof ApiError && error.status === 412);
        } finally {
          revising = false;
          save.disabled = !reload.hidden;
          render();
          if (!edit.childElementCount) {
            [...bodyButtons()].find(control => control.getAttribute("aria-label") === `Edit ${data.email}`)?.focus();
          }
        }
      };
    } catch (error) {
      if (selected === id) show(status, errorText(error), true);
    }
  }

  function bodyButtons(): NodeListOf<HTMLButtonElement> {
    return container.querySelectorAll("button");
  }

  more.onclick = () => void load();
  void load();
}
