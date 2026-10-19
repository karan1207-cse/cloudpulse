// Types mirror the JSON returned by src/cloudpulse/api.py.

export type Severity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type IncidentStatus = "OPEN" | "RECOVERED";
export type ServiceStatus = "healthy" | "unhealthy" | "unknown";

export const SEVERITIES: Severity[] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];
export const INCIDENT_STATUSES: IncidentStatus[] = ["OPEN", "RECOVERED"];

export interface HealthCheck {
  id: string;
  service: string;
  url: string;
  final_url?: string;
  checked_at: string;
  is_healthy: boolean;
  status_code: number | null;
  response_time_ms: number;
  error: string | null;
  error_type: string | null;
  redirects?: number;
}

export interface Incident {
  id: string;
  service_name: string;
  service_url: string;
  severity: Severity;
  status: IncidentStatus;
  created_at: string;
  updated_at: string | null;
  recovered_at: string | null;
  duration_seconds: number | null;
  failure_count: number;
  last_error: string | null;
  last_error_type: string | null;
  last_status_code: number | null;
  escalated_at?: string | null;
}

export interface ServiceSummary {
  name: string;
  url: string;
  timeout_seconds: number;
  failure_threshold: number;
  severity: Severity;
  accepted_status: [number, number];
  enabled: boolean;
  status: ServiceStatus;
  last_checked_at: string | null;
  last_status_code: number | null;
  last_response_time_ms: number | null;
  last_error: string | null;
  last_error_type: string | null;
  consecutive_failures: number;
  current_incident: { id: string; severity: Severity; created_at: string } | null;
}

export interface Overview {
  generated_at: string;
  totals: {
    services: number;
    healthy: number;
    unhealthy: number;
    unknown: number;
    open_incidents: number;
  };
  open_incidents: Incident[];
  recent_recoveries: Incident[];
  recent_checks: HealthCheck[];
}

export interface ServiceDetail {
  service: ServiceSummary;
  checks: HealthCheck[];
  incidents: Incident[];
}

export interface IncidentPage {
  items: Incident[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  truncated: boolean;
}

export type SortField = "created_at" | "recovered_at" | "severity" | "service_name" | "status";

export interface IncidentQuery {
  status?: IncidentStatus;
  severity?: Severity;
  service?: string;
  sort?: SortField;
  order?: "asc" | "desc";
  page?: number;
  page_size?: number;
}
