import { api, mockMode, type Order } from "./api";
import { countdown, label, money } from "./format";
import { idempotencyKey, retainIntent } from "./intent";
import { button, el, errorText, heading, interval, link, notice, panel, show, sleep } from "./view";

const ongoing = (status: string) => ["pending_payment", "refund_pending"].includes(status);

export function ordersPage(root: HTMLElement, signal: AbortSignal): void {
  const list = el("ul", { class: "record-list orders-list" });
  const feedback = notice();
  const more = button("Load more orders", "secondary");
  root.append(heading("My orders", "Manage ticket holds, payments and refunds."), panel(list, feedback, more));
  const countdowns = new Map<HTMLElement, string>();
  let cursor: string | null = null;
  let count = 0;

  async function load(): Promise<void> {
    more.disabled = true;
    show(feedback, "Loading orders…");
    try {
      const page = await api.orders(cursor, signal);
      if (signal.aborted) return;
      for (const order of page.items) {
        const time = el("span", { class: "muted" });
        if (order.status === "pending_payment" && order.expires_at) countdowns.set(time, order.expires_at);
        list.append(el("li", {},
          el("div", {}, link(order.event_title ?? `Order ${order.id.slice(0, 8)}`, `#/orders/${encodeURIComponent(order.id)}`),
            el("p", { class: "muted" }, label(order.status))),
          el("div", { class: "order-list-total" }, el("strong", {}, money(order.total_twd)), time)));
        count++;
      }
      updateCountdowns();
      cursor = page.next_cursor;
      more.hidden = cursor === null;
      show(feedback, count === 0 ? "You have no orders yet." : "");
    } catch (error) {
      show(feedback, errorText(error), true);
      more.textContent = "Retry loading orders";
    } finally {
      more.disabled = false;
    }
  }

  function updateCountdowns(): void {
    for (const [element, expiry] of countdowns) {
      element.textContent = Date.parse(expiry) > Date.now() ? `Hold ends in ${countdown(expiry)}` : "Hold time elapsed";
    }
  }
  more.onclick = () => void load();
  interval(updateCountdowns, 1000, signal);
  void load();
}

function checkoutLink(url: string): URL {
  const target = new URL(url, location.origin);
  if (!["http:", "https:"].includes(target.protocol) || target.username || target.password) {
    throw new Error("Invalid checkout URL");
  }
  return target;
}

export async function orderPage(root: HTMLElement, id: string, signal: AbortSignal): Promise<void> {
  let order = await api.order(id, signal);
  if (signal.aborted) return;
  const status = el("span", { class: "tag", role: "status" });
  const timer = el("p", { class: "countdown" });
  const items = el("ul", { class: "record-list" });
  const totals = el("dl", { class: "totals" });
  const feedback = notice();
  const updateStatus = notice();
  const pay = button("Open checkout");
  const cancel = button("Cancel order", "danger");
  const refresh = button("Check status", "secondary");
  const checkout = el("div");
  const shortId = id.slice(0, 8);
  root.append(heading(order.event_title ?? `Order ${shortId}`, `Order reference ${id}`),
    el("div", { class: "page-actions" }, link("All orders", "#/orders")),
    el("div", { class: "order-layout" }, panel(el("div", { class: "split" }, el("h2", {}, "Tickets"), status), items),
      panel(el("h2", {}, "Order summary"), totals, timer, el("div", { class: "actions" }, pay, cancel, refresh),
        checkout, feedback, updateStatus)));

  let busy = false;
  let checking = false;
  let needsRefresh = false;
  let payKey: string | undefined;
  let cancelKey: string | undefined;

  function render(): void {
    const pending = order.status === "pending_payment";
    const elapsed = order.expires_at !== null && Date.parse(order.expires_at) <= Date.now();
    status.textContent = label(order.status);
    timer.hidden = !pending || !order.expires_at;
    timer.textContent = !order.expires_at ? "" : elapsed
      ? "Hold time elapsed. Checking the server for final status."
      : `Hold expires in ${countdown(order.expires_at)}`;
    pay.hidden = !pending;
    cancel.hidden = !pending && order.status !== "paid";
    pay.disabled = busy || elapsed;
    cancel.disabled = busy;
    refresh.disabled = checking;
    pay.textContent = payKey ? "Retry checkout" : "Open checkout";
    cancel.textContent = cancelKey ? "Retry cancellation" : order.status === "paid" ? "Request refund" : "Cancel order";
    if (!pending) checkout.replaceChildren();
  }

  function renderOrder(): void {
    items.replaceChildren(...(order.items ?? []).map(item =>
      el("li", {}, `Zone ${item.zone_code ?? "—"}`,
        el("span", { class: "muted" }, item.row_label != null || item.seat_number != null
          ? `Row ${item.row_label ?? "—"}, seat ${item.seat_number ?? "—"}` : `Seat ${item.seat_id ?? "—"}`))));
    if (!items.childElementCount) items.append(el("li", { class: "muted" }, "Seat details are unavailable."));
    totals.replaceChildren(
      el("dt", {}, "Subtotal"), el("dd", {}, money(order.subtotal_twd)),
      el("dt", {}, "Service fee"), el("dd", {}, money(order.fee_twd)),
      el("dt", { class: "total" }, "Total"), el("dd", { class: "total" }, money(order.total_twd)));
    render();
  }

  async function check(): Promise<void> {
    if (checking || signal.aborted) return;
    checking = true;
    render();
    try {
      const latest = await api.order(id, signal);
      if (signal.aborted) return;
      order = latest;
      needsRefresh = false;
      show(updateStatus, "");
      renderOrder();
    } catch (error) {
      if (!signal.aborted) show(updateStatus, errorText(error), true);
    } finally {
      checking = false;
      render();
    }
  }

  async function poll(): Promise<void> {
    while (!signal.aborted) {
      await sleep(2000, signal);
      if (ongoing(order.status) || needsRefresh) await check();
    }
  }

  pay.onclick = async () => {
    if (busy) return;
    // Reserve the tab in the user's click gesture. If popups are blocked,
    // the checkout still has an explicit link once the intent completes.
    const tab = mockMode ? null : window.open("about:blank", "_blank");
    if (tab) tab.opener = null;
    payKey ??= idempotencyKey();
    busy = true;
    render();
    show(feedback, "Preparing checkout…");
    let launched = false;
    try {
      const payment = await api.pay(id, payKey, signal);
      payKey = undefined;
      if (signal.aborted) return;
      if (payment.checkout_url) {
        const url = checkoutLink(payment.checkout_url);
        checkout.replaceChildren(el("a", { href: url.href, target: "_blank", rel: "noopener noreferrer" }, "Open payment page"));
        if (tab && !tab.closed) {
          tab.location.replace(url.href);
          launched = true;
        }
        show(feedback, launched ? "Checkout opened. This page updates when payment is confirmed."
          : "Open the payment page to complete checkout.");
      } else {
        show(feedback, "Checking payment status…");
      }
    } catch (error) {
      if (!retainIntent(error)) payKey = undefined;
      if (!signal.aborted) show(feedback, errorText(error), true);
    } finally {
      if (!launched) tab?.close();
      busy = false;
      needsRefresh = true;
      render();
      void check();
    }
  };

  cancel.onclick = async () => {
    if (busy) return;
    if (!cancelKey && !window.confirm(order.status === "paid"
      ? "Request a refund for this order?" : "Cancel this order and release the held seats?")) return;
    cancelKey ??= idempotencyKey();
    busy = true;
    render();
    show(feedback, "Sending cancellation…");
    try {
      await api.cancel(id, cancelKey, signal);
      cancelKey = undefined;
      show(feedback, "Cancellation received.");
    } catch (error) {
      if (!retainIntent(error)) cancelKey = undefined;
      if (!signal.aborted) show(feedback, errorText(error), true);
    } finally {
      busy = false;
      needsRefresh = true;
      render();
      void check();
    }
  };
  refresh.onclick = () => void check();
  interval(render, 1000, signal);
  renderOrder();
  void poll();
}
