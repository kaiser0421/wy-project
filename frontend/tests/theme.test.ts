import { afterEach, describe, expect, it, vi } from "vitest";
import { onThemeChange, resolveTheme, setTheme } from "../src/theme";

afterEach(() => vi.unstubAllGlobals());

describe("resolveTheme", () => {
  it("uses a stored light or dark override even when the system differs", () => {
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });

  it.each([null, undefined, "system", "invalid"])("follows the system for %s", stored => {
    expect(resolveTheme(stored, false)).toBe("light");
    expect(resolveTheme(stored, true)).toBe("dark");
  });
});

describe("theme changes", () => {
  function stubBrowser() {
    const root = Object.assign(new EventTarget(), { dataset: {} as DOMStringMap });
    const system = Object.assign(new EventTarget(), { matches: false });
    const setItem = vi.fn();
    vi.stubGlobal("document", { documentElement: root });
    vi.stubGlobal("matchMedia", () => system);
    vi.stubGlobal("localStorage", { setItem });
    return { root, system, setItem };
  }

  it("notifies active renderers when the effective theme changes", () => {
    const { root, system, setItem } = stubBrowser();
    const route = new AbortController();
    const redraw = vi.fn();
    onThemeChange(redraw, route.signal);
    setTheme("dark");
    expect(root.dataset.theme).toBe("dark");
    expect(setItem).toHaveBeenLastCalledWith("ticketing-theme", "dark");
    expect(redraw).toHaveBeenCalledTimes(1);

    system.matches = true;
    system.dispatchEvent(new Event("change"));
    setTheme("system");
    expect(root.dataset.theme).toBeUndefined();
    expect(redraw).toHaveBeenCalledTimes(1);

    system.matches = false;
    system.dispatchEvent(new Event("change"));
    expect(redraw).toHaveBeenCalledTimes(2);
    route.abort();
    setTheme("dark");
    expect(redraw).toHaveBeenCalledTimes(2);
  });

  it("still applies a choice when storage rejects writes", () => {
    const { root, setItem } = stubBrowser();
    setItem.mockImplementation(() => { throw new Error("Storage unavailable"); });
    setTheme("dark");
    expect(root.dataset.theme).toBe("dark");
  });
});
