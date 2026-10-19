import { useApi } from "../api/context";
import {
  Card,
  EmptyState,
  ErrorState,
  IncidentStatusBadge,
  Loading,
  SeverityBadge,
  StatusBadge,
  Timestamp,
} from "../components/common";
import { ResponseTimeChart } from "../components/ResponseTimeChart";
import { ServiceActions } from "../components/ServiceActions";
import { SiteComponents } from "../components/SiteComponents";
import { formatDuration, formatMs } from "../format";
import { useAsync } from "../hooks";

export function ServiceDetailPage({ name }: { name: string }) {
  const api = useApi();
  const [state, reload] = useAsync((signal) => api.service(name, signal), [api, name]);

  if (state.status === "loading") return <Loading label={`Loading ${name}…`} />;
  if (state.status === "error") return <ErrorState error={state.error} onRetry={reload} />;
  const { service, checks, incidents, site } = state.data;
  const timeline = [...incidents].sort((a, b) => b.created_at.localeCompare(a.created_at));

  return (
    <>
      <p>
        <a href="#/services">← All services</a>
      </p>
      <header className="detail-header">
        <h2>{service.name}</h2>
        <StatusBadge status={service.status} />
        {!service.enabled && <span className="badge">paused</span>}
      </header>
      <ServiceActions service={service} onChanged={reload} />
      <dl className="facts">
        <div><dt>URL</dt><dd className="break">{service.url}</dd></div>
        <div><dt>Last check</dt><dd><Timestamp value={service.last_checked_at} /></dd></div>
        <div><dt>Last response</dt><dd>{formatMs(service.last_response_time_ms)} (HTTP {service.last_status_code ?? "—"})</dd></div>
        <div><dt>Consecutive failures</dt><dd>{service.consecutive_failures} (threshold {service.failure_threshold})</dd></div>
        <div><dt>Healthy status range</dt><dd>{service.accepted_status[0]}–{service.accepted_status[1]}</dd></div>
        <div><dt>Timeout</dt><dd>{service.timeout_seconds}s</dd></div>
        <div><dt>Check type</dt><dd>{service.check_mode === "full" ? "Full site" : "Quick (this URL only)"}</dd></div>
        <div><dt>Incident severity</dt><dd><SeverityBadge severity={service.severity} /></dd></div>
        {service.last_error && (
          <div><dt>Last error</dt><dd>{service.last_error_type}: {service.last_error}</dd></div>
        )}
      </dl>

      <SiteComponents service={service} site={site} onChanged={reload} />

      <Card title="Response-time trend">
        <ResponseTimeChart checks={checks} />
      </Card>

      <Card title="Incident history and recovery timeline">
        {timeline.length === 0 ? (
          <EmptyState>No incidents recorded for this service.</EmptyState>
        ) : (
          <ol className="timeline">
            {timeline.map((i) => (
              <li key={i.id} className={i.status === "OPEN" ? "open" : "recovered"}>
                <div>
                  <SeverityBadge severity={i.severity} /> <IncidentStatusBadge status={i.status} />{" "}
                  <span className="mono small">{i.id}</span>
                </div>
                <div>
                  Opened <Timestamp value={i.created_at} /> after {i.failure_count} failed checks
                  {i.last_error_type && <> ({i.last_error_type})</>}
                </div>
                <div>
                  {i.recovered_at ? (
                    <>
                      Recovered <Timestamp value={i.recovered_at} /> — open for{" "}
                      {formatDuration(i.duration_seconds)}
                    </>
                  ) : (
                    <strong>Still open</strong>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>

      <Card title={`Check history (last ${checks.length})`}>
        {checks.length === 0 ? (
          <EmptyState>No checks recorded yet.</EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="sr-only">Health-check history for {service.name}</caption>
              <thead>
                <tr>
                  <th scope="col">Checked (UTC)</th>
                  <th scope="col">Result</th>
                  <th scope="col">HTTP</th>
                  <th scope="col">Response time</th>
                  <th scope="col">Error</th>
                </tr>
              </thead>
              <tbody>
                {checks.map((c) => (
                  <tr key={c.id}>
                    <td><Timestamp value={c.checked_at} /></td>
                    <td>{c.is_healthy ? <span className="ok">Healthy</span> : <span className="fail-text">Failed</span>}</td>
                    <td>{c.status_code ?? "—"}</td>
                    <td>{formatMs(c.response_time_ms)}</td>
                    <td>{c.error ? `${c.error_type}: ${c.error}` : "—"}</td>
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
