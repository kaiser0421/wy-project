import { api, ApiError, orderStatuses, type AdminOrder, type AdminOrderFilters, type User } from "./api";
import { date, label, money } from "./format";
import { IntentUnavailable, MutationIntent } from "./intent";
import { button, el, errorText, field, formValue, heading, link, notice, panel, select, show, table } from "./view";

export function adminOrdersPage(root: HTMLElement, user: User, signal: AbortSignal): void {
  const eventId = el("input", { name: "event_id", inputmode: "numeric", pattern: "[1-9][0-9]*" });
  const status = select([["", "All statuses"], ...orderStatuses.map(value => [value, label(value)] as const)]);
  status.name = "status";
  const email = el("input", { name: "buyer_email", maxlength: 254 });
  const from = el("input", { name: "created_from", type: "datetime-local", step: 1 });
  const to = el("input", { name: "created_to", type: "datetime-local", step: 1 });
  const apply = button("Apply filters", "primary", "submit");
  const form = el("form", { class: "filters", "aria-label": "Order filters" },
    field("Event ID", eventId), field("Status", status), field("Buyer email prefix", email),
    field("Created from (local time)", from), field("Created to (local time)", to), apply);

  const orders = table(["Order", "Event", "Buyer email", "Status", "Quantity", "Total", "Created", "Expires", "Actions"]);
  orders.container.setAttribute("aria-label", "Order results");
  orders.container.hidden = true;
  const feedback = notice();
  const actionFeedback = notice();
  const previous = button("Previous page", "secondary");
  const next = button("Next page", "secondary");
  const pageNumber = el("span", { tabindex: -1 });
  const pager = el("nav", { class: "actions", "aria-label": "Order pages", hidden: true }, previous, pageNumber, next);
  root.append(heading("All orders"), panel(form, feedback, actionFeedback, orders.container, pager));

  let filters: AdminOrderFilters = {};
  let cursors: (string | null)[] = [null];
  let nextCursor: string | null = null;

  function renderRow(order: AdminOrder, activeFilters: AdminOrderFilters): HTMLTableRowElement {
    const detail = link(order.public_id, `#/orders/${encodeURIComponent(order.public_id)}`);
    const orderStatus = el("td", {}, label(order.status));
    const actions = el("td");
    const row = el("tr", {}, el("th", { scope: "row" }, detail),
      el("td", {}, `${order.event_title} (#${order.event_id})`), el("td", {}, order.buyer_email),
      orderStatus, el("td", {}, order.quantity), el("td", {}, order.total_twd === null ? "—" : money(order.total_twd)),
      el("td", {}, el("time", { datetime: order.created_at }, date(order.created_at))),
      el("td", {}, order.expires_at ? el("time", { datetime: order.expires_at }, date(order.expires_at)) : "—"), actions);
    const cancellation = new MutationIntent<null>(user.id, {
      operation: "cancellation", event: order.event_id, order: order.public_id,
    });
    const paid = order.status === "paid";
    if (!cancellation.current && !paid && order.status !== "pending_payment") return row;
    const action = paid ? "Request refund" : "Cancel order";
    const cancel = button(cancellation.current ? "Retry cancellation" : action, "danger");
    cancel.setAttribute("aria-label", `${cancel.textContent} ${order.public_id}`);
    actions.append(cancel);

    cancel.onclick = async () => {
      if (cancellation.expired) {
        show(actionFeedback, "Open the order to check its status and clear the expired request.", true);
        return;
      }
      if (cancellation.seconds) {
        show(actionFeedback, `Retry in ${cancellation.seconds} seconds.`, true);
        return;
      }
      const pending = cancellation.current;
      if (!pending && !window.confirm(paid
        ? `Request a refund for order ${order.public_id}?`
        : `Cancel order ${order.public_id} and release its held seats?`)) return;
      cancel.disabled = true;
      show(actionFeedback, "Sending cancellation…");
      try {
        const response = await cancellation.send(pending ? undefined : null,
          (_body, key) => api.cancel(order.public_id, key, signal));
        const result = await response.json() as Pick<AdminOrder, "status">;
        if (signal.aborted || !row.isConnected) return;
        show(actionFeedback, `Order ${order.public_id}: ${label(result.status)}.`);
        if (activeFilters.status && activeFilters.status !== result.status) {
          row.remove();
          (pager.hidden ? apply : pageNumber).focus();
          if (!orders.body.rows.length) show(feedback, "No matching orders on this page.");
        } else {
          orderStatus.textContent = label(result.status);
          detail.focus();
          cancel.remove();
        }
      } catch (error) {
        if (signal.aborted || !row.isConnected) return;
        cancel.textContent = cancellation.current ? "Retry cancellation" : action;
        cancel.setAttribute("aria-label", `${cancel.textContent} ${order.public_id}`);
        show(actionFeedback, error instanceof IntentUnavailable
          ? "Open the order to check the cancellation status or retry the request." : errorText(error), true);
      } finally {
        cancel.disabled = false;
      }
    };
    return row;
  }

  async function load(newFilters: AdminOrderFilters, trail: (string | null)[], focusPage = false): Promise<void> {
    apply.disabled = previous.disabled = next.disabled = true;
    orders.container.setAttribute("aria-busy", "true");
    show(feedback, "Loading orders…");
    show(actionFeedback, "");
    try {
      const page = await api.adminOrders(newFilters, trail.at(-1) ?? null, signal);
      if (signal.aborted) return;
      filters = newFilters;
      cursors = trail;
      nextCursor = page.next_cursor;
      orders.body.replaceChildren(...page.items.map(order => renderRow(order, filters)));
      orders.container.hidden = !page.items.length;
      pager.hidden = cursors.length === 1 && !nextCursor;
      pageNumber.textContent = `Page ${cursors.length}`;
      show(feedback, page.items.length ? "" : "No matching orders.");
      if (focusPage) pageNumber.focus();
    } catch (error) {
      if (!signal.aborted) show(feedback, error instanceof ApiError && error.code === "cursor-invalidated"
        ? "Apply filters to restart the order list." : errorText(error), true);
    } finally {
      orders.container.setAttribute("aria-busy", "false");
      apply.disabled = false;
      previous.disabled = cursors.length === 1;
      next.disabled = !nextCursor;
    }
  }

  form.onsubmit = event => {
    event.preventDefault();
    if (apply.disabled) return;
    void load({
      event_id: formValue(form, "event_id"),
      status: status.value,
      buyer_email: formValue(form, "buyer_email"),
      created_from: from.value ? new Date(from.value).toISOString() : undefined,
      created_to: to.value ? new Date(to.value).toISOString() : undefined,
    }, [null]);
  };
  previous.onclick = () => void load(filters, cursors.slice(0, -1), true);
  next.onclick = () => void load(filters, [...cursors, nextCursor], true);
  void load(filters, cursors);
}
