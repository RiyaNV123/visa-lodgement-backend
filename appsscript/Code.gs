

var PARENT_FOLDER_ID = 'REPLACE_WITH_YOUR_DRIVE_FOLDER_ID';
var SPREADSHEET_ID = 'REPLACE_WITH_YOUR_GOOGLE_SHEET_ID';
var SHARED_SECRET = 'REPLACE_WITH_A_RANDOM_SECRET';

var TABLE_COLUMNS = {
  Users: ['id', 'email', 'hashed_password', 'full_name', 'role', 'created_at'],
  Cases: ['id', 'owner_user_id', 'student_name', 'stream', 'status', 'drive_folder_id', 'created_at', 'eligibility_status', 'eligibility_reason', 'total_duration_weeks', 'visa_subclass', 'visa_length_of_stay_date', 'pte_valid_until_date', 'ovhc_relevant_date', 'afp_issue_date', 'document_validity_status', 'document_validity_reason', 'new_coe_start_date', 'lodgement_date_status', 'lodgement_date_reason', 'lodgement_date', 'lodgement_basis', 'duration_breakdown_json', 'document_validity_breakdown_json', 'lodgement_breakdown_json'],
  Courses: ['id', 'case_id', 'name', 'course_type', 'start_date', 'end_date', 'cricos_weeks', 'sort_order', 'cricos_code'],
  Documents: ['id', 'course_id', 'doc_type', 'file_name', 'drive_file_id', 'drive_view_link', 'uploaded_at', 'case_id']
};

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    if (body.secret !== SHARED_SECRET) return jsonResponse({ ok: false, error: 'Invalid secret' });

    var result;
    if (body.action === 'ensureFolder') result = ensureFolder(body.caseName);
    else if (body.action === 'uploadFile') result = uploadFile(body.folderId, body.fileName, body.mimeType, body.dataBase64);
    else if (body.action === 'downloadFile') result = downloadFile(body.fileId);
    else if (body.action === 'ocrFile') result = ocrFile(body.fileId);
    else if (body.action === 'initializeDataStore') result = initializeDataStore();
    else if (body.action === 'dataGet') result = { rows: getRows(body.table) };
    else if (body.action === 'dataInsert') result = { row: insertRow(body.table, body.row) };
    else if (body.action === 'dataUpdate') result = { row: updateRow(body.table, body.id, body.row) };
    else if (body.action === 'dataDelete') result = { deleted: deleteRow(body.table, body.id) };
    else if (body.action === 'replaceCaseQualifications') result = replaceCaseQualifications(body.caseId, body.caseRow, body.courses);
    else if (body.action === 'createUser') result = createUser(body.row);
    else if (body.action === 'createCaseWithCourses') result = createCaseWithCourses(body.caseRow, body.courses);
    else if (body.action === 'uploadCourseDocument') result = uploadCourseDocument(body);
    else if (body.action === 'uploadCaseDocument') result = uploadCaseDocument(body);
    else return jsonResponse({ ok: false, error: 'Unknown action: ' + body.action });

    return jsonResponse(Object.assign({ ok: true }, result));
  } catch (err) {
    return jsonResponse({ ok: false, error: String(err) });
  }
}

function ensureFolder(caseName) {
  var parent = DriveApp.getFolderById(PARENT_FOLDER_ID);
  var existing = parent.getFoldersByName(caseName);
  var folder = existing.hasNext() ? existing.next() : parent.createFolder(caseName);
  return { folderId: folder.getId() };
}

function uploadFile(folderId, fileName, mimeType, dataBase64) {
  var folder = DriveApp.getFolderById(folderId);
  var bytes = Utilities.base64Decode(dataBase64);
  var blob = Utilities.newBlob(bytes, mimeType, fileName);
  var file = folder.createFile(blob);
  return { fileId: file.getId(), webViewLink: file.getUrl() };
}

// Re-reads an already-uploaded file's own content back out of Drive -- used
// to re-run field extraction against a document that's already saved,
// without asking the student to upload it again, whenever a regex/extraction
// fix (or any other cause) needs to be re-applied to data already on file.
function downloadFile(fileId) {
  var file = DriveApp.getFileById(fileId);
  var blob = file.getBlob();
  return { mimeType: blob.getContentType(), dataBase64: Utilities.base64Encode(blob.getBytes()) };
}

// Fallback for a scanned/image-based document that has no embedded text at
// all (extractPdfText on the backend comes back empty for these -- there's
// nothing there to search, not a pattern-matching gap). Uses Drive's own
// built-in OCR conversion (Advanced Drive Service -- requires the "Drive
// API" advanced service enabled in this Apps Script project's Services
// list; no GCP Console project or external AI model involved) to make a
// throwaway OCR'd Google Doc copy of the existing file, reads its text back
// out, then deletes the throwaway copy so it doesn't clutter Drive.
function ocrFile(fileId) {
  var ocrDoc = Drive.Files.copy({ title: 'ocr-temp-' + fileId, mimeType: 'application/vnd.google-apps.document' }, fileId, { ocr: true, ocrLanguage: 'en' });
  try {
    var text = DocumentApp.openById(ocrDoc.id).getBody().getText();
    return { text: text };
  } finally {
    Drive.Files.remove(ocrDoc.id);
  }
}

function spreadsheet() {
  if (SPREADSHEET_ID === 'REPLACE_WITH_YOUR_GOOGLE_SHEET_ID') throw new Error('Set SPREADSHEET_ID in Code.gs');
  return SpreadsheetApp.openById(SPREADSHEET_ID);
}

function initializeDataStore() {
  var book = spreadsheet();
  Object.keys(TABLE_COLUMNS).forEach(function (table) {
    var sheet = book.getSheetByName(table) || book.insertSheet(table);
    if (sheet.getLastRow() === 0) sheet.appendRow(TABLE_COLUMNS[table]);
  });
  return { initialized: true };
}

// Touches only the ONE sheet needed, instead of the old behavior of
// checking/creating all four tabs on every single call.
function tableSheet(table) {
  if (!TABLE_COLUMNS[table]) throw new Error('Unknown table: ' + table);
  var book = spreadsheet();
  var sheet = book.getSheetByName(table);
  if (!sheet) {
    sheet = book.insertSheet(table);
    sheet.appendRow(TABLE_COLUMNS[table]);
  } else if (sheet.getLastRow() === 0) {
    sheet.appendRow(TABLE_COLUMNS[table]);
  } else {
    // A column got appended to TABLE_COLUMNS after this sheet already had
    // rows in it (e.g. Documents.case_id) -- widen the existing header row
    // in place rather than requiring a manual migration. Purely cosmetic:
    // getRows() already tolerates a row being shorter than the column list.
    var currentWidth = sheet.getLastColumn();
    var expectedColumns = TABLE_COLUMNS[table];
    if (currentWidth < expectedColumns.length) {
      var missing = expectedColumns.slice(currentWidth);
      sheet.getRange(1, currentWidth + 1, 1, missing.length).setValues([missing]);
    }
  }
  return sheet;
}

function getRows(table) {
  var sheet = tableSheet(table);
  if (sheet.getLastRow() < 2) return [];
  var values = sheet.getRange(2, 1, sheet.getLastRow() - 1, TABLE_COLUMNS[table].length).getDisplayValues();
  return values.filter(function (row) { return row[0] !== ''; }).map(function (row) { return rowToObject(table, row); });
}

// Fast O(1) id generation backed by a locked counter in Script Properties,
// instead of re-scanning the whole table on every insert. Bootstraps once
// from existing data (one full read) the first time a table is touched
// after this change; every insert after that is a single property write.
function nextId(table) {
  var props = PropertiesService.getScriptProperties();
  var key = 'nextId_' + table;
  var current = props.getProperty(key);
  var next;
  if (current) {
    next = parseInt(current, 10) + 1;
  } else {
    var existingRows = getRows(table);
    next = existingRows.reduce(function (max, row) { return Math.max(max, Number(row.id) || 0); }, 0) + 1;
  }
  props.setProperty(key, String(next));
  return next;
}

function withLock(fn) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    return fn();
  } finally {
    lock.releaseLock();
  }
}

// --- Unlocked record-level helpers. Only call these from inside a
// withLock(...) block -- either the wrapper functions right below, or one
// of the batched operations further down that already hold the lock for
// their whole multi-step operation.

function appendRecord(table, input) {
  var sheet = tableSheet(table);
  var id = nextId(table);
  var record = Object.assign({}, input, { id: String(id) });
  sheet.appendRow(TABLE_COLUMNS[table].map(function (column) { return record[column] == null ? '' : String(record[column]); }));
  return record;
}

function updateRecord(table, id, updates) {
  var sheet = tableSheet(table);
  var existingRows = getRows(table);
  var index = existingRows.findIndex(function (row) { return String(row.id) === String(id); });
  if (index < 0) throw new Error(table + ' record not found: ' + id);
  var record = Object.assign({}, existingRows[index], updates, { id: String(id) });
  sheet.getRange(index + 2, 1, 1, TABLE_COLUMNS[table].length).setValues([TABLE_COLUMNS[table].map(function (column) { return record[column] == null ? '' : String(record[column]); })]);
  return record;
}

function deleteRecord(table, id) {
  var sheet = tableSheet(table);
  var existingRows = getRows(table);
  var index = existingRows.findIndex(function (row) { return String(row.id) === String(id); });
  if (index < 0) return false;
  sheet.deleteRow(index + 2);
  return true;
}

function insertRow(table, input) {
  return withLock(function () { return appendRecord(table, input); });
}

function updateRow(table, id, updates) {
  return withLock(function () { return updateRecord(table, id, updates); });
}

function deleteRow(table, id) {
  return withLock(function () { return deleteRecord(table, id); });
}

// --- Batched high-traffic operations. Each does everything server-side in
// ONE execution/lock instead of forcing the backend to make several
// sequential HTTP round-trips (each of which is its own multi-second call).

function createUser(row) {
  return withLock(function () {
    var existing = getRows('Users').filter(function (u) { return u.email.toLowerCase() === String(row.email).toLowerCase(); });
    if (existing.length) throw new Error('DUPLICATE_EMAIL');
    return { row: appendRecord('Users', row) };
  });
}

function createCaseWithCourses(caseRow, courses) {
  return withLock(function () {
    var existing = getRows('Cases').filter(function (c) { return String(c.owner_user_id) === String(caseRow.owner_user_id); });
    if (existing.length) throw new Error('DUPLICATE_CASE');

    var createdCase = appendRecord('Cases', caseRow);
    var createdCourses = courses.map(function (course) {
      return appendRecord('Courses', Object.assign({}, course, { case_id: createdCase.id }));
    });
    return { caseRow: createdCase, courses: createdCourses };
  });
}

function uploadCourseDocument(body) {
  var courseId = body.courseId;
  var docType = body.docType;

  function checkDuplicate() {
    var existing = getRows('Documents').filter(function (d) { return String(d.course_id) === String(courseId) && d.doc_type === docType; });
    if (existing.length) throw new Error('DUPLICATE_DOCUMENT');
  }

  // Fail fast before doing the slow Drive upload if it's obviously a repeat.
  withLock(checkDuplicate);

  var folderId = body.folderId;
  var folderCreated = false;
  if (!folderId) {
    folderId = ensureFolder(body.caseName).folderId;
    folderCreated = true;
  }
  var uploaded = uploadFile(folderId, body.fileName, body.mimeType, body.dataBase64);

  // Re-check + insert atomically (guards a race between two identical
  // uploads that both passed the fast pre-check above), and record the
  // folder id on the case if we just created it -- all under one lock.
  var document = withLock(function () {
    checkDuplicate();
    var doc = appendRecord('Documents', {
      course_id: String(courseId),
      doc_type: docType,
      file_name: body.fileName,
      drive_file_id: uploaded.fileId,
      drive_view_link: uploaded.webViewLink,
      uploaded_at: body.uploadedAt
    });
    if (folderCreated && body.caseId) {
      updateRecord('Cases', body.caseId, { drive_folder_id: folderId });
    }
    return doc;
  });

  return { document: document, folderId: folderId };
}

// Same shape as uploadCourseDocument, but for the case-level documents
// (current visa, AFP, PTE, OVHC) that aren't tied to any one qualification --
// keyed by case_id instead of course_id, with course_id left blank.
function uploadCaseDocument(body) {
  var caseId = body.caseId;
  var docType = body.docType;

  function checkDuplicate() {
    var existing = getRows('Documents').filter(function (d) { return String(d.case_id) === String(caseId) && d.doc_type === docType; });
    if (existing.length) throw new Error('DUPLICATE_DOCUMENT');
  }

  withLock(checkDuplicate);

  var folderId = body.folderId;
  var folderCreated = false;
  if (!folderId) {
    folderId = ensureFolder(body.caseName).folderId;
    folderCreated = true;
  }
  var uploaded = uploadFile(folderId, body.fileName, body.mimeType, body.dataBase64);

  var document = withLock(function () {
    checkDuplicate();
    var doc = appendRecord('Documents', {
      case_id: String(caseId),
      doc_type: docType,
      file_name: body.fileName,
      drive_file_id: uploaded.fileId,
      drive_view_link: uploaded.webViewLink,
      uploaded_at: body.uploadedAt
    });
    if (folderCreated) {
      updateRecord('Cases', caseId, { drive_folder_id: folderId });
    }
    return doc;
  });

  return { document: document, folderId: folderId };
}

function replaceCaseQualifications(caseId, caseRow, courses) {
  return withLock(function () {
    updateRecord('Cases', caseId, caseRow);
    var oldCourses = getRows('Courses').filter(function (course) { return String(course.case_id) === String(caseId); });
    var oldCourseIds = oldCourses.map(function (course) { return String(course.id); });
    getRows('Documents').filter(function (document) { return oldCourseIds.indexOf(String(document.course_id)) >= 0; }).forEach(function (document) { deleteRecord('Documents', document.id); });
    oldCourses.forEach(function (course) { deleteRecord('Courses', course.id); });
    var insertedCourses = courses.map(function (course) { return appendRecord('Courses', Object.assign({}, course, { case_id: String(caseId) })); });
    return { caseRow: Object.assign({}, caseRow, { id: String(caseId) }), courses: insertedCourses };
  });
}

function rowToObject(table, row) {
  var object = {};
  TABLE_COLUMNS[table].forEach(function (column, index) { object[column] = row[index]; });
  return object;
}

function jsonResponse(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
