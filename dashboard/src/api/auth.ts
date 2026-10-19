// The admin token (only needed when the server sets CLOUDPULSE_ADMIN_TOKEN)
// is kept in sessionStorage: it disappears when the tab is closed and is
// never put in URLs or local storage.
const KEY = "cloudpulse.adminToken";

export function getAdminToken(): string | null {
  try {
    return window.sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setAdminToken(token: string | null): void {
  try {
    if (token) window.sessionStorage.setItem(KEY, token);
    else window.sessionStorage.removeItem(KEY);
  } catch {
    // Storage unavailable (private mode): the token simply is not remembered.
  }
}
