import { useCallback, useEffect, useRef, useState } from "react";

import { apiClient } from "./api/client";
import { BottomNav, type AppTab } from "./components/BottomNav";
import { DictionaryPage } from "./pages/DictionaryPage";
import { SettingsPage } from "./pages/SettingsPage";
import { StatsPage } from "./pages/StatsPage";
import { getTelegramInitData } from "./telegram";
import { ThemeProvider, useTheme } from "./theme/ThemeProvider";

const TAB_SUBTITLES: Record<AppTab, string> = {
  stats: "Прогрес за шкалою B1 та рекомендації для повторення",
  dictionary: "Особистий словник, імпорт та синхронізація слів",
  settings: "Оформлення, параметри навчання та щоденні нагадування",
};

function AppShell() {
  const { setThemeMode } = useTheme();
  const [activeTab, setActiveTab] = useState<AppTab>("stats");
  const userChangedThemeRef = useRef(false);
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

  const handleUserThemeChange = useCallback(() => {
    userChangedThemeRef.current = true;
  }, []);

  return (
    <div className="app-layout">
      <main className="app-shell">
        <header className="app-header">
          <h1 className="app-title">Тренажер німецької</h1>
          <p className="app-subtitle">{TAB_SUBTITLES[activeTab]}</p>
        </header>

        {activeTab === "stats" ? <StatsPage /> : null}
        {activeTab === "dictionary" ? <DictionaryPage /> : null}
        {activeTab === "settings" ? (
          <SettingsPage onUserThemeChange={handleUserThemeChange} />
        ) : null}
      </main>

      <BottomNav activeTab={activeTab} onSelectTab={setActiveTab} />
    </div>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <AppShell />
    </ThemeProvider>
  );
}
