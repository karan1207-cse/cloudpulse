import { useId, useRef, useState, type FormEvent } from "react";
import { setAdminToken } from "../api/auth";
import { ApiError } from "../api/errors";
import type { Health, ServiceInput, ServiceSummary, Severity } from "../api/types";
import { SEVERITIES } from "../api/types";

export type FieldName =
  | "name"
  | "url"
  | "timeout_seconds"
  | "failure_threshold"
  | "severity"
  | "accepted_status";

export interface FormValues {
  name: string;
  url: string;
  timeout_seconds: string;
  failure_threshold: string;
  severity: Severity;
  accepted_low: string;
  accepted_high: string;
  enabled: boolean;
}

export type FieldErrors = Partial<Record<FieldName, string>>;

const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
export const PRIVATE_MESSAGE =
  "This address is on a private or local network. For security, CloudPulse only monitors public internet addresses.";

/** Add https:// when the user typed just a domain such as "example.com". */
export function normalizeUrl(raw: string): string {
  const value = raw.trim();
  if (!value) return value;
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(value) ? value : `https://${value}`;
}

function isPrivateHost(hostname: string): boolean {
  const host = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (host === "localhost" || host.endsWith(".localhost") || host === "0.0.0.0") return true;
  if (host === "::1" || host.startsWith("fe80:") || /^f[cd][0-9a-f]{2}:/.test(host)) return true;
  const parts = host.split(".").map(Number);
  if (parts.length === 4 && parts.every((n) => Number.isInteger(n) && n >= 0 && n <= 255)) {
    const [a, b] = parts as [number, number, number, number];
    return (
      a === 10 ||
      a === 127 ||
      a === 0 ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 100 && b >= 64 && b <= 127)
    );
  }
  return false;
}

/** Client-side checks for instant feedback. The server re-validates everything. */
export function validateForm(values: FormValues): { errors: FieldErrors; input?: ServiceInput } {
  const errors: FieldErrors = {};
  const name = values.name.trim();
  if (!name) errors.name = "Please give this service a name.";
  else if (!NAME_PATTERN.test(name))
    errors.name =
      "Use 1-64 letters, digits, dots, dashes or underscores (no spaces), starting with a letter or digit.";

  const url = normalizeUrl(values.url);
  if (!url) {
    errors.url = "Please enter the web address to check, e.g. https://example.com.";
  } else {
    let parsed: URL | null = null;
    try {
      parsed = new URL(url);
    } catch {
      errors.url = "This does not look like a valid web address.";
    }
    if (parsed) {
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:")
        errors.url = "Only http:// and https:// addresses can be monitored.";
      else if (parsed.username || parsed.password)
        errors.url = "Please remove the username/password from the address.";
      else if (isPrivateHost(parsed.hostname)) errors.url = PRIVATE_MESSAGE;
    }
  }

  const timeout = Number(values.timeout_seconds);
  if (!values.timeout_seconds.trim() || !Number.isFinite(timeout) || timeout <= 0 || timeout > 30)
    errors.timeout_seconds = "Enter a timeout between 0.1 and 30 seconds.";

  const threshold = Number(values.failure_threshold);
  if (!Number.isInteger(threshold) || threshold < 1 || threshold > 100)
    errors.failure_threshold = "Enter a whole number from 1 to 100.";

  const low = Number(values.accepted_low);
  const high = Number(values.accepted_high);
  if (
    !Number.isInteger(low) ||
    !Number.isInteger(high) ||
    low < 100 ||
    high > 599 ||
    low > high
  )
    errors.accepted_status = "Use two HTTP codes between 100 and 599, lowest first (e.g. 200 and 399).";

  if (Object.keys(errors).length > 0) return { errors };
  return {
    errors,
    input: {
      name,
      url,
      timeout_seconds: timeout,
      failure_threshold: threshold,
      severity: values.severity,
      accepted_status: [low, high],
      enabled: values.enabled,
    },
  };
}

export function initialValues(health: Health | null, service?: ServiceSummary): FormValues {
  const defaults = health?.defaults ?? {
    timeout_seconds: 5,
    failure_threshold: 3,
    severity: "HIGH" as Severity,
    accepted_status: [200, 399] as [number, number],
  };
  return {
    name: service?.name ?? "",
    url: service?.url ?? "",
    timeout_seconds: String(service?.timeout_seconds ?? defaults.timeout_seconds),
    failure_threshold: String(service?.failure_threshold ?? defaults.failure_threshold),
    severity: service?.severity ?? defaults.severity,
    accepted_low: String(service?.accepted_status[0] ?? defaults.accepted_status[0]),
    accepted_high: String(service?.accepted_status[1] ?? defaults.accepted_status[1]),
    enabled: service?.enabled ?? true,
  };
}

interface Props {
  health: Health | null;
  service?: ServiceSummary;
  onSubmit: (input: ServiceInput) => Promise<void>;
  onCancel: () => void;
}

export function ServiceForm({ health, service, onSubmit, onCancel }: Props) {
  const editing = service !== undefined;
  const [values, setValues] = useState<FormValues>(() => initialValues(health, service));
  const [errors, setErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [needsToken, setNeedsToken] = useState(false);
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const id = useId();
  const refs = useRef<Partial<Record<FieldName, HTMLElement | null>>>({});

  const set = <K extends keyof FormValues>(key: K, value: FormValues[K]) =>
    setValues((v) => ({ ...v, [key]: value }));

  const focusFirstError = (fieldErrors: FieldErrors) => {
    const order: FieldName[] = ["name", "url", "timeout_seconds", "failure_threshold", "severity", "accepted_status"];
    const first = order.find((f) => fieldErrors[f]);
    if (!first) return;
    if (first !== "name" && first !== "url") setAdvancedOpen(true);
    window.setTimeout(() => refs.current[first]?.focus(), 0);
  };

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    const { errors: fieldErrors, input } = validateForm(values);
    setErrors(fieldErrors);
    if (!input) {
      focusFirstError(fieldErrors);
      return;
    }
    if (needsToken) setAdminToken(token.trim() || null);
    setSaving(true);
    try {
      await onSubmit(input);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setNeedsToken(true);
        setFormError("This server needs an admin token before changes can be saved.");
      } else if (error instanceof ApiError && error.field && error.field in fieldLabels) {
        const field = error.field as FieldName;
        const next = { [field]: error.message };
        setErrors(next);
        focusFirstError(next);
      } else {
        setFormError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      setSaving(false);
    }
  }

  const describe = (field: FieldName, hint?: boolean) =>
    [hint ? `${id}-${field}-hint` : null, errors[field] ? `${id}-${field}-error` : null]
      .filter(Boolean)
      .join(" ") || undefined;

  const errorText = (field: FieldName) =>
    errors[field] ? (
      <p className="field-error" id={`${id}-${field}-error`}>
        {errors[field]}
      </p>
    ) : null;

  return (
    <form className="service-form card" onSubmit={handleSubmit} noValidate aria-label={editing ? "Edit service" : "Add service"}>
      {formError && (
        <div className="form-error" role="alert">
          {formError}
        </div>
      )}

      <div className="field">
        <label htmlFor={`${id}-name`}>Name</label>
        <input
          id={`${id}-name`}
          ref={(el) => {
            refs.current.name = el;
          }}
          value={values.name}
          onChange={(e) => set("name", e.target.value)}
          disabled={editing}
          autoComplete="off"
          placeholder="my-website"
          aria-invalid={errors.name ? true : undefined}
          aria-describedby={describe("name", true)}
        />
        <p className="hint" id={`${id}-name-hint`}>
          {editing ? "Names cannot be changed." : "A short label, e.g. my-shop or api-prod."}
        </p>
        {errorText("name")}
      </div>

      <div className="field">
        <label htmlFor={`${id}-url`}>Website or API address (URL)</label>
        <input
          id={`${id}-url`}
          ref={(el) => {
            refs.current.url = el;
          }}
          value={values.url}
          onChange={(e) => set("url", e.target.value)}
          onBlur={() => set("url", normalizeUrl(values.url))}
          inputMode="url"
          autoComplete="url"
          placeholder="https://example.com/health"
          aria-invalid={errors.url ? true : undefined}
          aria-describedby={describe("url", true)}
        />
        <p className="hint" id={`${id}-url-hint`}>
          We will visit this address regularly. "example.com" becomes "https://example.com".
        </p>
        {errorText("url")}
      </div>

      <details
        className="advanced"
        open={advancedOpen}
        onToggle={(e) => setAdvancedOpen((e.target as HTMLDetailsElement).open)}
      >
        <summary>Advanced settings (optional)</summary>
        <div className="advanced-grid">
          <div className="field">
            <label htmlFor={`${id}-timeout`}>Timeout (seconds)</label>
            <input
              id={`${id}-timeout`}
              ref={(el) => {
                refs.current.timeout_seconds = el;
              }}
              type="number"
              min="0.1"
              max="30"
              step="0.1"
              value={values.timeout_seconds}
              onChange={(e) => set("timeout_seconds", e.target.value)}
              aria-invalid={errors.timeout_seconds ? true : undefined}
              aria-describedby={describe("timeout_seconds", true)}
            />
            <p className="hint" id={`${id}-timeout_seconds-hint`}>How long to wait for an answer.</p>
            {errorText("timeout_seconds")}
          </div>
          <div className="field">
            <label htmlFor={`${id}-threshold`}>Failures before an incident</label>
            <input
              id={`${id}-threshold`}
              ref={(el) => {
                refs.current.failure_threshold = el;
              }}
              type="number"
              min="1"
              max="100"
              step="1"
              value={values.failure_threshold}
              onChange={(e) => set("failure_threshold", e.target.value)}
              aria-invalid={errors.failure_threshold ? true : undefined}
              aria-describedby={describe("failure_threshold", true)}
            />
            <p className="hint" id={`${id}-failure_threshold-hint`}>Failed checks in a row before alerting.</p>
            {errorText("failure_threshold")}
          </div>
          <div className="field">
            <label htmlFor={`${id}-severity`}>Severity</label>
            <select
              id={`${id}-severity`}
              ref={(el) => {
                refs.current.severity = el;
              }}
              value={values.severity}
              onChange={(e) => set("severity", e.target.value as Severity)}
            >
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            {errorText("severity")}
          </div>
          <fieldset
            className="field"
            aria-describedby={describe("accepted_status", true)}
          >
            <legend>Healthy HTTP status codes</legend>
            <div className="range">
              <label htmlFor={`${id}-low`} className="sr-only">
                Lowest healthy status code
              </label>
              <input
                id={`${id}-low`}
                ref={(el) => {
                  refs.current.accepted_status = el;
                }}
                type="number"
                min="100"
                max="599"
                value={values.accepted_low}
                onChange={(e) => set("accepted_low", e.target.value)}
                aria-invalid={errors.accepted_status ? true : undefined}
              />
              <span aria-hidden="true">to</span>
              <label htmlFor={`${id}-high`} className="sr-only">
                Highest healthy status code
              </label>
              <input
                id={`${id}-high`}
                type="number"
                min="100"
                max="599"
                value={values.accepted_high}
                onChange={(e) => set("accepted_high", e.target.value)}
                aria-invalid={errors.accepted_status ? true : undefined}
              />
            </div>
            <p className="hint" id={`${id}-accepted_status-hint`}>Default 200 to 399.</p>
            {errorText("accepted_status")}
          </fieldset>
          <div className="field checkbox">
            <input
              id={`${id}-enabled`}
              type="checkbox"
              checked={values.enabled}
              onChange={(e) => set("enabled", e.target.checked)}
            />
            <label htmlFor={`${id}-enabled`}>Check this service automatically</label>
          </div>
        </div>
      </details>

      {needsToken && (
        <div className="field">
          <label htmlFor={`${id}-token`}>Admin token</label>
          <input
            id={`${id}-token`}
            type="password"
            autoComplete="off"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <p className="hint">Ask whoever runs this CloudPulse server. It is kept only for this browser tab.</p>
        </div>
      )}

      <div className="form-actions">
        <button type="submit" className="primary" disabled={saving}>
          {saving ? "Saving…" : editing ? "Save changes" : "Add service"}
        </button>
        <button type="button" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      </div>
    </form>
  );
}

const fieldLabels: Record<FieldName, true> = {
  name: true,
  url: true,
  timeout_seconds: true,
  failure_threshold: true,
  severity: true,
  accepted_status: true,
};
