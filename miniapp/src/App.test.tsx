import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function makeJsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App", () => {
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

  it("renders cleanly outside Telegram without calling backend and lets user switch themes locally", () => {
    const fetchSpy = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchSpy);

    render(<App />);

    expect(screen.getByRole("heading", { name: "Тренажер німецької" })).toBeTruthy();
    expect(fetchSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Темна" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(
      screen.getByText("Тему збережено локально на цьому пристрої."),
    ).toBeTruthy();
  });

  it("syncs theme changes with PATCH /api/users/settings when opened inside Telegram", async () => {
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

    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        makeJsonResponse({
          telegram_id: 77,
          level: "A1",
          exercises_per_set: 5,
          theme: "light",
          sheets_url: null,
          reminder_hour: 19,
          timezone: "Europe/Kyiv",
          daily_llm_calls: 0,
          created_at: "2026-11-24T10:00:00Z",
        }),
      )
      .mockResolvedValueOnce(
        makeJsonResponse({
          telegram_id: 77,
          level: "A1",
          exercises_per_set: 5,
          theme: "dark",
          sheets_url: null,
          reminder_hour: 19,
          timezone: "Europe/Kyiv",
          daily_llm_calls: 0,
          created_at: "2026-11-24T10:00:00Z",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "Темна" }));

    await waitFor(() => {
      expect(screen.getByText("Тему синхронізовано з профілем.")).toBeTruthy();
    });

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/users/settings");
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe("PATCH");
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBe(
      JSON.stringify({ theme: "dark" }),
    );
  });

  it("keeps local theme applied without crashing when backend PATCH fails", async () => {
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
    });
  });
});
