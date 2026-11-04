export async function apiFetch(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(options.headers);
  const isFormData = options.body instanceof FormData;
  if (!isFormData && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(path, { ...options, headers });
}

export async function apiJson<T>(
  path: string,
  options?: RequestInit,
  fallbackMessage = "请求失败",
): Promise<T> {
  const response = await apiFetch(path, options);
  const body = await response.text();
  let result: unknown = {};
  if (body) {
    try {
      result = JSON.parse(body);
    } catch {
      throw new Error("服务器返回无效 JSON 响应");
    }
  }
  if (!response.ok) {
    const detail =
      typeof result === "object" && result !== null && "detail" in result
        ? String(result.detail)
        : "";
    throw new Error(detail || `${fallbackMessage}（HTTP ${response.status}）`);
  }
  return result as T;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
