import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

import { apiClient, ApiError } from "../api/client";
import type { UserLevel, UserProfile, UserSettingsUpdate } from "../api/types";
import { useTheme, type ThemeMode } from "../theme/ThemeProvider";

type ProfileLoadStatus = "loading" | "error" | "success";

export const CUSTOM_TIMEZONE_OPTION = "__custom__";

export const COMMON_TIMEZONES: ReadonlyArray<{ value: string; label: string }> = [
  { value: "Europe/Kyiv", label: "Europe/Kyiv — Київ" },
  { value: "Europe/Berlin", label: "Europe/Berlin — Берлін" },
  { value: "Europe/Warsaw", label: "Europe/Warsaw — Варшава" },
  { value: "Europe/Vienna", label: "Europe/Vienna — Відень" },
  { value: "Europe/Zurich", label: "Europe/Zurich — Цюрих" },
  { value: "Europe/Prague", label: "Europe/Prague — Прага" },
  { value: "Europe/London", label: "Europe/London — Лондон" },
  { value: "Europe/Paris", label: "Europe/Paris — Париж" },
  { value: "Europe/Madrid", label: "Europe/Madrid — Мадрид" },
  { value: "Europe/Rome", label: "Europe/Rome — Рим" },
  { value: "America/New_York", label: "America/New_York — Нью-Йорк" },
  { value: "America/Toronto", label: "America/Toronto — Торонто" },
  { value: "America/Los_Angeles", label: "America/Los_Angeles — Лос-Анджелес" },
  { value: "UTC", label: "UTC — Всесвітній час" },
];

const COMMON_TIMEZONE_SET = new Set(COMMON_TIMEZONES.map((item) => item.value));

const THEME_OPTIONS: ReadonlyArray<{ value: ThemeMode; label: string }> = [
  { value: "system", label: "Auto" },
  { value: "dark", label: "Темна" },
  { value: "light", label: "Світла" },
];

const LEVEL_OPTIONS: ReadonlyArray<UserLevel> = ["A1", "A2", "B1"];

const RESOLVED_THEME_LABELS: Record<"dark" | "light", string> = {
  dark: "темна",
  light: "світла",
};

export function parseStrictIntegerInRange(
  raw: string,
  min: number,
  max: number,
): number | null {
  const trimmed = raw.trim();
  if (!/^\d+$/.test(trimmed)) {
    return null;
  }
  const parsed = Number(trimmed);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) {
    return null;
  }
  return parsed;
}

export function formatReminderHour(raw: string): string | null {
  const hour = parseStrictIntegerInRange(raw, 0, 23);
  if (hour === null) {
    return null;
  }
  return `${String(hour).padStart(2, "0")}:00`;
}

export interface SettingsPageProps {
  loadProfile?: () => Promise<UserProfile>;
  saveSettings?: (payload: UserSettingsUpdate) => Promise<UserProfile>;
  onUserThemeChange?: () => void;
}

export function SettingsPage({
  loadProfile,
  saveSettings,
  onUserThemeChange,
}: SettingsPageProps) {
  const { themeMode, resolvedTheme, setThemeMode } = useTheme();

  const [status, setStatus] = useState<ProfileLoadStatus>("loading");
  const [loadError, setLoadError] = useState<string | null>(null);

  const [level, setLevel] = useState<UserLevel>("A1");
  const [exercisesInput, setExercisesInput] = useState("5");
  const [sheetsUrlInput, setSheetsUrlInput] = useState("");
  const [reminderHourInput, setReminderHourInput] = useState("19");
  const [timezoneSelect, setTimezoneSelect] = useState("Europe/Kyiv");
  const [customTimezoneInput, setCustomTimezoneInput] = useState("");

  const [themeFeedback, setThemeFeedback] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [saveFeedback, setSaveFeedback] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);

  const isMountedRef = useRef(true);
  const loadRequestIdRef = useRef(0);
  const themeRequestIdRef = useRef(0);
  const userChangedThemeRef = useRef(false);
  const isSavingRef = useRef(false);

  const hydrateFormFromProfile = useCallback((profile: UserProfile) => {
    setLevel(profile.level);
    setExercisesInput(String(profile.exercises_per_set));
    setSheetsUrlInput(profile.sheets_url ?? "");
    setReminderHourInput(String(profile.reminder_hour));

    if (COMMON_TIMEZONE_SET.has(profile.timezone)) {
      setTimezoneSelect(profile.timezone);
      setCustomTimezoneInput("");
    } else {
      setTimezoneSelect(CUSTOM_TIMEZONE_OPTION);
      setCustomTimezoneInput(profile.timezone);
    }
  }, []);

  const fetchProfile = useCallback(async () => {
    const currentRequestId = ++loadRequestIdRef.current;
    setStatus("loading");
    setLoadError(null);

    try {
      const fetcher = loadProfile ?? (() => apiClient.getMe());
      const profile = await fetcher();
      if (!isMountedRef.current || loadRequestIdRef.current !== currentRequestId) {
        return;
      }
      hydrateFormFromProfile(profile);
      if (!userChangedThemeRef.current) {
        setThemeMode(profile.theme);
      }
      setStatus("success");
    } catch (error: unknown) {
      if (!isMountedRef.current || loadRequestIdRef.current !== currentRequestId) {
        return;
      }
      if (error instanceof ApiError) {
        setLoadError(error.message);
      } else {
        setLoadError(
          "Не вдалося завантажити налаштування. Перевірте з'єднання і спробуйте ще раз.",
        );
      }
      setStatus("error");
    }
  }, [hydrateFormFromProfile, loadProfile, setThemeMode]);

  useEffect(() => {
    isMountedRef.current = true;
    void fetchProfile();

    return () => {
      isMountedRef.current = false;
      loadRequestIdRef.current += 1;
      themeRequestIdRef.current += 1;
    };
  }, [fetchProfile]);

  const handleSelectTheme = (nextMode: ThemeMode) => {
    userChangedThemeRef.current = true;
    onUserThemeChange?.();
    setThemeMode(nextMode);
    setThemeFeedback(null);

    const requestId = ++themeRequestIdRef.current;
    const updater =
      saveSettings ??
      ((payload: UserSettingsUpdate) => apiClient.updateSettings(payload));

    void updater({ theme: nextMode })
      .then(() => {
        if (!isMountedRef.current || themeRequestIdRef.current !== requestId) {
          return;
        }
        setThemeFeedback({
          kind: "success",
          text: "Тему оформлення збережено.",
        });
      })
      .catch((error: unknown) => {
        if (!isMountedRef.current || themeRequestIdRef.current !== requestId) {
          return;
        }
        const detail =
          error instanceof ApiError
            ? error.message
            : "не вдалося зв'язатися із сервером";
        setThemeFeedback({
          kind: "error",
          text: `Тему застосовано локально (${detail})`,
        });
      });
  };

  const handleSubmitSettings = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isSavingRef.current) {
      return;
    }

    const parsedExercises = parseStrictIntegerInRange(exercisesInput, 1, 30);
    if (parsedExercises === null) {
      setSaveFeedback({
        kind: "error",
        text: "Кількість вправ у сеті має бути цілим числом від 1 до 30.",
      });
      return;
    }

    const parsedReminderHour = parseStrictIntegerInRange(reminderHourInput, 0, 23);
    if (parsedReminderHour === null) {
      setSaveFeedback({
        kind: "error",
        text: "Година нагадування має бути цілим числом від 0 до 23.",
      });
      return;
    }

    const effectiveTimezone =
      timezoneSelect === CUSTOM_TIMEZONE_OPTION
        ? customTimezoneInput.trim()
        : timezoneSelect;
    if (!effectiveTimezone) {
      setSaveFeedback({
        kind: "error",
        text: "Вкажіть часовий пояс у форматі IANA, наприклад Europe/Kyiv.",
      });
      return;
    }

    const trimmedSheetsUrl = sheetsUrlInput.trim();
    const payload: UserSettingsUpdate = {
      level,
      exercises_per_set: parsedExercises,
      sheets_url: trimmedSheetsUrl.length > 0 ? trimmedSheetsUrl : null,
      reminder_hour: parsedReminderHour,
      timezone: effectiveTimezone,
    };

    isSavingRef.current = true;
    setIsSaving(true);
    setSaveFeedback(null);

    try {
      const updater =
        saveSettings ??
        ((nextPayload: UserSettingsUpdate) => apiClient.updateSettings(nextPayload));
      const updatedProfile = await updater(payload);
      if (!isMountedRef.current) {
        return;
      }
      hydrateFormFromProfile(updatedProfile);
      setSaveFeedback({
        kind: "success",
        text: "Налаштування збережено.",
      });
    } catch (error: unknown) {
      if (!isMountedRef.current) {
        return;
      }
      const message =
        error instanceof ApiError
          ? error.detail ?? error.message
          : "Не вдалося зберегти налаштування. Спробуйте ще раз.";
      setSaveFeedback({
        kind: "error",
        text: message,
      });
    } finally {
      isSavingRef.current = false;
      if (isMountedRef.current) {
        setIsSaving(false);
      }
    }
  };

  if (status === "loading") {
    return (
      <section
        className="surface-card settings-loading-card"
        role="status"
        aria-live="polite"
        aria-busy="true"
      >
        <h2 className="card-title">Налаштування</h2>
        <p className="card-description">Завантажуємо налаштування профілю…</p>
        <div className="skeleton-list" aria-hidden="true">
          <div className="skeleton-card" />
          <div className="skeleton-card" />
          <div className="skeleton-card" />
        </div>
      </section>
    );
  }

  if (status === "error") {
    return (
      <section className="surface-card settings-error-card" role="alert">
        <h2 className="card-title">Не вдалося завантажити налаштування</h2>
        <p className="card-description">
          {loadError ??
            "Сталася непередбачена помилка. Спробуйте ще раз трохи згодом."}
        </p>
        <button
          type="button"
          className="retry-button"
          onClick={() => void fetchProfile()}
        >
          Спробувати ще раз
        </button>
      </section>
    );
  }

  const formattedHour = formatReminderHour(reminderHourInput);

  return (
    <div className="settings-page">
      <section className="surface-card" aria-labelledby="theme-section-title">
        <h2 id="theme-section-title" className="card-title">
          Оформлення
        </h2>
        <p className="card-description">
          Поточна палітра: {RESOLVED_THEME_LABELS[resolvedTheme]}. Оберіть
          висококонтрастну тему або автоматичне підлаштування під Telegram.
        </p>

        <div
          className="segmented-control"
          role="group"
          aria-label="Вибір теми оформлення"
        >
          {THEME_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className="segmented-option"
              aria-pressed={themeMode === option.value}
              onClick={() => handleSelectTheme(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>

        {themeFeedback ? (
          <p
            className={`feedback-banner${
              themeFeedback.kind === "error" ? " feedback-banner--error" : ""
            }`}
            role={themeFeedback.kind === "error" ? "alert" : "status"}
          >
            {themeFeedback.text}
          </p>
        ) : null}
      </section>

      <section className="surface-card" aria-labelledby="profile-settings-title">
        <h2 id="profile-settings-title" className="card-title">
          Параметри навчання та нагадувань
        </h2>

        <form
          className="stack-form"
          onSubmit={(event) => void handleSubmitSettings(event)}
          noValidate
        >
          <div className="form-field">
            <span id="settings-level-label" className="field-label">
              Рівень німецької
            </span>
            <div
              className="segmented-control"
              role="group"
              aria-labelledby="settings-level-label"
            >
              {LEVEL_OPTIONS.map((option) => (
                <button
                  key={option}
                  type="button"
                  className="segmented-option"
                  aria-pressed={level === option}
                  onClick={() => setLevel(option)}
                >
                  {option}
                </button>
              ))}
            </div>
          </div>

          <div className="form-field">
            <label htmlFor="settings-exercises" className="field-label">
              Кількість вправ у сеті (1–30)
            </label>
            <input
              id="settings-exercises"
              type="number"
              inputMode="numeric"
              min={1}
              max={30}
              step={1}
              className="text-input"
              value={exercisesInput}
              onChange={(event) => setExercisesInput(event.target.value)}
            />
          </div>

          <div className="form-field">
            <label htmlFor="settings-sheets-url" className="field-label">
              Посилання на Google Sheets
            </label>
            <div className="input-with-action">
              <input
                id="settings-sheets-url"
                type="url"
                className="text-input"
                value={sheetsUrlInput}
                onChange={(event) => setSheetsUrlInput(event.target.value)}
                placeholder="https://docs.google.com/spreadsheets/d/..."
              />
              {sheetsUrlInput.length > 0 ? (
                <button
                  type="button"
                  className="btn-secondary"
                  aria-label="Очистити посилання Google Sheets"
                  onClick={() => setSheetsUrlInput("")}
                >
                  Очистити
                </button>
              ) : null}
            </div>
            <span className="field-hint">
              Публічне посилання на таблицю. Залиште поле порожнім, щоб вимкнути
              синхронізацію.
            </span>
          </div>

          <div className="form-field">
            <label htmlFor="settings-reminder-hour" className="field-label">
              Година нагадування (0–23)
            </label>
            <input
              id="settings-reminder-hour"
              type="number"
              inputMode="numeric"
              min={0}
              max={23}
              step={1}
              className="text-input"
              value={reminderHourInput}
              onChange={(event) => setReminderHourInput(event.target.value)}
            />
            {formattedHour ? (
              <span className="field-hint">
                Щоденне нагадування надходитиме о {formattedHour}.
              </span>
            ) : null}
          </div>

          <div className="form-field">
            <label htmlFor="settings-timezone" className="field-label">
              Часовий пояс
            </label>
            <select
              id="settings-timezone"
              className="select-input"
              value={timezoneSelect}
              onChange={(event) => setTimezoneSelect(event.target.value)}
            >
              {COMMON_TIMEZONES.map((tz) => (
                <option key={tz.value} value={tz.value}>
                  {tz.label}
                </option>
              ))}
              <option value={CUSTOM_TIMEZONE_OPTION}>
                Інший часовий пояс (ввести вручну)
              </option>
            </select>
          </div>

          {timezoneSelect === CUSTOM_TIMEZONE_OPTION ? (
            <div className="form-field">
              <label htmlFor="settings-timezone-custom" className="field-label">
                Власний часовий пояс (IANA)
              </label>
              <input
                id="settings-timezone-custom"
                type="text"
                className="text-input"
                value={customTimezoneInput}
                onChange={(event) => setCustomTimezoneInput(event.target.value)}
                placeholder="Наприклад, Asia/Tokyo або America/Chicago"
                autoComplete="off"
              />
              <span className="field-hint">
                Укажіть чинну назву IANA-зони без пробілів.
              </span>
            </div>
          ) : null}

          <button
            type="submit"
            className="btn-primary submit-full-width"
            disabled={isSaving}
          >
            {isSaving ? "Зберігаємо…" : "Зберегти налаштування"}
          </button>
        </form>

        {saveFeedback ? (
          <p
            className={`feedback-banner${
              saveFeedback.kind === "error" ? " feedback-banner--error" : ""
            }`}
            role={saveFeedback.kind === "error" ? "alert" : "status"}
          >
            {saveFeedback.text}
          </p>
        ) : null}
      </section>
    </div>
  );
}
