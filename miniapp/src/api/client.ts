import { getTelegramInitData } from "../telegram";
import type {
  GetWordsParams,
  RadarStatsResponse,
  SheetsSyncRequest,
  UserProfile,
  UserSettingsUpdate,
  WordImportResponse,
  WordListResponse,
} from "./types";

export type ApiErrorCode =
  | "unauthorized"
  | "validation"
  | "rate_limit"
  | "server"
  | "network"
  | "http";

export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly detail?: string;

  constructor(
    message: string,
    options: { status: number; code: ApiErrorCode; detail?: string },
  ) {
    super(message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.detail = options.detail;
  }
}

export interface ApiClientOptions {
  baseUrl?: string;
  getInitData?: () => string | null | undefined;
  fetchImpl?: typeof fetch;
}

export interface ApiClient {
  getMe: () => Promise<UserProfile>;
  updateSettings: (payload: UserSettingsUpdate) => Promise<UserProfile>;
  getRadarStats: () => Promise<RadarStatsResponse>;
  getWords: (params?: GetWordsParams) => Promise<WordListResponse>;
  importWordsText: (text: string) => Promise<WordImportResponse>;
  syncSheets: (sheetsUrl?: string) => Promise<WordImportResponse>;
  deleteWord: (wordId: number) => Promise<void>;
}

function normalizeInitData(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") {
    return null;
  }
  const trimmed = raw.trim();
  return trimmed.length > 0 ? trimmed : null;
}

async function extractErrorDetail(response: Response): Promise<string | undefined> {
  try {
    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.includes("application/json")) {
      return undefined;
    }
    const payload: unknown = await response.json();
    if (
      payload &&
      typeof payload === "object" &&
      "detail" in payload &&
      typeof (payload as { detail: unknown }).detail === "string"
    ) {
      const detail = (payload as { detail: string }).detail.trim();
      return detail.length > 0 ? detail : undefined;
    }
    return undefined;
  } catch {
    return undefined;
  }
}

function mapHttpError(status: number, detail?: string): ApiError {
  if (status === 401) {
    return new ApiError(
      "Помилка авторизації Telegram. Відкрийте застосунок повторно з бота.",
      { status, code: "unauthorized", detail },
    );
  }
  if (status === 422) {
    return new ApiError(
      detail ?? "Некоректні дані запиту. Перевірте введені значення.",
      { status, code: "validation", detail },
    );
  }
  if (status === 429) {
    return new ApiError(
      "Перевищено ліміт запитів. Спробуйте повторити пізніше.",
      { status, code: "rate_limit", detail },
    );
  }
  if (status >= 500) {
    return new ApiError(
      "Помилка сервера. Спробуйте ще раз трохи згодом.",
      { status, code: "server", detail },
    );
  }
  return new ApiError(detail ?? "Не вдалося виконати запит.", {
    status,
    code: "http",
    detail,
  });
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const baseUrl = (options.baseUrl ?? "/api").replace(/\/+$/, "");
  const getInitData = options.getInitData ?? getTelegramInitData;

  async function request<T>(
    path: string,
    init: { method?: string; body?: unknown } = {},
  ): Promise<T> {
    const fetchFn = options.fetchImpl ?? globalThis.fetch;
    const headers = new Headers();
    headers.set("Accept", "application/json");

    const initData = normalizeInitData(getInitData());
    if (initData !== null) {
      headers.set("Authorization", `tma ${initData}`);
    }

    let serializedBody: string | undefined;
    if (init.body !== undefined) {
      headers.set("Content-Type", "application/json");
      serializedBody = JSON.stringify(init.body);
    }

    let response: Response;
    try {
      response = await fetchFn(`${baseUrl}${path}`, {
        method: init.method ?? "GET",
        headers,
        body: serializedBody,
      });
    } catch {
      throw new ApiError(
        "Немає з'єднання із сервером. Перевірте інтернет і спробуйте ще раз.",
        { status: 0, code: "network" },
      );
    }

    if (!response.ok) {
      const detail = await extractErrorDetail(response);
      throw mapHttpError(response.status, detail);
    }

    if (response.status === 204) {
      return undefined as T;
    }

    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiError("Отримано некоректну відповідь від сервера.", {
        status: response.status,
        code: "server",
      });
    }
  }

  return {
    getMe: () => request<UserProfile>("/users/me"),

    updateSettings: (payload: UserSettingsUpdate) =>
      request<UserProfile>("/users/settings", {
        method: "PATCH",
        body: payload,
      }),

    getRadarStats: () => request<RadarStatsResponse>("/stats/radar"),

    getWords: (params?: GetWordsParams) => {
      const query = new URLSearchParams();
      if (params?.search !== undefined && params.search.trim().length > 0) {
        query.set("search", params.search.trim());
      }
      if (params?.limit !== undefined) {
        query.set("limit", String(params.limit));
      }
      if (params?.offset !== undefined) {
        query.set("offset", String(params.offset));
      }
      const suffix = query.toString();
      return request<WordListResponse>(suffix ? `/words?${suffix}` : "/words");
    },

    importWordsText: (text: string) =>
      request<WordImportResponse>("/words/import", {
        method: "POST",
        body: { text },
      }),

    syncSheets: (sheetsUrl?: string) => {
      const body: SheetsSyncRequest | undefined =
        sheetsUrl !== undefined ? { sheets_url: sheetsUrl } : undefined;
      return request<WordImportResponse>("/words/sync-sheets", {
        method: "POST",
        body,
      });
    },

    deleteWord: (wordId: number) =>
      request<void>(`/words/${encodeURIComponent(String(wordId))}`, {
        method: "DELETE",
      }),
  };
}

export const apiClient = createApiClient();

export const getMe = () => apiClient.getMe();
export const updateSettings = (payload: UserSettingsUpdate) =>
  apiClient.updateSettings(payload);
export const getRadarStats = () => apiClient.getRadarStats();
export const getWords = (params?: GetWordsParams) => apiClient.getWords(params);
export const importWordsText = (text: string) => apiClient.importWordsText(text);
export const syncSheets = (sheetsUrl?: string) => apiClient.syncSheets(sheetsUrl);
export const deleteWord = (wordId: number) => apiClient.deleteWord(wordId);
