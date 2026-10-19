import { useApi } from "../api/context";
import {
  Card,
  EmptyState,
  ErrorState,
  IncidentStatusBadge,
  Loading,
  SeverityBadge,
  Timestamp,
} from "../components/common";
import { formatDuration, formatMs } from "../format";
import { useAsync } from "../hooks";

export function OverviewPage() {
  const api = useApi();
  const [state, reload] = useAsync((signal) => api.overview(signal), [api]);

  if (state.status === "loading") return <Loading label="Loading overview…" />;
  if (state.status === "error") return <ErrorState error={state.error} onRetry={reload} />;
  const { totals, open_incidents, recent_recoveries, recent_checks, generated_at } = state.data;

  if (totals.services === 0) {
    return (
      <EmptyState>
        No services are configured. Add services to your services file and run{" "}
        <code>cloudpulse run</code>.
      </EmptyState>
    );
  }

  const stats = [
    { label: "Services", value: totals.services, tone: "" },
    { label: "Healthy", value: totals.healthy, tone: "good" },
    { label: "Unhealthy", value: totals.unhealthy, tone: totals.unhealthy ? "bad" : "" },
    { label: "No data yet", value: totals.unknown, tone: "" },
    { label: "Open incidents", value: totals.open_incidents, tone: totals.open_incidents ? "bad" : "" },
  ];

  return (
    <>
      <p className="muted">
        Data as of <Timestamp value={generated_at} />{" "}
        <button type="button" className="link" onClick={reload}>
          Refresh
        </button>
      </p>
      <dl className="stats">
        {stats.map((s) => (
          <div key={s.label} className={`stat ${s.tone}`}>
            <dt>{s.label}</dt>
            <dd>{s.value}</dd>
          </div>
        ))}
      </dl>

      <div className="grid-2">
        <Card title="Open incidents">
          {open_incidents.length === 0 ? (
            <EmptyState>No open incidents.</EmptyState>
          ) : (
            <ul className="list">
              {open_incidents.map((i) => (
                <li key={i.id}>
                  <SeverityBadge severity={i.severity} />{" "}
                  <a href={`#/services/${encodeURIComponent(i.service_name)}`}>{i.service_name}</a>{" "}
                  — since <Timestamp value={i.created_at} />
                  {i.last_error && <div className="muted small">{i.last_error}</div>}
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Recent recoveries">
          {recent_recoveries.length === 0 ? (
            <EmptyState>No recoveries recorded yet.</EmptyState>
          ) : (
            <ul className="list">
              {recent_recoveries.map((i) => (
                <li key={i.id}>
                  <IncidentStatusBadge status={i.status} />{" "}
                  <a href={`#/services/${encodeURIComponent(i.service_name)}`}>{i.service_name}</a>{" "}
                  at <Timestamp value={i.recovered_at} />{" "}
                  <span className="muted">(open {formatDuration(i.duration_seconds)})</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card title="Recent checks">
        {recent_checks.length === 0 ? (
          <EmptyState>No checks recorded yet. Run <code>cloudpulse run</code>.</EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="sr-only">Most recent health checks across services</caption>
              <thead>
                <tr>
                  <th scope="col">Checked (UTC)</th>
                  <th scope="col">Service</th>
                  <th scope="col">Result</th>
                  <th scope="col">HTTP</th>
                  <th scope="col">Response time</th>
                </tr>
              </thead>
              <tbody>
                {recent_checks.map((c) => (
                  <tr key={c.id}>
                    <td><Timestamp value={c.checked_at} /></td>
                    <td>{c.service}</td>
                    <td>
                      {c.is_healthy ? (
                        <span className="ok">Healthy</span>
                      ) : (
                        <span className="fail-text">Failed: {c.error_type ?? "error"}</span>
                      )}
                    </td>
                    <td>{c.status_code ?? "—"}</td>
                    <td>{formatMs(c.response_time_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
