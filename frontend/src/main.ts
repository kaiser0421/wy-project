import { adminPage, usersPage } from "./admin";
import { api, ApiError, mockMode, onSessionExpired, type Role, type User } from "./api";
import { eventPage, eventsPage } from "./events";
import { label } from "./format";
import { orderPage, ordersPage } from "./orders";
import { managePage, organizerPage } from "./organizer";
import { button, el, errorText, field, heading, link, notice, panel, show } from "./view";
import "./style.css";

const app = document.querySelector<HTMLDivElement>("#app")!;
const main = el("main", { id: "main", class: "main", tabindex: -1 });
const header = el("header", { class: "app-header" });
const skip = link("Skip to content", "#main", "skip-link");
skip.onclick = event => { event.preventDefault(); main.focus(); };
app.append(skip, header, main);
let user: User | undefined;
let route = new AbortController();
let loginDestination = "#/events";
let ready = false;
const appNotice = notice();

const navigation = [
  { href: "#/events", title: "Events", roles: ["admin", "user", "viewer"] },
  { href: "#/orders", title: "My orders", roles: ["admin", "user"] },
  { href: "#/organizer", title: "Organizer", roles: ["admin", "user"] },
  { href: "#/admin", title: "Monitoring", roles: ["admin"] },
  { href: "#/admin/users", title: "Users", roles: ["admin"] },
];

function navigate(hash: string): void {
  if (location.hash === hash) void renderRoute();
  else location.hash = hash;
}

function signedIn(account: User, destination: string): void {
  user = account;
  show(appNotice, "");
  navigate(destination);
}

function headerFor(path: string): void {
  const start = link("Ticketing", user ? "#/events" : "#/login", "brand");
  start.prepend(el("span", { class: "brand-mark", "aria-hidden": true }));
  const content = el("div", { class: "header-inner" }, start);
  if (mockMode) content.append(el("span", { class: "demo-label" }, "Demo"));
  if (user) {
    const nav = el("nav", { "aria-label": "Main navigation" });
    for (const item of navigation) {
      if (!item.roles.includes(user.role)) continue;
      const tab = link(item.title, item.href);
      const exact = path === item.href;
      if (exact || (item.href !== "#/admin" && path.startsWith(item.href + "/"))) {
        tab.setAttribute("aria-current", exact ? "page" : "location");
      }
      nav.append(tab);
    }
    const signout = button("Sign out", "quiet");
    signout.onclick = async () => {
      signout.disabled = true;
      try {
        await api.logout();
        user = undefined;
        loginDestination = "#/events";
        show(appNotice, "");
        navigate("#/login");
      } catch (error) {
        if (user) show(appNotice, errorText(error), true);
      } finally {
        signout.disabled = false;
      }
    };
    content.append(nav, el("div", { class: "account" },
      el("span", { class: "account-role", title: user.email }, label(user.role)), signout));
  }
  header.replaceChildren(content);
}

function loginPage(root: HTMLElement, signal: AbortSignal): void {
  const email = el("input", { type: "email", name: "email", autocomplete: "username", required: true });
  const password = el("input", { type: "password", name: "password", autocomplete: "current-password", required: true });
  const login = button("Sign in", "primary", "submit");
  const feedback = notice();
  const form = el("form", { class: "edit-form" }, field("Email address", email), field("Password", password), login, feedback);
  const card = panel(heading("Sign in", "Access events, tickets and reports."), form);
  const controls = [login];

  async function submit(address: string, secret: string, destination: string): Promise<void> {
    controls.forEach(control => { control.disabled = true; });
    show(feedback, "Signing in…");
    try {
      const session = await api.login(address, secret, signal);
      if (!signal.aborted) signedIn(session.user, destination);
    } catch (error) {
      if (!signal.aborted) show(feedback, errorText(error), true);
    } finally {
      controls.forEach(control => { control.disabled = false; });
    }
  }
  form.onsubmit = event => {
    event.preventDefault();
    void submit(email.value.trim(), password.value, loginDestination);
  };
  if (mockMode) {
    const choices: [Role, string][] = [["user", "#/events/1"], ["viewer", "#/events"], ["admin", "#/admin"]];
    const demo = el("div", { class: "actions" });
    for (const [role, destination] of choices) {
      const choose = button(label(role), "secondary");
      controls.push(choose);
      choose.onclick = () => void submit(`${role}@demo.test`, "demo", destination);
      demo.append(choose);
    }
    card.append(el("hr"), el("h2", {}, "Try a demo role"), demo);
  }
  root.append(el("div", { class: "login-layout" }, card));
}

function validRoute(href: string): boolean {
  return /^#\/(?:events(?:\/[^/]+)?|orders(?:\/[^/]+)?|organizer(?:\/[^/]+)?|admin(?:\/users)?)$/.test(href);
}

function permitted(path: string, account: User): boolean {
  if (path === "admin") return account.role === "admin";
  if (path === "orders" || path === "organizer") return account.role !== "viewer";
  return true;
}

async function renderRoute(): Promise<void> {
  if (!ready) return;
  const hash = location.hash || "#/events";
  if (hash === "#/login" && user) {
    navigate("#/events");
    return;
  }
  if (hash !== "#/login" && !user) {
    if (validRoute(hash)) loginDestination = hash;
    navigate("#/login");
    return;
  }
  route.abort();
  route = new AbortController();
  const { signal } = route;
  const root = el("div", { class: "page" });
  root.addEventListener("refresh-route", () => void renderRoute());
  main.replaceChildren(appNotice, root);
  headerFor(hash);
  const [, part = "", identifier] = hash.split("/");
  const loading = notice();
  try {
    if (part === "login") loginPage(root, signal);
    else if (!user || !permitted(part, user)) {
      root.append(heading("Access restricted"), panel(el("p", {}, "Your role does not have access to this page."),
        link("Browse events", "#/events")));
    } else if (!validRoute(hash)) {
      root.append(heading("Page not found"), panel(link("Browse events", "#/events")));
    } else {
      const id = identifier && decodeURIComponent(identifier);
      if ((part === "events" || part === "orders" || part === "organizer") && id) {
        show(loading, "Loading…");
        root.append(loading);
        if (part === "events") await eventPage(root, id, user, signal);
        if (part === "orders") await orderPage(root, id, signal);
        if (part === "organizer") await managePage(root, id, user, signal);
      } else if (part === "events") eventsPage(root, signal);
      else if (part === "orders") ordersPage(root, signal);
      else if (part === "organizer") organizerPage(root, user, signal);
      else if (identifier === "users") usersPage(root, user, signal);
      else adminPage(root, signal);
    }
  } catch (error) {
    if (!signal.aborted) {
      const retry = button("Retry", "secondary");
      retry.onclick = () => void renderRoute();
      root.append(heading("Page unavailable"), panel(el("p", { class: "problem", role: "alert" },
        errorText(error)), el("div", { class: "actions" }, retry, link("Browse events", "#/events"))));
    }
  } finally {
    loading.remove();
    if (!signal.aborted) {
      const title = root.querySelector("h1");
      if (title) document.title = `${title.textContent} | Ticketing`;
      if (!root.contains(document.activeElement)) title?.focus({ preventScroll: true });
    }
  }
}

onSessionExpired(() => {
  if (!user) return;
  user = undefined;
  route.abort();
  show(appNotice, "Your session ended. Sign in again to continue.", true);
  void renderRoute();
});
window.addEventListener("hashchange", () => {
  window.scrollTo(0, 0);
  void renderRoute();
});

async function restore(): Promise<void> {
  show(appNotice, "Restoring session…");
  headerFor(location.hash);
  if (!appNotice.isConnected) main.append(appNotice);
  try {
    const session = await api.me(route.signal);
    user = session.user;
    if (mockMode && !location.hash) history.replaceState(null, "", "#/events/1");
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) {
      show(appNotice, errorText(error), true);
    } else if (mockMode && !location.hash) {
      const session = await api.login("user@demo.test", "demo", route.signal);
      user = session.user;
      history.replaceState(null, "", "#/events/1");
    }
  } finally {
    if (!appNotice.classList.contains("problem")) show(appNotice, "");
    ready = true;
    void renderRoute();
  }
}
void restore();
