import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { CloudPulseApi } from "../api/client";
import { ApiProvider } from "../api/context";
import { demoApi } from "../api/demo";
import { ApiError } from "../api/errors";
import type { Health, ServiceDetail, ServiceSummary, SiteView } from "../api/types";
import { describeCheck } from "../components/ServiceActions";

const health: Health = {
  status: "ok",
  backend: "local",
  can_write: true,
  can_check: true,
  auth_required: false,
  service_store: true,
  scheduler: { enabled: true, interval_seconds: 60 },
  limits: { max_services: 50, services: 1 },
  defaults: { timeout_seconds: 5, failure_threshold: 3, severity: "HIGH", accepted_status: [200, 399] },
};

const service: ServiceSummary = {
  name: "expense",
  url: "https://expense-tracker.example.app/",
  timeout_seconds: 5,
  failure_threshold: 3,
  severity: "HIGH",
  accepted_status: [200, 399],
  enabled: true,
  status: "unhealthy",
  last_checked_at: "2026-01-15T12:00:00+00:00",
  last_status_code: 200,
  last_response_time_ms: 233,
  last_error: "Backend http://localhost:5000/ points to a local/private address",
  last_error_type: "PRIVATE_BACKEND",
  consecutive_failures: 1,
  current_incident: null,
  source: "dashboard",
  editable: true,
  check_mode: "full",
  backend_timeout_seconds: 20,
};

const site: SiteView = {
  check_mode: "full",
  discovered_at: "2026-01-15T11:59:00+00:00",
  last_checked_at: "2026-01-15T12:00:00+00:00",
  limits: { max_manual: 10, max_backends: 5, max_pages: 10 },
  components: [
    { url: "https://expense-tracker.example.app/", kind: "page", source: "main", role: "main", status: "up", status_code: 200, response_time_ms: 233, error: null, error_type: null },
    { url: "https://expense-tracker.example.app/static/js/main.js", kind: "asset", source: "html", status: "up", status_code: 200, response_time_ms: 120, error: null, error_type: null },
    { url: "http://localhost:5000/", kind: "backend", source: "discovered", status: "down", status_code: null, response_time_ms: 2, error: null, error_type: "PRIVATE_BACKEND" },
    { url: "https://api.example-co.dev/", kind: "backend", source: "discovered", status: "down", status_code: null, response_time_ms: 20000, error: "Request timed out.", error_type: "TIMEOUT" },
    { url: "https://extra.example-co.dev/health", kind: "backend", source: "manual", status: "disabled", status_code: null, response_time_ms: null, error: null, error_type: null },
  ],
};

function makeApi(detail: Partial<ServiceDetail> = {}, overrides: Partial<CloudPulseApi> = {}): CloudPulseApi {
  return {
    ...demoApi,
    demo: false,
    health: vi.fn().mockResolvedValue(health),
    service: vi.fn().mockResolvedValue({ service, checks: [], incidents: [], site, ...detail }),
    addComponent: vi.fn().mockResolvedValue({ site }),
    componentAction: vi.fn().mockResolvedValue({ site }),
    createService: vi.fn().mockResolvedValue({ service, warnings: [] }),
    checkService: vi.fn().mockRejectedValue(new ApiError(503, "x", "unavailable")),
    ...overrides,
  };
}

function renderAt(api: CloudPulseApi, hash: string) {
  window.location.hash = hash;
  return render(
    <ApiProvider api={api}>
      <App />
    </ApiProvider>,
  );
}

describe("check mode in the form", () => {
  it("defaults new services to the full-site check and sends it", async () => {
    const api = makeApi();
    renderAt(api, "#/add");

    const form = await screen.findByRole("form", { name: "Add service" });
    const full = within(form).getByRole("radio", { name: /Full site \(recommended for web apps\)/ });
    expect(full).toBeChecked();
    expect(within(form).getByText(/catches a dead backend behind a working page/)).toBeInTheDocument();

    await userEvent.type(within(form).getByLabelText("Name"), "expense");
    await userEvent.type(within(form).getByLabelText(/Website or API address/), "expense-tracker.example.app");
    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));

    await waitFor(() => expect(api.createService).toHaveBeenCalled());
    expect(vi.mocked(api.createService).mock.calls[0]?.[0]).toMatchObject({
      check_mode: "full",
      backend_timeout_seconds: 20,
    });
  });

  it("can choose the quick check", async () => {
    const api = makeApi();
    renderAt(api, "#/add");

    const form = await screen.findByRole("form", { name: "Add service" });
    await userEvent.click(within(form).getByRole("radio", { name: /Quick \(this URL only\)/ }));
    await userEvent.type(within(form).getByLabelText("Name"), "api");
    await userEvent.type(within(form).getByLabelText(/Website or API address/), "https://api.example-co.dev/health");
    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));

    await waitFor(() => expect(api.createService).toHaveBeenCalled());
    expect(vi.mocked(api.createService).mock.calls[0]?.[0].check_mode).toBe("quick");
  });

  it("keeps an existing service's quick mode when editing", async () => {
    const api = makeApi({ service: { ...service, check_mode: "quick" } });
    renderAt(api, "#/services/expense/edit");

    const form = await screen.findByRole("form", { name: "Edit service" });
    expect(within(form).getByRole("radio", { name: /Quick/ })).toBeChecked();
  });

  it("validates the backend timeout", async () => {
    const api = makeApi();
    renderAt(api, "#/add");

    const form = await screen.findByRole("form", { name: "Add service" });
    await userEvent.type(within(form).getByLabelText("Name"), "expense");
    await userEvent.type(within(form).getByLabelText(/Website or API address/), "https://a.example-co.dev");
    await userEvent.click(within(form).getByText("Advanced settings (optional)"));
    const timeout = within(form).getByLabelText(/Backend timeout/);
    await userEvent.clear(timeout);
    await userEvent.type(timeout, "90");
    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));

    expect(await within(form).findByText(/between 1 and 60 seconds/)).toBeInTheDocument();
    expect(api.createService).not.toHaveBeenCalled();
  });
});

describe("What we check", () => {
  it("lists components with plain-language problems", async () => {
    renderAt(makeApi(), "#/services/expense");

    const card = await screen.findByRole("region", { name: "What we check" });
    const rows = within(card).getAllByRole("row");
    expect(rows).toHaveLength(6);
    expect(within(card).getByText("Main page")).toBeInTheDocument();
    expect(within(card).getByText(/visitors cannot reach/)).toBeInTheDocument();
    expect(within(card).getByText("Not responding (timeout).")).toBeInTheDocument();
    expect(within(card).getByText("Turned off")).toBeInTheDocument();
    expect(within(card).getByText("added by you")).toBeInTheDocument();
    expect(within(card).getByText("What a full-site check cannot catch")).toBeInTheDocument();
    expect(within(card).getByText(/add a/)).toHaveTextContent("/health");
  });

  it("turns a discovered component off and a disabled one on", async () => {
    const api = makeApi();
    renderAt(api, "#/services/expense");

    const card = await screen.findByRole("region", { name: "What we check" });
    await userEvent.click(within(card).getByRole("button", { name: "Turn off https://api.example-co.dev/" }));
    expect(api.componentAction).toHaveBeenCalledWith("expense", "disable", "https://api.example-co.dev/");
    expect(await screen.findByText(/Turned off https:\/\/api.example-co.dev\//)).toBeInTheDocument();

    await userEvent.click(within(card).getByRole("button", { name: "Turn on https://extra.example-co.dev/health" }));
    expect(api.componentAction).toHaveBeenCalledWith("expense", "enable", "https://extra.example-co.dev/health");
    // The main page is always checked: no "Turn off" for it.
    expect(
      within(card).queryByRole("button", { name: "Turn off https://expense-tracker.example.app/" }),
    ).not.toBeInTheDocument();
  });

  it("removes only manual components", async () => {
    const api = makeApi();
    renderAt(api, "#/services/expense");

    const card = await screen.findByRole("region", { name: "What we check" });
    expect(within(card).getAllByRole("button", { name: /^Remove/ })).toHaveLength(1);
    await userEvent.click(within(card).getByRole("button", { name: "Remove https://extra.example-co.dev/health" }));
    expect(api.componentAction).toHaveBeenCalledWith("expense", "remove", "https://extra.example-co.dev/health");
  });

  it("adds a missed backend and validates the address", async () => {
    const api = makeApi();
    renderAt(api, "#/services/expense");

    const form = await screen.findByRole("form", { name: "Add a component to check" });
    const input = within(form).getByLabelText("Add something we missed");
    await userEvent.type(input, "my-api.onrender.com");
    await userEvent.click(within(form).getByRole("button", { name: "Add" }));
    expect(await within(form).findByText(/starting with https:\/\//)).toBeInTheDocument();
    expect(api.addComponent).not.toHaveBeenCalled();

    await userEvent.clear(input);
    await userEvent.type(input, "https://my-api.onrender.com");
    await userEvent.click(within(form).getByRole("button", { name: "Add" }));
    expect(api.addComponent).toHaveBeenCalledWith("expense", "https://my-api.onrender.com", "backend");
  });

  it("shows server errors when adding a component", async () => {
    const api = makeApi(
      {},
      {
        addComponent: vi
          .fn()
          .mockRejectedValue(new ApiError(400, "validation_error", "Host resolves to a blocked address.", "url")),
      },
    );
    renderAt(api, "#/services/expense");

    const form = await screen.findByRole("form", { name: "Add a component to check" });
    await userEvent.type(within(form).getByLabelText("Add something we missed"), "https://sneaky.example-co.dev");
    await userEvent.click(within(form).getByRole("button", { name: "Add" }));

    expect(await within(form).findByText("Host resolves to a blocked address.")).toBeInTheDocument();
  });

  it("explains quick mode and links to edit", async () => {
    renderAt(makeApi({ service: { ...service, check_mode: "quick" } }), "#/services/expense");

    const card = await screen.findByRole("region", { name: "What we check" });
    expect(card).toHaveTextContent(/Quick check: only/);
    expect(within(card).getByRole("link", { name: "Edit" })).toHaveAttribute("href", "#/services/expense/edit");
  });
});

describe("check result wording", () => {
  it("names the failing component for full checks", () => {
    expect(
      describeCheck({
        outcome: {
          service: "expense",
          skipped: false,
          reason: null,
          transition: "INCIDENT_OPENED",
          is_healthy: false,
          status_code: 200,
          response_time_ms: 233,
          error_type: "TIMEOUT",
          error: "Backend https://x.onrender.com/ is not responding (timeout)",
          check_mode: "full",
        },
        failure: null,
        service,
      }),
    ).toBe(
      "expense is not healthy: Backend https://x.onrender.com/ is not responding (timeout). An incident was opened.",
    );
  });
});
