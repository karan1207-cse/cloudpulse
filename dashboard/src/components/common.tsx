import type { ReactNode } from "react";
import { ApiError } from "../api/errors";
import type { IncidentStatus, ServiceStatus, Severity } from "../api/types";
import { formatRelative, formatUtc } from "../format";

export function Timestamp({ value, now }: { value: string | null | undefined; now?: number }) {
  if (!value) return <span className="muted">—</span>;
  return (
    <time dateTime={value} title={formatUtc(value)}>
      {formatUtc(value)} <span className="muted">({formatRelative(value, now)})</span>
    </time>
  );
}

const STATUS_LABEL: Record<ServiceStatus, string> = {
  healthy: "Healthy",
  unhealthy: "Unhealthy",
  unknown: "No data yet",
};

export function StatusBadge({ status }: { status: ServiceStatus }) {
  return (
    <span className={`badge badge-${status}`}>
      <span aria-hidden="true" className="dot" />
      {STATUS_LABEL[status]}
    </span>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <span className={`badge severity-${severity.toLowerCase()}`}>{severity}</span>;
}

export function IncidentStatusBadge({ status }: { status: IncidentStatus }) {
  return (
    <span className={`badge ${status === "OPEN" ? "badge-unhealthy" : "badge-healthy"}`}>
      {status === "OPEN" ? "Open" : "Recovered"}
    </span>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  const detail =
    error instanceof ApiError && error.status ? ` (HTTP ${error.status})` : "";
  return (
    <div className="state state-error" role="alert">
      <p>
        <strong>Could not load data{detail}.</strong> {error.message}
      </p>
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="state state-empty">{children}</div>;
}

export function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="card" aria-label={title}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}
