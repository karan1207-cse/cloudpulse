import { useState } from "react";
import { useApi } from "../api/context";
import type { IncidentQuery, IncidentStatus, Severity, SortField } from "../api/types";
import { INCIDENT_STATUSES, SEVERITIES } from "../api/types";
import {
  EmptyState,
  ErrorState,
  IncidentStatusBadge,
  Loading,
  SeverityBadge,
  Timestamp,
} from "../components/common";
import { formatDuration } from "../format";
import { useAsync } from "../hooks";

const COLUMNS: { field: SortField | null; label: string }[] = [
  { field: null, label: "ID" },
  { field: "service_name", label: "Service" },
  { field: "severity", label: "Severity" },
  { field: "status", label: "Status" },
  { field: "created_at", label: "Created (UTC)" },
  { field: "recovered_at", label: "Recovered (UTC)" },
  { field: null, label: "Last error" },
];

const PAGE_SIZE = 20;

export function IncidentsPage() {
  const api = useApi();
  const [query, setQuery] = useState<IncidentQuery>({
    sort: "created_at",
    order: "desc",
    page: 1,
    page_size: PAGE_SIZE,
  });
  const [state, reload] = useAsync(
    (signal) => api.incidents(query, signal),
    [api, JSON.stringify(query)],
  );

  const update = (changes: Partial<IncidentQuery>) =>
    setQuery((q) => ({ ...q, page: 1, ...changes }));

  const toggleSort = (field: SortField) =>
    setQuery((q) => ({
      ...q,
      page: 1,
      sort: field,
      order: q.sort === field && q.order === "desc" ? "asc" : "desc",
    }));

  return (
    <>
      <form className="filters" onSubmit={(e) => e.preventDefault()} aria-label="Incident filters">
        <label>
          Status
          <select
            value={query.status ?? ""}
            onChange={(e) => update({ status: (e.target.value || undefined) as IncidentStatus })}
          >
            <option value="">All</option>
            {INCIDENT_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s === "OPEN" ? "Open" : "Recovered"}
              </option>
            ))}
          </select>
        </label>
        <label>
          Severity
          <select
            value={query.severity ?? ""}
            onChange={(e) => update({ severity: (e.target.value || undefined) as Severity })}
          >
            <option value="">All</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </form>

      {state.status === "loading" && <Loading label="Loading incidents…" />}
      {state.status === "error" && <ErrorState error={state.error} onRetry={reload} />}
      {state.status === "success" && state.data.items.length === 0 && (
        <EmptyState>No incidents match these filters.</EmptyState>
      )}
      {state.status === "success" && state.data.items.length > 0 && (
        <>
          <div className="table-wrap">
            <table>
              <caption>
                Incidents ({state.data.total}
                {state.data.truncated ? ", newest 1000 shown" : ""})
              </caption>
              <thead>
                <tr>
                  {COLUMNS.map(({ field, label }) => {
                    const active = field !== null && query.sort === field;
                    return (
                      <th
                        key={label}
                        scope="col"
                        aria-sort={
                          active ? (query.order === "asc" ? "ascending" : "descending") : undefined
                        }
                      >
                        {field ? (
                          <button type="button" className="sort" onClick={() => toggleSort(field)}>
                            {label}
                            <span aria-hidden="true">
                              {active ? (query.order === "asc" ? " ▲" : " ▼") : ""}
                            </span>
                          </button>
                        ) : (
                          label
                        )}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {state.data.items.map((i) => (
                  <tr key={i.id}>
                    <td className="mono small">{i.id}</td>
                    <td>
                      <a href={`#/services/${encodeURIComponent(i.service_name)}`}>{i.service_name}</a>
                    </td>
                    <td><SeverityBadge severity={i.severity} /></td>
                    <td><IncidentStatusBadge status={i.status} /></td>
                    <td><Timestamp value={i.created_at} /></td>
                    <td>
                      <Timestamp value={i.recovered_at} />
                      {i.duration_seconds !== null && (
                        <div className="muted small">open {formatDuration(i.duration_seconds)}</div>
                      )}
                    </td>
                    <td>
                      {i.last_error_type ? (
                        <>
                          <span className="mono small">{i.last_error_type}</span>
                          <div className="muted small">{i.last_error}</div>
                        </>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav className="pagination" aria-label="Incident pages">
            <button
              type="button"
              disabled={state.data.page <= 1}
              onClick={() => setQuery((q) => ({ ...q, page: (q.page ?? 1) - 1 }))}
            >
              Previous
            </button>
            <span>
              Page {state.data.page} of {state.data.total_pages}
            </span>
            <button
              type="button"
              disabled={state.data.page >= state.data.total_pages}
              onClick={() => setQuery((q) => ({ ...q, page: (q.page ?? 1) + 1 }))}
            >
              Next
            </button>
          </nav>
        </>
      )}
    </>
  );
}
