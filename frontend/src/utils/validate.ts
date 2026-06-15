// Basic syntax validators for config fields. Each `v*` helper returns an error
// message string when invalid, or null when valid.

export function isIPv4(s: string): boolean {
  const parts = s.split(".");
  if (parts.length !== 4) return false;
  return parts.every((p) => /^\d{1,3}$/.test(p) && Number(p) <= 255);
}

export function isIPv6(s: string): boolean {
  if (!s.includes(":")) return false;
  const re =
    /^(([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,7}:|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|:((:[0-9a-fA-F]{1,4}){1,7}|:))$/;
  return re.test(s);
}

export const isIP = (s: string): boolean => isIPv4(s) || isIPv6(s);

export function isHostname(s: string): boolean {
  if (!s || s.length > 253) return false;
  const re =
    /^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$/;
  return re.test(s);
}

export const isHostOrIP = (s: string): boolean => isIP(s) || isHostname(s);

export function isPort(n: number): boolean {
  return Number.isInteger(n) && n >= 1 && n <= 65535;
}

export function isHttpUrl(s: string): boolean {
  try {
    const u = new URL(s);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

// --- message-returning validators (null = valid) ---
const t = (s: string) => s.trim();

export const vIp = (s: string): string | null =>
  isIP(t(s)) ? null : "Enter a valid IPv4 or IPv6 address";

export const vIpOrLocal = (s: string): string | null => {
  const v = t(s);
  if (v === "local" || v === "system") return null;
  return isIP(v) ? null : "IPv4/IPv6 address, or 'local'";
};

export const vHostOrIp = (s: string): string | null =>
  isHostOrIP(t(s)) ? null : "Enter a hostname or IP address";

export const vHostname = (s: string): string | null =>
  isHostname(t(s)) ? null : "Enter a valid hostname";

export const vHttpUrl = (s: string): string | null =>
  isHttpUrl(t(s)) ? null : "Enter a valid http(s):// URL";

export const vPort = (n: number): string | null =>
  isPort(n) ? null : "Port must be 1–65535";

export const vPositive = (n: number): string | null =>
  Number.isFinite(n) && n > 0 ? null : "Must be greater than 0";
