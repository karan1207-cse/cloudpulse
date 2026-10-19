import { useId, useState, type FormEvent } from "react";
import { useApi } from "../api/context";
import { ApiError } from "../api/errors";
import type { ServiceSummary, SiteComponent, SiteView } from "../api/types";
import { useAppState } from "../appState";
import { formatMs } from "../format";
import { Card, Timestamp } from "./common";

const KIND_LABEL: Record<string, string> = { page: "Page", asset: "File", backend: "Backend API" };
const STATUS_LABEL: Record<string, string> = {
  up: "Up",
  down: "Down",
  skipped: "Skipped (time limit)",
  blocked: "Not checked (private address)",
  disabled: "Turned off",
  pending: "Not checked yet",
};
const SOURCE_LABEL: Record<string, string> = {
  main: "the address you entered",
  html: "found in the page",
  discovered: "found in the site's code",
  manual: "added by you",
};

function statusClass(status: string): string {
  if (status === "up") return "badge badge-healthy";
  if (status === "down") return "badge badge-unhealthy";
  return "badge";
}

function problem(component: SiteComponent): string | null {
  if (component.status !== "down") return null;
  if (component.error_type === "PRIVATE_BACKEND")
    return "Points to a local/private address that visitors cannot reach — the site was probably built with a development API URL.";
  if (component.error_type === "TIMEOUT") return "Not responding (timeout).";
  if (component.error_type === "DNS_ERROR") return "Address cannot be found (DNS).";
  if (component.status_code && component.status_code >= 500)
    return `Server error (HTTP ${component.status_code}).`;
  if (component.status_code === 404) return "Missing (HTTP 404).";
  return component.error ?? "Failed.";
}

interface Props {
  service: ServiceSummary;
  site: SiteView | undefined;
  onChanged: () => void;
}

export function SiteComponents({ service, site, onChanged }: Props) {
  const api = useApi();
  const { health, notify } = useAppState();
  const id = useId();
  const [busy, setBusy] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [kind, setKind] = useState<"backend" | "page">("backend");
  const [error, setError] = useState<string | null>(null);
  const canChange = Boolean(health?.can_write || health?.auth_required);

  if (service.check_mode !== "full") {
    return (
      <Card title="What we check">
        <p>
          Quick check: only <span className="break">{service.url}</span> itself. A page can answer
          normally while the app behind it is broken; switch to <strong>Full site</strong> in
          {service.editable ? (
            <>
              {" "}
              <a href={`#/services/${encodeURIComponent(service.name)}/edit`}>Edit</a>
            </>
          ) : (
            " the service settings"
          )}{" "}
          to also check its files, pages and backend APIs.
        </p>
      </Card>
    );
  }

  const components = site?.components ?? [];

  async function act(action: "disable" | "enable" | "remove", target: string) {
    setBusy(target);
    try {
      await api.componentAction(service.name, action, target);
      notify(
        action === "disable"
          ? `Turned off ${target}. It will no longer affect this service's health.`
          : action === "enable"
            ? `Turned on ${target}.`
            : `Removed ${target}.`,
      );
      onChanged();
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(null);
    }
  }

  async function add(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const value = url.trim();
    if (!/^https?:\/\//i.test(value)) {
      setError("Enter a full address starting with https:// (for example https://my-api.onrender.com).");
      return;
    }
    setBusy("add");
    try {
      await api.addComponent(service.name, value, kind);
      notify(`Added ${value}. It will be checked from the next check on.`);
      setUrl("");
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card title="What we check">
      <p className="muted small">
        Full-site check: the main page, its own files, up to {site?.limits.max_pages ?? 10} of its
        pages and up to {site?.limits.max_backends ?? 5} backend APIs found in its code. Backends
        only need to answer (even “not found” counts as up).
        {site?.discovered_at && (
          <>
            {" "}
            Last discovered <Timestamp value={site.discovered_at} />; refreshed every 6 hours and on
            “Check now”.
          </>
        )}
      </p>
      {components.length === 0 ? (
        <p className="muted">Nothing checked yet. Press “Check now” to discover what this site uses.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <caption className="sr-only">Components checked for {service.name}</caption>
            <thead>
              <tr>
                <th scope="col">What</th>
                <th scope="col">Status</th>
                <th scope="col">Response</th>
                {canChange && <th scope="col">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {components.map((c) => {
                const note = problem(c);
                return (
                  <tr key={`${c.kind}-${c.url}`}>
                    <th scope="row">
                      <span className="badge">{c.role === "main" ? "Main page" : KIND_LABEL[c.kind] ?? c.kind}</span>{" "}
                      <span className="break">{c.url}</span>
                      <div className="muted small">{SOURCE_LABEL[c.source] ?? c.source}</div>
                    </th>
                    <td>
                      <span className={statusClass(c.status)}>{STATUS_LABEL[c.status] ?? c.status}</span>
                      {note && <div className="fail-text small">{note}</div>}
                    </td>
                    <td>
                      {c.status_code ? `HTTP ${c.status_code}` : "—"}
                      {c.response_time_ms !== null && c.status !== "blocked" && (
                        <div className="muted small">{formatMs(c.response_time_ms)}</div>
                      )}
                    </td>
                    {canChange && (
                      <td>
                        {c.role !== "main" && (
                          <div className="actions compact">
                            {c.status === "disabled" ? (
                              <button type="button" onClick={() => act("enable", c.url)} disabled={busy !== null}>
                                Turn on<span className="sr-only"> {c.url}</span>
                              </button>
                            ) : (
                              <button type="button" onClick={() => act("disable", c.url)} disabled={busy !== null}>
                                Turn off<span className="sr-only"> {c.url}</span>
                              </button>
                            )}
                            {c.source === "manual" && (
                              <button
                                type="button"
                                className="danger"
                                onClick={() => act("remove", c.url)}
                                disabled={busy !== null}
                              >
                                Remove<span className="sr-only"> {c.url}</span>
                              </button>
                            )}
                          </div>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {canChange && (
        <form className="add-component" onSubmit={add} aria-label="Add a component to check" noValidate>
          <div className="field">
            <label htmlFor={`${id}-component-url`}>Add something we missed</label>
            <input
              id={`${id}-component-url`}
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://my-api.onrender.com"
              inputMode="url"
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? `${id}-component-error` : undefined}
            />
            {error && (
              <p className="field-error" id={`${id}-component-error`}>
                {error}
              </p>
            )}
          </div>
          <div className="field">
            <label htmlFor={`${id}-component-kind`}>Type</label>
            <select
              id={`${id}-component-kind`}
              value={kind}
              onChange={(e) => setKind(e.target.value as "backend" | "page")}
            >
              <option value="backend">Backend API</option>
              <option value="page">Page</option>
            </select>
          </div>
          <button type="submit" disabled={busy !== null}>
            Add
          </button>
        </form>
      )}

      <details className="limits-note">
        <summary>What a full-site check cannot catch</summary>
        <ul>
          <li>Features behind a login.</li>
          <li>
            Actions that fail while the server is up — for example a “Quick Add” whose AI provider
            returns an error. CloudPulse only sends GET requests and never submits forms.
          </li>
          <li>Backends whose address is only decided while the app runs (not in its code).</li>
        </ul>
        <p>
          For those, you can add a <code>/health</code> endpoint to your backend that tests its own
          dependencies, and add it here as a Backend API (optional).
        </p>
      </details>
    </Card>
  );
}
