import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import indexHtml from "../../index.html?raw";
import { THEME_STORAGE_KEY } from "./ThemeProvider";

function extractAntiFoucScript(html: string): string {
  const match = html.match(/<script>\s*([\s\S]*?)\s*<\/script>/);
  if (!match?.[1]) {
    throw new Error("Inline anti-FOUC script not found in miniapp/index.html");
  }
  return match[1];
}

function runAntiFoucScript(): void {
  const scriptBody = extractAntiFoucScript(indexHtml);
  new Function(scriptBody)();
}

describe("index.html anti-FOUC bootstrap script", () => {
  beforeEach(() => {
    window.localStorage.clear();
    delete window.Telegram;
    delete document.documentElement.dataset.theme;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.localStorage.clear();
    delete window.Telegram;
    delete document.documentElement.dataset.theme;
  });

  it("includes Telegram WebApp SDK script tag before the inline script", () => {
    const sdkIdx = indexHtml.indexOf(
      'https://telegram.org/js/telegram-web-app.js',
    );
    const inlineIdx = indexHtml.indexOf("deutsch_miniapp_theme");
    const appIdx = indexHtml.indexOf("/src/main.tsx");

    expect(sdkIdx).toBeGreaterThan(-1);
    expect(inlineIdx).toBeGreaterThan(sdkIdx);
    expect(appIdx).toBeGreaterThan(inlineIdx);
  });

  it("applies stored dark mode and syncs Telegram chrome colors before React mounts", () => {
    const setHeaderColor = vi.fn();
    const setBackgroundColor = vi.fn();
    window.Telegram = {
      WebApp: {
        colorScheme: "light",
        setHeaderColor,
        setBackgroundColor,
      },
    };
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");

    runAntiFoucScript();

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(setHeaderColor).toHaveBeenCalledWith("#000000");
    expect(setBackgroundColor).toHaveBeenCalledWith("#000000");
  });

  it("falls back to Telegram.WebApp.colorScheme on invalid localStorage value", () => {
    const setHeaderColor = vi.fn();
    const setBackgroundColor = vi.fn();
    window.Telegram = {
      WebApp: {
        colorScheme: "dark",
        setHeaderColor,
        setBackgroundColor,
      },
    };
    window.localStorage.setItem(THEME_STORAGE_KEY, "broken-value");

    runAntiFoucScript();

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(setHeaderColor).toHaveBeenCalledWith("#000000");
    expect(setBackgroundColor).toHaveBeenCalledWith("#000000");
  });

  it("handles localStorage SecurityError and falls back to prefers-color-scheme when outside Telegram", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Blocked", "SecurityError");
    });
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({ matches: true })),
    );

    runAntiFoucScript();

    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
