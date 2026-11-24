import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, createApiClient } from "./client";

function makeJsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api/client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete window.Telegram;
  });

  it("attaches Authorization: tma <initData> when initData is non-empty", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      makeJsonResponse({
        telegram_id: 1001,
        level: "A2",
        exercises_per_set: 5,
        theme: "dark",
        sheets_url: null,
        reminder_hour: 19,
        timezone: "Europe/Kyiv",
        daily_llm_calls: 0,
        created_at: "2026-11-24T10:00:00Z",
      }),
    );

    const client = createApiClient({
      getInitData: () => "user=%7B%22id%22%3A1001%7D&auth_date=1700000000&hash=sig",
      fetchImpl: fetchMock,
    });

    const profile = await client.getMe();

    expect(profile.telegram_id).toBe(1001);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/users/me");
    expect(init?.method).toBe("GET");
    const headers = new Headers(init?.headers);
    expect(headers.get("Authorization")).toBe(
      "tma user=%7B%22id%22%3A1001%7D&auth_date=1700000000&hash=sig",
    );
  });

  it("omits Authorization header when initData is missing, undefined, or whitespace", async () => {
    for (const emptyValue of [undefined, null, "", "   "]) {
      const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
        makeJsonResponse({ items: [], total: 0 }),
      );
      const client = createApiClient({
        getInitData: () => emptyValue,
        fetchImpl: fetchMock,
      });

      await client.getWords();

      const [, init] = fetchMock.mock.calls[0]!;
      const headers = new Headers(init?.headers);
      expect(headers.has("Authorization")).toBe(false);
    }
  });

  it("handles 204 No Content on deleteWord without calling response.json()", async () => {
    const jsonSpy = vi.fn(() => {
      throw new Error("response.json() must not be called for 204");
    });
    const mockResponse = {
      ok: true,
      status: 204,
      headers: new Headers(),
      json: jsonSpy,
    } as unknown as Response;

    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(mockResponse);
    const client = createApiClient({
      getInitData: () => "valid_init_data",
      fetchImpl: fetchMock,
    });

    await expect(client.deleteWord(42)).resolves.toBeUndefined();
    expect(jsonSpy).not.toHaveBeenCalled();

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/words/42");
    expect(init?.method).toBe("DELETE");
  });

  it("builds query parameters and payloads for typed API methods", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        makeJsonResponse({
          telegram_id: 1,
          level: "B1",
          exercises_per_set: 7,
          theme: "light",
          sheets_url: null,
          reminder_hour: 19,
          timezone: "Europe/Kyiv",
          daily_llm_calls: 2,
          created_at: "2026-11-24T10:00:00Z",
        }),
      )
      .mockResolvedValueOnce(makeJsonResponse({ axes: [], weak_topics: [] }))
      .mockResolvedValueOnce(makeJsonResponse({ items: [], total: 0 }))
      .mockResolvedValueOnce(
        makeJsonResponse({ imported: 2, duplicates: 0, skipped: 0 }),
      )
      .mockResolvedValueOnce(
        makeJsonResponse({ imported: 3, duplicates: 1, skipped: 0 }),
      );

    const client = createApiClient({
      getInitData: () => "tok",
      fetchImpl: fetchMock,
    });

    await client.updateSettings({ theme: "light", level: "B1" });
    await client.getRadarStats();
    await client.getWords({ search: "der Zug", limit: 20, offset: 40 });
    await client.importWordsText("der Zug - потяг");
    await client.syncSheets("https://docs.google.com/spreadsheets/d/123/edit");

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/users/settings");
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe("PATCH");
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(
      JSON.stringify({ theme: "light", level: "B1" }),
    );

    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/stats/radar");
    expect(fetchMock.mock.calls[2]?.[0]).toBe(
      "/api/words?search=der+Zug&limit=20&offset=40",
    );

    expect(fetchMock.mock.calls[3]?.[0]).toBe("/api/words/import");
    expect(fetchMock.mock.calls[3]?.[1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[3]?.[1]?.body).toBe(
      JSON.stringify({ text: "der Zug - потяг" }),
    );

    expect(fetchMock.mock.calls[4]?.[0]).toBe("/api/words/sync-sheets");
    expect(fetchMock.mock.calls[4]?.[1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[4]?.[1]?.body).toBe(
      JSON.stringify({
        sheets_url: "https://docs.google.com/spreadsheets/d/123/edit",
      }),
    );
  });

  it("throws typed ApiError with Ukrainian messages for 401, 422, 429, 500, and network failures", async () => {
    const cases: Array<{
      status: number;
      body?: unknown;
      expectedCode: ApiError["code"];
      messagePattern: RegExp;
    }> = [
      {
        status: 401,
        body: { detail: "Invalid credentials" },
        expectedCode: "unauthorized",
        messagePattern: /авторизації/i,
      },
      {
        status: 422,
        body: { detail: "Некоректний часовий пояс." },
        expectedCode: "validation",
        messagePattern: /Некоректний часовий пояс/,
      },
      {
        status: 422,
        body: { detail: [{ loc: ["body", "level"], msg: "Input error" }] },
        expectedCode: "validation",
        messagePattern: /Некоректні дані запиту/,
      },
      {
        status: 429,
        body: { detail: "Rate limit" },
        expectedCode: "rate_limit",
        messagePattern: /ліміт/i,
      },
      {
        status: 500,
        body: { detail: "Internal error" },
        expectedCode: "server",
        messagePattern: /Помилка сервера/i,
      },
      {
        status: 503,
        body: { detail: "Service unavailable" },
        expectedCode: "server",
        messagePattern: /Помилка сервера/i,
      },
    ];

    for (const testCase of cases) {
      const fetchMock = vi
        .fn<typeof fetch>()
        .mockResolvedValue(makeJsonResponse(testCase.body, testCase.status));
      const client = createApiClient({
        getInitData: () => "valid",
        fetchImpl: fetchMock,
      });

      await expect(client.getMe()).rejects.toSatisfy((error: unknown) => {
        expect(error).toBeInstanceOf(ApiError);
        const apiError = error as ApiError;
        expect(apiError.status).toBe(testCase.status);
        expect(apiError.code).toBe(testCase.expectedCode);
        expect(apiError.message).toMatch(testCase.messagePattern);
        return true;
      });
    }

    const networkFetch = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError("Failed to fetch"));
    const networkClient = createApiClient({
      getInitData: () => "valid",
      fetchImpl: networkFetch,
    });

    await expect(networkClient.getMe()).rejects.toSatisfy((error: unknown) => {
      expect(error).toBeInstanceOf(ApiError);
      const apiError = error as ApiError;
      expect(apiError.status).toBe(0);
      expect(apiError.code).toBe("network");
      expect(apiError.message).toMatch(/з'єднання/i);
      return true;
    });
  });
});
