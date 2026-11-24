import type { ThemeMode } from "../theme/ThemeProvider";

export type UserLevel = "A1" | "A2" | "B1";
export type UserTheme = ThemeMode;

export interface UserProfile {
  telegram_id: number;
  level: UserLevel;
  exercises_per_set: number;
  theme: UserTheme;
  sheets_url: string | null;
  reminder_hour: number;
  timezone: string;
  daily_llm_calls: number;
  created_at: string;
}

export interface UserSettingsUpdate {
  level?: UserLevel;
  exercises_per_set?: number;
  theme?: UserTheme;
  sheets_url?: string | null;
  reminder_hour?: number;
  timezone?: string;
}

export type RadarAxisKey =
  | "vocab"
  | "grammar"
  | "consistency"
  | "writing"
  | "listening";

export type RadarAxisStatus = "active" | "coming_soon";

export interface RadarAxis {
  axis: RadarAxisKey;
  label_ua: string;
  score: number;
  status: RadarAxisStatus;
}

export interface WeakTopicItem {
  grammar_topic: string;
  avg_grammar_score: number;
  attempts_count: number;
  youtube_topic: string | null;
}

export interface RadarStatsResponse {
  axes: RadarAxis[];
  weak_topics: WeakTopicItem[];
}

export type WordSource = "text" | "file" | "sheets" | "system";

export interface WordItem {
  id: number;
  word_de: string;
  translation_ua: string;
  part_of_speech: string | null;
  source: WordSource;
  state: number;
  stability: number;
  difficulty: number;
  due_at: string | null;
  reps: number;
  lapses: number;
}

export interface WordListResponse {
  items: WordItem[];
  total: number;
}

export interface GetWordsParams {
  search?: string;
  limit?: number;
  offset?: number;
}

export interface WordImportResponse {
  imported: number;
  duplicates: number;
  skipped: number;
}

export interface SheetsSyncRequest {
  sheets_url?: string | null;
}
