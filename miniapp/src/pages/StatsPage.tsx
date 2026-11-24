import { useCallback, useEffect, useRef, useState } from "react";

import { apiClient, ApiError } from "../api/client";
import type { RadarAxis, RadarStatsResponse, WeakTopicItem } from "../api/types";
import { RadarChart } from "../components/RadarChart";
import { getTelegramWebApp, type TelegramWebApp } from "../telegram";

type StatsLoadStatus = "loading" | "error" | "success";

type TelegramWebAppWithOpenLink = TelegramWebApp & {
  openLink?: (url: string, options?: { try_instant_view?: boolean }) => void;
};

export function buildYoutubeSearchUrl(topic: WeakTopicItem): string {
  const rawQuery = topic.youtube_topic?.trim() || topic.grammar_topic.trim();
  return `https://www.youtube.com/results?search_query=${encodeURIComponent(rawQuery)}`;
}

export function openExternalLink(url: string): boolean {
  const webApp = getTelegramWebApp() as TelegramWebAppWithOpenLink | null;
  if (webApp && typeof webApp.openLink === "function") {
    try {
      webApp.openLink(url);
      return true;
    } catch {
      return false;
    }
  }
  return false;
}

export function formatAttemptsCount(count: number): string {
  const absCount = Math.abs(Math.trunc(count));
  const mod100 = absCount % 100;
  const mod10 = absCount % 10;

  if (mod10 === 1 && mod100 !== 11) {
    return `${absCount} спроба`;
  }
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
    return `${absCount} спроби`;
  }
  return `${absCount} спроб`;
}

export function formatAverageScore(score: number): string {
  const clamped = Math.max(0, Math.min(100, score));
  const rounded = Math.round(clamped * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

export function isEmptyRadarStats(data: RadarStatsResponse): boolean {
  return (
    data.axes.every((axis) => axis.score === 0) &&
    data.weak_topics.length === 0
  );
}

function AxisCard({ axis }: { axis: RadarAxis }) {
  const isComingSoon = axis.status === "coming_soon";
  const displayScore = isComingSoon
    ? 0
    : Math.max(0, Math.min(100, Math.round(axis.score)));

  return (
    <li
      className={`axis-card${isComingSoon ? " axis-card--coming-soon" : ""}`}
      data-axis={axis.axis}
    >
      <div className="axis-card-header">
        <span className="axis-name">{axis.label_ua}</span>
        {isComingSoon ? (
          <span className="coming-soon-badge">Скоро</span>
        ) : null}
      </div>

      <div className="axis-score-row">
        <span className="axis-score">{displayScore}</span>
        <span className="axis-score-max">/ 100</span>
      </div>

      {!isComingSoon ? (
        <div className="axis-progress-track" aria-hidden="true">
          <div
            className="axis-progress-fill"
            style={{ width: `${displayScore}%` }}
          />
        </div>
      ) : null}
    </li>
  );
}

export interface StatsPageProps {
  loadRadarStats?: () => Promise<RadarStatsResponse>;
}

export function StatsPage({ loadRadarStats }: StatsPageProps) {
  const [status, setStatus] = useState<StatsLoadStatus>("loading");
  const [data, setData] = useState<RadarStatsResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const isMountedRef = useRef(true);
  const requestIdRef = useRef(0);

  const fetchStats = useCallback(async () => {
    const currentRequestId = ++requestIdRef.current;
    setStatus("loading");
    setErrorMessage(null);

    try {
      const fetcher = loadRadarStats ?? (() => apiClient.getRadarStats());
      const response = await fetcher();
      if (!isMountedRef.current || requestIdRef.current !== currentRequestId) {
        return;
      }
      setData(response);
      setStatus("success");
    } catch (error: unknown) {
      if (!isMountedRef.current || requestIdRef.current !== currentRequestId) {
        return;
      }
      if (error instanceof ApiError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage(
          "Не вдалося завантажити статистику. Перевірте з'єднання і спробуйте ще раз.",
        );
      }
      setStatus("error");
    }
  }, [loadRadarStats]);

  useEffect(() => {
    isMountedRef.current = true;
    void fetchStats();

    return () => {
      isMountedRef.current = false;
      requestIdRef.current += 1;
    };
  }, [fetchStats]);

  if (status === "loading") {
    return (
      <section
        className="surface-card stats-loading-card"
        role="status"
        aria-live="polite"
        aria-busy="true"
      >
        <h2 className="card-title">Статистика прогресу</h2>
        <p className="card-description">Завантажуємо вашу статистику…</p>
        <div className="stats-skeleton" aria-hidden="true">
          <div className="skeleton-circle" />
          <div className="skeleton-grid">
            <div className="skeleton-card" />
            <div className="skeleton-card" />
            <div className="skeleton-card" />
            <div className="skeleton-card" />
          </div>
        </div>
      </section>
    );
  }

  if (status === "error" || data === null) {
    return (
      <section className="surface-card stats-error-card" role="alert">
        <h2 className="card-title">Не вдалося завантажити статистику</h2>
        <p className="card-description">
          {errorMessage ??
            "Сталася непередбачена помилка. Спробуйте ще раз трохи згодом."}
        </p>
        <button
          type="button"
          className="retry-button"
          onClick={() => {
            void fetchStats();
          }}
        >
          Спробувати ще раз
        </button>
      </section>
    );
  }

  const isEmpty = isEmptyRadarStats(data);
  const weakTopics = data.weak_topics.slice(0, 3);

  return (
    <div className="stats-page">
      <section className="surface-card" aria-labelledby="stats-radar-title">
        <div className="stats-section-header">
          <h2 id="stats-radar-title" className="card-title">
            Статистика прогресу
          </h2>
          <p className="card-description">
            Шкала 0–100, де 100 — впевнений рівень B1
          </p>
        </div>

        <RadarChart axes={data.axes} />

        <ul className="axes-grid" aria-label="Показники за осями">
          {data.axes.map((axis) => (
            <AxisCard key={axis.axis} axis={axis} />
          ))}
        </ul>
      </section>

      {isEmpty ? (
        <section
          className="surface-card empty-stats-card"
          aria-labelledby="empty-stats-title"
        >
          <h2 id="empty-stats-title" className="card-title">
            Поки що немає даних для статистики
          </h2>
          <p className="card-description">
            Пройдіть перше тренування в Telegram-боті, щоб побачити свій прогрес
            за шкалою B1 та рекомендації для повторення.
          </p>
        </section>
      ) : (
        <section className="surface-card" aria-labelledby="weak-topics-title">
          <div className="stats-section-header">
            <h2 id="weak-topics-title" className="card-title">
              Топ-3 слабкі теми за 14 днів
            </h2>
          </div>

          {weakTopics.length === 0 ? (
            <p className="card-description">
              За останні 14 днів слабких тем не виявлено. Чудовий темп —
              продовжуйте практикуватися в боті!
            </p>
          ) : (
            <ul className="weak-topics-list">
              {weakTopics.map((topic) => {
                const youtubeUrl = buildYoutubeSearchUrl(topic);
                return (
                  <li key={topic.grammar_topic} className="weak-topic-item">
                    <div className="weak-topic-info">
                      <span className="weak-topic-name">
                        {topic.grammar_topic}
                      </span>
                      <span className="weak-topic-meta">
                        Середній бал: {formatAverageScore(topic.avg_grammar_score)} / 100 ·{" "}
                        {formatAttemptsCount(topic.attempts_count)}
                      </span>
                    </div>

                    <a
                      href={youtubeUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="youtube-link"
                      aria-label={`Знайти тему «${topic.grammar_topic}» на YouTube`}
                      onClick={(event) => {
                        if (openExternalLink(youtubeUrl)) {
                          event.preventDefault();
                        }
                      }}
                    >
                      Знайти на YouTube
                    </a>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
