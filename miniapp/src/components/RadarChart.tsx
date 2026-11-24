import {
  Chart as ChartJS,
  Filler,
  LineElement,
  PointElement,
  RadialLinearScale,
  Tooltip,
  type ChartData,
  type ChartOptions,
  type TooltipItem,
} from "chart.js";
import { useMemo } from "react";
import { Radar } from "react-chartjs-2";

import type { RadarAxis } from "../api/types";
import { useTheme, type ResolvedTheme } from "../theme/ThemeProvider";

ChartJS.register(RadialLinearScale, PointElement, LineElement, Filler, Tooltip);

export interface RadarThemePalette {
  pointLabelsColor: string;
  ticksColor: string;
  gridColor: string;
  angleLinesColor: string;
  borderColor: string;
  backgroundColor: string;
  tooltipBackgroundColor: string;
  tooltipTextColor: string;
  tooltipBorderColor: string;
}

export const RADAR_THEME_PALETTES: Record<ResolvedTheme, RadarThemePalette> = {
  dark: {
    pointLabelsColor: "#FFFFFF",
    ticksColor: "#A0A0A0",
    gridColor: "rgba(255, 255, 255, 0.20)",
    angleLinesColor: "rgba(255, 255, 255, 0.20)",
    borderColor: "#3B82F6",
    backgroundColor: "rgba(59, 130, 246, 0.25)",
    tooltipBackgroundColor: "#141414",
    tooltipTextColor: "#FFFFFF",
    tooltipBorderColor: "rgba(255, 255, 255, 0.14)",
  },
  light: {
    pointLabelsColor: "#000000",
    ticksColor: "#555555",
    gridColor: "rgba(0, 0, 0, 0.15)",
    angleLinesColor: "rgba(0, 0, 0, 0.15)",
    borderColor: "#2563EB",
    backgroundColor: "rgba(37, 99, 235, 0.22)",
    tooltipBackgroundColor: "#F5F5F7",
    tooltipTextColor: "#000000",
    tooltipBorderColor: "rgba(0, 0, 0, 0.12)",
  },
};

function normalizeAxisScore(axis: RadarAxis): number {
  if (axis.status === "coming_soon") {
    return 0;
  }
  return Math.max(0, Math.min(100, Math.round(axis.score)));
}

export function buildRadarChartData(
  axes: ReadonlyArray<RadarAxis>,
  theme: ResolvedTheme,
): ChartData<"radar"> {
  const palette = RADAR_THEME_PALETTES[theme];

  return {
    labels: axes.map((axis) => axis.label_ua),
    datasets: [
      {
        label: "Ваш прогрес",
        data: axes.map(normalizeAxisScore),
        fill: true,
        borderColor: palette.borderColor,
        backgroundColor: palette.backgroundColor,
        borderWidth: 2,
        pointBackgroundColor: palette.borderColor,
        pointBorderColor: palette.borderColor,
        pointHoverBackgroundColor: palette.pointLabelsColor,
        pointHoverBorderColor: palette.borderColor,
        pointRadius: 4,
        pointHoverRadius: 6,
      },
    ],
  };
}

export function buildRadarChartOptions(
  axes: ReadonlyArray<RadarAxis>,
  theme: ResolvedTheme,
): ChartOptions<"radar"> {
  const palette = RADAR_THEME_PALETTES[theme];

  return {
    responsive: true,
    maintainAspectRatio: false,
    scales: {
      r: {
        min: 0,
        max: 100,
        beginAtZero: true,
        ticks: {
          stepSize: 25,
          color: palette.ticksColor,
          backdropColor: "transparent",
          font: {
            size: 11,
          },
        },
        grid: {
          color: palette.gridColor,
        },
        angleLines: {
          color: palette.angleLinesColor,
        },
        pointLabels: {
          color: palette.pointLabelsColor,
          font: {
            size: 13,
            weight: 600,
          },
        },
      },
    },
    plugins: {
      legend: {
        display: false,
      },
      tooltip: {
        backgroundColor: palette.tooltipBackgroundColor,
        titleColor: palette.tooltipTextColor,
        bodyColor: palette.tooltipTextColor,
        borderColor: palette.tooltipBorderColor,
        borderWidth: 1,
        displayColors: false,
        callbacks: {
          label(context: TooltipItem<"radar">) {
            const axis = axes[context.dataIndex];
            if (axis?.status === "coming_soon") {
              return `${axis.label_ua}: Скоро`;
            }
            const score =
              typeof context.parsed?.r === "number"
                ? context.parsed.r
                : (axis?.score ?? 0);
            const label = axis?.label_ua ?? context.label;
            return `${label}: ${score} / 100`;
          },
        },
      },
    },
  };
}

export interface RadarChartProps {
  axes: ReadonlyArray<RadarAxis>;
}

export function RadarChart({ axes }: RadarChartProps) {
  const { resolvedTheme } = useTheme();

  const data = useMemo(
    () => buildRadarChartData(axes, resolvedTheme),
    [axes, resolvedTheme],
  );

  const options = useMemo(
    () => buildRadarChartOptions(axes, resolvedTheme),
    [axes, resolvedTheme],
  );

  return (
    <div
      className="radar-chart-container"
      role="img"
      aria-label="Спайдерплот прогресу за 5 осями"
    >
      <Radar data={data} options={options} />
    </div>
  );
}
