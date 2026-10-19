import { useEffect, useRef, useState } from "react";
import { useApi } from "../api/context";
import type { CheckResult, ServiceSummary } from "../api/types";
import { useAppState } from "../appState";
import { formatMs } from "../format";
import { navigate } from "../hooks";

export function describeCheck(result: CheckResult): string {
  const name = result.service.name;
  if (result.failure) return `Could not check ${name}: ${result.failure.error}`;
  const outcome = result.outcome;
  if (!outcome) return `Checked ${name}.`;
  if (outcome.is_healthy) {
    const recovered = outcome.transition === "INCIDENT_RECOVERED" ? " The incident is now recovered." : "";
    return `${name} is healthy (HTTP ${outcome.status_code}, ${formatMs(outcome.response_time_ms)}).${recovered}`;
  }
  const opened = outcome.transition === "INCIDENT_OPENED" ? " An incident was opened." : "";
  const detail = outcome.status_code ? `HTTP ${outcome.status_code}` : outcome.error_type ?? "error";
  return `${name} is not healthy (${detail}).${opened}`;
}

interface Props {
  service: ServiceSummary;
  onChanged: () => void;
  /** Where to go after deleting (default: the services list). */
  afterDelete?: string;
  compact?: boolean;
}

export function ServiceActions({ service, onChanged, afterDelete = "/services", compact }: Props) {
  const api = useApi();
  const { health, notify, refreshHealth } = useAppState();
  const [checking, setChecking] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const deleteRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
  }, [confirming]);

  const canCheck = Boolean(health?.can_check || health?.auth_required);
  const canEdit = Boolean((health?.can_write || health?.auth_required) && service.editable);
  const readOnlyReason =
    service.source === "config" ? "Defined in the services file, so it can only be changed there." : undefined;

  async function checkNow() {
    setChecking(true);
    try {
      const result = await api.checkService(service.name);
      const healthy = result.outcome?.is_healthy;
      notify(describeCheck(result), healthy === false || result.failure ? "error" : "success");
      onChanged();
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), "error");
    } finally {
      setChecking(false);
    }
  }

  async function remove() {
    setDeleting(true);
    try {
      await api.deleteService(service.name);
      notify(`Deleted ${service.name}. Its past checks and incidents are kept.`);
      refreshHealth();
      setConfirming(false);
      onChanged();
      navigate(afterDelete);
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), "error");
      setDeleting(false);
    }
  }

  if (!canCheck && !canEdit && !readOnlyReason) return null;

  return (
    <div className={`actions${compact ? " compact" : ""}`}>
      {canCheck && (
        <button type="button" onClick={checkNow} disabled={checking} aria-busy={checking || undefined}>
          {checking ? "Checking…" : "Check now"}
          <span className="sr-only"> {service.name}</span>
        </button>
      )}
      {canEdit ? (
        <>
          <a className="button" href={`#/services/${encodeURIComponent(service.name)}/edit`}>
            Edit<span className="sr-only"> {service.name}</span>
          </a>
          <button
            type="button"
            ref={deleteRef}
            className="danger"
            onClick={() => setConfirming(true)}
            disabled={confirming}
          >
            Delete<span className="sr-only"> {service.name}</span>
          </button>
        </>
      ) : (
        readOnlyReason && (
          <span className="muted small" title={readOnlyReason}>
            Read-only (services file)
          </span>
        )
      )}
      {confirming && (
        <div className="confirm" role="alertdialog" aria-labelledby={`confirm-${service.name}`}>
          <p id={`confirm-${service.name}`}>
            Delete <strong>{service.name}</strong>? It will no longer be checked. Past checks and
            incidents are kept.
          </p>
          <button type="button" className="danger" ref={confirmRef} onClick={remove} disabled={deleting}>
            {deleting ? "Deleting…" : "Yes, delete"}
          </button>
          <button
            type="button"
            onClick={() => {
              setConfirming(false);
              deleteRef.current?.focus();
            }}
            disabled={deleting}
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

export function CheckAllButton({ onDone }: { onDone: () => void }) {
  const api = useApi();
  const { health, notify } = useAppState();
  const [running, setRunning] = useState(false);
  if (!health?.can_check && !health?.auth_required) return null;

  async function run() {
    setRunning(true);
    try {
      const result = await api.checkAll();
      const failed = result.unhealthy + result.failures.length;
      notify(
        result.checked === 0
          ? "There are no enabled services to check."
          : `Checked ${result.checked} service${result.checked === 1 ? "" : "s"}: ${result.healthy} healthy, ${failed} with problems.`,
        failed ? "error" : "success",
      );
      onDone();
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), "error");
    } finally {
      setRunning(false);
    }
  }

  return (
    <button type="button" onClick={run} disabled={running} aria-busy={running || undefined}>
      {running ? "Checking all…" : "Check all now"}
    </button>
  );
}

export function AddServiceLink({ primary = true }: { primary?: boolean }) {
  const { health } = useAppState();
  if (!health?.can_write && !health?.auth_required) return null;
  return (
    <a className={`button${primary ? " primary" : ""}`} href="#/add">
      + Add service
    </a>
  );
}
