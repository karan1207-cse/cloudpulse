import { useApi } from "../api/context";
import { EmptyState, ErrorState, Loading, StatusBadge, Timestamp } from "../components/common";
import { AddServiceLink, CheckAllButton, ServiceActions } from "../components/ServiceActions";
import { FirstRunCallToAction } from "../components/FirstRun";
import { formatMs } from "../format";
import { useAsync } from "../hooks";

export function ServicesPage() {
  const api = useApi();
  const [state, reload] = useAsync((signal) => api.services(signal), [api]);

  if (state.status === "loading") return <Loading label="Loading services…" />;
  if (state.status === "error") return <ErrorState error={state.error} onRetry={reload} />;
  if (state.data.length === 0) {
    return (
      <FirstRunCallToAction>
        <EmptyState>No services are being monitored yet.</EmptyState>
      </FirstRunCallToAction>
    );
  }

  return (
    <>
      <div className="toolbar">
        <AddServiceLink />
        <CheckAllButton onDone={reload} />
      </div>
      <div className="table-wrap">
        <table>
          <caption>Monitored services ({state.data.length})</caption>
          <thead>
            <tr>
              <th scope="col">Service</th>
              <th scope="col">Status</th>
              <th scope="col">Last check (UTC)</th>
              <th scope="col">HTTP</th>
              <th scope="col">Response time</th>
              <th scope="col">Consecutive failures</th>
              <th scope="col">Current incident</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {state.data.map((s) => (
              <tr key={s.name}>
                <th scope="row">
                  <a href={`#/services/${encodeURIComponent(s.name)}`}>{s.name}</a>
                  {!s.enabled && <span className="badge"> paused</span>}
                  <div className="muted small break">{s.url}</div>
                </th>
                <td>
                  <StatusBadge status={s.status} />
                </td>
                <td>
                  <Timestamp value={s.last_checked_at} />
                </td>
                <td>{s.last_status_code ?? "—"}</td>
                <td>{formatMs(s.last_response_time_ms)}</td>
                <td>
                  {s.consecutive_failures} / {s.failure_threshold}
                </td>
                <td>
                  {s.current_incident ? (
                    <span>
                      {s.current_incident.severity} since{" "}
                      <Timestamp value={s.current_incident.created_at} />
                    </span>
                  ) : (
                    <span className="muted">None</span>
                  )}
                </td>
                <td>
                  <ServiceActions service={s} onChanged={reload} compact />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
