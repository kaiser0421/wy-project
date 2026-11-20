import { api, ApiError, mockMode, type Id } from "./api";

type Child = Node | string | number | null | undefined | false;
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K, attrs: Record<string, string | number | boolean | undefined> = {}, ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== undefined && value !== false) node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children) {
    if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

export const panel = (...children: Child[]) => el("section", { class: "panel" }, ...children);
export const link = (text: string, href: string, className?: string) => el("a", { href, class: className }, text);
export function heading(title: string, description?: string): HTMLElement {
  return el("header", { class: "page-title" }, el("h1", { tabindex: -1 }, title),
    description ? el("p", {}, description) : undefined);
}

export function button(text: string, style = "primary", type = "button"): HTMLButtonElement {
  return el("button", { type, class: style }, text);
}

export function field(label: string, input: HTMLElement): HTMLLabelElement {
  return el("label", { class: "field" }, el("span", {}, label), input);
}

export function select(values: readonly (readonly [string, string])[]): HTMLSelectElement {
  return el("select", {}, ...values.map(([value, label]) => el("option", { value }, label)));
}

export function notice(): HTMLParagraphElement {
  return el("p", { class: "notice", role: "status", "aria-live": "polite" });
}

const messages: Record<string, string> = {
  "seat-unavailable": "One or more seats are no longer available. Check the live map and choose again.",
  "seat-contended": "Another buyer is trying to hold these seats. Try again shortly.",
  "insufficient-seats": "There are too few seats available in that zone. Try fewer tickets or another zone.",
  "allocation-contended": "Automatic allocation is busy. Try again shortly.",
  "purchase-limit-exceeded": "You can hold or buy up to four tickets for this event.",
  "event-not-on-sale": "Tickets for this event are outside the sale window.",
  "idempotency-in-progress": "This request is still processing. Retry to recover the result.",
  "payment-declined": "The payment was declined. Check the order for its current status.",
  "cursor-invalidated": "The event list changed. Refresh results to start again.",
  "precondition-failed": "Someone updated this record. Reload it before making another change.",
};

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const retry = error.retryable ? error.retryAfter && /^\d+$/.test(error.retryAfter)
      ? ` Retry in about ${error.retryAfter} seconds.` : " You can retry this request." : "";
    return (error.status === 410 ? "This list has expired. Refresh results to start again."
      : messages[error.code] ?? error.message) + retry + (error.requestId ? ` Request ID ${error.requestId}.` : "");
  }
  if (error instanceof DOMException && error.name === "TimeoutError") return "The request timed out.";
  return error instanceof TypeError ? "The server could not be reached. Check your connection and retry."
    : "The request could not be completed. Try again.";
}

export function show(target: HTMLElement, text: string, error = false): void {
  target.classList.toggle("problem", error);
  target.textContent = text;
}

export function sleep(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise(resolve => {
    const timer = setTimeout(finish, ms);
    function finish() {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    }
    signal.addEventListener("abort", finish, { once: true });
  });
}

export function interval(work: () => void, ms: number, signal: AbortSignal): void {
  const timer = setInterval(work, ms);
  signal.addEventListener("abort", () => clearInterval(timer), { once: true });
}

export function formValue(form: HTMLFormElement, key: string): string {
  return String(new FormData(form).get(key) ?? "").trim();
}

export async function download(response: Promise<Response>, name: string): Promise<void> {
  const file = URL.createObjectURL(await (await response).blob());
  const anchor = link("Save file", file);
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(file), 30_000);
}

export function reportButton(retrieve: () => Promise<Response>, name: string, status: HTMLElement): HTMLButtonElement {
  const control = button(name, "secondary");
  control.onclick = async () => {
    control.disabled = true;
    show(status, "Preparing download…");
    try {
      await download(retrieve(), "seat-import-errors.csv");
      show(status, "Download started.");
    } catch (error) {
      show(status, errorText(error), true);
    } finally {
      control.disabled = false;
    }
  };
  return control;
}

export function salesReport(id: Id): HTMLElement {
  return mockMode
    ? el("p", { class: "muted" }, "Sales exports are available when connected to the API.")
    : el("a", { href: api.reportUrl(id), class: "download-link", download: "sales.xlsx" }, "Download sales report");
}

export function table(headers: string[]): { container: HTMLElement; body: HTMLTableSectionElement } {
  const body = el("tbody");
  return {
    body,
    container: el("div", { class: "table-scroll", tabindex: 0, role: "region", "aria-label": headers.join(", ") },
      el("table", {}, el("thead", {}, el("tr", {}, ...headers.map(title => el("th", { scope: "col" }, title)))), body)),
  };
}
