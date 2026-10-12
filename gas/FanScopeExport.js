/**
 * FanScope metadata export — companion for the upstream taku3jp/x-collect
 * Apps Script (Code.js). Read-only: it adds a new POST action that returns
 * the「X収集」tab's metadata columns as a strict JSON envelope. It never
 * writes cells, never calls ensureSheets_, and never returns post bodies,
 * media links or Drive URLs.
 *
 * Install:
 *   1. Paste this file into the same Apps Script project as the upstream
 *      Code.js (it reuses DATA_SHEET, getToken_() and ok()).
 *   2. Patch the upstream doPost so the export is answered BEFORE the lock
 *      and before checkToken/ensureSheets_ run. Insert as the very first
 *      statements of doPost(e):
 *
 *        var __fanscope;
 *        try { __fanscope = JSON.parse((e && e.postData && e.postData.contents) || "{}"); }
 *        catch (_) { __fanscope = null; }
 *        if (__fanscope && __fanscope.action === "fanscope_export") return fanscopeExport_(e);
 *
 *      (then the existing `const lock = LockService.getScriptLock();` line).
 *   3. Insert at the very start of doGet(e), before auth/initialization:
 *        if (e && e.parameter && e.parameter.action === "fanscope_export_capability") return fanscopeExportCapability_();
 *   4. Deploy a new web-app version and point FanScope's
 *      X_COLLECT_APPS_SCRIPT_URL at the /macros/s/<id>/exec URL.
 *
 * Request:  POST {"action":"fanscope_export","token":"<script TOKEN>"}
 * Response: {"version":1,"source":"taku3jp/x-collect","rows":[{"url","date",
 *            "impressions","likes","reposts","bookmarks"}]}
 *   date is the sheet's upstream collection time formatted Asia/Tokyo
 *   "yyyy/MM/dd HH:mm" (Date cells are formatted, existing strings pass
 *   through). Metric cells that are empty or 0 export as null.
 */

var FANSCOPE_EXPORT_MAX_ROWS = 3000;

function fanscopeExport_(e) {
  try {
    var body;
    try {
      body = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    } catch (parseErr) {
      return ok({ error: "invalid_request" });
    }
    if (!body || body.action !== "fanscope_export") return ok({ error: "invalid_request" });
    var expected = getToken_();
    if (!expected) return ok({ error: "export_not_configured" });
    if (typeof body.token !== "string" || body.token !== expected) return ok({ error: "invalid token" });

    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(DATA_SHEET);
    if (!sheet) return ok({ error: "data_sheet_missing" });
    var count = Math.max(0, sheet.getLastRow() - 1);
    if (count > FANSCOPE_EXPORT_MAX_ROWS) return ok({ error: "export_too_large" });
    // One bounded read: columns [No,date,url,text,media,video,image,
    // impressions,likes,reposts,bookmarks]. Only url/date/metrics leave.
    var values = count ? sheet.getRange(2, 1, count, 11).getValues() : [];
    var rows = [];
    for (var i = 0; i < values.length; i++) {
      var v = values[i];
      rows.push({
        url: String(v[2] || ""),
        date: fanscopeDate_(v[1]),
        impressions: fanscopeMetric_(v[7]),
        likes: fanscopeMetric_(v[8]),
        reposts: fanscopeMetric_(v[9]),
        bookmarks: fanscopeMetric_(v[10])
      });
    }
    return ok({ version: 1, source: "taku3jp/x-collect", rows: rows });
  } catch (err) {
    return ok({ error: "export_failed" });
  }
}

function fanscopeDate_(value) {
  if (Object.prototype.toString.call(value) === "[object Date]")
    return Utilities.formatDate(value, "Asia/Tokyo", "yyyy/MM/dd HH:mm");
  return String(value || "");
}

function fanscopeMetric_(value) {
  if (value === "" || value === null || value === undefined) return null;
  if (typeof value !== "number" && (typeof value !== "string" || !/^[0-9]+$/.test(value))) throw new Error("invalid_metric");
  var n = Number(value);
  if (!Number.isSafeInteger(n) || n < 0) throw new Error("invalid_metric");
  return n === 0 ? null : n;
}

// Insert before any existing statements in doGet(e):
// if (e && e.parameter && e.parameter.action === "fanscope_export_capability") return fanscopeExportCapability_();
function fanscopeExportCapability_() {
  return ok({ version: 1, source: "taku3jp/x-collect", capability: "fanscope_export" });
}
