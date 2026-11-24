import type { ReactNode } from "react";

export type AppTab = "stats" | "dictionary" | "settings";

export interface BottomNavProps {
  activeTab: AppTab;
  onSelectTab: (tab: AppTab) => void;
}

interface NavItemConfig {
  id: AppTab;
  label: string;
  icon: ReactNode;
}

const NAV_ITEMS: ReadonlyArray<NavItemConfig> = [
  {
    id: "stats",
    label: "Статистика",
    icon: (
      <svg
        className="bottom-nav-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M12 3l8 5.5v7L12 21l-8-5.5v-7L12 3z" />
        <path d="M12 12l8-3.5" />
        <path d="M12 12v9" />
        <path d="M12 12L4 8.5" />
      </svg>
    ),
  },
  {
    id: "dictionary",
    label: "Словник",
    icon: (
      <svg
        className="bottom-nav-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v18H6.5A2.5 2.5 0 0 1 4 18.5v-13z" />
        <path d="M4 18.5A2.5 2.5 0 0 1 6.5 16H20" />
        <path d="M9 7h7" />
        <path d="M9 11h5" />
      </svg>
    ),
  },
  {
    id: "settings",
    label: "Налаштування",
    icon: (
      <svg
        className="bottom-nav-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.08a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.08a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.08a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
      </svg>
    ),
  },
];

export function BottomNav({ activeTab, onSelectTab }: BottomNavProps) {
  return (
    <nav className="bottom-nav" aria-label="Основна навігація">
      <div className="bottom-nav-inner">
        {NAV_ITEMS.map((item) => {
          const isActive = item.id === activeTab;
          return (
            <button
              key={item.id}
              type="button"
              className="bottom-nav-item"
              aria-current={isActive ? "page" : undefined}
              onClick={() => onSelectTab(item.id)}
            >
              {item.icon}
              <span className="bottom-nav-label">{item.label}</span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}
