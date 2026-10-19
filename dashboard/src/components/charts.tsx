import { useId } from "react";
import { money } from "../format";

/** A price line with a soft area underneath. Green if it ends above where it started. */
export function Sparkline({ points, label, width = 140, height = 40 }: {
  points: number[]; label: string; width?: number; height?: number;
}) {
  const gradient = useId();
  if (points.length < 2) return null;
  let low = points[0] as number;
  let high = low;
  for (const point of points) {
    low = Math.min(low, point);
    high = Math.max(high, point);
  }
  const span = high - low || 1;
  const step = width / (points.length - 1);
  const coords = points.map((point, index) => {
    const x = index * step;
    const y = height - 3 - ((point - low) / span) * (height - 6);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const rising = (points[points.length - 1] as number) >= (points[0] as number);
  const tone = rising ? "spark-up" : "spark-down";
  return (
    <svg className={`sparkline ${tone}`} viewBox={`0 0 ${width} ${height}`} role="img"
         aria-label={label} preserveAspectRatio="none">
      <defs>
        <linearGradient id={gradient} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor="currentColor" stopOpacity="0.28" />
          <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={`0,${height} ${coords.join(" ")} ${width},${height}`}
               fill={`url(#${gradient})`} />
      <polyline points={coords.join(" ")} fill="none" stroke="currentColor" strokeWidth="1.6"
                strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export interface Slice {
  label: string;
  value: number;
}

const RADIUS = 42;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** Portfolio allocation as a donut. The accessible name lists every slice in dollars. */
export function Donut({ title, slices }: { title: string; slices: Slice[] }) {
  const total = slices.reduce((sum, slice) => sum + Math.max(slice.value, 0), 0) || 1;
  const arcs: { dash: number; offset: number }[] = [];
  let start = 0;
  for (const slice of slices) {
    const dash = (Math.max(slice.value, 0) / total) * CIRCUMFERENCE;
    arcs.push({ dash, offset: start });
    start += dash;
  }
  const summary = slices.map((slice) => `${slice.label} ${money(slice.value)}`).join(", ");
  return (
    <figure className="donut">
      <svg viewBox="0 0 100 100" role="img" aria-label={`${title}: ${summary}`}>
        <circle cx="50" cy="50" r={RADIUS} className="donut-track" />
        {slices.map((slice, index) => {
          const arc = arcs[index] ?? { dash: 0, offset: 0 };
          return (
            <circle key={slice.label} cx="50" cy="50" r={RADIUS}
                    className={`donut-slice slice-${index % 8}`}
                    strokeDasharray={`${arc.dash} ${CIRCUMFERENCE - arc.dash}`}
                    strokeDashoffset={-arc.offset} transform="rotate(-90 50 50)" />
          );
        })}
      </svg>
      <figcaption>
        <ul className="legend">
          {slices.map((slice, index) => (
            <li key={slice.label}>
              <span className={`swatch slice-${index % 8}`} aria-hidden="true" />
              <span>{slice.label}</span>
              <span className="muted">{((Math.max(slice.value, 0) / total) * 100).toFixed(1)}%</span>
            </li>
          ))}
        </ul>
      </figcaption>
    </figure>
  );
}
