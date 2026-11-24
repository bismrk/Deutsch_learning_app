export type TelegramColorScheme = "dark" | "light";

export interface TelegramWebApp {
  initData?: string;
  colorScheme?: string;
  ready?: () => void;
  expand?: () => void;
  setHeaderColor?: (color: string) => void;
  setBackgroundColor?: (color: string) => void;
  onEvent?: (eventType: string, callback: () => void) => void;
  offEvent?: (eventType: string, callback: () => void) => void;
}

declare global {
  interface Window {
    Telegram?: {
      WebApp?: TelegramWebApp;
    };
  }
}

export function getTelegramWebApp(): TelegramWebApp | null {
  if (typeof window === "undefined") {
    return null;
  }
  return window.Telegram?.WebApp ?? null;
}

export function getTelegramInitData(): string | null {
  const raw = getTelegramWebApp()?.initData;
  if (typeof raw !== "string") {
    return null;
  }
  const trimmed = raw.trim();
  return trimmed.length > 0 ? trimmed : null;
}

export function getTelegramColorScheme(): TelegramColorScheme | null {
  const scheme = getTelegramWebApp()?.colorScheme;
  if (scheme === "dark" || scheme === "light") {
    return scheme;
  }
  return null;
}

export function initTelegramWebApp(): void {
  const webApp = getTelegramWebApp();
  if (!webApp) {
    return;
  }
  try {
    webApp.ready?.();
  } catch {
    // Ignore SDK errors in partial or unsupported WebView environments.
  }
  try {
    webApp.expand?.();
  } catch {
    // Ignore SDK errors in partial or unsupported WebView environments.
  }
}

export function syncTelegramChromeColors(bgColor: string): void {
  const webApp = getTelegramWebApp();
  if (!webApp) {
    return;
  }
  try {
    webApp.setHeaderColor?.(bgColor);
  } catch {
    // Ignore SDK errors in partial or unsupported WebView environments.
  }
  try {
    webApp.setBackgroundColor?.(bgColor);
  } catch {
    // Ignore SDK errors in partial or unsupported WebView environments.
  }
}
