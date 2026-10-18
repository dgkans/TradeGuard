export interface Bar {
  label: string;
  value: number | null;
  /** Optional range drawn as a whisker (min and max over repeats). */
  low?: number | null;
  high?: number | null;
}

interface Props {
  title: string;
  bars: Bar[];
  format: (value: number) => string;
  /** Scale maximum. Defaults to the largest value. */
  max?: number;
  /** Width of the label column, in chart units (the chart is 360 wide). */
  labelWidth?: number;
}

const ROW = 28;
const VALUE_W = 64;
const WIDTH = 360;

/** Horizontal bar chart as plain SVG. Missing values are labelled "n/a", never drawn as 0. */
export function BarChart({ title, bars, format, max, labelWidth = 92 }: Props) {
  const LABEL_W = labelWidth;
  let top = max ?? 0;
  if (max === undefined) {
    for (const bar of bars) {
      top = Math.max(top, bar.high ?? bar.value ?? 0);
    }
  }
  const scale = top > 0 ? top : 1;
  const plot = WIDTH - LABEL_W - VALUE_W;
  const height = bars.length * ROW + 8;
  const summary = bars
    .map((bar) => `${bar.label} ${bar.value === null ? "n/a" : format(bar.value)}`)
    .join(", ");

  return (
    <figure className="chart">
      <figcaption>{title}</figcaption>
      <svg viewBox={`0 0 ${WIDTH} ${height}`} role="img" aria-label={`${title}: ${summary}`}
           preserveAspectRatio="xMinYMin meet">
        {bars.map((bar, index) => {
          const y = index * ROW + 4;
          const width = bar.value === null ? 0 : Math.max((bar.value / scale) * plot, 1.5);
          const low = bar.low ?? null;
          const high = bar.high ?? null;
          return (
            <g key={bar.label}>
              <text x={LABEL_W - 8} y={y + ROW / 2} className="chart-label" textAnchor="end"
                    dominantBaseline="middle">
                {bar.label}
              </text>
              <rect x={LABEL_W} y={y + 5} width={plot} height={ROW - 10} className="chart-track"
                    rx={3} />
              {bar.value !== null ? (
                <rect x={LABEL_W} y={y + 5} width={width} height={ROW - 10}
                      className={`chart-bar chart-bar-${index % 3}`} rx={3} />
              ) : null}
              {low !== null && high !== null && high > low ? (
                <line x1={LABEL_W + (low / scale) * plot} x2={LABEL_W + (high / scale) * plot}
                      y1={y + ROW / 2} y2={y + ROW / 2} className="chart-whisker" />
              ) : null}
              <text x={WIDTH - VALUE_W + 6} y={y + ROW / 2} className="chart-value"
                    dominantBaseline="middle">
                {bar.value === null ? "n/a" : format(bar.value)}
              </text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}
