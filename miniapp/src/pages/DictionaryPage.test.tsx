import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WordItem, WordListResponse } from "../api/types";
import {
  DictionaryPage,
  formatWordsCount,
  getFsrsBadge,
} from "./DictionaryPage";

function makeJsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeNoContentResponse(): Response {
  return new Response(null, { status: 204 });
}

const SAMPLE_WORDS: WordItem[] = [
  {
    id: 1,
    word_de: "der Zug",
    translation_ua: "потяг",
    part_of_speech: "noun",
    source: "text",
    state: 0,
    stability: 0,
    difficulty: 0,
    due_at: "2020-01-01T00:00:00Z",
    reps: 0,
    lapses: 0,
  },
  {
    id: 2,
    word_de: "die Verspätung",
    translation_ua: "затримка",
    part_of_speech: "noun",
    source: "sheets",
    state: 2,
    stability: 5.5,
    difficulty: 4.2,
    due_at: "2020-01-01T00:00:00Z",
    reps: 3,
    lapses: 1,
  },
  {
    id: 3,
    word_de: "das Treffen",
    translation_ua: "зустріч",
    part_of_speech: "noun",
    source: "text",
    state: 2,
    stability: 30,
    difficulty: 3.1,
    due_at: "2099-01-01T00:00:00Z",
    reps: 6,
    lapses: 0,
  },
  {
    id: 4,
    word_de: "sich beeilen",
    translation_ua: "поспішати",
    part_of_speech: "verb",
    source: "text",
    state: 1,
    stability: 1.2,
    difficulty: 5.0,
    due_at: "2099-01-01T00:00:00Z",
    reps: 1,
    lapses: 0,
  },
];

const SAMPLE_LIST: WordListResponse = {
  items: SAMPLE_WORDS,
  total: SAMPLE_WORDS.length,
};

describe("DictionaryPage", () => {
  beforeEach(() => {
    delete window.Telegram;
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    delete window.Telegram;
  });

  it("classifies FSRS badges deterministically for new, due, learning, and mastered cards", () => {
    const fixedNowMs = Date.parse("2026-11-24T12:00:00Z");

    expect(getFsrsBadge(SAMPLE_WORDS[0]!, fixedNowMs)).toEqual({
      key: "new",
      label: "Нове",
    });
    expect(getFsrsBadge(SAMPLE_WORDS[1]!, fixedNowMs)).toEqual({
      key: "due",
      label: "Дозріло",
    });
    expect(getFsrsBadge(SAMPLE_WORDS[2]!, fixedNowMs)).toEqual({
      key: "mastered",
      label: "Закріплене",
    });
    expect(getFsrsBadge(SAMPLE_WORDS[3]!, fixedNowMs)).toEqual({
      key: "learning",
      label: "Вивчається",
    });

    expect(formatWordsCount(1)).toBe("1 слово");
    expect(formatWordsCount(2)).toBe("2 слова");
    expect(formatWordsCount(5)).toBe("5 слів");
    expect(formatWordsCount(11)).toBe("11 слів");
    expect(formatWordsCount(21)).toBe("21 слово");
  });

  it("renders loading state and then word items with Ukrainian FSRS badges", async () => {
    let resolveWords: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn<typeof fetch>(
      () =>
        new Promise<Response>((resolve) => {
          resolveWords = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<DictionaryPage />);

    const statusRegion = screen.getByRole("status");
    expect(statusRegion.getAttribute("aria-busy")).toBe("true");
    expect(screen.getByText("Завантажуємо словник…")).toBeTruthy();

    resolveWords?.(makeJsonResponse(SAMPLE_LIST));

    await waitFor(() => {
      expect(screen.getByText("der Zug")).toBeTruthy();
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/words");

    const list = screen.getByRole("list", { name: "Список слів у словнику" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(4);

    expect(within(items[0]!).getByText("Нове")).toBeTruthy();
    expect(within(items[1]!).getByText("Дозріло")).toBeTruthy();
    expect(within(items[2]!).getByText("Закріплене")).toBeTruthy();
    expect(within(items[3]!).getByText("Вивчається")).toBeTruthy();
  });

  it("renders API error state with retry button and recovers on retry", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(makeJsonResponse({ detail: "Server down" }, 500))
      .mockResolvedValueOnce(makeJsonResponse(SAMPLE_LIST));
    vi.stubGlobal("fetch", fetchMock);

    render(<DictionaryPage />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });
    expect(screen.getByText(/Помилка сервера/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Спробувати ще раз" }));

    await waitFor(() => {
      expect(screen.getByText("der Zug")).toBeTruthy();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("renders empty dictionary state with hint to add first words", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(makeJsonResponse({ items: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<DictionaryPage />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Словник поки що порожній" }),
      ).toBeTruthy();
    });
    expect(
      screen.getByText(/Додайте перші німецькі слова через форму вище/),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Очистити пошук" })).toBeNull();
  });

  it("searches words, distinguishes empty search result from empty dictionary, and clears search", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url === "/api/words") {
        return makeJsonResponse(SAMPLE_LIST);
      }
      if (url === "/api/words?search=Bahnhof") {
        return makeJsonResponse({ items: [], total: 0 });
      }
      if (url === "/api/words?search=%D0%BF%D0%BE%D1%82%D1%8F%D0%B3") {
        return makeJsonResponse({ items: [SAMPLE_WORDS[0]!], total: 1 });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DictionaryPage />);

    await waitFor(() => {
      expect(screen.getByText("der Zug")).toBeTruthy();
    });

    const searchInput = screen.getByLabelText("Пошук у словнику");

    // Search by Ukrainian translation
    fireEvent.change(searchInput, { target: { value: "потяг" } });
    fireEvent.submit(searchInput.closest("form")!);

    await waitFor(() => {
      expect(screen.getByText("1 слово")).toBeTruthy();
    });
    expect(screen.getByText("der Zug")).toBeTruthy();
    expect(screen.queryByText("die Verspätung")).toBeNull();

    // Search with no matches -> empty search result state (not empty dictionary)
    fireEvent.change(searchInput, { target: { value: "Bahnhof" } });
    fireEvent.submit(searchInput.closest("form")!);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Нічого не знайдено" }),
      ).toBeTruthy();
    });
    expect(
      screen.getByText(/За запитом «Bahnhof» у словнику немає збігів/),
    ).toBeTruthy();
    expect(screen.queryByText("Словник поки що порожній")).toBeNull();

    // Clear search button restores full dictionary
    const clearButtons = screen.getAllByRole("button", {
      name: "Очистити пошук",
    });
    fireEvent.click(clearButtons[clearButtons.length - 1]!);

    await waitFor(() => {
      expect(screen.getByText("die Verspätung")).toBeTruthy();
    });
    expect((searchInput as HTMLInputElement).value).toBe("");
  });

  it("handles quick-add import with summary, validation, and double-submit guard", async () => {
    let resolveImport: ((response: Response) => void) | undefined;
    let wordsFetchCount = 0;

    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url === "/api/words" && (init?.method ?? "GET") === "GET") {
        wordsFetchCount += 1;
        return makeJsonResponse(
          wordsFetchCount === 1
            ? { items: [], total: 0 }
            : { items: [SAMPLE_WORDS[0]!], total: 1 },
        );
      }
      if (url === "/api/words/import" && init?.method === "POST") {
        return new Promise<Response>((resolve) => {
          resolveImport = resolve;
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DictionaryPage />);

    await waitFor(() => {
      expect(screen.getByText("Словник поки що порожній")).toBeTruthy();
    });

    // Empty submit triggers client validation without POST
    fireEvent.click(screen.getByRole("button", { name: "Додати слова" }));
    expect(screen.getByRole("alert").textContent).toMatch(
      /Введіть хоча б одне слово/,
    );
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/api/words/import"),
    ).toHaveLength(0);

    const textarea = screen.getByLabelText("Слова у форматі «слово - переклад»");
    fireEvent.change(textarea, {
      target: { value: "der Zug - потяг\ninvalid line" },
    });

    const form = textarea.closest("form")!;
    // Trigger rapid double submit while first request is still pending
    fireEvent.submit(form);
    fireEvent.submit(form);

    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/api/words/import"),
    ).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Додаємо…" })).toBeTruthy();

    const importCall = fetchMock.mock.calls.find(
      ([url]) => url === "/api/words/import",
    );
    expect(importCall?.[1]?.body).toBe(
      JSON.stringify({ text: "der Zug - потяг\ninvalid line" }),
    );

    resolveImport?.(
      makeJsonResponse({ imported: 1, duplicates: 0, skipped: 1 }),
    );

    await waitFor(() => {
      expect(
        screen.getByText(
          "Імпорт завершено: додано 1, дублікатів 0, пропущено 1.",
        ),
      ).toBeTruthy();
      expect(screen.getByText("der Zug")).toBeTruthy();
    });
    expect((textarea as HTMLTextAreaElement).value).toBe("");
  });

  it("handles Google Sheets sync success and 400/422/502/504 errors without corrupting local word list", async () => {
    let syncAttempt = 0;
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url === "/api/words") {
        return makeJsonResponse(SAMPLE_LIST);
      }
      if (url === "/api/words/sync-sheets" && init?.method === "POST") {
        syncAttempt += 1;
        if (syncAttempt === 1) {
          return makeJsonResponse(
            { detail: "Посилання на Google Sheets не налаштоване." },
            400,
          );
        }
        if (syncAttempt === 2) {
          return makeJsonResponse({ detail: "Некоректний формат таблиці." }, 422);
        }
        if (syncAttempt === 3) {
          return makeJsonResponse({}, 502);
        }
        if (syncAttempt === 4) {
          return makeJsonResponse({}, 504);
        }
        return makeJsonResponse({ imported: 2, duplicates: 1, skipped: 0 });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DictionaryPage />);

    await waitFor(() => {
      expect(screen.getByText("der Zug")).toBeTruthy();
    });

    const syncButton = screen.getByRole("button", {
      name: "Синхронізувати Google Sheets",
    });

    // 400 error
    fireEvent.click(syncButton);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain(
        "Посилання на Google Sheets не налаштоване.",
      );
    });
    expect(screen.getByText("der Zug")).toBeTruthy();
    expect(screen.getByText("die Verspätung")).toBeTruthy();

    // 422 error
    fireEvent.click(syncButton);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain(
        "Некоректний формат таблиці.",
      );
    });
    expect(screen.getByText("der Zug")).toBeTruthy();

    // 502 error
    fireEvent.click(syncButton);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toMatch(
        /Google Sheets тимчасово недоступний/,
      );
    });
    expect(screen.getByText("der Zug")).toBeTruthy();

    // 504 error
    fireEvent.click(syncButton);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toMatch(
        /Перевищено час очікування відповіді від Google Sheets/,
      );
    });
    expect(screen.getByText("der Zug")).toBeTruthy();

    // Success
    fireEvent.click(syncButton);
    await waitFor(() => {
      expect(
        screen.getByText(
          "Синхронізацію Google Sheets завершено: додано 2, дублікатів 1, пропущено 0.",
        ),
      ).toBeTruthy();
    });
  });

  it("requires confirmation before deleting a word and handles delete success and error safely", async () => {
    let deleteCallCount = 0;
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url === "/api/words") {
        return makeJsonResponse(SAMPLE_LIST);
      }
      if (url === "/api/words/2" && init?.method === "DELETE") {
        deleteCallCount += 1;
        if (deleteCallCount === 1) {
          return makeJsonResponse({ detail: "Помилка бази даних." }, 500);
        }
        return makeNoContentResponse();
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DictionaryPage />);

    await waitFor(() => {
      expect(screen.getByText("die Verspätung")).toBeTruthy();
    });

    // Clicking Delete opens confirmation without sending DELETE yet
    fireEvent.click(
      screen.getByRole("button", { name: "Видалити слово «die Verspätung»" }),
    );
    expect(
      screen.getByText("Видалити «die Verspätung» зі словника?"),
    ).toBeTruthy();
    expect(deleteCallCount).toBe(0);

    // Cancel closes confirmation without deleting
    fireEvent.click(screen.getByRole("button", { name: "Скасувати" }));
    expect(
      screen.queryByText("Видалити «die Verspätung» зі словника?"),
    ).toBeNull();
    expect(deleteCallCount).toBe(0);

    // Reopen confirmation and confirm -> first attempt fails, word stays intact
    fireEvent.click(
      screen.getByRole("button", { name: "Видалити слово «die Verspätung»" }),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "Підтвердити видалення слова «die Verspätung»",
      }),
    );

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain(
        "Помилка бази даних.",
      );
    });
    expect(screen.getByText("die Verspätung")).toBeTruthy();
    expect(screen.getByText("4 слова")).toBeTruthy();

    // Confirm again -> succeeds (204) and removes word from local list
    fireEvent.click(
      screen.getByRole("button", {
        name: "Підтвердити видалення слова «die Verspätung»",
      }),
    );

    await waitFor(() => {
      expect(screen.queryByText("die Verspätung")).toBeNull();
    });
    expect(screen.getByText("3 слова")).toBeTruthy();
    expect(
      screen.getByText("Слово «die Verspätung» видалено зі словника."),
    ).toBeTruthy();
  });
});
