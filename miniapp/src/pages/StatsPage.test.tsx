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

import type { RadarStatsResponse } from "../api/types";
import { ThemeProvider, useTheme } from "../theme/ThemeProvider";

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

import {
  buildYoutubeSearchUrl,
  formatAttemptsCount,
  StatsPage,
} from "./StatsPage";

function makeJsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const ZERO_STATS: RadarStatsResponse = {
  axes: [
    { axis: "vocab", label_ua: "Словник", score: 0, status: "active" },
    { axis: "grammar", label_ua: "Граматика", score: 0, status: "active" },
    { axis: "consistency", label_ua: "Стабільність", score: 0, status: "active" },
    { axis: "writing", label_ua: "Шрайбен", score: 0, status: "coming_soon" },
    { axis: "listening", label_ua: "Хьорен", score: 0, status: "coming_soon" },
  ],
  weak_topics: [],
};

const POPULATED_STATS: RadarStatsResponse = {
  axes: [
    { axis: "vocab", label_ua: "Словник", score: 72, status: "active" },
    { axis: "grammar", label_ua: "Граматика", score: 58, status: "active" },
    { axis: "consistency", label_ua: "Стабільність", score: 90, status: "active" },
    { axis: "writing", label_ua: "Шрайбен", score: 0, status: "coming_soon" },
    { axis: "listening", label_ua: "Хьорен", score: 0, status: "coming_soon" },
  ],
  weak_topics: [
    {
      grammar_topic: "Nebensätze mit weil",
      avg_grammar_score: 54.5,
      attempts_count: 3,
      youtube_topic: "Порядок слів у реченні з weil & obwohl (B1/A2 + übung?)",
    },
    {
      grammar_topic: "Dativ vs Akkusativ",
      avg_grammar_score: 68,
      attempts_count: 1,
      youtube_topic: "Dativ і Akkusativ: прийменники німецька",
    },
    {
      grammar_topic: "Perfekt mit sein/haben",
      avg_grammar_score: 74.2,
      attempts_count: 5,
      youtube_topic: null,
    },
  ],
};

const NO_WEAK_TOPICS_STATS: RadarStatsResponse = {
  axes: POPULATED_STATS.axes,
  weak_topics: [],
};

function renderStatsPage() {
  return render(
    <ThemeProvider>
      <StatsPage />
    </ThemeProvider>,
  );
}

function StatsWithThemeControls() {
  const { setThemeMode } = useTheme();
  return (
    <div>
      <button type="button" onClick={() => setThemeMode("dark")}>
        Темна
      </button>
      <button type="button" onClick={() => setThemeMode("light")}>
        Світла
      </button>
      <StatsPage />
    </div>
  );
}

describe("StatsPage", () => {
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

  it("renders loading state while GET /api/stats/radar is pending", () => {
    const pendingFetch = vi.fn<typeof fetch>(() => new Promise(() => {}));
    vi.stubGlobal("fetch", pendingFetch);

    renderStatsPage();

    const loadingSection = screen.getByRole("status");
    expect(loadingSection.getAttribute("aria-busy")).toBe("true");
    expect(screen.getByText("Завантажуємо вашу статистику…")).toBeTruthy();
    expect(pendingFetch).toHaveBeenCalledTimes(1);
    expect(pendingFetch.mock.calls[0]?.[0]).toBe("/api/stats/radar");
  });

  it("does not update state after unmount if request resolves or rejects late", async () => {
    let resolveFetch: ((response: Response) => void) | undefined;
    const deferredFetch = vi.fn<typeof fetch>(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", deferredFetch);

    const consoleErrorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});

    const { unmount } = renderStatsPage();
    expect(screen.getByText("Завантажуємо вашу статистику…")).toBeTruthy();

    unmount();

    resolveFetch?.(makeJsonResponse(POPULATED_STATS));
    await Promise.resolve();
    await Promise.resolve();

    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it("renders error state for 401, 500, and offline failures and retries on button click", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        makeJsonResponse({ detail: "Invalid initData" }, 401),
      )
      .mockResolvedValueOnce(
        makeJsonResponse({ detail: "Internal server error" }, 500),
      )
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(makeJsonResponse(POPULATED_STATS));
    vi.stubGlobal("fetch", fetchMock);

    renderStatsPage();

    // 401 unauthorized state
    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });
    expect(screen.getByText(/Помилка авторизації Telegram/)).toBeTruthy();
    const retryButton401 = screen.getByRole("button", {
      name: "Спробувати ще раз",
    });

    // Retry -> 500 server error state
    fireEvent.click(retryButton401);
    await waitFor(() => {
      expect(screen.getByText(/Помилка сервера/)).toBeTruthy();
    });

    // Retry -> network offline error state
    fireEvent.click(screen.getByRole("button", { name: "Спробувати ще раз" }));
    await waitFor(() => {
      expect(screen.getByText(/Немає з'єднання із сервером/)).toBeTruthy();
    });

    // Retry -> success
    fireEvent.click(screen.getByRole("button", { name: "Спробувати ще раз" }));
    await waitFor(() => {
      expect(screen.getByTestId("radar-canvas")).toBeTruthy();
    });

    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(screen.getByText("72")).toBeTruthy();
    expect(screen.getByText("Топ-3 слабкі теми за 14 днів")).toBeTruthy();
  });

  it("renders zero/empty stats state with bot training prompt and coming_soon badges", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(makeJsonResponse(ZERO_STATS));
    vi.stubGlobal("fetch", fetchMock);

    renderStatsPage();

    await waitFor(() => {
      expect(screen.getByTestId("radar-canvas")).toBeTruthy();
    });

    expect(
      screen.getByRole("heading", { name: "Поки що немає даних для статистики" }),
    ).toBeTruthy();
    expect(
      screen.getByText(/Пройдіть перше тренування в Telegram-боті/),
    ).toBeTruthy();
    expect(
      screen.queryByRole("heading", { name: "Топ-3 слабкі теми за 14 днів" }),
    ).toBeNull();

    const axesList = screen.getByRole("list", { name: "Показники за осями" });
    const axisCards = within(axesList).getAllByRole("listitem");
    expect(axisCards).toHaveLength(5);

    const comingSoonBadges = within(axesList).getAllByText("Скоро");
    expect(comingSoonBadges).toHaveLength(2);
  });

  it("renders positive weak-topics empty state when user has non-zero progress and weak_topics = []", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(makeJsonResponse(NO_WEAK_TOPICS_STATS));
    vi.stubGlobal("fetch", fetchMock);

    renderStatsPage();

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Топ-3 слабкі теми за 14 днів" }),
      ).toBeTruthy();
    });

    expect(
      screen.getByText(/За останні 14 днів слабких тем не виявлено/),
    ).toBeTruthy();
    expect(
      screen.queryByText("Поки що немає даних для статистики"),
    ).toBeNull();
  });

  it("renders populated axis cards, coming_soon badges, and properly encoded YouTube links", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(makeJsonResponse(POPULATED_STATS));
    vi.stubGlobal("fetch", fetchMock);

    renderStatsPage();

    await waitFor(() => {
      expect(screen.getByTestId("radar-canvas")).toBeTruthy();
    });

    // Active axis cards show numeric scores 0-100
    const axesList = screen.getByRole("list", { name: "Показники за осями" });
    const axisItems = within(axesList).getAllByRole("listitem");
    expect(axisItems).toHaveLength(5);

    const vocabCard = axisItems[0]!;
    expect(within(vocabCard).getByText("Словник")).toBeTruthy();
    expect(within(vocabCard).getByText("72")).toBeTruthy();
    expect(within(vocabCard).queryByText("Скоро")).toBeNull();

    const grammarCard = axisItems[1]!;
    expect(within(grammarCard).getByText("Граматика")).toBeTruthy();
    expect(within(grammarCard).getByText("58")).toBeTruthy();

    const consistencyCard = axisItems[2]!;
    expect(within(consistencyCard).getByText("Стабільність")).toBeTruthy();
    expect(within(consistencyCard).getByText("90")).toBeTruthy();

    // Coming soon axes show muted Скоро badge and score 0
    const writingCard = axisItems[3]!;
    expect(within(writingCard).getByText("Шрайбен")).toBeTruthy();
    expect(within(writingCard).getByText("Скоро")).toBeTruthy();
    expect(within(writingCard).getByText("0")).toBeTruthy();
    expect(writingCard.className).toContain("axis-card--coming-soon");

    const listeningCard = axisItems[4]!;
    expect(within(listeningCard).getByText("Хьорен")).toBeTruthy();
    expect(within(listeningCard).getByText("Скоро")).toBeTruthy();
    expect(within(listeningCard).getByText("0")).toBeTruthy();
    expect(listeningCard.className).toContain("axis-card--coming-soon");

    // Weak topics with Cyrillic, umlauts, special characters, and null youtube_topic fallback
    expect(screen.getByText("Nebensätze mit weil")).toBeTruthy();
    expect(screen.getByText(/Середній бал: 54\.5 \/ 100 · 3 спроби/)).toBeTruthy();
    expect(screen.getByText(/Середній бал: 68 \/ 100 · 1 спроба/)).toBeTruthy();
    expect(screen.getByText(/Середній бал: 74\.2 \/ 100 · 5 спроб/)).toBeTruthy();

    const links = screen.getAllByRole("link", { name: /YouTube/ });
    expect(links).toHaveLength(3);

    const expectedFirstUrl = `https://www.youtube.com/results?search_query=${encodeURIComponent(
      "Порядок слів у реченні з weil & obwohl (B1/A2 + übung?)",
    )}`;
    expect(links[0]!.getAttribute("href")).toBe(expectedFirstUrl);
    expect(links[0]!.getAttribute("target")).toBe("_blank");
    expect(links[0]!.getAttribute("rel")).toBe("noopener noreferrer");

    const expectedSecondUrl = `https://www.youtube.com/results?search_query=${encodeURIComponent(
      "Dativ і Akkusativ: прийменники німецька",
    )}`;
    expect(links[1]!.getAttribute("href")).toBe(expectedSecondUrl);

    // Fallback to grammar_topic when youtube_topic is null
    const expectedThirdUrl = `https://www.youtube.com/results?search_query=${encodeURIComponent(
      "Perfekt mit sein/haben",
    )}`;
    expect(links[2]!.getAttribute("href")).toBe(expectedThirdUrl);

    // Outside Telegram, clicking preserves native target="_blank" navigation
    const browserClickEvent = new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
    });
    expect(links[0]!.dispatchEvent(browserClickEvent)).toBe(true);
  });

  it("opens YouTube links via Telegram.WebApp.openLink inside Telegram and falls back cleanly outside Telegram", async () => {
    const openLinkMock = vi.fn();
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
        openLink: openLinkMock,
      } as typeof window.Telegram extends { WebApp?: infer W }
        ? W & { openLink?: (url: string) => void }
        : never,
    };

    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(makeJsonResponse(POPULATED_STATS));
    vi.stubGlobal("fetch", fetchMock);

    renderStatsPage();

    await waitFor(() => {
      expect(screen.getAllByRole("link", { name: /YouTube/ })).toHaveLength(3);
    });

    const firstLink = screen.getAllByRole("link", { name: /YouTube/ })[0]!;
    const clickEvent = new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
    });
    const notCanceled = firstLink.dispatchEvent(clickEvent);

    expect(notCanceled).toBe(false);
    expect(openLinkMock).toHaveBeenCalledTimes(1);
    expect(openLinkMock).toHaveBeenCalledWith(
      buildYoutubeSearchUrl(POPULATED_STATS.weak_topics[0]!),
    );
  });

  it("updates RadarChart color config dynamically when theme changes while StatsPage is mounted", async () => {
    window.Telegram = {
      WebApp: {
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
      .mockResolvedValueOnce(makeJsonResponse(POPULATED_STATS));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ThemeProvider>
        <StatsWithThemeControls />
      </ThemeProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("radar-canvas")).toBeTruthy();
    });

    const initialCall = capturedRadarProps.calls.at(-1)!;
    expect(initialCall.data.datasets[0]?.borderColor).toBe("#3B82F6");

    fireEvent.click(screen.getByRole("button", { name: "Світла" }));

    const lightCall = capturedRadarProps.calls.at(-1)!;
    expect(lightCall.data.datasets[0]?.borderColor).toBe("#2563EB");
    expect(
      (
        (lightCall.options.scales?.r as Record<string, unknown>)
          .pointLabels as Record<string, unknown>
      ).color,
    ).toBe("#000000");
  });

  it("formats Ukrainian pluralization for attempts_count accurately", () => {
    expect(formatAttemptsCount(1)).toBe("1 спроба");
    expect(formatAttemptsCount(2)).toBe("2 спроби");
    expect(formatAttemptsCount(4)).toBe("4 спроби");
    expect(formatAttemptsCount(5)).toBe("5 спроб");
    expect(formatAttemptsCount(11)).toBe("11 спроб");
    expect(formatAttemptsCount(14)).toBe("14 спроб");
    expect(formatAttemptsCount(21)).toBe("21 спроба");
    expect(formatAttemptsCount(23)).toBe("23 спроби");
  });
});
