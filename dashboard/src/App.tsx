import type { ReactElement } from "react";
import { useApi } from "./api/context";
import { AppStateProvider } from "./appState";
import { AddServicePage, EditServicePage } from "./pages/ServiceFormPage";
import { useHashRoute } from "./hooks";
import { IncidentsPage } from "./pages/IncidentsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { ServiceDetailPage } from "./pages/ServiceDetailPage";
import { ServicesPage } from "./pages/ServicesPage";

const NAV = [
  { path: "/", label: "Overview" },
  { path: "/services", label: "Services" },
  { path: "/incidents", label: "Incidents" },
];

function resolve(path: string): { title: string; page: ReactElement } {
  if (path === "/add") return { title: "Add a service", page: <AddServicePage /> };
  const edit = /^\/services\/([^/]+)\/edit$/.exec(path);
  if (edit?.[1]) {
    const name = decodeURIComponent(edit[1]);
    return { title: `Edit ${name}`, page: <EditServicePage key={name} name={name} /> };
  }
  const detail = /^\/services\/([^/]+)$/.exec(path);
  if (detail?.[1]) {
    const name = decodeURIComponent(detail[1]);
    return { title: `Service: ${name}`, page: <ServiceDetailPage key={name} name={name} /> };
  }
  if (path === "/services") return { title: "Services", page: <ServicesPage /> };
  if (path === "/incidents") return { title: "Incidents", page: <IncidentsPage /> };
  if (path === "/") return { title: "Overview", page: <OverviewPage /> };
  return {
    title: "Not found",
    page: (
      <p>
        This page does not exist. <a href="#/">Go to the overview.</a>
      </p>
    ),
  };
}

export function App() {
  return (
    <AppStateProvider>
      <Shell />
    </AppStateProvider>
  );
}

function Shell() {
  const api = useApi();
  const path = useHashRoute();
  const { title, page } = resolve(path);
  const section = path.startsWith("/services") || path === "/add" ? "/services" : path;

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      {api.demo && (
        <div className="demo-banner" role="note">
          <strong>Demo mode:</strong> the data on this page is fabricated sample data, not real
          monitoring results. Remove <code>?demo=1</code> to use the CloudPulse API.
        </div>
      )}
      <header className="topbar">
        <h1>
          <svg aria-hidden="true" viewBox="0 0 16 16" width="22" height="22">
            <path d="M1 9h3l2-5 3 9 2-6 1 2h3" fill="none" stroke="currentColor" strokeWidth="2" />
          </svg>{" "}
          CloudPulse
        </h1>
        <nav aria-label="Main">
          <ul>
            {NAV.map((item) => (
              <li key={item.path}>
                <a
                  href={`#${item.path}`}
                  aria-current={section === item.path ? "page" : undefined}
                >
                  {item.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main id="main" tabIndex={-1}>
        <h2 className="page-title">{title}</h2>
        {page}
      </main>
      <footer className="muted small">All times are shown in UTC.</footer>
    </>
  );
}
