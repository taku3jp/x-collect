/**
 * X収集 webhook for Google Sheets.
 * Bound to the spreadsheet. Tabs:
 *   「X収集テスト」 … collected rows (newest on top)
 *   「設定」        … B1=ON/OFF, B2=impression threshold, A5+=account IDs
 *
 * 初回は doGet?action=bootstrap でTOKEN自動生成+タブ自動作成される。
 */

const DATA_SHEET = "X収集テスト";
const CONFIG_SHEET = "設定";
const IMAGE_FOLDER_NAME = "X収集画像";
const IMAGE_COL = 6;        // F列 = 動画内容
const IMAGE_ROW_HEIGHT = 220;
const MAX_EXISTING_SCAN = 3000;

function getToken_() {
  return PropertiesService.getScriptProperties().getProperty("TOKEN") || "";
}

function ensureSheets_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  let cfg = ss.getSheetByName(CONFIG_SHEET);
  if (!cfg) cfg = ss.insertSheet(CONFIG_SHEET);
  if (!cfg.getRange("A1").getValue()) {
    cfg.getRange("A1").setValue("収集ON/OFF");
    cfg.getRange("B1").setValue("ON");
    cfg.getRange("A2").setValue("インプ閾値");
    cfg.getRange("B2").setValue(300000);
    cfg.getRange("A3").setValue("センシティブ含むもののみ");
    cfg.getRange("B3").setValue("ON");
    cfg.getRange("A4").setValue("対象アカウント（@なし・1行1件）");
    cfg.getRange("D4").setValue("対象キーワード/ドメイン（1行1件・空なら無フィルタ）");
  }

  let data = ss.getSheetByName(DATA_SHEET);
  if (!data) data = ss.insertSheet(DATA_SHEET, 0);
  if (!data.getRange("A1").getValue()) {
    data.getRange(1, 1, 1, 11).setValues([[
      "No.", "日付", "参考リンク", "ポスト文", "素材リンク",
      "動画内容", "参考画像", "インプ", "いいね", "リポスト", "保存数"
    ]]);
    data.setFrozenRows(1);
  }
  data.setColumnWidth(IMAGE_COL, 420);
}

function ok(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function checkToken(e) {
  const token = (e && e.parameter && e.parameter.token) ||
    (e && e.postData && JSON.parse(e.postData.contents || "{}").token);
  if (token !== getToken_()) throw new Error("invalid token");
}

function doGet(e) {
  try {
    const props = PropertiesService.getScriptProperties();
    const action = (e && e.parameter && e.parameter.action) || "";

    // 初回ブートストラップ: TOKEN未設定のときだけ生成して返す
    if (action === "bootstrap") {
      let t = props.getProperty("TOKEN");
      const created = !t;
      if (!t) {
        t = Utilities.getUuid().replace(/-/g, "");
        props.setProperty("TOKEN", t);
      } else if ((e.parameter || {}).token !== t) {
        return ok({ error: "already initialized" });
      }
      ensureSheets_();
      return ok({ token: t, bootstrapped: created });
    }

    checkToken(e);
    ensureSheets_();

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const cfg = ss.getSheetByName(CONFIG_SHEET);

    // 設定更新: ?action=setconfig&accounts=a,b,c&threshold=300000&enabled=ON
    if (action === "setconfig") {
      const p = e.parameter || {};
      if (p.threshold !== undefined && p.threshold !== "")
        cfg.getRange("B2").setValue(Number(p.threshold));
      if (p.enabled !== undefined && p.enabled !== "")
        cfg.getRange("B1").setValue(String(p.enabled).toUpperCase() === "OFF" ? "OFF" : "ON");
      if (p.sensitiveOnly !== undefined && p.sensitiveOnly !== "")
        cfg.getRange("B3").setValue(String(p.sensitiveOnly).toUpperCase() === "OFF" ? "OFF" : "ON");
      if (p.accounts !== undefined) {
        const last = Math.max(cfg.getLastRow(), 5);
        cfg.getRange(5, 1, last - 4, 1).clearContent();
        const list = String(p.accounts).split(",").map(s => s.trim().replace(/^@/, "")).filter(Boolean);
        list.forEach((a, i) => cfg.getRange(5 + i, 1).setValue(a));
      }
      if (p.keywords !== undefined) {
        const last = Math.max(cfg.getLastRow(), 5);
        cfg.getRange(5, 4, last - 4, 1).clearContent();
        const list = String(p.keywords).split(",").map(s => s.trim()).filter(Boolean);
        list.forEach((k, i) => cfg.getRange(5 + i, 4).setValue(k));
      }
      return ok({ saved: true });
    }

    const enabled = String(cfg.getRange("B1").getValue()).toUpperCase() !== "OFF";
    const threshold = Number(cfg.getRange("B2").getValue()) || 300000;
    const sensitiveOnly = String(cfg.getRange("B3").getValue()).toUpperCase() !== "OFF";
    const lastCfg = cfg.getLastRow();
    const accounts = lastCfg >= 5
      ? cfg.getRange(5, 1, lastCfg - 4, 1).getValues()
        .flat().map(String).map(s => s.trim().replace(/^@/, "")).filter(Boolean)
      : [];
    const keywords = lastCfg >= 5
      ? cfg.getRange(5, 4, lastCfg - 4, 1).getValues()
        .flat().map(String).map(s => s.trim()).filter(Boolean)
      : [];

    const sheet = ss.getSheetByName(DATA_SHEET);
    const last = Math.min(sheet.getLastRow(), MAX_EXISTING_SCAN + 1);
    const existingIds = [];
    if (last >= 2) {
      const urls = sheet.getRange(2, 3, last - 1, 1).getValues().flat();
      for (const u of urls) {
        const m = String(u).match(/\/status\/(\d+)/);
        if (m) existingIds.push(m[1]);
      }
    }
    return ok({ enabled, threshold, sensitiveOnly, accounts, keywords, existingIds });
  } catch (err) {
    return ok({ error: String(err) });
  }
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    checkToken(e);
    ensureSheets_();
    const body = JSON.parse(e.postData.contents);
    const r = body.row || {};
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = ss.getSheetByName(DATA_SHEET);

    // 既存No.の最大+1を採番
    const last = sheet.getLastRow();
    let maxNo = 0;
    if (last >= 2) {
      maxNo = Math.max.apply(null,
        sheet.getRange(2, 1, last - 1, 1).getValues().flat()
          .map(v => Number(v) || 0));
    }
    const no = maxNo + 1;

    sheet.insertRowBefore(2);
    sheet.setRowHeight(2, IMAGE_ROW_HEIGHT);
    sheet.getRange(2, 1, 1, 11).setValues([[
      no, r.date || "", r.url || "", r.text || "", r.media || "",
      "", "", r.impressions || 0, r.likes || 0, r.reposts || 0, r.bookmarks || 0
    ]]);

    if (body.imageBase64) {
      const blob = Utilities.newBlob(
        Utilities.base64Decode(body.imageBase64), "image/png", `tweet_${no}.png`);
      try {
        const file = getImageFolder_().createFile(blob);
        file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
        const cellImage = SpreadsheetApp.newCellImage()
          .setSourceUrl(`https://drive.google.com/thumbnail?id=${file.getId()}&sz=w1000`)
          .build();
        sheet.getRange(2, IMAGE_COL).setValue(cellImage);
      } catch (imgErr) {
        sheet.insertImage(blob, IMAGE_COL, 2);
      }
    }
    return ok({ saved: true, no });
  } catch (err) {
    return ok({ error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

function getImageFolder_() {
  const it = DriveApp.getFoldersByName(IMAGE_FOLDER_NAME);
  return it.hasNext() ? it.next() : DriveApp.createFolder(IMAGE_FOLDER_NAME);
}
