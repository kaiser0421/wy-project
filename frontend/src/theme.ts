export function resolveTheme(stored: string | null | undefined, systemDark: boolean): "light" | "dark" {
  if (stored === "light" || stored === "dark") return stored;
  return systemDark ? "dark" : "light";
}

export function setTheme(preference: string): void {
  const root = document.documentElement;
  if (preference === "system") delete root.dataset.theme;
  else root.dataset.theme = preference;
  try {
    localStorage.setItem("ticketing-theme", preference);
  } catch {
    // The preference still applies for this page.
  }
  root.dispatchEvent(new Event("themechange"));
}

export function onThemeChange(redraw: () => void, signal: AbortSignal): void {
  const root = document.documentElement;
  const system = matchMedia("(prefers-color-scheme: dark)");
  let theme = resolveTheme(root.dataset.theme, system.matches);
  const check = () => {
    const next = resolveTheme(root.dataset.theme, system.matches);
    if (next === theme) return;
    theme = next;
    redraw();
  };
  root.addEventListener("themechange", check, { signal });
  system.addEventListener("change", check, { signal });
}
