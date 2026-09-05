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

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: JSON_HEADERS });
}

function unauthorized(): Response {
  return json({ error: "Unauthorized" }, 401);
}

function isAuthorized(request: Request, env: Env): boolean {
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
  const publicKey = hexBytes(env.DISCORD_PUBLIC_KEY, 64);
  const signatureBytes = signature ? hexBytes(signature, 128) : null;
  if (!timestamp || !publicKey || !signatureBytes) return false;
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

async function getState(env: Env): Promise<Response> {
  const [tasks, sessions, overrides] = await Promise.all([
    env.DB.prepare("SELECT task_uid FROM completed_tasks").all<{ task_uid: string }>(),
    env.DB.prepare("SELECT session_id FROM study_sessions WHERE status = 'completed'").all<{ session_id: string }>(),
    env.DB.prepare("SELECT session_id, start_at, end_at FROM study_sessions WHERE status='scheduled' AND manual_override=1").all<{ session_id: string; start_at: string; end_at: string }>(),
  ]);
  return json({
    completed_tasks: tasks.results.map((item) => item.task_uid),
    completed_sessions: sessions.results.map((item) => item.session_id),
    rescheduled_sessions: Object.fromEntries(overrides.results.map((item) => [item.session_id, { start: item.start_at, end: item.end_at }])),
  });
}

function validSession(value: unknown): value is SyncedSession {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  const keys = ["session_id", "task_uid", "title", "course_name", "start", "end", "task_due_at", "calendar_id", "event_id"];
  return keys.every((key) => typeof item[key] === "string" && item[key] !== "");
}

async function syncSessions(request: Request, env: Env): Promise<Response> {
  let payload: { sessions?: unknown[]; replace?: boolean };
  try {
    payload = await request.json();
  } catch {
    return json({ error: "Invalid JSON" }, 400);
  }
  const sessions = (payload.sessions ?? []).filter(validSession);
  if (sessions.length !== (payload.sessions ?? []).length || sessions.length > 250) {
    return json({ error: "Invalid sessions" }, 400);
  }
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
      item.session_id, item.task_uid, item.title, item.course_name, item.start,
      item.end, item.task_due_at, item.calendar_id, item.event_id, marker, now,
    ),
  );
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
  const response = await fetch(`${DISCORD_API}/channels/${env.DISCORD_STUDY_CHANNEL_ID}/messages`, {
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
  if (!response.ok) throw new Error(`Discord reminder failed: ${response.status}`);
  const message = await response.json() as { id: string };
  await env.DB.prepare(
    "UPDATE study_sessions SET notified=1, message_id=?, updated_at=? WHERE session_id=?",
  ).bind(message.id, new Date().toISOString(), session.session_id).run();
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
    try {
      await sendReminder(session, env);
    } catch (error) {
      console.error("Reminder failed", session.session_id, error);
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
  const response = await fetch("https://oauth2.googleapis.com/token", {
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
  const response = await fetch(url, { method: "DELETE", headers: { authorization: `Bearer ${token}` } });
  if (!response.ok && response.status !== 404 && response.status !== 410) {
    throw new Error(`Calendar delete failed: ${response.status}`);
  }
}

async function originalResponse(interaction: DiscordInteraction, body: unknown): Promise<void> {
  const url = `${DISCORD_API}/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`;
  const response = await fetch(url, {
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
  for (const row of rows.results) await deleteCalendarEvent(row, token);
  const now = new Date().toISOString();
  await env.DB.batch([
    env.DB.prepare("INSERT OR REPLACE INTO completed_tasks(task_uid, completed_at) VALUES (?, ?)").bind(session.task_uid, now),
    env.DB.prepare("UPDATE study_sessions SET status='completed', updated_at=? WHERE task_uid=?").bind(now, session.task_uid),
  ]);
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
  const response = await fetch("https://www.googleapis.com/calendar/v3/users/me/calendarList?minAccessRole=reader&maxResults=250", {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error(`Calendar list failed: ${response.status}`);
  const payload = await response.json() as { items?: Array<{ id?: string }> };
  return (payload.items ?? []).flatMap((item) => item.id && item.id !== excluded ? [item.id] : []).slice(0, 50);
}

async function busyIntervals(token: string, ids: string[], start: Date, end: Date): Promise<Array<{ start: string; end: string }>> {
  const response = await fetch("https://www.googleapis.com/calendar/v3/freeBusy", {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({ timeMin: start.toISOString(), timeMax: end.toISOString(), timeZone: TIME_ZONE, items: ids.map((id) => ({ id })) }),
  });
  if (!response.ok) throw new Error(`Calendar availability failed: ${response.status}`);
  const payload = await response.json() as { calendars?: Record<string, { busy?: Array<{ start: string; end: string }> }> };
  return Object.values(payload.calendars ?? {}).flatMap((value) => value.busy ?? []);
}

async function nextSlot(session: StudySession, token: string, env: Env): Promise<{ start: Date; end: Date } | null> {
  const duration = new Date(session.end_at).getTime() - new Date(session.start_at).getTime();
  const now = new Date();
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
    for (const [windowStart, windowEnd] of WINDOWS[weekday]) {
      for (let minute = windowStart; minute + duration / 60_000 <= windowEnd; minute += 15) {
        const start = zonedToUtc(day.year, day.month, day.day, Math.floor(minute / 60), minute % 60);
        const end = new Date(start.getTime() + duration);
        if (start.getTime() < now.getTime() + 15 * 60_000 || end >= deadline) continue;
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
  const slot = await nextSlot(session, token, env);
  if (!slot) {
    await originalResponse(interaction, { content: "⚠️ No acceptable free slot was found before the deadline.", embeds: [], components: [] });
    return;
  }
  const url = `https://www.googleapis.com/calendar/v3/calendars/${encodeURIComponent(session.calendar_id)}/events/${encodeURIComponent(session.event_id)}`;
  const response = await fetch(url, {
    method: "PATCH",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({ start: { dateTime: slot.start.toISOString(), timeZone: TIME_ZONE }, end: { dateTime: slot.end.toISOString(), timeZone: TIME_ZONE } }),
  });
  if (!response.ok) throw new Error(`Calendar reschedule failed: ${response.status}`);
  await env.DB.prepare(
    "UPDATE study_sessions SET start_at=?, end_at=?, notified=0, manual_override=1, message_id=NULL, updated_at=? WHERE session_id=?",
  ).bind(slot.start.toISOString(), slot.end.toISOString(), new Date().toISOString(), session.session_id).run();
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
  if (match[1] === "complete") await completeSession(interaction, session, env);
  else if (match[1] === "task") await completeTask(interaction, session, env);
  else await reschedule(interaction, session, env);
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
  if (interaction.type === 1) return json({ type: 1 });
  if (interaction.type !== 3) return json({ type: 4, data: { content: "Unsupported interaction", flags: 64 } });
  const userId = interaction.member?.user?.id ?? interaction.user?.id;
  if (userId !== env.DISCORD_OWNER_USER_ID) {
    return json({ type: 4, data: { content: "Only the Attendr owner can use these controls.", flags: 64 } });
  }
  ctx.waitUntil(handleButton(interaction, env).catch((error) => {
    console.error("Button action failed", error);
    return originalResponse(interaction, { content: "⚠️ Attendr could not complete that action. Please try again.", embeds: [], components: [] });
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
    ctx.waitUntil(sendDueReminders(env));
  },
} satisfies ExportedHandler<Env>;
