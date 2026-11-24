import { useEffect, useRef, useState } from "react";

import { apiClient, ApiError } from "./api/client";
import { getTelegramInitData } from "./telegram";
import { ThemeProvider, useTheme, type ThemeMode } from "./theme/ThemeProvider";

const THEME_OPTIONS: ReadonlyArray<{ value: ThemeMode; label: string }> = [
  { value: "system", label: "Авто" },
  { value: "dark", label: "Темна" },
  { value: "light", label: "Світла" },
];

const RESOLVED_THEME_LABELS: Record<"dark" | "light", string> = {
  dark: "темна",
  light: "світла",
};

function AppShell() {
  const { themeMode, resolvedTheme, setThemeMode } = useTheme();
  const [syncMessage, setSyncMessage] = useState<string | null>(null);
  const userChangedThemeRef = useRef(false);
  const syncRequestIdRef = useRef(0);
  const hasTelegramAuth = getTelegramInitData() !== null;

  useEffect(() => {
    if (!hasTelegramAuth) {
      return;
    }

    let cancelled = false;
    apiClient
      .getMe()
      .then((profile) => {
        if (!cancelled && !userChangedThemeRef.current) {
          setThemeMode(profile.theme);
        }
      })
      .catch(() => {
        // Keep cached/local theme when backend is unavailable.
      });

    return () => {
      cancelled = true;
    };
  }, [hasTelegramAuth, setThemeMode]);

  const handleSelectTheme = (nextMode: ThemeMode) => {
    userChangedThemeRef.current = true;
    setThemeMode(nextMode);

    if (!hasTelegramAuth) {
      setSyncMessage("Тему збережено локально на цьому пристрої.");
      return;
    }

    const requestId = ++syncRequestIdRef.current;
    setSyncMessage(null);
    void apiClient
      .updateSettings({ theme: nextMode })
      .then(() => {
        if (syncRequestIdRef.current === requestId) {
          setSyncMessage("Тему синхронізовано з профілем.");
        }
      })
      .catch((error: unknown) => {
        if (syncRequestIdRef.current !== requestId) {
          return;
        }
        if (error instanceof ApiError) {
          setSyncMessage(`Тему застосовано локально (${error.message})`);
        } else {
          setSyncMessage("Тему застосовано локально без синхронізації із сервером.");
        }
      });
  };

  return (
    <main className="app-shell">
      <header className="app-header">
        <h1 className="app-title">Тренажер німецької</h1>
        <p className="app-subtitle">
          Особистий кабінет для відстеження прогресу та словника
        </p>
      </header>

      <section className="surface-card" aria-labelledby="theme-section-title">
        <h2 id="theme-section-title" className="card-title">
          Оформлення
        </h2>
        <p className="card-description">
          Поточна палітра: {RESOLVED_THEME_LABELS[resolvedTheme]}. Оберіть зручний
          висококонтрастний режим або автоматичне підлаштування під Telegram.
        </p>

        <div
          className="theme-switcher"
          role="group"
          aria-label="Вибір теми оформлення"
        >
          {THEME_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className="theme-option"
              aria-pressed={themeMode === option.value}
              onClick={() => handleSelectTheme(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>

        {syncMessage ? (
          <p className="status-note" role="status">
            {syncMessage}
          </p>
        ) : !hasTelegramAuth ? (
          <p className="status-note">
            Режим попереднього перегляду поза Telegram — налаштування зберігаються
            локально.
          </p>
        ) : null}
      </section>
    </main>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <AppShell />
    </ThemeProvider>
  );
}
