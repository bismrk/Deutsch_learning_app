import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TelegramWebApp } from "../telegram";
import {
  THEME_STORAGE_KEY,
  ThemeProvider,
  useTheme,
} from "./ThemeProvider";

interface MockTelegramController {
  webApp: TelegramWebApp;
  ready: ReturnType<typeof vi.fn>;
  expand: ReturnType<typeof vi.fn>;
  setHeaderColor: ReturnType<typeof vi.fn>;
  setBackgroundColor: ReturnType<typeof vi.fn>;
  onEvent: ReturnType<typeof vi.fn>;
  offEvent: ReturnType<typeof vi.fn>;
  emit: (eventType: string) => void;
  setColorScheme: (scheme: string) => void;
}

function createMockTelegram(initialScheme = "dark"): MockTelegramController {
  const listeners = new Map<string, Set<() => void>>();

  const ready = vi.fn();
  const expand = vi.fn();
  const setHeaderColor = vi.fn();
  const setBackgroundColor = vi.fn();

  const onEvent = vi.fn((eventType: string, callback: () => void) => {
    const set = listeners.get(eventType) ?? new Set<() => void>();
    set.add(callback);
    listeners.set(eventType, set);
  });

  const offEvent = vi.fn((eventType: string, callback: () => void) => {
    listeners.get(eventType)?.delete(callback);
  });

  const webApp: TelegramWebApp = {
    initData: "query_id=test&auth_date=1700000000&hash=abc",
    colorScheme: initialScheme,
    ready,
    expand,
    setHeaderColor,
    setBackgroundColor,
    onEvent,
    offEvent,
  };

  window.Telegram = { WebApp: webApp };

  return {
    webApp,
    ready,
    expand,
    setHeaderColor,
    setBackgroundColor,
    onEvent,
    offEvent,
    emit(eventType: string) {
      listeners.get(eventType)?.forEach((cb) => cb());
    },
    setColorScheme(scheme: string) {
      webApp.colorScheme = scheme;
    },
  };
}

function mockMatchMedia(prefersDark: boolean) {
  const changeListeners = new Set<() => void>();
  let matches = prefersDark;

  const mediaQueryList = {
    get matches() {
      return matches;
    },
    media: "(prefers-color-scheme: dark)",
    onchange: null,
    addEventListener: vi.fn((_event: string, listener: () => void) => {
      changeListeners.add(listener);
    }),
    removeEventListener: vi.fn((_event: string, listener: () => void) => {
      changeListeners.delete(listener);
    }),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  };

  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => mediaQueryList),
  );

  return {
    mediaQueryList,
    setMatches(next: boolean) {
      matches = next;
      changeListeners.forEach((cb) => cb());
    },
  };
}

function ThemeProbe() {
  const { themeMode, resolvedTheme, setThemeMode } = useTheme();
  return (
    <div>
      <span data-testid="mode">{themeMode}</span>
      <span data-testid="resolved">{resolvedTheme}</span>
      <button type="button" onClick={() => setThemeMode("dark")}>
        set-dark
      </button>
      <button type="button" onClick={() => setThemeMode("light")}>
        set-light
      </button>
      <button type="button" onClick={() => setThemeMode("system")}>
        set-system
      </button>
    </div>
  );
}

describe("ThemeProvider", () => {
  beforeEach(() => {
    window.localStorage.clear();
    delete window.Telegram;
    delete document.documentElement.dataset.theme;
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.localStorage.clear();
    delete window.Telegram;
    delete document.documentElement.dataset.theme;
  });

  it("switches dark, light, and system modes, updating data-theme, localStorage, and Telegram colors", () => {
    const tg = createMockTelegram("dark");

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    expect(tg.ready).toHaveBeenCalledTimes(1);
    expect(tg.expand).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("mode").textContent).toBe("system");
    expect(screen.getByTestId("resolved").textContent).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(tg.setHeaderColor).toHaveBeenLastCalledWith("#000000");
    expect(tg.setBackgroundColor).toHaveBeenLastCalledWith("#000000");

    fireEvent.click(screen.getByText("set-light"));
    expect(screen.getByTestId("mode").textContent).toBe("light");
    expect(screen.getByTestId("resolved").textContent).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(tg.setHeaderColor).toHaveBeenLastCalledWith("#FFFFFF");
    expect(tg.setBackgroundColor).toHaveBeenLastCalledWith("#FFFFFF");

    fireEvent.click(screen.getByText("set-dark"));
    expect(screen.getByTestId("mode").textContent).toBe("dark");
    expect(screen.getByTestId("resolved").textContent).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(tg.setHeaderColor).toHaveBeenLastCalledWith("#000000");
    expect(tg.setBackgroundColor).toHaveBeenLastCalledWith("#000000");

    tg.setColorScheme("light");
    fireEvent.click(screen.getByText("set-system"));
    expect(screen.getByTestId("mode").textContent).toBe("system");
    expect(screen.getByTestId("resolved").textContent).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("system");
    expect(tg.setHeaderColor).toHaveBeenLastCalledWith("#FFFFFF");
    expect(tg.setBackgroundColor).toHaveBeenLastCalledWith("#FFFFFF");
  });

  it("subscribes to Telegram themeChanged in system mode and unsubscribes on unmount", () => {
    const tg = createMockTelegram("dark");

    const { unmount } = render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    expect(tg.onEvent).toHaveBeenCalledWith("themeChanged", expect.any(Function));
    const registeredHandler = tg.onEvent.mock.calls[0]?.[1];

    act(() => {
      tg.setColorScheme("light");
      tg.emit("themeChanged");
    });

    expect(screen.getByTestId("resolved").textContent).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(tg.setHeaderColor).toHaveBeenLastCalledWith("#FFFFFF");
    expect(tg.setBackgroundColor).toHaveBeenLastCalledWith("#FFFFFF");

    unmount();

    expect(tg.offEvent).toHaveBeenCalledWith("themeChanged", registeredHandler);
  });

  it("ignores Telegram themeChanged events when manual dark or light mode is active", () => {
    const tg = createMockTelegram("dark");

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    fireEvent.click(screen.getByText("set-dark"));
    const headerCallsBefore = tg.setHeaderColor.mock.calls.length;

    act(() => {
      tg.setColorScheme("light");
      tg.emit("themeChanged");
    });

    expect(screen.getByTestId("mode").textContent).toBe("dark");
    expect(screen.getByTestId("resolved").textContent).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(tg.setHeaderColor).toHaveBeenCalledTimes(headerCallsBefore);

    fireEvent.click(screen.getByText("set-light"));
    const headerCallsLight = tg.setHeaderColor.mock.calls.length;

    act(() => {
      tg.setColorScheme("dark");
      tg.emit("themeChanged");
    });

    expect(screen.getByTestId("mode").textContent).toBe("light");
    expect(screen.getByTestId("resolved").textContent).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(tg.setHeaderColor).toHaveBeenCalledTimes(headerCallsLight);
  });

  it("falls back safely when localStorage contains a corrupted value", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "ultra-neon-corrupted");
    createMockTelegram("dark");

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    expect(screen.getByTestId("mode").textContent).toBe("system");
    expect(screen.getByTestId("resolved").textContent).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("does not crash when localStorage throws SecurityError on read and write", () => {
    const securityError = new DOMException("Storage disabled", "SecurityError");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw securityError;
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw securityError;
    });

    createMockTelegram("light");

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    expect(screen.getByTestId("mode").textContent).toBe("system");
    expect(screen.getByTestId("resolved").textContent).toBe("light");

    fireEvent.click(screen.getByText("set-dark"));
    expect(screen.getByTestId("mode").textContent).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("falls back to prefers-color-scheme and reacts to changes when window.Telegram is absent", () => {
    const media = mockMatchMedia(true);

    const { unmount } = render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    expect(screen.getByTestId("mode").textContent).toBe("system");
    expect(screen.getByTestId("resolved").textContent).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");

    act(() => {
      media.setMatches(false);
    });

    expect(screen.getByTestId("resolved").textContent).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");

    unmount();
    expect(media.mediaQueryList.removeEventListener).toHaveBeenCalledWith(
      "change",
      expect.any(Function),
    );
  });

  it("defaults to light when both window.Telegram and matchMedia are unavailable", () => {
    vi.stubGlobal("matchMedia", undefined);

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    expect(screen.getByTestId("mode").textContent).toBe("system");
    expect(screen.getByTestId("resolved").textContent).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});
