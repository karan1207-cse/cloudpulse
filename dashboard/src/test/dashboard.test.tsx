import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { CloudPulseApi } from "../api/client";
import { createHttpApi, isDemoMode } from "../api/client";
import { ApiProvider } from "../api/context";
import { demoApi } from "../api/demo";
import { ApiError } from "../api/errors";
import type { IncidentPage, Overview } from "../api/types";
import { formatDuration, formatRelative, formatUtc } from "../format";

function renderApp(api: CloudPulseApi, hash = "#/") {
  window.location.hash = hash;
  return render(
    <ApiProvider api={api}>
      <App />
    </ApiProvider>,
  );
}

const emptyOverview: Overview = {
  generated_at: "2026-01-01T00:00:00+00:00",
  totals: { services: 0, healthy: 0, unhealthy: 0, unknown: 0, open_incidents: 0 },
  open_incidents: [],
  recent_recoveries: [],
  recent_checks: [],
};

function fakeApi(overrides: Partial<CloudPulseApi> = {}): CloudPulseApi {
  return { ...demoApi, demo: false, ...overrides };
}

describe("format helpers", () => {
  it("formats UTC timestamps unambiguously", () => {
    expect(formatUtc("2026-01-15T12:00:00+05:30")).toBe("2026-01-15 06:30:00 UTC");
    expect(formatUtc(null)).toBe("—");
    expect(formatUtc("nope")).toBe("invalid time");
  });

  it("formats relative times and durations", () => {
    const now = Date.parse("2026-01-15T12:00:00Z");
    expect(formatRelative("2026-01-15T11:59:30Z", now)).toBe("30s ago");
    expect(formatRelative("2026-01-15T10:00:00Z", now)).toBe("2 h ago");
    expect(formatRelative(null, now)).toBe("never");
    expect(formatDuration(3725)).toBe("1h 2m");
    expect(formatDuration(null)).toBe("—");
  });
});

describe("API client", () => {
  it("builds query strings and parses JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], page: 1, page_size: 20, total: 0, total_pages: 1 }), {
        status: 200,
      }),
    );
    const api = createHttpApi("", fetchMock);

    await api.incidents({ status: "OPEN", severity: undefined, page: 2 });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/incidents?status=OPEN&page=2");
  });

  it("encodes service names in paths", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    await createHttpApi("", fetchMock).service("a/b");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/services/a%2Fb");
  });

  it("surfaces API error messages", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: "invalid_parameter", message: "bad status" }), {
        status: 400,
      }),
    );

    await expect(createHttpApi("", fetchMock).overview()).rejects.toMatchObject({
      status: 400,
      code: "invalid_parameter",
      message: "bad status",
    });
  });

  it("reports network failures clearly", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(createHttpApi("", fetchMock).overview()).rejects.toMatchObject({
      code: "network_error",
    });
  });

  it("rejects non-JSON responses", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("<html>", { status: 200 }));

    await expect(createHttpApi("", fetchMock).overview()).rejects.toMatchObject({
      code: "invalid_response",
    });
  });

  it("enables demo mode only when asked", () => {
    expect(isDemoMode("")).toBe(false);
    expect(isDemoMode("?demo=0")).toBe(false);
    expect(isDemoMode("?demo=1")).toBe(true);
  });
});

describe("App", () => {
  it("shows a demo banner only in demo mode", async () => {
    const { unmount } = renderApp(demoApi);
    expect(screen.getByRole("note")).toHaveTextContent(/fabricated sample data/);
    await screen.findByText("Open incidents", { selector: "dt" });
    unmount();

    renderApp(fakeApi());
    await screen.findByText("Open incidents", { selector: "dt" });
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("renders overview totals", async () => {
    renderApp(fakeApi());

    const stats = await screen.findByText("Services", { selector: "dt" });
    expect(stats.nextElementSibling).toHaveTextContent("3");
    expect(screen.getByText("Unhealthy", { selector: "dt" }).nextElementSibling).toHaveTextContent(
      "1",
    );
    expect(screen.getByRole("region", { name: "Recent checks" })).toBeInTheDocument();
  });

  it("shows a loading state, then an error with retry", async () => {
    let fail = true;
    const overview = vi.fn(async () => {
      if (fail) throw new ApiError(503, "storage_unavailable", "Monitoring data is unavailable.");
      return emptyOverview;
    });
    renderApp(fakeApi({ overview }));

    expect(screen.getByRole("status")).toHaveTextContent(/Loading overview/);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("HTTP 503");
    expect(alert).toHaveTextContent("Monitoring data is unavailable.");

    fail = false;
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    // Empty state is now the first-run call to action.
    expect(
      await screen.findByRole("heading", { name: "Add your first URL to monitor" }),
    ).toBeInTheDocument();
    expect(overview).toHaveBeenCalledTimes(2);
  });

  it("lists services with status and links to details", async () => {
    renderApp(fakeApi(), "#/services");

    const table = await screen.findByRole("table", { name: /Monitored services/ });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(4);
    expect(within(table).getByText("Unhealthy")).toBeInTheDocument();
    expect(within(table).getByText("No data yet")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "demo-payments" })).toHaveAttribute(
      "href",
      "#/services/demo-payments",
    );
  });

  it("filters, sorts and paginates incidents", async () => {
    const incidents = vi.fn(demoApi.incidents);
    renderApp(fakeApi({ incidents }), "#/incidents");

    const table = await screen.findByRole("table", { name: /Incidents \(3\)/ });
    expect(within(table).getAllByRole("row")).toHaveLength(4);

    await userEvent.selectOptions(screen.getByLabelText("Status"), "OPEN");
    await screen.findByRole("table", { name: /Incidents \(1\)/ });
    expect(incidents).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "OPEN", page: 1 }),
      expect.anything(),
    );

    await userEvent.selectOptions(screen.getByLabelText("Status"), "");
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "LOW");
    expect(await screen.findByText("No incidents match these filters.")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Severity"), "");
    const sortButton = await screen.findByRole("button", { name: /Severity/ });
    await userEvent.click(sortButton);
    await waitFor(() =>
      expect(incidents).toHaveBeenLastCalledWith(
        expect.objectContaining({ sort: "severity", order: "desc" }),
        expect.anything(),
      ),
    );
    const header = screen.getByRole("columnheader", { name: /Severity/ });
    expect(header).toHaveAttribute("aria-sort", "descending");
  });

  it("disables pagination at the edges", async () => {
    const page: IncidentPage = {
      items: (await demoApi.incidents({})).items.slice(0, 1),
      page: 1,
      page_size: 1,
      total: 3,
      total_pages: 3,
      truncated: false,
    };
    const incidents = vi.fn().mockResolvedValue(page);
    renderApp(fakeApi({ incidents }), "#/incidents");

    expect(await screen.findByRole("button", { name: "Previous" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() =>
      expect(incidents).toHaveBeenLastCalledWith(
        expect.objectContaining({ page: 2 }),
        expect.anything(),
      ),
    );
  });

  it("renders service detail with trend and recovery timeline", async () => {
    renderApp(fakeApi(), "#/services/demo-payments");

    expect(await screen.findByRole("heading", { name: "demo-payments" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Response time for the last 40 checks/ })).toBeInTheDocument();
    const timeline = screen.getByRole("region", { name: /recovery timeline/ });
    expect(within(timeline).getByText("Still open")).toBeInTheDocument();
    expect(within(timeline).getByText(/open for/)).toBeInTheDocument();
  });

  it("shows not-found errors for unknown services", async () => {
    renderApp(fakeApi(), "#/services/missing");

    expect(await screen.findByRole("alert")).toHaveTextContent(/HTTP 404/);
  });

  it("navigates with the hash router and marks the current page", async () => {
    renderApp(fakeApi());
    await screen.findByText("Services", { selector: "dt" });

    await act(async () => {
      window.location.hash = "#/incidents";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });

    expect(await screen.findByRole("heading", { name: "Incidents", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Incidents" })).toHaveAttribute("aria-current", "page");
  });

  it("handles unknown routes", () => {
    renderApp(fakeApi(), "#/nope");
    expect(screen.getByText(/This page does not exist/)).toBeInTheDocument();
  });
});
