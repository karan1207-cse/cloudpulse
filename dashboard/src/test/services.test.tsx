import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { getAdminToken, setAdminToken } from "../api/auth";
import type { CloudPulseApi } from "../api/client";
import { createHttpApi } from "../api/client";
import { ApiProvider } from "../api/context";
import { demoApi } from "../api/demo";
import { ApiError } from "../api/errors";
import type { CheckResult, Health, ServiceSummary } from "../api/types";
import { normalizeUrl, PRIVATE_MESSAGE, validateForm, type FormValues } from "../components/ServiceForm";

const writableHealth: Health = {
  status: "ok",
  backend: "local",
  can_write: true,
  can_check: true,
  auth_required: false,
  service_store: true,
  scheduler: { enabled: true, interval_seconds: 60, active_here: true, last_run_at: null },
  limits: { max_services: 50, services: 0 },
  defaults: { timeout_seconds: 5, failure_threshold: 3, severity: "HIGH", accepted_status: [200, 399] },
};

function summary(overrides: Partial<ServiceSummary> = {}): ServiceSummary {
  return {
    name: "shop",
    url: "https://shop.example.com/",
    timeout_seconds: 5,
    failure_threshold: 3,
    severity: "HIGH",
    accepted_status: [200, 399],
    enabled: true,
    status: "unknown",
    last_checked_at: null,
    last_status_code: null,
    last_response_time_ms: null,
    last_error: null,
    last_error_type: null,
    consecutive_failures: 0,
    current_incident: null,
    source: "dashboard",
    editable: true,
    ...overrides,
  };
}

function checkResult(healthy: boolean, name = "shop"): CheckResult {
  return {
    outcome: {
      service: name,
      skipped: false,
      reason: null,
      transition: healthy ? "NONE" : "FAILURE_RECORDED",
      is_healthy: healthy,
      status_code: healthy ? 200 : 503,
      response_time_ms: 120,
      error_type: healthy ? null : "HTTP_STATUS",
    },
    failure: null,
    service: summary({ name, status: healthy ? "healthy" : "unhealthy" }),
  };
}

function makeApi(overrides: Partial<CloudPulseApi> = {}, health: Health = writableHealth): CloudPulseApi {
  return {
    ...demoApi,
    demo: false,
    health: vi.fn().mockResolvedValue(health),
    createService: vi.fn(),
    updateService: vi.fn(),
    deleteService: vi.fn().mockResolvedValue(undefined),
    checkService: vi.fn().mockResolvedValue(checkResult(true)),
    checkAll: vi.fn().mockResolvedValue({ checked: 2, healthy: 1, unhealthy: 1, failures: [] }),
    ...overrides,
  };
}

function renderApp(api: CloudPulseApi, hash = "#/") {
  window.location.hash = hash;
  return render(
    <ApiProvider api={api}>
      <App />
    </ApiProvider>,
  );
}

const valid: FormValues = {
  name: "shop",
  url: "https://shop.example.com/",
  timeout_seconds: "5",
  failure_threshold: "3",
  severity: "HIGH",
  accepted_low: "200",
  accepted_high: "399",
  enabled: true,
};

describe("validateForm", () => {
  it("accepts a valid service and builds the API payload", () => {
    const { errors, input } = validateForm({ ...valid, name: " shop ", url: "shop.example.com" });
    expect(errors).toEqual({});
    expect(input).toEqual({
      name: "shop",
      url: "https://shop.example.com",
      timeout_seconds: 5,
      failure_threshold: 3,
      severity: "HIGH",
      accepted_status: [200, 399],
      enabled: true,
    });
  });

  it.each([
    [{ name: "" }, "name", /give this service a name/],
    [{ name: "has space" }, "name", /no spaces/],
    [{ url: "" }, "url", /enter the web address/],
    [{ url: "http://" }, "url", /valid web address/],
    [{ url: "ftp://files.example.com" }, "url", /http:\/\/ and https:\/\//],
    [{ url: "https://user:pw@example.com" }, "url", /username\/password/],
    [{ url: "localhost:8080" }, "url", /private or local network/],
    [{ url: "http://127.0.0.1" }, "url", /private or local network/],
    [{ url: "http://10.0.0.5/" }, "url", /private or local network/],
    [{ url: "http://192.168.1.1/" }, "url", /private or local network/],
    [{ url: "http://172.20.0.1/" }, "url", /private or local network/],
    [{ url: "http://169.254.169.254/latest" }, "url", /private or local network/],
    [{ url: "http://[::1]/" }, "url", /private or local network/],
    [{ timeout_seconds: "0" }, "timeout_seconds", /between 0.1 and 30/],
    [{ timeout_seconds: "31" }, "timeout_seconds", /between 0.1 and 30/],
    [{ failure_threshold: "1.5" }, "failure_threshold", /whole number/],
    [{ accepted_low: "400", accepted_high: "200" }, "accepted_status", /lowest first/],
  ])("rejects %j", (change, field, message) => {
    const { errors, input } = validateForm({ ...valid, ...change });
    expect(input).toBeUndefined();
    expect(errors[field as keyof typeof errors]).toMatch(message);
  });

  it("normalizes bare domains to https", () => {
    expect(normalizeUrl("  example.com/health ")).toBe("https://example.com/health");
    expect(normalizeUrl("http://example.com")).toBe("http://example.com");
    expect(PRIVATE_MESSAGE).toMatch(/public internet/);
  });
});

describe("API client writes", () => {
  it("sends JSON and the admin token when one is set", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ service: summary(), warnings: [] }), { status: 201 }),
    );
    setAdminToken("secret-token-value");
    try {
      await createHttpApi("", fetchMock).createService({
        name: "shop",
        url: "https://shop.example.com/",
        timeout_seconds: 5,
        failure_threshold: 3,
        severity: "HIGH",
        accepted_status: [200, 399],
        enabled: true,
      });
    } finally {
      setAdminToken(null);
    }
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/services");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer secret-token-value");
    expect(JSON.parse(init.body as string).name).toBe("shop");
    expect(getAdminToken()).toBeNull();
  });

  it("exposes the field of validation errors", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ error: "validation_error", message: "Already exists.", field: "name" }),
        { status: 400 },
      ),
    );
    await expect(createHttpApi("", fetchMock).deleteService("x")).rejects.toMatchObject({
      status: 400,
      field: "name",
    });
  });

  it("uses DELETE and POST for delete and check now", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response("{}", { status: 200 }));
    const api = createHttpApi("", fetchMock);
    await api.deleteService("a b");
    await api.checkService("a b");
    await api.checkAll();
    expect(fetchMock.mock.calls.map((c) => [(c[1] as RequestInit).method, c[0]])).toEqual([
      ["DELETE", "/api/services/a%20b"],
      ["POST", "/api/services/a%20b/check"],
      ["POST", "/api/checks"],
    ]);
  });
});

describe("First run", () => {
  const emptyOverview = {
    generated_at: "2026-01-01T00:00:00+00:00",
    totals: { services: 0, healthy: 0, unhealthy: 0, unknown: 0, open_incidents: 0 },
    open_incidents: [],
    recent_recoveries: [],
    recent_checks: [],
  };

  it("invites the user to add a URL", async () => {
    renderApp(makeApi({ overview: vi.fn().mockResolvedValue(emptyOverview) }));

    expect(await screen.findByRole("heading", { name: "Add your first URL to monitor" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "+ Add a URL" })).toHaveAttribute("href", "#/add");
  });

  it("does not offer adding when the server is read-only", async () => {
    renderApp(
      makeApi({ overview: vi.fn().mockResolvedValue(emptyOverview) }, { ...writableHealth, can_write: false, can_check: false }),
    );

    expect(await screen.findByText(/only possible on the computer running CloudPulse/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "+ Add a URL" })).not.toBeInTheDocument();
  });
});

describe("Add service", () => {
  it("keeps advanced settings collapsed and validates inline", async () => {
    const api = makeApi();
    renderApp(api, "#/add");

    const form = await screen.findByRole("form", { name: "Add service" });
    const details = form.querySelector("details");
    expect(details?.open).toBe(false);

    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));

    const name = within(form).getByLabelText("Name");
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(within(form).getByText("Please give this service a name.")).toBeInTheDocument();
    expect(within(form).getByText(/enter the web address/)).toBeInTheDocument();
    await waitFor(() => expect(name).toHaveFocus());
    expect(api.createService).not.toHaveBeenCalled();
  });

  it("creates, runs a first check, confirms with a toast and opens the service", async () => {
    const api = makeApi({
      createService: vi.fn().mockResolvedValue({ service: summary(), warnings: [] }),
    });
    renderApp(api, "#/add");

    const form = await screen.findByRole("form", { name: "Add service" });
    await userEvent.type(within(form).getByLabelText("Name"), "shop");
    await userEvent.type(within(form).getByLabelText(/Website or API address/), "shop.example.com");
    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));

    await waitFor(() => expect(api.createService).toHaveBeenCalled());
    expect(vi.mocked(api.createService).mock.calls[0]?.[0]).toMatchObject({
      name: "shop",
      url: "https://shop.example.com",
      failure_threshold: 3,
    });
    expect(api.checkService).toHaveBeenCalledWith("shop");
    expect(await screen.findByText(/Added shop\. First check: shop is healthy/)).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe("#/services/shop"));
  });

  it("shows server field errors next to the field (e.g. duplicate name)", async () => {
    const api = makeApi({
      createService: vi
        .fn()
        .mockRejectedValue(new ApiError(400, "validation_error", "A service named 'shop' already exists.", "name")),
    });
    renderApp(api, "#/add");

    const form = await screen.findByRole("form", { name: "Add service" });
    await userEvent.type(within(form).getByLabelText("Name"), "shop");
    await userEvent.type(within(form).getByLabelText(/Website or API address/), "https://shop.example.com");
    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));

    expect(await within(form).findByText("A service named 'shop' already exists.")).toBeInTheDocument();
    expect(within(form).getByLabelText("Name")).toHaveAttribute("aria-invalid", "true");
  });

  it("opens advanced settings to show a server error for an advanced field", async () => {
    const api = makeApi({
      createService: vi
        .fn()
        .mockRejectedValue(new ApiError(400, "validation_error", "Timeout too long.", "timeout_seconds")),
    });
    renderApp(api, "#/add");

    const form = await screen.findByRole("form", { name: "Add service" });
    await userEvent.type(within(form).getByLabelText("Name"), "shop");
    await userEvent.type(within(form).getByLabelText(/Website or API address/), "https://shop.example.com");
    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));

    expect(await within(form).findByText("Timeout too long.")).toBeInTheDocument();
    await waitFor(() => expect(form.querySelector("details")?.open).toBe(true));
  });

  it("shows limit and blocked-address errors without a field as a form alert", async () => {
    const api = makeApi({
      createService: vi
        .fn()
        .mockRejectedValue(new ApiError(400, "validation_error", "You can monitor at most 50 services.")),
    });
    renderApp(api, "#/add");

    const form = await screen.findByRole("form", { name: "Add service" });
    await userEvent.type(within(form).getByLabelText("Name"), "shop");
    await userEvent.type(within(form).getByLabelText(/Website or API address/), "https://shop.example.com");
    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent("at most 50 services");
  });

  it("asks for the admin token after a 401 and sends it on retry", async () => {
    const createService = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(401, "unauthorized", "An admin token is required."))
      .mockResolvedValueOnce({ service: summary(), warnings: [] });
    const api = makeApi({ createService }, { ...writableHealth, can_write: false, can_check: false, auth_required: true });
    renderApp(api, "#/add");

    const form = await screen.findByRole("form", { name: "Add service" });
    await userEvent.type(within(form).getByLabelText("Name"), "shop");
    await userEvent.type(within(form).getByLabelText(/Website or API address/), "https://shop.example.com");
    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));

    const tokenInput = await within(form).findByLabelText("Admin token");
    expect(within(form).getByRole("alert")).toHaveTextContent(/admin token/);
    await userEvent.type(tokenInput, "my-admin-token");
    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));

    await waitFor(() => expect(createService).toHaveBeenCalledTimes(2));
    expect(getAdminToken()).toBe("my-admin-token");
    setAdminToken(null);
  });
});

describe("Services page actions", () => {
  const services = [
    summary({ name: "shop", status: "healthy" }),
    summary({ name: "from-file", source: "config", editable: false }),
  ];

  it("checks one service now and reports the result", async () => {
    const api = makeApi({
      services: vi.fn().mockResolvedValue(services),
      checkService: vi.fn().mockResolvedValue(checkResult(false)),
    });
    renderApp(api, "#/services");

    await userEvent.click(await screen.findByRole("button", { name: "Check now shop" }));

    expect(api.checkService).toHaveBeenCalledWith("shop");
    expect(await screen.findByText(/shop is not healthy \(HTTP 503\)/)).toBeInTheDocument();
  });

  it("checks all services", async () => {
    const api = makeApi({ services: vi.fn().mockResolvedValue(services) });
    renderApp(api, "#/services");

    await userEvent.click(await screen.findByRole("button", { name: "Check all now" }));

    expect(await screen.findByText("Checked 2 services: 1 healthy, 1 with problems.")).toBeInTheDocument();
  });

  it("deletes only after confirmation", async () => {
    const api = makeApi({ services: vi.fn().mockResolvedValue(services) });
    renderApp(api, "#/services");

    await userEvent.click(await screen.findByRole("button", { name: "Delete shop" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("Delete shop?");
    expect(within(dialog).getByRole("button", { name: "Yes, delete" })).toHaveFocus();

    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(api.deleteService).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Delete shop" }));
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Yes, delete" }));

    expect(api.deleteService).toHaveBeenCalledWith("shop");
    expect(await screen.findByText(/Deleted shop\. Its past checks and incidents are kept\./)).toBeInTheDocument();
  });

  it("marks services from the services file as read-only", async () => {
    renderApp(makeApi({ services: vi.fn().mockResolvedValue(services) }), "#/services");

    await screen.findByRole("button", { name: "Delete shop" });
    expect(screen.queryByRole("button", { name: "Delete from-file" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit from-file" })).not.toBeInTheDocument();
    expect(screen.getByText("Read-only (services file)")).toBeInTheDocument();
  });

  it("hides write actions when the server does not allow changes", async () => {
    renderApp(
      makeApi({ services: vi.fn().mockResolvedValue(services) }, { ...writableHealth, can_write: false, can_check: false }),
      "#/services",
    );

    await screen.findByRole("table", { name: /Monitored services/ });
    expect(screen.queryByRole("button", { name: /Check now/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "+ Add service" })).not.toBeInTheDocument();
  });
});

describe("Edit service", () => {
  it("prefills the form, keeps the name fixed and saves", async () => {
    const existing = summary({ failure_threshold: 4 });
    const api = makeApi({
      service: vi.fn().mockResolvedValue({ service: existing, checks: [], incidents: [] }),
      updateService: vi.fn().mockResolvedValue({ service: { ...existing, failure_threshold: 6 }, warnings: [] }),
    });
    renderApp(api, "#/services/shop/edit");

    const form = await screen.findByRole("form", { name: "Edit service" });
    expect(within(form).getByLabelText("Name")).toBeDisabled();
    expect(within(form).getByLabelText(/Website or API address/)).toHaveValue("https://shop.example.com/");

    await userEvent.click(within(form).getByText("Advanced settings (optional)"));
    const threshold = within(form).getByLabelText("Failures before an incident");
    expect(threshold).toHaveValue(4);
    await userEvent.clear(threshold);
    await userEvent.type(threshold, "6");
    await userEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(api.updateService).toHaveBeenCalledWith("shop", expect.objectContaining({ failure_threshold: 6 })),
    );
    expect(await screen.findByText("Saved changes to shop.")).toBeInTheDocument();
  });

  it("explains why config services cannot be edited", async () => {
    const api = makeApi({
      service: vi.fn().mockResolvedValue({
        service: summary({ name: "from-file", source: "config", editable: false }),
        checks: [],
        incidents: [],
      }),
    });
    renderApp(api, "#/services/from-file/edit");

    expect(await screen.findByText(/defined in the services file/)).toBeInTheDocument();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
  });
});

describe("Demo mode", () => {
  it("is read-only", async () => {
    await expect(demoApi.createService(validateForm(valid).input!)).rejects.toMatchObject({
      code: "demo_read_only",
    });
    expect((await demoApi.health()).can_write).toBe(false);
  });
});
