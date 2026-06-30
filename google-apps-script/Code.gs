const SERVICE_URL = 'https://todo-render-rxto.onrender.com/ping';
const PING_HANDLER = 'pingWebService';
const PING_INTERVAL_MS = 14 * 60 * 1000;
const DISCORD_WEBHOOK_PROPERTY = 'DISCORD_WEBHOOK_URL';
const SPREADSHEET_ID_PROPERTY = 'SPREADSHEET_ID';
const DAILY_DIGEST_HANDLER = 'sendDailyTaskDigest';
const TODO_SHEET = 'todos';
const BANGKOK_TIMEZONE = 'Asia/Bangkok';
const DIGEST_TASK_LIMIT = 10;

/** Pings the Render service and schedules the next invocation. */
function pingWebService() {
  const lock = LockService.getScriptLock();

  if (!lock.tryLock(30000)) {
    return;
  }

  try {
    const response = UrlFetchApp.fetch(SERVICE_URL, {
      method: 'get',
      muteHttpExceptions: true,
    });
    const statusCode = response.getResponseCode();

    console.log(`Ping returned HTTP ${statusCode}: ${response.getContentText()}`);

    if (statusCode < 200 || statusCode >= 300) {
      throw new Error(`Ping failed with HTTP ${statusCode}`);
    }
  } catch (error) {
    try {
      sendDiscordNotification_(error);
    } catch (notificationError) {
      console.error(`Discord notification failed: ${notificationError.message}`);
    }

    throw error;
  } finally {
    replacePingTrigger_();
    lock.releaseLock();
  }
}

/** Run once manually to authorize the script and start the schedule. */
function setupPingTrigger() {
  pingWebService();
}

/** Run manually to stop future pings. */
function stopPingTrigger() {
  deletePingTriggers_();
}

/** Run manually to verify the configured Discord webhook. */
function testDiscordNotification() {
  sendDiscordNotification_(new Error('Manual notification test'));
}

function sendDiscordNotification_(error) {
  const webhookUrl = PropertiesService.getScriptProperties()
    .getProperty(DISCORD_WEBHOOK_PROPERTY);

  if (!webhookUrl) {
    throw new Error(`Missing script property: ${DISCORD_WEBHOOK_PROPERTY}`);
  }

  const errorMessage = String(error && error.message ? error.message : error)
    .slice(0, 1500);
  const content = [
    '🚨 Render service ping failed',
    `URL: ${SERVICE_URL}`,
    `Time: ${new Date().toISOString()}`,
    `Error: ${errorMessage}`,
  ].join('\n');

  UrlFetchApp.fetch(webhookUrl, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({
      content,
      allowed_mentions: {parse: []},
    }),
  });
}

function replacePingTrigger_() {
  deletePingTriggers_();
  ScriptApp.newTrigger(PING_HANDLER)
    .timeBased()
    .after(PING_INTERVAL_MS)
    .create();
}

function deletePingTriggers_() {
  ScriptApp.getProjectTriggers()
    .filter((trigger) => trigger.getHandlerFunction() === PING_HANDLER)
    .forEach((trigger) => ScriptApp.deleteTrigger(trigger));
}

/** Reads tasks and sends the normal 09:00 Bangkok Discord digest. */
function sendDailyTaskDigest() {
  const payload = buildDigestPayload_(readDigestTasks_(), new Date(), false);
  payload.content = '@everyone';
  payload.allowed_mentions = {parse: ['everyone']};
  postDiscordPayload_(payload);
  return payload;
}

/** Dry run: reads the real sheet, logs the payload, and never contacts Discord. */
function previewDailyTaskDigest() {
  const payload = buildDigestPayload_(readDigestTasks_(), new Date(), true);
  console.log(JSON.stringify(payload, null, 2));
  return payload;
}

/** Dry run with generated sample tasks. Does not read or write the sheet. */
function previewRandomTaskDigest() {
  const now = new Date();
  const payload = buildDigestPayload_(randomDigestTasks_(now), now, true);
  console.log(JSON.stringify(payload, null, 2));
  return payload;
}

/** Sends a clearly labelled randomized sample to Discord. Never touches the sheet. */
function sendRandomTestDigest() {
  const now = new Date();
  const payload = buildDigestPayload_(randomDigestTasks_(now), now, true);
  postDiscordPayload_(payload);
  return payload;
}

/** Run once to install or replace the daily trigger. */
function installDailyDigestTrigger() {
  deleteDailyDigestTriggers_();
  ScriptApp.newTrigger(DAILY_DIGEST_HANDLER)
    .timeBased()
    .atHour(9)
    .nearMinute(0)
    .everyDays(1)
    .inTimezone(BANGKOK_TIMEZONE)
    .create();
}

function stopDailyDigestTrigger() {
  deleteDailyDigestTriggers_();
}

function deleteDailyDigestTriggers_() {
  ScriptApp.getProjectTriggers()
    .filter((trigger) => trigger.getHandlerFunction() === DAILY_DIGEST_HANDLER)
    .forEach((trigger) => ScriptApp.deleteTrigger(trigger));
}

/** Read-only: this function never calls setValue, appendRow, update, or delete. */
function readDigestTasks_() {
  const spreadsheetId = PropertiesService.getScriptProperties()
    .getProperty(SPREADSHEET_ID_PROPERTY);
  if (!spreadsheetId) {
    throw new Error(`Missing script property: ${SPREADSHEET_ID_PROPERTY}`);
  }

  const sheet = SpreadsheetApp.openById(spreadsheetId).getSheetByName(TODO_SHEET);
  if (!sheet) {
    throw new Error(`Missing sheet: ${TODO_SHEET}`);
  }

  const values = sheet.getDataRange().getValues();
  if (values.length < 2) {
    return [];
  }

  const headers = values[0].map((value) => String(value));
  const indexes = headers.reduce((result, header, index) => {
    result[header] = index;
    return result;
  }, {});
  ['title', 'status', 'priority', 'due_date'].forEach((header) => {
    if (indexes[header] === undefined) {
      throw new Error(`Missing todos column: ${header}`);
    }
  });

  return values.slice(1).map((row) => ({
    title: String(row[indexes.title] || '').trim(),
    status: String(row[indexes.status] || 'inbox').toLowerCase(),
    priority: String(row[indexes.priority] || 'P3').toUpperCase(),
    dueDate: normalizeSheetDay_(row[indexes.due_date]),
    dueAt: indexes.due_at === undefined ? '' : normalizeSheetDateTime_(row[indexes.due_at]),
  })).filter((task) => task.title && !['done', 'removed'].includes(task.status));
}

function buildDigestPayload_(tasks, now, isTest) {
  const today = formatBangkokDay_(now);
  const tomorrow = formatBangkokDay_(new Date(now.getTime() + 24 * 60 * 60 * 1000));
  const groups = {overdue: [], today: [], tomorrow: []};

  tasks.forEach((task) => {
    if (!task.dueDate) return;
    if (isTaskOverdue_(task, now, today)) groups.overdue.push(task);
    else if (task.dueDate === today) groups.today.push(task);
    else if (task.dueDate === tomorrow) groups.tomorrow.push(task);
  });

  Object.keys(groups).forEach((key) => groups[key].sort(compareDigestTasks_));
  const total = groups.overdue.length + groups.today.length + groups.tomorrow.length;
  const dateLabel = Utilities.formatDate(now, BANGKOK_TIMEZONE, 'EEE, d MMM yyyy');
  const prefix = isTest ? '🧪 SAMPLE · ' : '';

  return {
    username: 'Focus Board',
    allowed_mentions: {parse: []},
    embeds: [{
      title: `${prefix}🗂️ Daily Focus Brief`,
      description: [
        `📅 **${dateLabel}** · Bangkok`,
        `🔎 **${total}** relevant · 🚨 ${groups.overdue.length} · ☀️ ${groups.today.length} · 🌤️ ${groups.tomorrow.length}`,
      ].join('\n'),
      color: groups.overdue.length ? 14437476 : 7162623,
      fields: [
        digestField_('🚨 Overdue', groups.overdue),
        digestField_('☀️ Today', groups.today),
        digestField_('🌤️ Tomorrow', groups.tomorrow),
      ],
      footer: {text: isTest ? 'Dry/sample mode · No Sheet changes' : 'Read-only Google Sheets digest'},
      timestamp: now.toISOString(),
    }],
  };
}

function digestField_(label, tasks) {
  if (!tasks.length) {
    return {name: `${label} · 0`, value: '✅ None', inline: false};
  }

  const visible = tasks.slice(0, DIGEST_TASK_LIMIT);
  const lines = visible.map((task) => {
    const priorityEmoji = {P1: '🔴', P2: '🟠', P3: '🟣', P4: '🔵'}[task.priority] || '⚪';
    const time = task.dueAt
      ? Utilities.formatDate(new Date(task.dueAt), BANGKOK_TIMEZONE, 'HH:mm')
      : 'all day';
    return `${priorityEmoji} **${escapeDiscord_(task.title)}** · ${task.priority} · ${time}`;
  });
  if (tasks.length > visible.length) {
    lines.push(`➕ ${tasks.length - visible.length} more`);
  }
  return {name: `${label} · ${tasks.length}`, value: lines.join('\n').slice(0, 1024), inline: false};
}

function isTaskOverdue_(task, now, today) {
  if (task.dueAt) {
    const due = new Date(task.dueAt);
    return !isNaN(due.getTime()) && due.getTime() < now.getTime();
  }
  return task.dueDate < today;
}

function compareDigestTasks_(left, right) {
  const priorityOrder = {P1: 0, P2: 1, P3: 2, P4: 3};
  const leftDue = left.dueAt || `${left.dueDate}T23:59:00+07:00`;
  const rightDue = right.dueAt || `${right.dueDate}T23:59:00+07:00`;
  return String(leftDue).localeCompare(String(rightDue))
    || (priorityOrder[left.priority] ?? 9) - (priorityOrder[right.priority] ?? 9)
    || left.title.localeCompare(right.title);
}

function normalizeSheetDay_(value) {
  if (!value) return '';
  if (value instanceof Date) return formatBangkokDay_(value);
  const match = String(value).match(/^\d{4}-\d{2}-\d{2}/);
  return match ? match[0] : '';
}

function normalizeSheetDateTime_(value) {
  if (!value) return '';
  if (value instanceof Date) return value.toISOString();
  const parsed = new Date(String(value));
  return isNaN(parsed.getTime()) ? '' : parsed.toISOString();
}

function formatBangkokDay_(date) {
  return Utilities.formatDate(date, BANGKOK_TIMEZONE, 'yyyy-MM-dd');
}

function escapeDiscord_(value) {
  return String(value).replace(/@/g, '＠').replace(/([\\`*_~|>])/g, '\\$1').slice(0, 180);
}

function randomDigestTasks_(now) {
  const titles = [
    'Review launch checklist', 'Reply to project update', 'Prepare weekly notes',
    'Confirm design feedback', 'Plan next sprint', 'Check invoice details',
    'Update customer summary', 'Book follow-up meeting', 'Clean up backlog',
  ];
  const count = 4 + Math.floor(Math.random() * 5);
  return Array.from({length: count}, (_, index) => {
    const offset = -2 + Math.floor(Math.random() * 4);
    const due = new Date(now.getTime() + offset * 24 * 60 * 60 * 1000);
    due.setUTCHours(2 + Math.floor(Math.random() * 10), Math.floor(Math.random() * 4) * 15, 0, 0);
    return {
      title: titles[index % titles.length],
      status: ['inbox', 'planned', 'in_progress'][Math.floor(Math.random() * 3)],
      priority: ['P1', 'P2', 'P3', 'P4'][Math.floor(Math.random() * 4)],
      dueDate: formatBangkokDay_(due),
      dueAt: due.toISOString(),
    };
  });
}

function postDiscordPayload_(payload) {
  const webhookUrl = PropertiesService.getScriptProperties()
    .getProperty(DISCORD_WEBHOOK_PROPERTY);
  if (!webhookUrl) {
    throw new Error(`Missing script property: ${DISCORD_WEBHOOK_PROPERTY}`);
  }

  const response = UrlFetchApp.fetch(webhookUrl + '?wait=true', {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  const statusCode = response.getResponseCode();
  if (statusCode < 200 || statusCode >= 300) {
    throw new Error(`Discord webhook failed with HTTP ${statusCode}: ${response.getContentText()}`);
  }
}
