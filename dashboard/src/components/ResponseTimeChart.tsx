import type { HealthCheck } from "../api/types";
import { formatMs, formatUtc } from "../format";

const WIDTH = 640;
const HEIGHT = 160;
const PAD = { top: 12, right: 12, bottom: 24, left: 48 };

/** Response-time trend (oldest → newest). Failed checks are marked in red. */
export function ResponseTimeChart({ checks }: { checks: HealthCheck[] }) {
  const points = [...checks].sort((a, b) => a.checked_at.localeCompare(b.checked_at));
  if (points.length < 2) {
    return <p className="muted">Not enough checks yet to draw a trend.</p>;
  }
  const max = Math.max(...points.map((p) => p.response_time_ms), 1);
  const innerW = WIDTH - PAD.left - PAD.right;
  const innerH = HEIGHT - PAD.top - PAD.bottom;
  const x = (i: number) => PAD.left + (i / (points.length - 1)) * innerW;
  const y = (v: number) => PAD.top + innerH - (v / max) * innerH;
  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.response_time_ms).toFixed(1)}`)
    .join(" ");
  const first = points[0];
  const last = points[points.length - 1];
  const avg = points.reduce((sum, p) => sum + p.response_time_ms, 0) / points.length;

  return (
    <figure className="chart">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={`Response time for the last ${points.length} checks; average ${formatMs(avg)}, maximum ${formatMs(max)}.`}
      >
        <line className="axis" x1={PAD.left} x2={WIDTH - PAD.right} y1={y(0)} y2={y(0)} />
        <line className="grid" x1={PAD.left} x2={WIDTH - PAD.right} y1={y(max)} y2={y(max)} />
        <text className="tick" x={PAD.left - 6} y={y(max) + 4} textAnchor="end">
          {Math.round(max)}
        </text>
        <text className="tick" x={PAD.left - 6} y={y(0) + 4} textAnchor="end">
          0
        </text>
        <path className="line" d={path} fill="none" />
        {points.map((p, i) =>
          p.is_healthy ? null : (
            <circle key={p.id} className="fail" cx={x(i)} cy={y(p.response_time_ms)} r={3.5}>
              <title>{`${formatUtc(p.checked_at)}: failed (${p.error_type ?? "error"})`}</title>
            </circle>
          ),
        )}
      </svg>
      <figcaption className="muted">
        ms per check, {formatUtc(first?.checked_at)} → {formatUtc(last?.checked_at)}. Red dots
        are failed checks.
      </figcaption>
    </figure>
  );
}
