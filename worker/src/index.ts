interface Env {
  DB: D1Database;
  DISCORD_APPLICATION_ID: string;
  DISCORD_PUBLIC_KEY: string;
  DISCORD_BOT_TOKEN: string;
  DISCORD_STUDY_CHANNEL_ID: string;
  DISCORD_OWNER_USER_ID: string;
  STUDY_SYNC_SECRET: string;
  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;
  GOOGLE_REFRESH_TOKEN: string;
}

interface StudySession {
  session_id: string;
  task_uid: string;
  title: string;
  course_name: string;
  start_at: string;
  end_at: string;
  task_due_at: string;
  calendar_id: string;
  event_id: string;
  status: string;
  notified: number;
  manual_override: number;
  message_id: string | null;
}

interface SyncedSession {
  session_id: string;
  task_uid: string;
  title: string;
  course_name: string;
  start: string;
  end: string;
  task_due_at: string;
  calendar_id: string;
  event_id: string;
}

interface DiscordInteraction {
  id: string;
  type: number;
  token: string;
  application_id: string;
  member?: { user?: { id?: string } };
  user?: { id?: string };
  data?: { custom_id?: string };
}

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };
const DISCORD_API = "https://discord.com/api/v10";
const TIME_ZONE = "America/Toronto";
const WINDOWS: Record<number, Array<[number, number]>> = {
  0: [[660, 1080]],
  1: [[780, 990], [1125, 1170]],
  2: [[1125, 1320]],
  3: [[1215, 1320]],
  4: [],
  5: [[1215, 1320]],
  6: [[660, 1080]],
};

async function boundedFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  return fetch(input, { ...init, signal: AbortSignal.timeout(15_000) });
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: JSON_HEADERS });
}

function unauthorized(): Response {
  return json({ error: "Unauthorized" }, 401);
}

function isAuthorized(request: Request, env: Env): boolean {
  if (typeof env.STUDY_SYNC_SECRET !== "string" || !env.STUDY_SYNC_SECRET.trim()) return false;
  const value = request.headers.get("authorization") ?? "";
  return value === `Bearer ${env.STUDY_SYNC_SECRET}`;
}

function hexBytes(value: string, length: number): ArrayBuffer | null {
  if (value.length !== length || !/^[0-9a-f]+$/i.test(value)) return null;
  const buffer = new ArrayBuffer(value.length / 2);
  const bytes = new Uint8Array(buffer);
  value.match(/.{2}/g)!.forEach((byte, index) => { bytes[index] = parseInt(byte, 16); });
  return buffer;
}

async function verifyDiscord(request: Request, body: string, env: Env): Promise<boolean> {
  const signature = request.headers.get("x-signature-ed25519");
  const timestamp = request.headers.get("x-signature-timestamp");
  const publicKey = typeof env.DISCORD_PUBLIC_KEY === "string" ? hexBytes(env.DISCORD_PUBLIC_KEY, 64) : null;
  const signatureBytes = signature ? hexBytes(signature, 128) : null;
  if (!timestamp || !/^\d+$/.test(timestamp) || Math.abs(Date.now() / 1000 - Number(timestamp)) > 300 || !publicKey || !signatureBytes) return false;
  try {
    const key = await crypto.subtle.importKey("raw", publicKey, { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      signatureBytes,
      new TextEncoder().encode(timestamp + body),
    );
  } catch {
    return false;
  }
}

async function planLease(request: Request, env: Env, release: boolean): Promise<Response> {
  let body: { token?: unknown };
  try { body = await request.json(); } catch { return json({ error: "Invalid JSON" }, 400); }
  if (!body || typeof body.token !== "string" || !/^[a-f0-9]{32}$/.test(body.token)) return json({ error: "Invalid plan token" }, 400);
  if (release) {
    await env.DB.prepare("DELETE FROM plan_lease WHERE id=1 AND token=?").bind(body.token).run();
    return json({ released: true });
  }
  const now = Date.now();
  const acquired = await env.DB.prepare(`INSERT INTO plan_lease(id,token,lease_until)
    SELECT 1,?,? WHERE NOT EXISTS(SELECT 1 FROM study_operations WHERE status!='done')
    ON CONFLICT(id) DO UPDATE SET token=excluded.token,lease_until=excluded.lease_until
    WHERE plan_lease.lease_until<? OR plan_lease.token=excluded.token RETURNING token`)
    .bind(body.token, now + 20 * 60_000, now).first();
  return acquired ? json({ acquired: true }) : json({ error: "Another planner or button operation is active" }, 409);
}

const STATE_LEASE_MS = 20 * 60_000;

function stateLeaseToken(request: Request): string {
  return request.headers.get("x-attendr-lease") ?? "";
}

async function stateLease(request: Request, env: Env, release: boolean): Promise<Response> {
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

async function readState(request: Request, env: Env): Promise<Response> {
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

async function writeState(request: Request, env: Env): Promise<Response> {
  let body: { revision?: unknown; sha256?: unknown; size?: unknown; chunks?: unknown };
  try { body = await request.json(); } catch { return json({ error: "Invalid JSON" }, 400); }
  const chunks = body?.chunks;
  if (!Number.isInteger(body?.revision) || !Number.isInteger(body?.size) ||
      (body.size as number) < 1 || (body.size as number) > 8 * 1024 * 1024 ||
      typeof body?.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(body.sha256) ||
      !Array.isArray(chunks) || chunks.length < 1 || chunks.length > 48 ||
      !chunks.every((chunk) => typeof chunk === "string" && chunk.length > 0 && chunk.length <= 256 * 1024 && /^[A-Za-z0-9+/=]+$/.test(chunk))) {
    return json({ error: "Invalid state checkpoint" }, 400);
  }
  let bytes: Uint8Array;
  try {
    const binary = atob(chunks.join(""));
    bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return json({ error: "Invalid state checkpoint encoding" }, 400);
  }
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes.buffer as ArrayBuffer)))
    .map((value) => value.toString(16).padStart(2, "0")).join("");
  if (bytes.length !== body.size || digest !== body.sha256) {
    return json({ error: "State checkpoint integrity mismatch" }, 400);
  }
  const token = stateLeaseToken(request);
  const control = await env.DB.prepare("SELECT revision FROM attendr_state_control WHERE id=1 AND lease_token=? AND lease_until>?")
    .bind(token, Date.now()).first<{ revision: number }>();
  if (!control) return json({ error: "State lease missing or expired" }, 409);
  if (control.revision !== body.revision) return json({ error: "State revision conflict" }, 409);
  const revision = control.revision + 1;
  const statements = [env.DB.prepare("DELETE FROM attendr_state_chunks")];
  chunks.forEach((chunk, index) => statements.push(
    env.DB.prepare("INSERT INTO attendr_state_chunks(revision,chunk_index,data) VALUES(?,?,?)").bind(revision, index, chunk)
  ));
  statements.push(env.DB.prepare(`UPDATE attendr_state_control
    SET revision=?,sha256=?,size=?,chunk_count=?,lease_until=? WHERE id=1 AND lease_token=?`)
    .bind(revision, body.sha256, body.size, chunks.length, Date.now() + STATE_LEASE_MS, token));
  await env.DB.batch(statements);
  return json({ revision });
}

async function getState(env: Env): Promise<Response> {
  const [tasks, sessions, overrides, operations] = await Promise.all([
    env.DB.prepare("SELECT task_uid FROM completed_tasks").all<{ task_uid: string }>(),
    env.DB.prepare("SELECT session_id FROM study_sessions WHERE status = 'completed'").all<{ session_id: string }>(),
    env.DB.prepare("SELECT session_id, start_at, end_at FROM study_sessions WHERE status='scheduled' AND manual_override=1").all<{ session_id: string; start_at: string; end_at: string }>(),
    env.DB.prepare("SELECT interaction_id FROM study_operations WHERE status!='done' LIMIT 1").all(),
  ]);
  return json({
    operations_pending: operations.results.length > 0,
    completed_tasks: tasks.results.map((item) => item.task_uid),
    completed_sessions: sessions.results.map((item) => item.session_id),
    rescheduled_sessions: Object.fromEntries(overrides.results.map((item) => [item.session_id, { start: item.start_at, end: item.end_at }])),
  });
}

function validSession(value: unknown): value is SyncedSession {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  const keys = ["session_id", "task_uid", "title", "course_name", "start", "end", "task_due_at", "calendar_id", "event_id"];
  if (!keys.every((key) => typeof item[key] === "string" && item[key] !== "" && (item[key] as string).length <= 1024)) return false;
  if (!/^attendr:study:[a-f0-9]{20}:\d+$/.test(item.session_id as string)) return false;
  const times = [item.start, item.end, item.task_due_at] as string[];
  if (!times.every((value) => /^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value)))) return false;
  return Date.parse(times[0]) < Date.parse(times[1]) && Date.parse(times[1]) <= Date.parse(times[2]);
}

interface StudyProfile { windows: Record<string, Array<[number, number]>>; }

function validProfile(value: unknown): value is StudyProfile {
  if (!value || typeof value !== "object") return false;
  const windows = (value as StudyProfile).windows;
  if (!windows || typeof windows !== "object" || Object.keys(windows).sort().join() !== "0,1,2,3,4,5,6") return false;
  return Object.values(windows).every((day) => {
    if (!Array.isArray(day)) return false;
    let previous = -1;
    return day.every((window) => {
      if (!Array.isArray(window) || window.length !== 2 || !window.every(Number.isInteger)) return false;
      const [start, end] = window;
      if (start < 0 || end > 1439 || start >= end || start < previous) return false;
      previous = end;
      return true;
    });
  });
}

async function studyWindows(env: Env): Promise<Record<string, Array<[number, number]>>> {
  const record = await env.DB.prepare("SELECT payload FROM study_preferences WHERE name='profile'").first<{ payload: string }>();
  if (!record) return WINDOWS;
  const profile: unknown = JSON.parse(record.payload);
  if (!validProfile(profile)) throw new Error("Invalid stored study profile");
  return profile.windows;
}

async function syncSessions(request: Request, env: Env): Promise<Response> {
  let payload: { sessions?: unknown[]; replace?: boolean; plan_token?: string; profile?: unknown };
  try {
    payload = await request.json();
  } catch {
    return json({ error: "Invalid JSON" }, 400);
  }
  if (!payload || typeof payload !== "object" || !Array.isArray(payload.sessions) ||
      (payload.replace !== undefined && typeof payload.replace !== "boolean")) return json({ error: "Invalid sync envelope" }, 400);
  if (payload.profile !== undefined && !validProfile(payload.profile)) return json({ error: "Invalid study profile" }, 400);
  const sessions = payload.sessions.filter(validSession);
  if (sessions.length !== (payload.sessions ?? []).length || sessions.length > 250) {
    return json({ error: "Invalid sessions" }, 400);
  }
  if (new Set(sessions.map((item) => item.session_id)).size !== sessions.length) return json({ error: "Duplicate session IDs" }, 400);
  const lease = await env.DB.prepare("SELECT token FROM plan_lease WHERE id=1 AND token=? AND lease_until>?")
    .bind(payload.plan_token ?? "", Date.now()).first();
  if (!lease) return json({ error: "Acquire the planner lease before syncing sessions" }, 409);
  const active = await env.DB.prepare("SELECT 1 AS active FROM study_operations WHERE status!='done' LIMIT 1").first();
  if (active) return json({ error: "Study operation pending; retry sync later" }, 409);
  const marker = crypto.randomUUID();
  const now = new Date().toISOString();
  const statements = sessions.map((item) =>
    env.DB.prepare(
      `INSERT INTO study_sessions
       (session_id, task_uid, title, course_name, start_at, end_at, task_due_at,
        calendar_id, event_id, status, notified, last_synced, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', 0, ?, ?)
       ON CONFLICT(session_id) DO UPDATE SET
         task_uid=excluded.task_uid, title=excluded.title, course_name=excluded.course_name,
         start_at=CASE
           WHEN study_sessions.manual_override=1 AND study_sessions.task_due_at=excluded.task_due_at THEN study_sessions.start_at
           ELSE excluded.start_at
         END,
         end_at=CASE
           WHEN study_sessions.manual_override=1 AND study_sessions.task_due_at=excluded.task_due_at THEN study_sessions.end_at
           ELSE excluded.end_at
         END,
         task_due_at=excluded.task_due_at, calendar_id=excluded.calendar_id,
         event_id=excluded.event_id, last_synced=excluded.last_synced,
         updated_at=excluded.updated_at,
         notified=CASE
           WHEN study_sessions.manual_override=0 AND study_sessions.start_at != excluded.start_at THEN 0
           ELSE study_sessions.notified
         END,
         manual_override=CASE
           WHEN study_sessions.task_due_at != excluded.task_due_at THEN 0
           ELSE study_sessions.manual_override
         END,
         status=CASE
           WHEN study_sessions.status = 'completed' THEN 'completed'
           ELSE 'scheduled'
         END`,
    ).bind(
      item.session_id, item.task_uid, item.title, item.course_name, new Date(item.start).toISOString(),
      new Date(item.end).toISOString(), new Date(item.task_due_at).toISOString(), item.calendar_id, item.event_id, marker, now,
    ),
  );
  if (payload.profile !== undefined) statements.push(env.DB.prepare("INSERT OR REPLACE INTO study_preferences(name,payload) VALUES('profile',?)").bind(JSON.stringify(payload.profile)));
  if (payload.replace !== false) {
    statements.push(
      env.DB.prepare(
        "UPDATE study_sessions SET status='cancelled', updated_at=? WHERE status='scheduled' AND last_synced != ?",
      ).bind(now, marker),
    );
  }
  await env.DB.batch(statements);
  return json({ synced: sessions.length });
}

function sessionComponents(sessionId: string): unknown[] {
  return [{
    type: 1,
    components: [
      { type: 2, style: 3, label: "Session complete", custom_id: `study:complete:${sessionId}` },
      { type: 2, style: 2, label: "Reschedule", custom_id: `study:reschedule:${sessionId}` },
      { type: 2, style: 4, label: "Task complete", custom_id: `study:task:${sessionId}` },
    ],
  }];
}

function discordTimestamp(value: string): string {
  return `<t:${Math.floor(new Date(value).getTime() / 1000)}:t>`;
}

async function sendReminder(session: StudySession, env: Env): Promise<void> {
  const response = await boundedFetch(`${DISCORD_API}/channels/${env.DISCORD_STUDY_CHANNEL_ID}/messages`, {
    method: "POST",
    headers: {
      authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      allowed_mentions: { parse: [] },
      embeds: [{
        title: "Study session starting",
        description: session.title,
        color: 0x5865f2,
        fields: [
          { name: "Course", value: session.course_name, inline: true },
          { name: "Time", value: `${discordTimestamp(session.start_at)}–${discordTimestamp(session.end_at)}`, inline: true },
        ],
        footer: { text: "Attendr · use a button when you finish" },
      }],
      components: sessionComponents(session.session_id),
    }),
  });
  if (response.status === 429) {
    await env.DB.prepare("UPDATE study_sessions SET notified=0 WHERE session_id=? AND start_at=? AND notified=-1")
      .bind(session.session_id, session.start_at).run();
  }
  if (!response.ok) throw new Error(`Discord reminder failed: ${response.status}`);
  const message = await response.json() as { id: string };
  await env.DB.prepare(
    "UPDATE study_sessions SET notified=1, message_id=?, updated_at=? WHERE session_id=? AND start_at=? AND notified=-1",
  ).bind(message.id, new Date().toISOString(), session.session_id, session.start_at).run();
}

async function sendDueReminders(env: Env): Promise<void> {
  const now = Date.now();
  const lower = new Date(now - 15 * 60_000).toISOString();
  const upper = new Date(now + 5 * 60_000).toISOString();
  const result = await env.DB.prepare(
    `SELECT * FROM study_sessions
     WHERE status='scheduled' AND notified=0 AND start_at >= ? AND start_at <= ?
     ORDER BY start_at LIMIT 25`,
  ).bind(lower, upper).all<StudySession>();
  for (const session of result.results) {
    // -1 is an unresolved send, including a crash after Discord accepted it.
    const claim = await env.DB.prepare("UPDATE study_sessions SET notified=-1,updated_at=? WHERE session_id=? AND status='scheduled' AND notified=0 AND start_at=? AND NOT EXISTS(SELECT 1 FROM plan_lease WHERE lease_until>?) AND NOT EXISTS(SELECT 1 FROM study_operations WHERE status!='done') RETURNING session_id")
      .bind(new Date().toISOString(), session.session_id, session.start_at, Date.now()).first();
    if (!claim) continue;
    try {
      await sendReminder(session, env);
    } catch (error) {
      console.error("Reminder delivery unresolved", session.session_id);
    }
  }
}

async function googleToken(env: Env): Promise<string> {
  const body = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID,
    client_secret: env.GOOGLE_CLIENT_SECRET,
    refresh_token: env.GOOGLE_REFRESH_TOKEN,
    grant_type: "refresh_token",
  });
  const response = await boundedFetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok) throw new Error(`Google token refresh failed: ${response.status}`);
  const payload = await response.json() as { access_token?: string };
  if (!payload.access_token) throw new Error("Google token refresh returned no access token");
  return payload.access_token;
}

async function deleteCalendarEvent(session: StudySession, token: string): Promise<void> {
  const url = `https://www.googleapis.com/calendar/v3/calendars/${encodeURIComponent(session.calendar_id)}/events/${encodeURIComponent(session.event_id)}`;
  const response = await boundedFetch(url, { method: "DELETE", headers: { authorization: `Bearer ${token}` } });
  if (!response.ok && response.status !== 404 && response.status !== 410) {
    throw new Error(`Calendar delete failed: ${response.status}`);
  }
}

async function originalResponse(interaction: DiscordInteraction, body: unknown): Promise<void> {
  if (!interaction.token) return;
  const url = `${DISCORD_API}/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`;
  const response = await boundedFetch(url, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`Discord interaction update failed: ${response.status}`);
}

async function getSession(id: string, env: Env): Promise<StudySession | null> {
  return env.DB.prepare("SELECT * FROM study_sessions WHERE session_id=?").bind(id).first<StudySession>();
}

async function completeSession(interaction: DiscordInteraction, session: StudySession, env: Env): Promise<void> {
  const token = await googleToken(env);
  await deleteCalendarEvent(session, token);
  await env.DB.prepare(
    "UPDATE study_sessions SET status='completed', updated_at=? WHERE session_id=?",
  ).bind(new Date().toISOString(), session.session_id).run();
  await env.DB.prepare("UPDATE study_operations SET status='done',lease_until=0 WHERE interaction_id=?").bind(interaction.id).run();
  await originalResponse(interaction, {
    content: "✅ Study session completed and removed from Google Calendar.",
    embeds: [],
    components: [],
  });
}

async function completeTask(interaction: DiscordInteraction, session: StudySession, env: Env): Promise<void> {
  const rows = await env.DB.prepare(
    "SELECT * FROM study_sessions WHERE task_uid=? AND status='scheduled'",
  ).bind(session.task_uid).all<StudySession>();
  const token = await googleToken(env);
  for (const row of rows.results) {
    await deleteCalendarEvent(row, token);
    await env.DB.prepare("UPDATE study_sessions SET status='completed',updated_at=? WHERE session_id=?")
      .bind(new Date().toISOString(), row.session_id).run();
  }
  const now = new Date().toISOString();
  await env.DB.batch([
    env.DB.prepare("INSERT OR REPLACE INTO completed_tasks(task_uid, completed_at) VALUES (?, ?)").bind(session.task_uid, now),
    env.DB.prepare("UPDATE study_sessions SET status='completed', updated_at=? WHERE task_uid=?").bind(now, session.task_uid),
  ]);
  await env.DB.prepare("UPDATE study_operations SET status='done',lease_until=0 WHERE interaction_id=?").bind(interaction.id).run();
  await originalResponse(interaction, {
    content: `✅ ${session.course_name} task completed. All remaining study sessions were removed.`,
    embeds: [],
    components: [],
  });
}

function localParts(value: Date): { year: number; month: number; day: number; hour: number; minute: number } {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const parts = Object.fromEntries(formatter.formatToParts(value).map((part) => [part.type, part.value]));
  return { year: +parts.year, month: +parts.month, day: +parts.day, hour: +parts.hour, minute: +parts.minute };
}

function zonedToUtc(year: number, month: number, day: number, hour: number, minute: number): Date {
  const wanted = Date.UTC(year, month - 1, day, hour, minute);
  let guess = new Date(wanted);
  for (let pass = 0; pass < 2; pass++) {
    const actual = localParts(guess);
    const represented = Date.UTC(actual.year, actual.month - 1, actual.day, actual.hour, actual.minute);
    guess = new Date(guess.getTime() + wanted - represented);
  }
  return guess;
}

function addLocalDays(parts: ReturnType<typeof localParts>, days: number): { year: number; month: number; day: number } {
  const value = new Date(Date.UTC(parts.year, parts.month - 1, parts.day + days));
  return { year: value.getUTCFullYear(), month: value.getUTCMonth() + 1, day: value.getUTCDate() };
}

function overlaps(start: Date, end: Date, busy: Array<{ start: string; end: string }>): boolean {
  return busy.some((item) => start < new Date(item.end) && end > new Date(item.start));
}

async function allCalendarIds(token: string, excluded: string): Promise<string[]> {
  const ids = new Set<string>();
  const seen = new Set<string>();
  let pageToken = "";
  do {
    const url = new URL("https://www.googleapis.com/calendar/v3/users/me/calendarList");
    url.searchParams.set("minAccessRole", "reader");
    url.searchParams.set("maxResults", "250");
    if (pageToken) url.searchParams.set("pageToken", pageToken);
    const response = await boundedFetch(url, { headers: { authorization: `Bearer ${token}` } });
    if (!response.ok) throw new Error(`Calendar list failed: ${response.status}`);
    const payload = await response.json() as { items?: Array<{ id?: string }>; nextPageToken?: string };
    for (const item of payload.items ?? []) if (item.id && item.id !== excluded) ids.add(item.id);
    pageToken = payload.nextPageToken ?? "";
    if (pageToken && seen.has(pageToken)) throw new Error("Calendar pagination repeated a token");
    seen.add(pageToken);
  } while (pageToken);
  return [...ids];
}

async function busyIntervals(token: string, ids: string[], start: Date, end: Date): Promise<Array<{ start: string; end: string }>> {
  const busy: Array<{ start: string; end: string }> = [];
  for (let offset = 0; offset < ids.length; offset += 50) {
    const batch = ids.slice(offset, offset + 50);
    const response = await boundedFetch("https://www.googleapis.com/calendar/v3/freeBusy", {
      method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify({ timeMin: start.toISOString(), timeMax: end.toISOString(), timeZone: TIME_ZONE, items: batch.map((id) => ({ id })) }),
    });
    if (!response.ok) throw new Error(`Calendar availability failed: ${response.status}`);
    const payload = await response.json() as { calendars?: Record<string, { errors?: unknown[]; busy?: Array<{ start: string; end: string }> }> };
    for (const id of batch) {
      const value = payload.calendars?.[id];
      if (!value || value.errors?.length || !Array.isArray(value.busy)) throw new Error("Incomplete calendar availability");
      for (const interval of value.busy) {
        if (!Number.isFinite(Date.parse(interval.start)) || !Number.isFinite(Date.parse(interval.end)) || Date.parse(interval.end) <= Date.parse(interval.start)) throw new Error("Invalid busy interval");
        busy.push(interval);
      }
    }
  }
  return busy;
}

async function nextSlot(session: StudySession, token: string, env: Env, now = new Date()): Promise<{ start: Date; end: Date } | null> {
  const windows = await studyWindows(env);
  const duration = new Date(session.end_at).getTime() - new Date(session.start_at).getTime();
  const deadline = new Date(session.task_due_at);
  const searchEnd = new Date(Math.min(deadline.getTime(), now.getTime() + 21 * 86_400_000));
  const ids = await allCalendarIds(token, session.calendar_id);
  const busy = await busyIntervals(token, ids, now, searchEnd);
  const otherSessions = await currentStudySessions(session, searchEnd, env);
  busy.push(...otherSessions.intervals);
  const today = localParts(now);
  for (let offset = 0; offset <= 21; offset++) {
    const day = addLocalDays(today, offset);
    const weekday = new Date(Date.UTC(day.year, day.month - 1, day.day)).getUTCDay();
    const dayKey = `${day.year}-${String(day.month).padStart(2, "0")}-${String(day.day).padStart(2, "0")}`;
    if (otherSessions.days.has(dayKey)) continue;
    for (const [windowStart, windowEnd] of windows[String(weekday)]) {
      const limit = zonedToUtc(day.year, day.month, day.day, Math.floor(windowEnd / 60), windowEnd % 60);
      const limitLocal = localParts(limit);
      if (limitLocal.hour * 60 + limitLocal.minute !== windowEnd || limitLocal.day !== day.day) continue;
      for (let minute = windowStart; minute < windowEnd; minute += 15) {
        const start = zonedToUtc(day.year, day.month, day.day, Math.floor(minute / 60), minute % 60);
        const end = new Date(start.getTime() + duration);
        const actual = localParts(start);
        if (actual.day !== day.day || actual.hour * 60 + actual.minute !== minute || end > limit) continue;
        if (start.getTime() < now.getTime() + 15 * 60_000 || end >= deadline || end > searchEnd) continue;
        if (!overlaps(start, end, busy)) return { start, end };
      }
    }
  }
  return null;
}

async function currentStudySessions(
  session: StudySession,
  searchEnd: Date,
  env: Env,
): Promise<{ intervals: Array<{ start: string; end: string }>; days: Set<string> }> {
  const result = await env.DB.prepare(
    "SELECT * FROM study_sessions WHERE status='scheduled' AND session_id != ? AND start_at < ?",
  ).bind(session.session_id, searchEnd.toISOString()).all<StudySession>();
  const rows = result.results;
  const days = new Set(rows.map((row) => {
    const local = localParts(new Date(row.start_at));
    return `${local.year}-${String(local.month).padStart(2, "0")}-${String(local.day).padStart(2, "0")}`;
  }));
  return {
    intervals: rows.map((row) => ({ start: row.start_at, end: row.end_at })),
    days,
  };
}

async function reschedule(interaction: DiscordInteraction, session: StudySession, env: Env): Promise<void> {
  const token = await googleToken(env);
  const saved = await env.DB.prepare("SELECT target_start,target_end FROM study_operations WHERE interaction_id=?").bind(interaction.id).first<{ target_start: string | null; target_end: string | null }>();
  const slot = saved?.target_start && saved.target_end
    ? { start: new Date(saved.target_start), end: new Date(saved.target_end) }
    : await nextSlot(session, token, env);
  if (!slot) {
    await originalResponse(interaction, { content: "⚠️ No acceptable free slot was found before the deadline.", embeds: [], components: [] });
    return;
  }
  await env.DB.prepare("UPDATE study_operations SET target_start=?,target_end=? WHERE interaction_id=?")
    .bind(slot.start.toISOString(), slot.end.toISOString(), interaction.id).run();
  const url = `https://www.googleapis.com/calendar/v3/calendars/${encodeURIComponent(session.calendar_id)}/events/${encodeURIComponent(session.event_id)}`;
  const response = await boundedFetch(url, {
    method: "PATCH",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({ start: { dateTime: slot.start.toISOString(), timeZone: TIME_ZONE }, end: { dateTime: slot.end.toISOString(), timeZone: TIME_ZONE } }),
  });
  if (!response.ok) throw new Error(`Calendar reschedule failed: ${response.status}`);
  await env.DB.prepare(
    "UPDATE study_sessions SET start_at=?, end_at=?, notified=0, manual_override=1, message_id=NULL, updated_at=? WHERE session_id=?",
  ).bind(slot.start.toISOString(), slot.end.toISOString(), new Date().toISOString(), session.session_id).run();
  await env.DB.prepare("UPDATE study_operations SET status='done',lease_until=0 WHERE interaction_id=?").bind(interaction.id).run();
  await originalResponse(interaction, {
    content: `↪️ Rescheduled to ${discordTimestamp(slot.start.toISOString())}–${discordTimestamp(slot.end.toISOString())}.`,
    embeds: [],
    components: [],
  });
}

async function handleButton(interaction: DiscordInteraction, env: Env): Promise<void> {
  const customId = interaction.data?.custom_id ?? "";
  const match = /^study:(complete|reschedule|task):(attendr:study:[a-f0-9]{20}:\d+)$/.exec(customId);
  if (!match) {
    await originalResponse(interaction, { content: "⚠️ This Attendr action is invalid.", embeds: [], components: [] });
    return;
  }
  const session = await getSession(match[2], env);
  if (!session || session.status !== "scheduled") {
    await originalResponse(interaction, { content: "ℹ️ This study session was already handled.", embeds: [], components: [] });
    return;
  }
  const claim = await env.DB.prepare(
    "INSERT OR IGNORE INTO study_operations(interaction_id,task_uid,action,payload,status,lease_until) SELECT ?,task_uid,?,json_object('session_id',session_id,'task_uid',task_uid,'title',title,'course_name',course_name,'start_at',start_at,'end_at',end_at,'task_due_at',task_due_at,'calendar_id',calendar_id,'event_id',event_id,'status',status,'notified',notified,'manual_override',manual_override,'message_id',message_id),'running',? FROM study_sessions WHERE session_id=? AND status='scheduled' AND NOT EXISTS(SELECT 1 FROM plan_lease WHERE lease_until>?) RETURNING payload"
  ).bind(interaction.id, match[1], Date.now() + 600_000, session.session_id, Date.now()).first<{ payload: string }>();
  if (!claim) {
    await originalResponse(interaction, { content: "Attendr is updating this plan or handling an earlier action. Try again shortly." });
    return;
  }
  await executeOperation(interaction, match[1], JSON.parse(claim.payload) as StudySession, env);
}

async function executeOperation(interaction: DiscordInteraction, action: string, session: StudySession, env: Env): Promise<void> {
  if (action === "complete") await completeSession(interaction, session, env);
  else if (action === "task") await completeTask(interaction, session, env);
  else await reschedule(interaction, session, env);
  await env.DB.prepare("UPDATE study_operations SET status='done',lease_until=0 WHERE interaction_id=?").bind(interaction.id).run();
}

async function recoverOperations(env: Env): Promise<void> {
  const rows = await env.DB.prepare("SELECT interaction_id,action,payload FROM study_operations WHERE status!='done' AND lease_until<? LIMIT 10")
    .bind(Date.now()).all<{ interaction_id: string; action: string; payload: string }>();
  for (const row of rows.results) {
    const claim = await env.DB.prepare("UPDATE study_operations SET lease_until=? WHERE interaction_id=? AND status!='done' AND lease_until<? RETURNING interaction_id")
      .bind(Date.now() + 600_000, row.interaction_id, Date.now()).first();
    if (!claim) continue;
    try {
      await executeOperation({ id: row.interaction_id, type: 3, token: "", application_id: "" }, row.action, JSON.parse(row.payload) as StudySession, env);
    } catch {
      console.error("Study operation awaiting retry", row.interaction_id);
    }
  }
}

async function discordInteraction(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const body = await request.text();
  if (!await verifyDiscord(request, body, env)) return new Response("Invalid signature", { status: 401 });
  let interaction: DiscordInteraction;
  try {
    interaction = JSON.parse(body) as DiscordInteraction;
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }
  if (!interaction || typeof interaction !== "object") return json({ error: "Invalid interaction" }, 400);
  if (interaction.type === 1) return json({ type: 1 });
  if (interaction.type !== 3) return json({ type: 4, data: { content: "Unsupported interaction", flags: 64 } });
  if (!interaction || typeof interaction.id !== "string" || !/^\d+$/.test(interaction.id)) return json({ error: "Missing interaction ID" }, 400);
  const userId = interaction.member?.user?.id ?? interaction.user?.id;
  if (!env.DISCORD_OWNER_USER_ID || !userId || userId !== env.DISCORD_OWNER_USER_ID) {
    return json({ type: 4, data: { content: "Only the Attendr owner can use these controls.", flags: 64 } });
  }
  ctx.waitUntil(handleButton(interaction, env).catch((error) => {
    console.error("Button operation awaiting recovery", interaction.id);
    return originalResponse(interaction, { content: "⚠️ Attendr could not complete that action. The saved operation will retry automatically.", embeds: [], components: [] });
  }));
  return json({ type: 6 });
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/interactions") {
      return discordInteraction(request, env, ctx);
    }
    if (url.pathname === "/health") return json({ ok: true });
    if ((url.pathname === "/api/plan/acquire" || url.pathname === "/api/plan/release") && request.method === "POST") {
      return isAuthorized(request, env) ? planLease(request, env, url.pathname.endsWith("release")) : unauthorized();
    }
    if ((url.pathname === "/api/state-store/acquire" || url.pathname === "/api/state-store/release") && request.method === "POST") {
      return isAuthorized(request, env) ? stateLease(request, env, url.pathname.endsWith("release")) : unauthorized();
    }
    if (url.pathname === "/api/state-store" && request.method === "GET") {
      return isAuthorized(request, env) ? readState(request, env) : unauthorized();
    }
    if (url.pathname === "/api/state-store" && request.method === "PUT") {
      return isAuthorized(request, env) ? writeState(request, env) : unauthorized();
    }
    if (url.pathname === "/api/state" && request.method === "GET") {
      return isAuthorized(request, env) ? getState(env) : unauthorized();
    }
    if (url.pathname === "/api/sessions/sync" && request.method === "POST") {
      return isAuthorized(request, env) ? syncSessions(request, env) : unauthorized();
    }
    if (url.pathname === "/api/reminders/run" && request.method === "POST") {
      if (!isAuthorized(request, env)) return unauthorized();
      await sendDueReminders(env);
      return json({ ok: true });
    }
    return new Response("Not found", { status: 404 });
  },

  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(recoverOperations(env).then(() => sendDueReminders(env)));
  },
} satisfies ExportedHandler<Env>;
