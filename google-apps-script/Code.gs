const SERVICE_URL = 'https://todo-render-rxto.onrender.com/ping';
const PING_HANDLER = 'pingWebService';
const PING_INTERVAL_MS = 14 * 60 * 1000;
const DISCORD_WEBHOOK_PROPERTY = 'DISCORD_WEBHOOK_URL';

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
