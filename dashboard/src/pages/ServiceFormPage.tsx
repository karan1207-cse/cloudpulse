import { useApi } from "../api/context";
import type { ServiceInput } from "../api/types";
import { useAppState } from "../appState";
import { ErrorState, Loading } from "../components/common";
import { describeCheck } from "../components/ServiceActions";
import { ServiceForm } from "../components/ServiceForm";
import { navigate, useAsync } from "../hooks";

export function AddServicePage() {
  const api = useApi();
  const { health, notify, refreshHealth } = useAppState();

  if (health && !health.can_write && !health.auth_required) {
    return (
      <div className="state state-empty">
        <p>Services cannot be added from this browser.</p>
        <p className="muted small">
          {health.service_store
            ? "Changes are only allowed on the computer running CloudPulse."
            : "This server has no service store configured."}
        </p>
      </div>
    );
  }

  async function submit(input: ServiceInput) {
    const saved = await api.createService(input);
    refreshHealth();
    let message = `Added ${saved.service.name}.`;
    for (const warning of saved.warnings) notify(warning, "info");
    if (health?.can_check) {
      try {
        const result = await api.checkService(saved.service.name);
        message = `Added ${saved.service.name}. First check: ${describeCheck(result)}`;
      } catch {
        message = `Added ${saved.service.name}. It will be checked automatically.`;
      }
    } else if (health?.scheduler?.enabled) {
      message = `Added ${saved.service.name}. It will be checked automatically.`;
    }
    notify(message, "success");
    navigate(`/services/${encodeURIComponent(saved.service.name)}`);
  }

  return (
    <>
      <p className="lead">
        Enter a web address and CloudPulse will check it regularly, open an incident if it keeps
        failing, and tell you when it recovers.
      </p>
      <ServiceForm health={health} onSubmit={submit} onCancel={() => navigate("/services")} />
    </>
  );
}

export function EditServicePage({ name }: { name: string }) {
  const api = useApi();
  const { health, notify } = useAppState();
  const [state, reload] = useAsync((signal) => api.service(name, signal), [api, name]);

  if (state.status === "loading") return <Loading label={`Loading ${name}…`} />;
  if (state.status === "error") return <ErrorState error={state.error} onRetry={reload} />;
  const service = state.data.service;
  if (!service.editable) {
    return (
      <div className="state state-empty">
        <p>{service.name} cannot be edited here.</p>
        <p className="muted small">
          {service.source === "config"
            ? "It is defined in the services file. Edit it there, or run `cloudpulse services import` to manage it from the dashboard."
            : "Changes are only allowed on the computer running CloudPulse."}
        </p>
      </div>
    );
  }

  async function submit(input: ServiceInput) {
    const saved = await api.updateService(name, input);
    for (const warning of saved.warnings) notify(warning, "info");
    notify(`Saved changes to ${saved.service.name}.`);
    navigate(`/services/${encodeURIComponent(name)}`);
  }

  return (
    <ServiceForm
      health={health}
      service={service}
      onSubmit={submit}
      onCancel={() => navigate(`/services/${encodeURIComponent(name)}`)}
    />
  );
}
