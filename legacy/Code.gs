const CONFIG = Object.freeze({
  TODO_SHEET: 'todos',
  ATTACHMENT_SHEET: 'attachments',
  META_SHEET: 'meta',
  ACTIVITY_SHEET: 'activity_log',
  PAGE_SIZE: 50,
  TIMEZONE: 'Asia/Bangkok',
  SCHEMA_VERSION: 2,
  MAX_ATTACHMENTS: 5,
  MAX_IMAGE_BYTES: 5 * 1024 * 1024,
  SPREADSHEET_ID_PROPERTY: 'SPREADSHEET_ID',
  DRIVE_ROOT_ID_PROPERTY: 'DRIVE_ROOT_FOLDER_ID',
  DISCORD_WEBHOOK_PROPERTY: 'DISCORD_WEBHOOK_URL',
  ACTIVITY_LOG_PROPERTY: 'ACTIVITY_LOG_ENABLED'
});

const TODO_HEADERS = Object.freeze([
  'id', 'title', 'details', 'status', 'priority', 'due_date', 'tags_json',
  'created_at', 'updated_at', 'completed_at', 'version'
]);

const ATTACHMENT_HEADERS = Object.freeze([
  'id', 'task_id', 'drive_file_id', 'file_name', 'mime_type', 'byte_size',
  'width', 'height', 'sort_order', 'cache_version', 'created_at'
]);

const ACTIVITY_HEADERS = Object.freeze([
  'event_id', 'task_id', 'action', 'task_version', 'database_version',
  'changed_at', 'snapshot_json'
]);

const STATUSES = Object.freeze(['inbox', 'planned', 'in_progress', 'blocked', 'done']);
const PRIORITIES = Object.freeze(['P1', 'P2', 'P3', 'P4']);

function doGet(e) {
  const action = e && e.parameter && e.parameter.action;
  if (!action) {
    return HtmlService.createTemplateFromFile('Index').evaluate()
      .setTitle('Focus Board')
      .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
  }

  try {
    let payload;
    if (action === 'meta') payload = getMeta_();
    else if (action === 'summary') payload = getSummary_();
    else if (action === 'list') payload = listTasks_({
      limit: e.parameter.limit, cursor: e.parameter.cursor, status: e.parameter.status
    });
    else fail_('Unknown GET action: ' + action);
    return json_({ ok: true, data: payload });
  } catch (error) {
    return json_({ ok: false, error: error.message });
  }
}

function doPost(e) {
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    return json_({ ok: true, data: api(body.action, body.payload || {}) });
  } catch (error) {
    return json_({ ok: false, error: error.message });
  }
}

/** Router shared by google.script.run and JSON clients. */
function api(action, payload) {
  switch (action) {
    case 'meta': return getMeta_();
    case 'summary': return getSummary_();
    case 'list': return listTasks_(payload || {});
    case 'create': return createTask_(payload || {});
    case 'update': return updateTask_(payload || {});
    case 'uploadAttachment': return uploadAttachment_(payload || {});
    case 'getAttachment': return getAttachment_(payload || {});
    case 'deleteAttachment': return deleteAttachment_(payload || {});
    default: return fail_('Unknown action: ' + action);
  }
}

/** Run once before deploying. Safe to run repeatedly. */
function setup() {
  const spreadsheet = getSpreadsheet_();
  ensureTodosSheet_(spreadsheet);
  ensureSheet_(spreadsheet, CONFIG.ATTACHMENT_SHEET, ATTACHMENT_HEADERS);
  const metaSheet = ensureSheet_(spreadsheet, CONFIG.META_SHEET, ['key', 'value']);
  ensureMeta_(metaSheet);
  ensureSheet_(spreadsheet, CONFIG.ACTIVITY_SHEET, ACTIVITY_HEADERS);
  const driveRoot = getDriveRoot_();
  getOrCreateChildFolder_(driveRoot, 'tasks');

  const meta = readMeta_(metaSheet);
  return {
    spreadsheetId: spreadsheet.getId(),
    spreadsheetUrl: spreadsheet.getUrl(),
    driveRootId: driveRoot.getId(),
    driveRootUrl: driveRoot.getUrl(),
    schemaVersion: Number(meta.schema_version),
    databaseVersion: Number(meta.database_version)
  };
}

function getMeta_() {
  const spreadsheet = getSpreadsheet_();
  const meta = readMeta_(requireSheet_(spreadsheet, CONFIG.META_SHEET));
  return {
    schemaVersion: Number(meta.schema_version || 0),
    databaseVersion: Number(meta.database_version || 0),
    lastUpdatedAt: meta.last_updated_at || null,
    serverTime: new Date().toISOString()
  };
}

function listTasks_(options) {
  const spreadsheet = getSpreadsheet_();
  const tasks = readTaskRows_(spreadsheet);
  const attachmentsByTask = readAttachmentsByTask_(spreadsheet);
  const requestedStatus = String(options.status || 'all').toLowerCase();
  const limit = Math.max(1, Math.min(Number(options.limit) || CONFIG.PAGE_SIZE, 100));
  const offset = decodeCursor_(options.cursor);

  const filtered = tasks
    .filter(task => requestedStatus === 'all' || task.status === requestedStatus)
    .sort(compareFocusTasks_);
  const page = filtered.slice(offset, offset + limit)
    .map(task => taskForClient_(task, attachmentsByTask[task.id] || []));
  const nextOffset = offset + page.length;

  return {
    items: page,
    nextCursor: nextOffset < filtered.length ? encodeCursor_(nextOffset) : null,
    databaseVersion: getMeta_().databaseVersion
  };
}

function getSummary_() {
  const tasks = readTaskRows_(getSpreadsheet_());
  const today = today_();
  const monday = weekStart_(today);
  const nextMonday = addDays_(monday, 7);
  const active = tasks.filter(task => task.status !== 'done');
  const doneThisWeek = tasks.filter(task => {
    const day = task.completed_at ? formatDay_(new Date(task.completed_at)) : '';
    return day && day >= monday && day < nextMonday;
  }).length;
  const unfinishedDueThisWeek = active.filter(task =>
    task.due_date && task.due_date >= monday && task.due_date < nextMonday).length;
  const denominator = doneThisWeek + unfinishedDueThisWeek;

  return {
    dueToday: active.filter(task => task.due_date === today).length,
    overdue: active.filter(task => task.due_date && task.due_date < today).length,
    inProgress: tasks.filter(task => task.status === 'in_progress').length,
    doneThisWeek: doneThisWeek,
    completionPercent: denominator ? Math.round(doneThisWeek / denominator * 100) : 0,
    today: today,
    weekStartsAt: monday
  };
}

function createTask_(input) {
  return withScriptLock_(function () {
    validateTaskInput_(input, false);
    const spreadsheet = getSpreadsheet_();
    const now = new Date().toISOString();
    const status = normalizeStatus_(input.status || 'inbox');
    const task = {
      id: Utilities.getUuid(),
      title: String(input.title).trim(),
      details: String(input.details || '').trim(),
      status: status,
      priority: normalizePriority_(input.priority || 'P3'),
      due_date: normalizeDate_(input.dueDate),
      tags_json: JSON.stringify(normalizeTags_(input.tags)),
      created_at: now,
      updated_at: now,
      completed_at: status === 'done' ? now : '',
      version: 1
    };
    requireSheet_(spreadsheet, CONFIG.TODO_SHEET)
      .appendRow(TODO_HEADERS.map(header => task[header]));
    const databaseVersion = bumpDatabaseVersion_(spreadsheet, now);
    logActivity_(spreadsheet, 'create', task, databaseVersion, []);
    return { item: taskForClient_(task, []), databaseVersion: databaseVersion };
  });
}

function updateTask_(input) {
  return withScriptLock_(function () {
    const spreadsheet = getSpreadsheet_();
    const record = findTaskRecord_(spreadsheet, input.id);
    assertVersion_(record.task, input.version);
    validateTaskInput_(input, true);

    const task = Object.assign({}, record.task);
    if (has_(input, 'title')) task.title = String(input.title).trim();
    if (has_(input, 'details')) task.details = String(input.details || '').trim();
    if (has_(input, 'status')) task.status = normalizeStatus_(input.status);
    if (has_(input, 'priority')) task.priority = normalizePriority_(input.priority);
    if (has_(input, 'dueDate')) task.due_date = normalizeDate_(input.dueDate);
    if (has_(input, 'tags')) task.tags_json = JSON.stringify(normalizeTags_(input.tags));
    task.updated_at = new Date().toISOString();
    if (task.status === 'done' && !task.completed_at) task.completed_at = task.updated_at;
    if (task.status !== 'done') task.completed_at = '';
    task.version = Number(record.task.version) + 1;

    writeTaskRecord_(record, task);
    const attachments = attachmentsForTask_(spreadsheet, task.id);
    const databaseVersion = bumpDatabaseVersion_(spreadsheet, task.updated_at);
    logActivity_(spreadsheet, 'update', task, databaseVersion, attachments);
    return { item: taskForClient_(task, attachments), databaseVersion: databaseVersion };
  });
}

function uploadAttachment_(input) {
  return withScriptLock_(function () {
    const spreadsheet = getSpreadsheet_();
    const record = findTaskRecord_(spreadsheet, input.taskId);
    assertVersion_(record.task, input.version);
    const existing = attachmentsForTask_(spreadsheet, record.task.id);
    if (existing.length >= CONFIG.MAX_ATTACHMENTS) fail_('A task can have at most 5 images.');

    const mimeType = String(input.mimeType || '');
    if (['image/jpeg', 'image/png', 'image/webp'].indexOf(mimeType) < 0) {
      fail_('Use JPEG, PNG, or WebP images.');
    }
    const parsed = parseDataUrl_(input.dataUrl, mimeType);
    if (parsed.bytes.length > CONFIG.MAX_IMAGE_BYTES || Number(input.byteSize) > CONFIG.MAX_IMAGE_BYTES) {
      fail_('Each image must be 5 MB or smaller.');
    }

    const attachmentId = Utilities.getUuid();
    const safeName = sanitizeFileName_(input.fileName || 'image');
    const taskFolder = getTaskFolder_(record.task.id);
    const file = taskFolder.createFile(Utilities.newBlob(parsed.bytes, mimeType, attachmentId + '-' + safeName));
    const now = new Date().toISOString();
    const attachment = {
      id: attachmentId,
      task_id: record.task.id,
      drive_file_id: file.getId(),
      file_name: safeName,
      mime_type: mimeType,
      byte_size: parsed.bytes.length,
      width: Math.max(1, Number(input.width) || 1),
      height: Math.max(1, Number(input.height) || 1),
      sort_order: existing.length,
      cache_version: 1,
      created_at: now
    };

    let attachmentRow = 0;
    try {
      const attachmentSheet = requireSheet_(spreadsheet, CONFIG.ATTACHMENT_SHEET);
      attachmentSheet.appendRow(ATTACHMENT_HEADERS.map(header => attachment[header]));
      attachmentRow = attachmentSheet.getLastRow();
      const task = Object.assign({}, record.task, {
        updated_at: now,
        version: Number(record.task.version) + 1
      });
      writeTaskRecord_(record, task);
      const allAttachments = existing.concat([attachmentForClient_(attachment)]);
      const databaseVersion = bumpDatabaseVersion_(spreadsheet, now);
      logActivity_(spreadsheet, 'attachment_create', task, databaseVersion, allAttachments);
      return {
        item: taskForClient_(task, allAttachments),
        attachment: attachmentForClient_(attachment),
        databaseVersion: databaseVersion
      };
    } catch (error) {
      try { file.setTrashed(true); } catch (_) {}
      if (attachmentRow > 1) {
        try { requireSheet_(spreadsheet, CONFIG.ATTACHMENT_SHEET).deleteRow(attachmentRow); } catch (_) {}
      }
      throw error;
    }
  });
}

function getAttachment_(input) {
  const spreadsheet = getSpreadsheet_();
  const attachment = findAttachmentRecord_(spreadsheet, input.id).attachment;
  if (has_(input, 'cacheVersion') && Number(input.cacheVersion) !== Number(attachment.cache_version)) {
    fail_('Attachment cache version changed. Refresh task data.');
  }
  const blob = DriveApp.getFileById(String(attachment.drive_file_id)).getBlob();
  return {
    id: String(attachment.id),
    cacheVersion: Number(attachment.cache_version),
    dataUrl: 'data:' + attachment.mime_type + ';base64,' + Utilities.base64Encode(blob.getBytes())
  };
}

function deleteAttachment_(input) {
  return withScriptLock_(function () {
    const spreadsheet = getSpreadsheet_();
    const taskRecord = findTaskRecord_(spreadsheet, input.taskId);
    assertVersion_(taskRecord.task, input.version);
    const attachmentRecord = findAttachmentRecord_(spreadsheet, input.attachmentId);
    if (String(attachmentRecord.attachment.task_id) !== String(taskRecord.task.id)) {
      fail_('Attachment does not belong to this task.');
    }

    const now = new Date().toISOString();
    const task = Object.assign({}, taskRecord.task, {
      updated_at: now,
      version: Number(taskRecord.task.version) + 1
    });
    writeTaskRecord_(taskRecord, task);
    attachmentRecord.sheet.deleteRow(attachmentRecord.rowNumber);
    try { DriveApp.getFileById(String(attachmentRecord.attachment.drive_file_id)).setTrashed(true); } catch (_) {}
    normalizeAttachmentOrder_(spreadsheet, task.id);

    const attachments = attachmentsForTask_(spreadsheet, task.id);
    const databaseVersion = bumpDatabaseVersion_(spreadsheet, now);
    logActivity_(spreadsheet, 'attachment_delete', task, databaseVersion, attachments);
    return { item: taskForClient_(task, attachments), databaseVersion: databaseVersion };
  });
}

function sendDailyDiscordDigest() {
  const webhookUrl = PropertiesService.getScriptProperties().getProperty(CONFIG.DISCORD_WEBHOOK_PROPERTY);
  if (!webhookUrl) fail_('Set DISCORD_WEBHOOK_URL in Script Properties first.');
  const tasks = readTaskRows_(getSpreadsheet_())
    .filter(task => task.status !== 'done')
    .sort(compareFocusTasks_);
  const lines = tasks.slice(0, 25).map((task, index) => {
    const due = task.due_date ? ' — due ' + task.due_date : '';
    return (index + 1) + '. [' + task.priority + '] ' + task.title + due;
  });
  const content = lines.length
    ? '**Daily Focus Board (' + tasks.length + ' active)**\n' + lines.join('\n')
    : '**Daily Focus Board**\nEverything is done. Nice.';
  UrlFetchApp.fetch(webhookUrl, {
    method: 'post', contentType: 'application/json',
    payload: JSON.stringify({ content: content.slice(0, 2000) }), muteHttpExceptions: false
  });
}

function installDailyDigestTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(trigger => trigger.getHandlerFunction() === 'sendDailyDiscordDigest')
    .forEach(trigger => ScriptApp.deleteTrigger(trigger));
  ScriptApp.newTrigger('sendDailyDiscordDigest').timeBased().everyDays(1).atHour(8).create();
}

function getSpreadsheet_() {
  const id = PropertiesService.getScriptProperties().getProperty(CONFIG.SPREADSHEET_ID_PROPERTY);
  if (id) return SpreadsheetApp.openById(id);
  const active = SpreadsheetApp.getActiveSpreadsheet();
  if (!active) fail_('Set SPREADSHEET_ID in Script Properties or bind this project to a Sheet.');
  return active;
}

function getDriveRoot_() {
  const properties = PropertiesService.getScriptProperties();
  const configuredId = properties.getProperty(CONFIG.DRIVE_ROOT_ID_PROPERTY);
  if (configuredId) {
    try { return DriveApp.getFolderById(configuredId); } catch (_) {}
  }
  const folder = DriveApp.createFolder('TODOApp');
  properties.setProperty(CONFIG.DRIVE_ROOT_ID_PROPERTY, folder.getId());
  return folder;
}

function getTaskFolder_(taskId) {
  const tasksFolder = getOrCreateChildFolder_(getDriveRoot_(), 'tasks');
  return getOrCreateChildFolder_(tasksFolder, String(taskId));
}

function getOrCreateChildFolder_(parent, name) {
  const folders = parent.getFoldersByName(name);
  return folders.hasNext() ? folders.next() : parent.createFolder(name);
}

function ensureTodosSheet_(spreadsheet) {
  const sheet = spreadsheet.getSheetByName(CONFIG.TODO_SHEET);
  if (!sheet || sheet.getLastRow() === 0) return ensureSheet_(spreadsheet, CONFIG.TODO_SHEET, TODO_HEADERS);
  const actual = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0]
    .map(value => String(value)).filter(Boolean);
  if (actual.join('|') === TODO_HEADERS.join('|')) return sheet;
  const v1 = ['id', 'title', 'notes', 'status', 'due_date', 'created_at', 'updated_at', 'version'];
  if (actual.join('|') === v1.join('|')) return migrateTodosV1_(sheet);
  fail_('Unexpected schema in sheet: ' + CONFIG.TODO_SHEET);
}

function migrateTodosV1_(sheet) {
  const oldRows = sheet.getLastRow() > 1
    ? sheet.getRange(2, 1, sheet.getLastRow() - 1, 8).getValues() : [];
  const migrated = oldRows.map(row => {
    const oldStatus = String(row[3] || 'open');
    const status = oldStatus === 'doing' ? 'in_progress' : oldStatus === 'done' ? 'done' : 'inbox';
    const updatedAt = toIsoString_(row[6]);
    return [
      String(row[0]), String(row[1]), String(row[2] || ''), status, 'P3', normalizeDate_(row[4]), '[]',
      toIsoString_(row[5]), updatedAt, status === 'done' ? updatedAt : '', Number(row[7]) || 1
    ];
  });
  sheet.clearContents();
  sheet.getRange(1, 1, 1, TODO_HEADERS.length).setValues([TODO_HEADERS]);
  if (migrated.length) sheet.getRange(2, 1, migrated.length, TODO_HEADERS.length).setValues(migrated);
  formatHeader_(sheet, TODO_HEADERS.length);
  return sheet;
}

function ensureSheet_(spreadsheet, name, headers) {
  const sheet = spreadsheet.getSheetByName(name) || spreadsheet.insertSheet(name);
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    formatHeader_(sheet, headers.length);
  } else {
    const actual = sheet.getRange(1, 1, 1, headers.length).getValues()[0].map(String);
    if (actual.join('|') !== headers.join('|')) fail_('Unexpected schema in sheet: ' + name);
  }
  return sheet;
}

function formatHeader_(sheet, width) {
  sheet.setFrozenRows(1);
  sheet.getRange(1, 1, 1, width).setFontWeight('bold').setBackground('#ece8ff');
}

function ensureMeta_(sheet) {
  const meta = readMeta_(sheet);
  const now = new Date().toISOString();
  if (!has_(meta, 'schema_version')) sheet.appendRow(['schema_version', CONFIG.SCHEMA_VERSION]);
  else setMetaValue_(sheet, 'schema_version', CONFIG.SCHEMA_VERSION);
  if (!has_(meta, 'database_version')) sheet.appendRow(['database_version', 0]);
  if (!has_(meta, 'last_updated_at')) sheet.appendRow(['last_updated_at', now]);
}

function setMetaValue_(sheet, key, value) {
  const values = sheet.getDataRange().getValues();
  for (let index = 1; index < values.length; index += 1) {
    if (String(values[index][0]) === key) {
      sheet.getRange(index + 1, 2).setValue(value);
      return;
    }
  }
  sheet.appendRow([key, value]);
}

function readMeta_(sheet) {
  if (sheet.getLastRow() < 2) return {};
  return sheet.getRange(2, 1, sheet.getLastRow() - 1, 2).getValues()
    .reduce((result, row) => { result[String(row[0])] = row[1]; return result; }, {});
}

function bumpDatabaseVersion_(spreadsheet, updatedAt) {
  const sheet = requireSheet_(spreadsheet, CONFIG.META_SHEET);
  const meta = readMeta_(sheet);
  const next = Number(meta.database_version || 0) + 1;
  setMetaValue_(sheet, 'database_version', next);
  setMetaValue_(sheet, 'last_updated_at', updatedAt);
  return next;
}

function readTaskRows_(spreadsheet) {
  return readObjects_(requireSheet_(spreadsheet, CONFIG.TODO_SHEET), TODO_HEADERS)
    .map(task => {
      task.id = String(task.id);
      task.status = String(task.status || 'inbox');
      task.priority = String(task.priority || 'P3');
      task.due_date = task.due_date ? String(task.due_date).slice(0, 10) : '';
      task.version = Number(task.version) || 1;
      return task;
    });
}

function readAttachmentsByTask_(spreadsheet) {
  const result = {};
  readObjects_(requireSheet_(spreadsheet, CONFIG.ATTACHMENT_SHEET), ATTACHMENT_HEADERS)
    .forEach(attachment => {
      const taskId = String(attachment.task_id);
      if (!result[taskId]) result[taskId] = [];
      result[taskId].push(attachmentForClient_(attachment));
    });
  Object.keys(result).forEach(taskId => result[taskId].sort((a, b) => a.sortOrder - b.sortOrder));
  return result;
}

function attachmentsForTask_(spreadsheet, taskId) {
  return (readAttachmentsByTask_(spreadsheet)[String(taskId)] || []);
}

function findTaskRecord_(spreadsheet, id) {
  const taskId = String(id || '').trim();
  if (!taskId) fail_('Task id is required.');
  const sheet = requireSheet_(spreadsheet, CONFIG.TODO_SHEET);
  const values = sheet.getDataRange().getValues();
  for (let index = 1; index < values.length; index += 1) {
    if (String(values[index][0]) === taskId) {
      return { sheet: sheet, rowNumber: index + 1, task: objectFromRow_(TODO_HEADERS, values[index]) };
    }
  }
  return fail_('Task not found.');
}

function writeTaskRecord_(record, task) {
  record.sheet.getRange(record.rowNumber, 1, 1, TODO_HEADERS.length)
    .setValues([TODO_HEADERS.map(header => task[header])]);
}

function findAttachmentRecord_(spreadsheet, id) {
  const attachmentId = String(id || '').trim();
  if (!attachmentId) fail_('Attachment id is required.');
  const sheet = requireSheet_(spreadsheet, CONFIG.ATTACHMENT_SHEET);
  const values = sheet.getDataRange().getValues();
  for (let index = 1; index < values.length; index += 1) {
    if (String(values[index][0]) === attachmentId) {
      return { sheet: sheet, rowNumber: index + 1, attachment: objectFromRow_(ATTACHMENT_HEADERS, values[index]) };
    }
  }
  return fail_('Attachment not found.');
}

function normalizeAttachmentOrder_(spreadsheet, taskId) {
  const sheet = requireSheet_(spreadsheet, CONFIG.ATTACHMENT_SHEET);
  if (sheet.getLastRow() < 2) return;
  const values = sheet.getRange(2, 1, sheet.getLastRow() - 1, ATTACHMENT_HEADERS.length).getValues();
  let order = 0;
  values.forEach((row, index) => {
    if (String(row[1]) === String(taskId)) {
      sheet.getRange(index + 2, 9).setValue(order);
      order += 1;
    }
  });
}

function taskForClient_(task, attachments) {
  return {
    id: String(task.id),
    title: String(task.title),
    details: String(task.details || ''),
    status: String(task.status || 'inbox'),
    priority: String(task.priority || 'P3'),
    dueDate: task.due_date ? String(task.due_date).slice(0, 10) : '',
    tags: parseTags_(task.tags_json),
    attachments: attachments || [],
    createdAt: toIsoString_(task.created_at),
    updatedAt: toIsoString_(task.updated_at),
    completedAt: task.completed_at ? toIsoString_(task.completed_at) : null,
    version: Number(task.version)
  };
}

function attachmentForClient_(attachment) {
  if (has_(attachment, 'taskId')) return attachment;
  return {
    id: String(attachment.id),
    taskId: String(attachment.task_id),
    fileName: String(attachment.file_name),
    mimeType: String(attachment.mime_type),
    byteSize: Number(attachment.byte_size),
    width: Number(attachment.width),
    height: Number(attachment.height),
    storageId: String(attachment.drive_file_id),
    cacheVersion: Number(attachment.cache_version),
    sortOrder: Number(attachment.sort_order),
    createdAt: toIsoString_(attachment.created_at)
  };
}

function validateTaskInput_(input, partial) {
  if (!partial || has_(input, 'title')) {
    if (!String(input.title || '').trim()) fail_('Title is required.');
  }
  if (has_(input, 'status')) normalizeStatus_(input.status);
  if (has_(input, 'priority')) normalizePriority_(input.priority);
  if (has_(input, 'dueDate')) normalizeDate_(input.dueDate);
  if (has_(input, 'tags')) normalizeTags_(input.tags);
}

function normalizeStatus_(status) {
  const value = String(status || '').toLowerCase();
  if (STATUSES.indexOf(value) < 0) fail_('Invalid task status.');
  return value;
}

function normalizePriority_(priority) {
  const value = String(priority || '').toUpperCase();
  if (PRIORITIES.indexOf(value) < 0) fail_('Invalid priority.');
  return value;
}

function normalizeDate_(value) {
  if (!value) return '';
  if (value instanceof Date) return formatDay_(value);
  const day = String(value).slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) fail_('Due date must use YYYY-MM-DD.');
  return day;
}

function normalizeTags_(tags) {
  if (tags === undefined || tags === null) return [];
  if (!Array.isArray(tags)) fail_('Tags must be an array.');
  const unique = [];
  tags.forEach(tag => {
    const value = String(tag || '').trim().toLowerCase();
    if (value && unique.indexOf(value) < 0 && unique.length < 8) unique.push(value);
  });
  return unique;
}

function parseTags_(json) {
  try { return normalizeTags_(JSON.parse(String(json || '[]'))); } catch (_) { return []; }
}

function parseDataUrl_(dataUrl, expectedMimeType) {
  const match = String(dataUrl || '').match(/^data:([^;]+);base64,([A-Za-z0-9+/=]+)$/);
  if (!match) fail_('Image data is required.');
  if (match[1] !== expectedMimeType) fail_('Image MIME type does not match its data.');
  return { bytes: Utilities.base64Decode(match[2]) };
}

function sanitizeFileName_(value) {
  const cleaned = String(value).replace(/[\\/:*?"<>|\u0000-\u001f]/g, '-').replace(/\s+/g, ' ').trim();
  return (cleaned || 'image').slice(0, 140);
}

function assertVersion_(task, expectedVersion) {
  if (!Number.isInteger(Number(expectedVersion))) fail_('An integer task version is required.');
  if (Number(task.version) !== Number(expectedVersion)) {
    throw new Error('VERSION_CONFLICT: This task changed elsewhere. Refresh and try again.');
  }
}

function compareFocusTasks_(a, b) {
  const today = today_();
  const score = task => task.status !== 'done' && task.due_date && task.due_date < today ? 0
    : task.status !== 'done' && task.due_date === today ? 1 : 2;
  return score(a) - score(b)
    || PRIORITIES.indexOf(a.priority) - PRIORITIES.indexOf(b.priority)
    || String(a.due_date || '9999-99-99').localeCompare(String(b.due_date || '9999-99-99'))
    || String(b.updated_at).localeCompare(String(a.updated_at));
}

function today_() { return formatDay_(new Date()); }
function formatDay_(date) { return Utilities.formatDate(date, CONFIG.TIMEZONE, 'yyyy-MM-dd'); }
function weekStart_(day) {
  const date = new Date(day + 'T12:00:00+07:00');
  return addDays_(day, -((date.getUTCDay() + 6) % 7));
}
function addDays_(day, amount) {
  const date = new Date(day + 'T12:00:00+07:00');
  date.setUTCDate(date.getUTCDate() + amount);
  return formatDay_(date);
}

function logActivity_(spreadsheet, action, task, databaseVersion, attachments) {
  if (PropertiesService.getScriptProperties().getProperty(CONFIG.ACTIVITY_LOG_PROPERTY) !== 'true') return;
  requireSheet_(spreadsheet, CONFIG.ACTIVITY_SHEET).appendRow([
    Utilities.getUuid(), task.id, action, task.version, databaseVersion,
    task.updated_at, JSON.stringify(taskForClient_(task, attachments || []))
  ]);
}

function readObjects_(sheet, headers) {
  if (sheet.getLastRow() < 2) return [];
  return sheet.getRange(2, 1, sheet.getLastRow() - 1, headers.length).getValues()
    .map(row => objectFromRow_(headers, row));
}

function objectFromRow_(headers, row) {
  return headers.reduce((object, header, index) => {
    object[header] = row[index] instanceof Date ? row[index].toISOString() : row[index];
    return object;
  }, {});
}

function toIsoString_(value) {
  if (!value) return '';
  if (value instanceof Date) return value.toISOString();
  const parsed = new Date(value);
  return isNaN(parsed.getTime()) ? String(value) : parsed.toISOString();
}

function encodeCursor_(offset) { return Utilities.base64EncodeWebSafe(String(offset)); }
function decodeCursor_(cursor) {
  if (!cursor) return 0;
  try {
    const value = Number(Utilities.newBlob(Utilities.base64DecodeWebSafe(cursor)).getDataAsString());
    return Number.isInteger(value) && value >= 0 ? value : 0;
  } catch (_) { return 0; }
}

function withScriptLock_(callback) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try { return callback(); } finally { lock.releaseLock(); }
}

function requireSheet_(spreadsheet, name) {
  const sheet = spreadsheet.getSheetByName(name);
  if (!sheet) fail_('Missing sheet "' + name + '". Run setup() first.');
  return sheet;
}

function has_(object, key) { return Object.prototype.hasOwnProperty.call(object, key); }
function json_(payload) { return ContentService.createTextOutput(JSON.stringify(payload)).setMimeType(ContentService.MimeType.JSON); }
function fail_(message) { throw new Error(message); }
