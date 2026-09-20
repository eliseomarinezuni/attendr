import type { Env } from "./env";
import { json } from "./http";

const STATE_LEASE_MS = 20 * 60_000;

function stateLeaseToken(request: Request): string {
  return request.headers.get("x-attendr-lease") ?? "";
}
export async function stateLease(request: Request, env: Env, release: boolean): Promise<Response> {
  let body: { token?: unknown };
  try { body = await request.json(); } catch { return json({ error: "Invalid JSON" }, 400); }
  if (!body || typeof body.token !== "string" || !/^[a-f0-9]{32}$/.test(body.token)) {
    return json({ error: "Invalid state lease token" }, 400);
  }
  if (release) {
    await env.DB.prepare("UPDATE attendr_state_control SET lease_token=NULL,lease_until=0 WHERE id=1 AND lease_token=?")
      .bind(body.token).run();
    return json({ released: true });
  }
  const now = Date.now();
  const row = await env.DB.prepare(`UPDATE attendr_state_control
    SET lease_token=?,lease_until=? WHERE id=1 AND (lease_until<? OR lease_token=?)
    RETURNING revision,sha256,size,chunk_count`)
    .bind(body.token, now + STATE_LEASE_MS, now, body.token)
    .first<{ revision: number; sha256: string | null; size: number; chunk_count: number }>();
  return row ? json({ acquired: true, ...row }) : json({ error: "State is in use" }, 409);
}

export async function renewState(request: Request, env: Env): Promise<Response> {
  const row = await env.DB.prepare(`UPDATE attendr_state_control
    SET lease_until=CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)+?
    WHERE id=1 AND lease_token=? AND lease_until>(julianday('now')-2440587.5)*86400000
    RETURNING revision`).bind(STATE_LEASE_MS, stateLeaseToken(request)).first();
  return row ? json({ renewed: true }) : json({ error: "State lease lost" }, 409);
}

export async function readState(request: Request, env: Env): Promise<Response> {
  const token = stateLeaseToken(request);
  const control = await env.DB.prepare(`SELECT revision,sha256,size,chunk_count
    FROM attendr_state_control WHERE id=1 AND lease_token=? AND lease_until>?`)
    .bind(token, Date.now()).first<{ revision: number; sha256: string | null; size: number; chunk_count: number }>();
  if (!control) return json({ error: "State lease missing or expired" }, 409);
  const chunks = await env.DB.prepare(
    "SELECT data FROM attendr_state_chunks WHERE revision=? ORDER BY chunk_index"
  ).bind(control.revision).all<{ data: string }>();
  if (chunks.results.length !== control.chunk_count) return json({ error: "State checkpoint incomplete" }, 500);
  return json({ ...control, chunks: chunks.results.map((item) => item.data) });
}

export async function writeState(request: Request, env: Env): Promise<Response> {
  let body: { revision?: unknown; sha256?: unknown; size?: unknown; chunks?: unknown };
  try { body = await request.json(); } catch { return json({ error: "Invalid JSON" }, 400); }
  const chunks = body?.chunks;
  if (!Number.isSafeInteger(body?.revision) || (body.revision as number) < 0 ||
      (body.revision as number) >= Number.MAX_SAFE_INTEGER || !Number.isInteger(body?.size) ||
      (body.size as number) < 1 || (body.size as number) > 8 * 1024 * 1024 ||
      typeof body?.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(body.sha256) ||
      !Array.isArray(chunks) || chunks.length < 1 || chunks.length > 48 ||
      !chunks.every((chunk) => typeof chunk === "string" && chunk.length > 0 && chunk.length <= 256 * 1024 && /^[A-Za-z0-9+/=]+$/.test(chunk))) {
    return json({ error: "Invalid state checkpoint" }, 400);
  }
  let bytes: Uint8Array;
  try {
    const binary = atob(chunks.join(""));
    bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  } catch {
    return json({ error: "Invalid state checkpoint encoding" }, 400);
  }
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes.buffer as ArrayBuffer)))
    .map((value) => value.toString(16).padStart(2, "0")).join("");
  if (bytes.length !== body.size || digest !== body.sha256) {
    return json({ error: "State checkpoint integrity mismatch" }, 400);
  }
  const token = stateLeaseToken(request);
  const revision = (body.revision as number) + 1;
  // D1 batches are transactions. Gate the first insert using the database clock;
  // changes() carries that decision through each immediately preceding write.
  // Keep chunks in separate bindings to stay below D1's per-value size limit.
  const statements = [env.DB.prepare(`INSERT INTO attendr_state_chunks(revision,chunk_index,data)
    SELECT ?,0,? WHERE EXISTS(SELECT 1 FROM attendr_state_control
      WHERE id=1 AND revision=? AND lease_token=?
      AND lease_until>(julianday('now')-2440587.5)*86400000)`)
    .bind(revision, chunks[0], body.revision, token)];
  chunks.slice(1).forEach((chunk, index) => statements.push(
    env.DB.prepare(`INSERT INTO attendr_state_chunks(revision,chunk_index,data)
      SELECT ?,?,? WHERE changes()=1`).bind(revision, index + 1, chunk)
  ));
  statements.push(env.DB.prepare(`UPDATE attendr_state_control
    SET revision=?,sha256=?,size=?,chunk_count=?,
      lease_until=CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)+?
    WHERE id=1 AND revision=? AND lease_token=? AND changes()=1 RETURNING revision`)
    .bind(revision, body.sha256, body.size, chunks.length, STATE_LEASE_MS, body.revision, token));
  statements.push(env.DB.prepare("DELETE FROM attendr_state_chunks WHERE changes()=1 AND revision!=?")
    .bind(revision));
  const results = await env.DB.batch<{ revision: number }>(statements);
  return results[chunks.length].results.length
    ? json({ revision })
    : json({ error: "State lease expired, ownership changed, or revision conflicted" }, 409);
}
