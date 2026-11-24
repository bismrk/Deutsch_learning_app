import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UserProfile } from "../api/types";
import { THEME_STORAGE_KEY, ThemeProvider } from "../theme/ThemeProvider";
import { CUSTOM_TIMEZONE_OPTION, SettingsPage } from "./SettingsPage";

function makeJsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const DEFAULT_PROFILE: UserProfile = {
  telegram_id: 42,
  level: "A2",
  exercises_per_set: 7,
  theme: "light",
  sheets_url: "https://docs.google.com/spreadsheets/d/abc123/edit",
  reminder_hour: 18,
  timezone: "Europe/Kyiv",
  daily_llm_calls: 3,
  created_at: "2026-11-24T10:00:00Z",
};

function renderSettingsPage() {
  return render(
    <ThemeProvider>
      <SettingsPage />
    </ThemeProvider>,
  );
}

describe("SettingsPage", () => {
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

  it("renders loading state, handles load error with retry, and populates profile fields", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(makeJsonResponse({ detail: "Unauthorized" }, 401))
      .mockResolvedValueOnce(makeJsonResponse(DEFAULT_PROFILE));
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsPage();

    expect(screen.getByRole("status").getAttribute("aria-busy")).toBe("true");
    expect(screen.getByText("Завантажуємо налаштування профілю…")).toBeTruthy();

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });
    expect(screen.getByText(/Помилка авторизації Telegram/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Спробувати ще раз" }));

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Параметри навчання та нагадувань" }),
      ).toBeTruthy();
    });

    expect(
      screen.getByRole("button", { name: "A2" }).getAttribute("aria-pressed"),
    ).toBe("true");
    expect(
      (
        screen.getByLabelText(
          "Кількість вправ у сеті (1–30)",
        ) as HTMLInputElement
      ).value,
    ).toBe("7");
    expect(
      (
        screen.getByLabelText(
          "Посилання на Google Sheets",
        ) as HTMLInputElement
      ).value,
    ).toBe("https://docs.google.com/spreadsheets/d/abc123/edit");
    expect(
      (
        screen.getByLabelText("Година нагадування (0–23)") as HTMLInputElement
      ).value,
    ).toBe("18");
    expect(
      (screen.getByLabelText("Часовий пояс") as HTMLSelectElement).value,
    ).toBe("Europe/Kyiv");
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("displays an arbitrary saved non-preset IANA timezone cleanly", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      makeJsonResponse({
        ...DEFAULT_PROFILE,
        timezone: "Asia/Tokyo",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsPage();

    await waitFor(() => {
      expect(screen.getByLabelText("Часовий пояс")).toBeTruthy();
    });

    expect(
      (screen.getByLabelText("Часовий пояс") as HTMLSelectElement).value,
    ).toBe(CUSTOM_TIMEZONE_OPTION);
    expect(
      (
        screen.getByLabelText(
          "Власний часовий пояс (IANA)",
        ) as HTMLInputElement
      ).value,
    ).toBe("Asia/Tokyo");
  });

  it("switches Auto / Темна / Світла immediately in ThemeProvider, localStorage, Telegram SDK, and syncs via PATCH", async () => {
    const setHeaderColor = vi.fn();
    const setBackgroundColor = vi.fn();
    window.Telegram = {
      WebApp: {
        initData: "user=%7B%22id%22%3A42%7D&auth_date=1700000000&hash=ok",
        colorScheme: "dark",
        ready: vi.fn(),
        expand: vi.fn(),
        setHeaderColor,
        setBackgroundColor,
        onEvent: vi.fn(),
        offEvent: vi.fn(),
      },
    };

    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url === "/api/users/me") {
        return makeJsonResponse(DEFAULT_PROFILE);
      }
      if (url === "/api/users/settings" && init?.method === "PATCH") {
        const body = JSON.parse(String(init.body)) as { theme: UserProfile["theme"] };
        return makeJsonResponse({ ...DEFAULT_PROFILE, theme: body.theme });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsPage();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Темна" })).toBeTruthy();
    });

    // Switch to Темна
    fireEvent.click(screen.getByRole("button", { name: "Темна" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(setHeaderColor).toHaveBeenLastCalledWith("#000000");
    expect(setBackgroundColor).toHaveBeenLastCalledWith("#000000");

    await waitFor(() => {
      expect(screen.getByText("Тему оформлення збережено.")).toBeTruthy();
    });

    // Switch to Світла
    fireEvent.click(screen.getByRole("button", { name: "Світла" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(setHeaderColor).toHaveBeenLastCalledWith("#FFFFFF");
    expect(setBackgroundColor).toHaveBeenLastCalledWith("#FFFFFF");

    // Switch to Auto (resolves to Telegram colorScheme = "dark")
    fireEvent.click(screen.getByRole("button", { name: "Auto" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("system");
    expect(setHeaderColor).toHaveBeenLastCalledWith("#000000");
    expect(setBackgroundColor).toHaveBeenLastCalledWith("#000000");

    await waitFor(() => {
      const patchBodies = fetchMock.mock.calls
        .filter(([url, init]) => url === "/api/users/settings" && init?.method === "PATCH")
        .map(([, init]) => JSON.parse(String(init?.body)));
      expect(patchBodies).toEqual([
        { theme: "dark" },
        { theme: "light" },
        { theme: "system" },
      ]);
    });
  });

  it("saves updated settings including custom IANA timezone and clearing sheets_url to null", async () => {
    const patchBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url === "/api/users/me") {
        return makeJsonResponse(DEFAULT_PROFILE);
      }
      if (url === "/api/users/settings" && init?.method === "PATCH") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        patchBodies.push(body);
        return makeJsonResponse({
          ...DEFAULT_PROFILE,
          ...body,
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsPage();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "B1" })).toBeTruthy();
    });

    // Change level to B1, exercises to 12, reminder hour to 9, custom timezone America/Chicago, clear sheets_url
    fireEvent.click(screen.getByRole("button", { name: "B1" }));
    fireEvent.change(
      screen.getByLabelText("Кількість вправ у сеті (1–30)"),
      { target: { value: "12" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Очистити посилання Google Sheets" }),
    );
    expect(
      (
        screen.getByLabelText(
          "Посилання на Google Sheets",
        ) as HTMLInputElement
      ).value,
    ).toBe("");

    fireEvent.change(screen.getByLabelText("Година нагадування (0–23)"), {
      target: { value: "9" },
    });
    fireEvent.change(screen.getByLabelText("Часовий пояс"), {
      target: { value: CUSTOM_TIMEZONE_OPTION },
    });
    fireEvent.change(screen.getByLabelText("Власний часовий пояс (IANA)"), {
      target: { value: "America/Chicago" },
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Зберегти налаштування" }),
    );

    await waitFor(() => {
      expect(screen.getByText("Налаштування збережено.")).toBeTruthy();
    });

    expect(patchBodies).toEqual([
      {
        level: "B1",
        exercises_per_set: 12,
        sheets_url: null,
        reminder_hour: 9,
        timezone: "America/Chicago",
      },
    ]);
  });

  it("validates exercises_per_set (1-30) and reminder_hour (0-23) client-side without losing user inputs", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(makeJsonResponse(DEFAULT_PROFILE));
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsPage();

    await waitFor(() => {
      expect(
        screen.getByLabelText("Кількість вправ у сеті (1–30)"),
      ).toBeTruthy();
    });

    const exercisesInput = screen.getByLabelText(
      "Кількість вправ у сеті (1–30)",
    ) as HTMLInputElement;
    const reminderInput = screen.getByLabelText(
      "Година нагадування (0–23)",
    ) as HTMLInputElement;
    const saveButton = screen.getByRole("button", {
      name: "Зберегти налаштування",
    });

    // Invalid exercises_per_set = 31
    fireEvent.change(exercisesInput, { target: { value: "31" } });
    fireEvent.click(saveButton);

    expect(screen.getByRole("alert").textContent).toContain(
      "Кількість вправ у сеті має бути цілим числом від 1 до 30.",
    );
    expect(exercisesInput.value).toBe("31");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Fix exercises, set invalid reminder_hour = 24
    fireEvent.change(exercisesInput, { target: { value: "10" } });
    fireEvent.change(reminderInput, { target: { value: "24" } });
    fireEvent.click(saveButton);

    expect(screen.getByRole("alert").textContent).toContain(
      "Година нагадування має бути цілим числом від 0 до 23.",
    );
    expect(reminderInput.value).toBe("24");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows 422 and network errors on save without resetting unsaved form inputs", async () => {
    let patchCount = 0;
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url === "/api/users/me") {
        return makeJsonResponse(DEFAULT_PROFILE);
      }
      if (url === "/api/users/settings" && init?.method === "PATCH") {
        patchCount += 1;
        if (patchCount === 1) {
          return makeJsonResponse(
            { detail: "Посилання має вести на docs.google.com/spreadsheets/." },
            422,
          );
        }
        throw new TypeError("Failed to fetch");
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsPage();

    await waitFor(() => {
      expect(screen.getByLabelText("Посилання на Google Sheets")).toBeTruthy();
    });

    const sheetsInput = screen.getByLabelText(
      "Посилання на Google Sheets",
    ) as HTMLInputElement;
    const exercisesInput = screen.getByLabelText(
      "Кількість вправ у сеті (1–30)",
    ) as HTMLInputElement;

    fireEvent.click(screen.getByRole("button", { name: "B1" }));
    fireEvent.change(exercisesInput, { target: { value: "15" } });
    fireEvent.change(sheetsInput, {
      target: { value: "https://example.com/not-sheets" },
    });

    // 422 response
    fireEvent.click(
      screen.getByRole("button", { name: "Зберегти налаштування" }),
    );

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain(
        "Посилання має вести на docs.google.com/spreadsheets/.",
      );
    });
    // All user-entered values are preserved!
    expect(
      screen.getByRole("button", { name: "B1" }).getAttribute("aria-pressed"),
    ).toBe("true");
    expect(exercisesInput.value).toBe("15");
    expect(sheetsInput.value).toBe("https://example.com/not-sheets");

    // Network failure
    fireEvent.click(
      screen.getByRole("button", { name: "Зберегти налаштування" }),
    );

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toMatch(
        /Немає з'єднання із сервером/,
      );
    });
    expect(exercisesInput.value).toBe("15");
    expect(sheetsInput.value).toBe("https://example.com/not-sheets");
  });
});
