import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("renders StatsPage and lets user switch themes locally outside Telegram without calling profile endpoints", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url === "/api/stats/radar") {
        return makeJsonResponse(SAMPLE_RADAR_STATS);
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(screen.getByRole("heading", { name: "Тренажер німецької" })).toBeTruthy();

    await waitFor(() => {
      expect(screen.getByTestId("radar-canvas")).toBeTruthy();
    });

    const calledUrls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(calledUrls).toEqual(["/api/stats/radar"]);

    fireEvent.click(screen.getByRole("button", { name: "Темна" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(
      screen.getByText("Тему збережено локально на цьому пристрої."),
    ).toBeTruthy();
    expect(capturedRadarProps.calls.at(-1)?.data.datasets[0]?.borderColor).toBe(
      "#3B82F6",
    );
  });

  it("syncs theme changes with PATCH /api/users/settings and updates RadarChart when opened inside Telegram", async () => {
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

    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url === "/api/users/me") {
        return makeJsonResponse({
          telegram_id: 77,
          level: "A1",
          exercises_per_set: 5,
          theme: "light",
          sheets_url: null,
          reminder_hour: 19,
          timezone: "Europe/Kyiv",
          daily_llm_calls: 0,
          created_at: "2026-11-24T10:00:00Z",
        });
      }
      if (url === "/api/stats/radar") {
        return makeJsonResponse(SAMPLE_RADAR_STATS);
      }
      if (url === "/api/users/settings" && init?.method === "PATCH") {
        return makeJsonResponse({
          telegram_id: 77,
          level: "A1",
          exercises_per_set: 5,
          theme: "dark",
          sheets_url: null,
          reminder_hour: 19,
          timezone: "Europe/Kyiv",
          daily_llm_calls: 0,
          created_at: "2026-11-24T10:00:00Z",
        });
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

    fireEvent.click(screen.getByRole("button", { name: "Темна" }));

    await waitFor(() => {
      expect(screen.getByText("Тему синхронізовано з профілем.")).toBeTruthy();
    });

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(capturedRadarProps.calls.at(-1)?.data.datasets[0]?.borderColor).toBe(
      "#3B82F6",
    );

    const patchCall = fetchMock.mock.calls.find(
      ([url, init]) => url === "/api/users/settings" && init?.method === "PATCH",
    );
    expect(patchCall).toBeDefined();
    expect(patchCall?.[1]?.body).toBe(JSON.stringify({ theme: "dark" }));
  });

  it("keeps local theme applied and shows StatsPage error state without crashing when backend fails", async () => {
    window.Telegram = {
      WebApp: {
        initData: "user=%7B%22id%22%3A77%7D&auth_date=1700000000&hash=abc",
        colorScheme: "dark",
        ready: vi.fn(),
        expand: vi.fn(),
        setHeaderColor: vi.fn(),
        setBackgroundColor: vi.fn(),
        onEvent: vi.fn(),
        offEvent: vi.fn(),
      },
    };

    const fetchMock = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError("Network down"));
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Світла" }));

    expect(document.documentElement.dataset.theme).toBe("light");
    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(
        /Тему застосовано локально/,
      );
      expect(screen.getByRole("alert")).toBeTruthy();
    });
  });
});
