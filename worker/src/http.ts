const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };

export function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: JSON_HEADERS });
}
