/**
 * X収集 webhook for Google Sheets.
 * Bound to the spreadsheet. Tabs:
 *   「X収集テスト」 … collected rows (newest on top)
 *   「設定」        … B1=ON/OFF, B2=impression threshold, A5+=account IDs
 *
 * Deploy: デプロイ → 新しいデプロイ → ウェブアプリ
 *   実行ユーザー: 自分 / アクセス: 全員
 */

const TOKEN = "CHANGE_ME_TO_RANDOM_STRING"; // GitHub Secret APPS_SCRIPT_TOKEN と同じ値
const DATA_SHEET = "X収集テスト";
const CONFIG_SHEET = "設定";
const IMAGE_FOLDER_NAME = "X収集画像";
const IMAGE_COL = 6;        // F列 = 動画内容
const IMAGE_ROW_HEIGHT = 220;
const MAX_EXISTING_SCAN = 3000;

function ok(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function checkToken(e) {
  const token = (e && e.parameter && e.parameter.token) ||
    (e && e.postData && JSON.parse(e.postData.contents || "{}").token);
  if (token !== TOKEN) throw new Error("invalid token");
}

function doGet(e) {
  try {
    checkToken(e);
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const cfg = ss.getSheetByName(CONFIG_SHEET);
    const enabled = String(cfg.getRange("B1").getValue()).toUpperCase() !== "OFF";
    const threshold = Number(cfg.getRange("B2").getValue()) || 300000;
    const lastCfg = cfg.getLastRow();
    const accounts = lastCfg >= 5
      ? cfg.getRange(5, 1, lastCfg - 4, 1).getValues()
        .flat().map(String).map(s => s.trim().replace(/^@/, "")).filter(Boolean)
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
    return ok({ enabled, threshold, accounts, existingIds });
  } catch (err) {
    return ok({ error: String(err) });
  }
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    checkToken(e);
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
