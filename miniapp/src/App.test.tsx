import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ChartData, ChartOptions } from "chart.js";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const capturedRadarProps = vi.hoisted(() => ({
  calls: [] as Array<{
    data: ChartData<"radar">;
    options: ChartOptions<"radar">;
  }>,
}));

vi.mock("react-chartjs-2", () => ({
  Radar: (props: {
    data: ChartData<"radar">;
    options: ChartOptions<"radar">;
  }) => {
    capturedRadarProps.calls.push({
      data: props.data,
      options: props.options,
    });
    return <canvas data-testid="radar-canvas" />;
  },
}));

import App from "./App";

const SAMPLE_RADAR_STATS = {
  axes: [
    { axis: "vocab", label_ua: "Словник", score: 60, status: "active" },
    { axis: "grammar", label_ua: "Граматика", score: 45, status: "active" },
    { axis: "consistency", label_ua: "Стабільність", score: 80, status: "active" },
    { axis: "writing", label_ua: "Шрайбен", score: 0, status: "coming_soon" },
    { axis: "listening", label_ua: "Хьорен", score: 0, status: "coming_soon" },
  ],
  weak_topics: [],
};

const SAMPLE_WORDS_LIST = {
  items: [
    {
      id: 10,
      word_de: "der Bahnhof",
      translation_ua: "вокзал",
      part_of_speech: "noun",
      source: "text",
      state: 0,
      stability: 0,
      difficulty: 0,
      due_at: "2026-11-24T10:00:00Z",
      reps: 0,
      lapses: 0,
    },
  ],
  total: 1,
};

const SAMPLE_PROFILE = {
  telegram_id: 77,
  level: "A1",
  exercises_per_set: 5,
  theme: "light",
  sheets_url: null,
  reminder_hour: 19,
  timezone: "Europe/Kyiv",
  daily_llm_calls: 0,
  created_at: "2026-11-24T10:00:00Z",
};

function makeJsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App", () => {
  beforeEach(() => {
    capturedRadarProps.calls.length = 0;
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

  it("switches across all 3 tabs (Статистика, Словник, Налаштування) and sets aria-current='page'", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url === "/api/stats/radar") {
        return makeJsonResponse(SAMPLE_RADAR_STATS);
      }
      if (url === "/api/words") {
        return makeJsonResponse(SAMPLE_WORDS_LIST);
      }
      if (url === "/api/users/me") {
        return makeJsonResponse(SAMPLE_PROFILE);
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(
      screen.getByRole("heading", { name: "Тренажер німецької" }),
    ).toBeTruthy();

    const nav = screen.getByRole("navigation", { name: "Основна навігація" });
    const statsTab = within(nav).getByRole("button", { name: "Статистика" });
    const dictionaryTab = within(nav).getByRole("button", { name: "Словник" });
    const settingsTab = within(nav).getByRole("button", {
      name: "Налаштування",
    });

    // Starts on Статистика
    expect(statsTab.getAttribute("aria-current")).toBe("page");
    expect(dictionaryTab.getAttribute("aria-current")).toBeNull();
    expect(settingsTab.getAttribute("aria-current")).toBeNull();

    await waitFor(() => {
      expect(screen.getByTestId("radar-canvas")).toBeTruthy();
    });

    // Switch to Словник
    fireEvent.click(dictionaryTab);
    expect(statsTab.getAttribute("aria-current")).toBeNull();
    expect(dictionaryTab.getAttribute("aria-current")).toBe("page");
    expect(settingsTab.getAttribute("aria-current")).toBeNull();

    await waitFor(() => {
      expect(screen.getByText("der Bahnhof")).toBeTruthy();
    });
    expect(screen.queryByTestId("radar-canvas")).toBeNull();

    // Switch to Налаштування
    fireEvent.click(settingsTab);
    expect(statsTab.getAttribute("aria-current")).toBeNull();
    expect(dictionaryTab.getAttribute("aria-current")).toBeNull();
    expect(settingsTab.getAttribute("aria-current")).toBe("page");

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Параметри навчання та нагадувань" }),
      ).toBeTruthy();
    });
    expect(screen.queryByText("der Bahnhof")).toBeNull();

    // Switch back to Статистика
    fireEvent.click(statsTab);
    expect(statsTab.getAttribute("aria-current")).toBe("page");

    await waitFor(() => {
      expect(screen.getByTestId("radar-canvas")).toBeTruthy();
    });
  });

  it("syncs theme from Settings tab via PATCH /api/users/settings and updates RadarChart palette", async () => {
    window.Telegram = {
      WebApp: {
        initData: "user=%7B%22id%22%3A77%7D&auth_date=1700000000&hash=abc",
        colorScheme: "light",
        ready: vi.fn(),
        expand: vi.fn(),
        setHeaderColor: vi.fn(),
        setBackgroundColor: vi.fn(),
        onEvent: vi.fn(),
        offEvent: vi.fn(),
      },
    };

    let savedTheme = "light";
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url === "/api/users/me") {
        return makeJsonResponse({ ...SAMPLE_PROFILE, theme: savedTheme });
      }
      if (url === "/api/stats/radar") {
        return makeJsonResponse(SAMPLE_RADAR_STATS);
      }
      if (url === "/api/users/settings" && init?.method === "PATCH") {
        const body = JSON.parse(String(init.body)) as { theme?: string };
        if (body.theme) {
          savedTheme = body.theme;
        }
        return makeJsonResponse({ ...SAMPLE_PROFILE, theme: savedTheme });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId("radar-canvas")).toBeTruthy();
    });
    expect(capturedRadarProps.calls.at(-1)?.data.datasets[0]?.borderColor).toBe(
      "#2563EB",
    );

    const nav = screen.getByRole("navigation", { name: "Основна навігація" });
    fireEvent.click(within(nav).getByRole("button", { name: "Налаштування" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Темна" })).toBeTruthy();
    });

    fireEvent.click(screen.getByRole("button", { name: "Темна" }));

    await waitFor(() => {
      expect(screen.getByText("Тему оформлення збережено.")).toBeTruthy();
    });
    expect(document.documentElement.dataset.theme).toBe("dark");

    const patchCall = fetchMock.mock.calls.find(
      ([url, init]) => url === "/api/users/settings" && init?.method === "PATCH",
    );
    expect(patchCall?.[1]?.body).toBe(JSON.stringify({ theme: "dark" }));

    // Return to Stats -> RadarChart renders with dark theme palette
    fireEvent.click(within(nav).getByRole("button", { name: "Статистика" }));

    await waitFor(() => {
      expect(screen.getByTestId("radar-canvas")).toBeTruthy();
    });
    expect(capturedRadarProps.calls.at(-1)?.data.datasets[0]?.borderColor).toBe(
      "#3B82F6",
    );
  });

  it("handles backend failures gracefully across tabs without crashing", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError("Network down"));
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });

    const nav = screen.getByRole("navigation", { name: "Основна навігація" });
    fireEvent.click(within(nav).getByRole("button", { name: "Словник" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });

    fireEvent.click(within(nav).getByRole("button", { name: "Налаштування" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });
  });
});
