const SERVICE_URL = 'https://todo-render-rxto.onrender.com/ping';
const PING_HANDLER = 'pingWebService';
const PING_INTERVAL_MS = 14 * 60 * 1000;

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
