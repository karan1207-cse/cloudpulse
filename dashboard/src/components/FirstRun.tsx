import type { ReactNode } from "react";
import { useAppState } from "../appState";
import { formatRelative } from "../format";

/** First-run call to action shown when nothing is monitored yet. */
export function FirstRunCallToAction({ children }: { children?: ReactNode }) {
  const { health } = useAppState();
  const canAdd = Boolean(health?.can_write || health?.auth_required);
  return (
    <section className="first-run card" aria-labelledby="first-run-title">
      <h2 id="first-run-title">Add your first URL to monitor</h2>
      <p>
        Paste the address of a website or API. CloudPulse checks it every{" "}
        {health?.scheduler?.interval_seconds ?? 60} seconds, opens an incident if it keeps failing,
        and records when it recovers.
      </p>
      {canAdd ? (
        <a className="button primary large" href="#/add">
          + Add a URL
        </a>
      ) : (
        children ?? (
          <p className="muted">
            Adding services is only possible on the computer running CloudPulse.
          </p>
        )
      )}
    </section>
  );
}

/** One-line status of automatic background checks. */
export function SchedulerLine() {
  const { health } = useAppState();
  const scheduler = health?.scheduler;
  if (!scheduler) return null;
  if (!scheduler.enabled) {
    return <p className="muted small">Automatic checks are not running in this server.</p>;
  }
  const every = `every ${scheduler.interval_seconds ?? 60} seconds`;
  if (scheduler.active_here === false && scheduler.last_run_at === null) {
    return (
      <p className="muted small">
        Automatic checks {every} — waiting: another CloudPulse scheduler is already checking.
      </p>
    );
  }
  return (
    <p className="muted small">
      Automatic checks {every}
      {scheduler.last_run_at ? ` · last run ${formatRelative(scheduler.last_run_at)}` : " · starting…"}
    </p>
  );
}
