import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  getTelegramColorScheme,
  getTelegramWebApp,
  initTelegramWebApp,
  syncTelegramChromeColors,
} from "../telegram";

export type ThemeMode = "system" | "dark" | "light";
export type ResolvedTheme = "dark" | "light";

export const THEME_STORAGE_KEY = "deutsch_miniapp_theme";

export const THEME_BG_COLORS: Record<ResolvedTheme, string> = {
  dark: "#000000",
  light: "#FFFFFF",
};

export interface ThemeContextValue {
  themeMode: ThemeMode;
  resolvedTheme: ResolvedTheme;
  setThemeMode: (mode: ThemeMode) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function isThemeMode(value: unknown): value is ThemeMode {
  return value === "system" || value === "dark" || value === "light";
}

export function readStoredThemeMode(): ThemeMode {
  if (typeof window === "undefined") {
    return "system";
  }
  try {
    const raw = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemeMode(raw) ? raw : "system";
  } catch {
    return "system";
  }
}

export function writeStoredThemeMode(mode: ThemeMode): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch {
    // Ignore SecurityError / QuotaExceededError in restricted WebViews.
  }
}

export function getSystemTheme(): ResolvedTheme {
  const tgScheme = getTelegramColorScheme();
  if (tgScheme !== null) {
    return tgScheme;
  }
  if (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  ) {
    return "dark";
  }
  return "light";
}

export function resolveTheme(mode: ThemeMode): ResolvedTheme {
  return mode === "system" ? getSystemTheme() : mode;
}

export function applyThemeToDomAndTelegram(resolved: ResolvedTheme): void {
  if (typeof document !== "undefined") {
    document.documentElement.dataset.theme = resolved;
  }
  syncTelegramChromeColors(THEME_BG_COLORS[resolved]);
}

export interface ThemeProviderProps {
  children: ReactNode;
}

export function ThemeProvider({ children }: ThemeProviderProps) {
  const [themeMode, setThemeModeState] = useState<ThemeMode>(() => readStoredThemeMode());
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(() => getSystemTheme());

  const themeModeRef = useRef<ThemeMode>(themeMode);
  themeModeRef.current = themeMode;

  const resolvedTheme: ResolvedTheme = themeMode === "system" ? systemTheme : themeMode;

  useEffect(() => {
    initTelegramWebApp();
  }, []);

  useLayoutEffect(() => {
    applyThemeToDomAndTelegram(resolvedTheme);
  }, [resolvedTheme]);

  useEffect(() => {
    const webApp = getTelegramWebApp();

    const handleTelegramThemeChanged = () => {
      if (themeModeRef.current !== "system") {
        return;
      }
      setSystemTheme(getSystemTheme());
    };

    if (webApp && typeof webApp.onEvent === "function") {
      try {
        webApp.onEvent("themeChanged", handleTelegramThemeChanged);
      } catch {
        // Ignore SDK subscription failures in unsupported environments.
      }
    }

    const mediaQuery =
      typeof window !== "undefined" && typeof window.matchMedia === "function"
        ? window.matchMedia("(prefers-color-scheme: dark)")
        : null;

    const handleMediaChange = () => {
      if (themeModeRef.current !== "system") {
        return;
      }
      setSystemTheme(getSystemTheme());
    };

    if (mediaQuery && typeof mediaQuery.addEventListener === "function") {
      mediaQuery.addEventListener("change", handleMediaChange);
    }

    return () => {
      if (webApp && typeof webApp.offEvent === "function") {
        try {
          webApp.offEvent("themeChanged", handleTelegramThemeChanged);
        } catch {
          // Ignore SDK unsubscribe failures.
        }
      }
      if (mediaQuery && typeof mediaQuery.removeEventListener === "function") {
        mediaQuery.removeEventListener("change", handleMediaChange);
      }
    };
  }, []);

  const setThemeMode = useCallback((nextMode: ThemeMode) => {
    if (!isThemeMode(nextMode)) {
      return;
    }
    writeStoredThemeMode(nextMode);
    if (nextMode === "system") {
      setSystemTheme(getSystemTheme());
    }
    setThemeModeState(nextMode);
  }, []);

  const value = useMemo<ThemeContextValue>(
    () => ({
      themeMode,
      resolvedTheme,
      setThemeMode,
    }),
    [themeMode, resolvedTheme, setThemeMode],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (ctx === null) {
    throw new Error("useTheme має викликатися всередині ThemeProvider.");
  }
  return ctx;
}
