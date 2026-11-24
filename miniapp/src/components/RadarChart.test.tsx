import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Chart as ChartJS, type ChartData, type ChartOptions } from "chart.js";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RadarAxis } from "../api/types";
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
  buildRadarChartData,
  buildRadarChartOptions,
  RadarChart,
} from "./RadarChart";

const SAMPLE_AXES: RadarAxis[] = [
  { axis: "vocab", label_ua: "Словник", score: 64, status: "active" },
  { axis: "grammar", label_ua: "Граматика", score: 52, status: "active" },
  { axis: "consistency", label_ua: "Стабільність", score: 85, status: "active" },
  { axis: "writing", label_ua: "Шрайбен", score: 0, status: "coming_soon" },
  { axis: "listening", label_ua: "Хьорен", score: 0, status: "coming_soon" },
];

function ThemeControlledRadar({ axes }: { axes: RadarAxis[] }) {
  const { setThemeMode } = useTheme();
  return (
    <div>
      <button type="button" onClick={() => setThemeMode("dark")}>
        Увімкнути темну
      </button>
      <button type="button" onClick={() => setThemeMode("light")}>
        Увімкнути світлу
      </button>
      <RadarChart axes={axes} />
    </div>
  );
}

describe("RadarChart", () => {
  beforeEach(() => {
    capturedRadarProps.calls.length = 0;
    window.localStorage.clear();
    delete window.Telegram;
    delete document.documentElement.dataset.theme;
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.localStorage.clear();
    delete window.Telegram;
    delete document.documentElement.dataset.theme;
  });

  it("registers required Chart.js modules (RadialLinearScale, PointElement, LineElement, Filler, Tooltip)", () => {
    expect(ChartJS.registry.scales.get("radialLinear")).toBeDefined();
    expect(ChartJS.registry.elements.get("point")).toBeDefined();
    expect(ChartJS.registry.elements.get("line")).toBeDefined();
    expect(ChartJS.registry.plugins.get("filler")).toBeDefined();
    expect(ChartJS.registry.plugins.get("tooltip")).toBeDefined();
  });

  it("builds exact dark theme color tokens, 0-100 scale, and transparent tick backdrop", () => {
    const data = buildRadarChartData(SAMPLE_AXES, "dark");
    const options = buildRadarChartOptions(SAMPLE_AXES, "dark");

    expect(data.labels).toEqual([
      "Словник",
      "Граматика",
      "Стабільність",
      "Шрайбен",
      "Хьорен",
    ]);
    expect(data.datasets[0]?.data).toEqual([64, 52, 85, 0, 0]);
    expect(data.datasets[0]?.borderColor).toBe("#3B82F6");
    expect(data.datasets[0]?.backgroundColor).toBe("rgba(59, 130, 246, 0.25)");

    const rScale = options.scales?.r as Record<string, unknown>;
    expect(rScale.min).toBe(0);
    expect(rScale.max).toBe(100);
    expect((rScale.pointLabels as Record<string, unknown>).color).toBe("#FFFFFF");
    expect((rScale.ticks as Record<string, unknown>).color).toBe("#A0A0A0");
    expect((rScale.ticks as Record<string, unknown>).backdropColor).toBe(
      "transparent",
    );
    expect((rScale.grid as Record<string, unknown>).color).toBe(
      "rgba(255, 255, 255, 0.20)",
    );
    expect((rScale.angleLines as Record<string, unknown>).color).toBe(
      "rgba(255, 255, 255, 0.20)",
    );
  });

  it("builds exact light theme color tokens, 0-100 scale, and transparent tick backdrop", () => {
    const data = buildRadarChartData(SAMPLE_AXES, "light");
    const options = buildRadarChartOptions(SAMPLE_AXES, "light");

    expect(data.datasets[0]?.borderColor).toBe("#2563EB");
    expect(data.datasets[0]?.backgroundColor).toBe("rgba(37, 99, 235, 0.22)");

    const rScale = options.scales?.r as Record<string, unknown>;
    expect(rScale.min).toBe(0);
    expect(rScale.max).toBe(100);
    expect((rScale.pointLabels as Record<string, unknown>).color).toBe("#000000");
    expect((rScale.ticks as Record<string, unknown>).color).toBe("#555555");
    expect((rScale.ticks as Record<string, unknown>).backdropColor).toBe(
      "transparent",
    );
    expect((rScale.grid as Record<string, unknown>).color).toBe(
      "rgba(0, 0, 0, 0.15)",
    );
    expect((rScale.angleLines as Record<string, unknown>).color).toBe(
      "rgba(0, 0, 0, 0.15)",
    );
  });

  it("forces coming_soon axes to 0 and formats Ukrainian tooltip labels", () => {
    const axesWithUnexpectedScore: RadarAxis[] = [
      { axis: "vocab", label_ua: "Словник", score: 120, status: "active" },
      { axis: "grammar", label_ua: "Граматика", score: -5, status: "active" },
      { axis: "consistency", label_ua: "Стабільність", score: 45, status: "active" },
      { axis: "writing", label_ua: "Шрайбен", score: 30, status: "coming_soon" },
      { axis: "listening", label_ua: "Хьорен", score: 10, status: "coming_soon" },
    ];

    const data = buildRadarChartData(axesWithUnexpectedScore, "dark");
    expect(data.datasets[0]?.data).toEqual([100, 0, 45, 0, 0]);

    const options = buildRadarChartOptions(axesWithUnexpectedScore, "dark");
    const labelCallback = options.plugins?.tooltip?.callbacks?.label as (
      ctx: unknown,
    ) => string;

    expect(
      labelCallback({ dataIndex: 2, label: "Стабільність", parsed: { r: 45 } }),
    ).toBe("Стабільність: 45 / 100");
    expect(
      labelCallback({ dataIndex: 3, label: "Шрайбен", parsed: { r: 0 } }),
    ).toBe("Шрайбен: Скоро");
  });

  it("dynamically updates Chart.js config when resolvedTheme switches dark <-> light", () => {
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

    render(
      <ThemeProvider>
        <ThemeControlledRadar axes={SAMPLE_AXES} />
      </ThemeProvider>,
    );

    expect(screen.getByTestId("radar-canvas")).toBeTruthy();

    const darkCall = capturedRadarProps.calls.at(-1)!;
    expect(darkCall.data.datasets[0]?.borderColor).toBe("#3B82F6");
    expect(darkCall.data.datasets[0]?.backgroundColor).toBe(
      "rgba(59, 130, 246, 0.25)",
    );
    const darkScale = darkCall.options.scales?.r as Record<string, unknown>;
    expect((darkScale.pointLabels as Record<string, unknown>).color).toBe(
      "#FFFFFF",
    );
    expect((darkScale.ticks as Record<string, unknown>).color).toBe("#A0A0A0");
    expect((darkScale.ticks as Record<string, unknown>).backdropColor).toBe(
      "transparent",
    );
    expect((darkScale.grid as Record<string, unknown>).color).toBe(
      "rgba(255, 255, 255, 0.20)",
    );
    expect((darkScale.angleLines as Record<string, unknown>).color).toBe(
      "rgba(255, 255, 255, 0.20)",
    );

    fireEvent.click(screen.getByRole("button", { name: "Увімкнути світлу" }));

    const lightCall = capturedRadarProps.calls.at(-1)!;
    expect(lightCall.data.datasets[0]?.borderColor).toBe("#2563EB");
    expect(lightCall.data.datasets[0]?.backgroundColor).toBe(
      "rgba(37, 99, 235, 0.22)",
    );
    const lightScale = lightCall.options.scales?.r as Record<string, unknown>;
    expect((lightScale.pointLabels as Record<string, unknown>).color).toBe(
      "#000000",
    );
    expect((lightScale.ticks as Record<string, unknown>).color).toBe("#555555");
    expect((lightScale.ticks as Record<string, unknown>).backdropColor).toBe(
      "transparent",
    );
    expect((lightScale.grid as Record<string, unknown>).color).toBe(
      "rgba(0, 0, 0, 0.15)",
    );
    expect((lightScale.angleLines as Record<string, unknown>).color).toBe(
      "rgba(0, 0, 0, 0.15)",
    );

    fireEvent.click(screen.getByRole("button", { name: "Увімкнути темну" }));

    const backToDarkCall = capturedRadarProps.calls.at(-1)!;
    expect(backToDarkCall.data.datasets[0]?.borderColor).toBe("#3B82F6");
    const backToDarkScale = backToDarkCall.options.scales?.r as Record<
      string,
      unknown
    >;
    expect((backToDarkScale.pointLabels as Record<string, unknown>).color).toBe(
      "#FFFFFF",
    );
  });
});
