import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

import { apiClient, ApiError } from "../api/client";
import type {
  GetWordsParams,
  WordImportResponse,
  WordItem,
  WordListResponse,
} from "../api/types";

type ListLoadStatus = "loading" | "error" | "success";

export type FsrsBadgeKey = "new" | "due" | "learning" | "mastered";

export interface FsrsBadgeInfo {
  key: FsrsBadgeKey;
  label: string;
}

const SEARCH_DEBOUNCE_MS = 250;

export function getFsrsBadge(
  word: WordItem,
  nowMs: number = Date.now(),
): FsrsBadgeInfo {
  if (word.state === 0 || word.reps === 0) {
    return { key: "new", label: "Нове" };
  }

  if (word.due_at !== null) {
    const dueMs = Date.parse(word.due_at);
    if (!Number.isNaN(dueMs) && dueMs <= nowMs) {
      return { key: "due", label: "Дозріло" };
    }
  }

  if (word.state === 2) {
    return { key: "mastered", label: "Закріплене" };
  }

  return { key: "learning", label: "Вивчається" };
}

export function formatWordsCount(count: number): string {
  const absCount = Math.abs(Math.trunc(count));
  const mod100 = absCount % 100;
  const mod10 = absCount % 10;

  if (mod10 === 1 && mod100 !== 11) {
    return `${absCount} слово`;
  }
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
    return `${absCount} слова`;
  }
  return `${absCount} слів`;
}

export function formatImportSummary(
  prefix: string,
  result: WordImportResponse,
): string {
  return `${prefix}: додано ${result.imported}, дублікатів ${result.duplicates}, пропущено ${result.skipped}.`;
}

export function formatSyncSheetsError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) {
      return (
        error.detail ??
        "Не вдалося синхронізувати таблицю. Перевірте посилання Google Sheets у налаштуваннях."
      );
    }
    if (error.status === 422) {
      return (
        error.detail ??
        "Некоректні дані для синхронізації Google Sheets. Перевірте налаштування."
      );
    }
    if (error.status === 502) {
      return (
        error.detail ??
        "Google Sheets тимчасово недоступний. Спробуйте повторити пізніше."
      );
    }
    if (error.status === 504) {
      return (
        error.detail ??
        "Перевищено час очікування відповіді від Google Sheets. Спробуйте ще раз."
      );
    }
    return error.detail ?? error.message;
  }
  return "Не вдалося синхронізувати Google Sheets. Спробуйте ще раз.";
}

export interface DictionaryPageProps {
  loadWords?: (params?: GetWordsParams) => Promise<WordListResponse>;
  importWords?: (text: string) => Promise<WordImportResponse>;
  syncSheets?: () => Promise<WordImportResponse>;
  removeWord?: (wordId: number) => Promise<void>;
}

export function DictionaryPage({
  loadWords,
  importWords,
  syncSheets,
  removeWord,
}: DictionaryPageProps) {
  const [status, setStatus] = useState<ListLoadStatus>("loading");
  const [words, setWords] = useState<WordItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isLoadingMore, setIsLoadingMore] = useState(false);

  const [searchInput, setSearchInput] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");

  const [importText, setImportText] = useState("");
  const [isImporting, setIsImporting] = useState(false);
  const [importFeedback, setImportFeedback] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);

  const [isSyncing, setIsSyncing] = useState(false);
  const [syncFeedback, setSyncFeedback] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);

  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  const [deletingWordId, setDeletingWordId] = useState<number | null>(null);
  const [deleteFeedback, setDeleteFeedback] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);

  const isMountedRef = useRef(true);
  const requestIdRef = useRef(0);
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isImportingRef = useRef(false);
  const isSyncingRef = useRef(false);
  const deletingWordIdRef = useRef<number | null>(null);

  const fetchWords = useCallback(
    async (searchQuery: string, options?: { appendOffset?: number }) => {
      const appendOffset = options?.appendOffset;
      const isAppending = appendOffset !== undefined && appendOffset > 0;
      const currentRequestId = ++requestIdRef.current;

      if (isAppending) {
        setIsLoadingMore(true);
      } else {
        setStatus("loading");
        setLoadError(null);
      }

      try {
        const fetcher = loadWords ?? ((params?: GetWordsParams) => apiClient.getWords(params));
        const params: GetWordsParams = {};
        if (searchQuery.length > 0) {
          params.search = searchQuery;
        }
        if (isAppending) {
          params.offset = appendOffset;
        }
        const response = await fetcher(
          Object.keys(params).length > 0 ? params : undefined,
        );

        if (!isMountedRef.current || requestIdRef.current !== currentRequestId) {
          return;
        }

        setTotal(response.total);
        if (isAppending) {
          setWords((prev) => {
            const seenIds = new Set(prev.map((item) => item.id));
            const nextItems = response.items.filter((item) => !seenIds.has(item.id));
            return [...prev, ...nextItems];
          });
          setIsLoadingMore(false);
        } else {
          setWords(response.items);
          setStatus("success");
        }
      } catch (error: unknown) {
        if (!isMountedRef.current || requestIdRef.current !== currentRequestId) {
          return;
        }
        const message =
          error instanceof ApiError
            ? error.message
            : "Не вдалося завантажити словник. Перевірте з'єднання і спробуйте ще раз.";
        if (isAppending) {
          setIsLoadingMore(false);
          setDeleteFeedback({ kind: "error", text: message });
        } else {
          setLoadError(message);
          setStatus("error");
        }
      }
    },
    [loadWords],
  );

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      requestIdRef.current += 1;
      if (debounceTimerRef.current !== null) {
        clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    void fetchWords(appliedSearch);
  }, [appliedSearch, fetchWords]);

  const cancelPendingSearchDebounce = () => {
    if (debounceTimerRef.current !== null) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }
  };

  const handleSearchChange = (nextValue: string) => {
    setSearchInput(nextValue);
    cancelPendingSearchDebounce();
    const trimmed = nextValue.trim();
    debounceTimerRef.current = setTimeout(() => {
      debounceTimerRef.current = null;
      setAppliedSearch(trimmed);
    }, SEARCH_DEBOUNCE_MS);
  };

  const handleSearchSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    cancelPendingSearchDebounce();
    const trimmed = searchInput.trim();
    if (trimmed === appliedSearch) {
      void fetchWords(trimmed);
    } else {
      setAppliedSearch(trimmed);
    }
  };

  const handleClearSearch = () => {
    cancelPendingSearchDebounce();
    setSearchInput("");
    if (appliedSearch === "") {
      void fetchWords("");
    } else {
      setAppliedSearch("");
    }
  };

  const handleImportSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isImportingRef.current) {
      return;
    }

    const trimmed = importText.trim();
    if (!trimmed) {
      setImportFeedback({
        kind: "error",
        text: "Введіть хоча б одне слово з перекладом, наприклад: der Zug - потяг.",
      });
      return;
    }

    isImportingRef.current = true;
    setIsImporting(true);
    setImportFeedback(null);

    try {
      const importer = importWords ?? ((text: string) => apiClient.importWordsText(text));
      const result = await importer(trimmed);
      if (!isMountedRef.current) {
        return;
      }
      setImportText("");
      setImportFeedback({
        kind: "success",
        text: formatImportSummary("Імпорт завершено", result),
      });
      await fetchWords(appliedSearch);
    } catch (error: unknown) {
      if (!isMountedRef.current) {
        return;
      }
      const message =
        error instanceof ApiError
          ? error.detail ?? error.message
          : "Не вдалося додати слова. Спробуйте ще раз.";
      setImportFeedback({ kind: "error", text: message });
    } finally {
      isImportingRef.current = false;
      if (isMountedRef.current) {
        setIsImporting(false);
      }
    }
  };

  const handleSyncSheets = async () => {
    if (isSyncingRef.current) {
      return;
    }

    isSyncingRef.current = true;
    setIsSyncing(true);
    setSyncFeedback(null);

    try {
      const syncer = syncSheets ?? (() => apiClient.syncSheets());
      const result = await syncer();
      if (!isMountedRef.current) {
        return;
      }
      setSyncFeedback({
        kind: "success",
        text: formatImportSummary("Синхронізацію Google Sheets завершено", result),
      });
      await fetchWords(appliedSearch);
    } catch (error: unknown) {
      if (!isMountedRef.current) {
        return;
      }
      setSyncFeedback({
        kind: "error",
        text: formatSyncSheetsError(error),
      });
    } finally {
      isSyncingRef.current = false;
      if (isMountedRef.current) {
        setIsSyncing(false);
      }
    }
  };

  const handleConfirmDelete = async (word: WordItem) => {
    if (deletingWordIdRef.current !== null) {
      return;
    }

    deletingWordIdRef.current = word.id;
    setDeletingWordId(word.id);
    setDeleteFeedback(null);

    try {
      const deleter = removeWord ?? ((id: number) => apiClient.deleteWord(id));
      await deleter(word.id);
      if (!isMountedRef.current) {
        return;
      }
      setWords((prev) => prev.filter((item) => item.id !== word.id));
      setTotal((prev) => Math.max(0, prev - 1));
      setConfirmDeleteId(null);
      setDeleteFeedback({
        kind: "success",
        text: `Слово «${word.word_de}» видалено зі словника.`,
      });
    } catch (error: unknown) {
      if (!isMountedRef.current) {
        return;
      }
      const message =
        error instanceof ApiError
          ? error.detail ?? error.message
          : "Не вдалося видалити слово. Спробуйте ще раз.";
      setDeleteFeedback({ kind: "error", text: message });
    } finally {
      deletingWordIdRef.current = null;
      if (isMountedRef.current) {
        setDeletingWordId(null);
      }
    }
  };

  return (
    <div className="dictionary-page">
      <section className="surface-card" aria-labelledby="quick-add-title">
        <div className="section-header-row">
          <h2 id="quick-add-title" className="card-title">
            Швидке додавання слів
          </h2>
        </div>

        <form className="stack-form" onSubmit={(event) => void handleImportSubmit(event)} noValidate>
          <div className="form-field">
            <label htmlFor="quick-add-textarea" className="field-label">
              Слова у форматі «слово - переклад»
            </label>
            <textarea
              id="quick-add-textarea"
              className="text-area"
              rows={3}
              value={importText}
              onChange={(event) => setImportText(event.target.value)}
              placeholder={"der Zug - потяг\ndie Verspätung - затримка"}
              disabled={isImporting}
            />
            <span className="field-hint">
              Вводьте кожну пару з нового рядка. Дублікати пропускаються автоматично.
            </span>
          </div>

          <div className="actions-row">
            <button
              type="submit"
              className="btn-primary"
              disabled={isImporting}
            >
              {isImporting ? "Додаємо…" : "Додати слова"}
            </button>

            <button
              type="button"
              className="btn-secondary"
              disabled={isSyncing}
              onClick={() => void handleSyncSheets()}
            >
              {isSyncing ? "Синхронізуємо…" : "Синхронізувати Google Sheets"}
            </button>
          </div>
        </form>

        {importFeedback ? (
          <p
            className={`feedback-banner${
              importFeedback.kind === "error" ? " feedback-banner--error" : ""
            }`}
            role={importFeedback.kind === "error" ? "alert" : "status"}
          >
            {importFeedback.text}
          </p>
        ) : null}

        {syncFeedback ? (
          <p
            className={`feedback-banner${
              syncFeedback.kind === "error" ? " feedback-banner--error" : ""
            }`}
            role={syncFeedback.kind === "error" ? "alert" : "status"}
          >
            {syncFeedback.text}
          </p>
        ) : null}
      </section>

      <section className="surface-card" aria-labelledby="word-list-title">
        <div className="section-header-row">
          <h2 id="word-list-title" className="card-title">
            Словник
          </h2>
          {status === "success" ? (
            <span className="count-pill" aria-label={`Усього: ${formatWordsCount(total)}`}>
              {formatWordsCount(total)}
            </span>
          ) : null}
        </div>

        <form className="search-form" role="search" onSubmit={handleSearchSubmit}>
          <label htmlFor="dictionary-search" className="sr-only">
            Пошук у словнику
          </label>
          <input
            id="dictionary-search"
            type="search"
            className="text-input search-input"
            value={searchInput}
            onChange={(event) => handleSearchChange(event.target.value)}
            placeholder="Пошук німецького слова або перекладу"
            autoComplete="off"
          />
          {searchInput.length > 0 || appliedSearch.length > 0 ? (
            <button
              type="button"
              className="btn-icon"
              aria-label="Очистити пошук"
              onClick={handleClearSearch}
            >
              ×
            </button>
          ) : null}
          <button type="submit" className="btn-secondary">
            Знайти
          </button>
        </form>

        {deleteFeedback ? (
          <p
            className={`feedback-banner${
              deleteFeedback.kind === "error" ? " feedback-banner--error" : ""
            }`}
            role={deleteFeedback.kind === "error" ? "alert" : "status"}
          >
            {deleteFeedback.text}
          </p>
        ) : null}

        {status === "loading" ? (
          <div
            className="loading-state"
            role="status"
            aria-live="polite"
            aria-busy="true"
          >
            <p className="card-description">Завантажуємо словник…</p>
            <div className="skeleton-list" aria-hidden="true">
              <div className="skeleton-card" />
              <div className="skeleton-card" />
              <div className="skeleton-card" />
            </div>
          </div>
        ) : status === "error" ? (
          <div className="error-state" role="alert">
            <p className="card-description">
              {loadError ?? "Сталася помилка під час завантаження словника."}
            </p>
            <button
              type="button"
              className="retry-button"
              onClick={() => void fetchWords(appliedSearch)}
            >
              Спробувати ще раз
            </button>
          </div>
        ) : words.length === 0 ? (
          appliedSearch.length > 0 ? (
            <div className="empty-state">
              <h3 className="empty-state-title">Нічого не знайдено</h3>
              <p className="card-description">
                За запитом «{appliedSearch}» у словнику немає збігів.
              </p>
              <button
                type="button"
                className="btn-secondary"
                onClick={handleClearSearch}
              >
                Очистити пошук
              </button>
            </div>
          ) : (
            <div className="empty-state">
              <h3 className="empty-state-title">Словник поки що порожній</h3>
              <p className="card-description">
                Додайте перші німецькі слова через форму вище, надішліть їх у
                Telegram-бот або синхронізуйте Google Sheets.
              </p>
            </div>
          )
        ) : (
          <>
            <ul className="word-list" aria-label="Список слів у словнику">
              {words.map((word) => {
                const badge = getFsrsBadge(word);
                const isConfirming = confirmDeleteId === word.id;
                const isDeleting = deletingWordId === word.id;

                return (
                  <li key={word.id} className="word-item">
                    <div className="word-item-main">
                      <div className="word-item-text">
                        <span className="word-de">{word.word_de}</span>
                        <span className="word-ua">{word.translation_ua}</span>
                      </div>

                      <div className="word-item-controls">
                        <span
                          className={`fsrs-badge fsrs-badge--${badge.key}`}
                          data-fsrs-state={badge.key}
                        >
                          {badge.label}
                        </span>

                        {!isConfirming ? (
                          <button
                            type="button"
                            className="btn-delete"
                            aria-label={`Видалити слово «${word.word_de}»`}
                            disabled={deletingWordId !== null}
                            onClick={() => {
                              setDeleteFeedback(null);
                              setConfirmDeleteId(word.id);
                            }}
                          >
                            Видалити
                          </button>
                        ) : null}
                      </div>
                    </div>

                    {isConfirming ? (
                      <div
                        className="delete-confirm-bar"
                        role="region"
                        aria-label={`Підтвердження видалення слова «${word.word_de}»`}
                      >
                        <span className="delete-confirm-text">
                          Видалити «{word.word_de}» зі словника?
                        </span>
                        <div className="delete-confirm-actions">
                          <button
                            type="button"
                            className="btn-danger"
                            aria-label={`Підтвердити видалення слова «${word.word_de}»`}
                            disabled={isDeleting}
                            onClick={() => void handleConfirmDelete(word)}
                          >
                            {isDeleting ? "Видаляємо…" : "Так, видалити"}
                          </button>
                          <button
                            type="button"
                            className="btn-secondary"
                            disabled={isDeleting}
                            onClick={() => setConfirmDeleteId(null)}
                          >
                            Скасувати
                          </button>
                        </div>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>

            {words.length < total ? (
              <button
                type="button"
                className="btn-secondary load-more-button"
                disabled={isLoadingMore}
                onClick={() =>
                  void fetchWords(appliedSearch, { appendOffset: words.length })
                }
              >
                {isLoadingMore ? "Завантажуємо…" : "Показати ще"}
              </button>
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}
