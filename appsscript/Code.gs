

var PARENT_FOLDER_ID = '';
var SHARED_SECRET = '';
var SPREADSHEET_ID = '';

var TABLE_COLUMNS = {
  Users: ['id', 'email', 'hashed_password', 'full_name', 'role', 'created_at'],
  Cases: ['id', 'owner_user_id', 'student_name', 'stream', 'status', 'drive_folder_id', 'created_at', 'eligibility_status', 'eligibility_reason', 'total_duration_weeks', 'visa_subclass', 'visa_length_of_stay_date', 'pte_valid_until_date', 'ovhc_relevant_date', 'afp_issue_date', 'document_validity_status', 'document_validity_reason', 'new_coe_start_date', 'lodgement_date_status', 'lodgement_date_reason', 'lodgement_date', 'lodgement_basis', 'duration_breakdown_json', 'document_validity_breakdown_json', 'lodgement_breakdown_json'],
  Courses: ['id', 'case_id', 'name', 'course_type', 'start_date', 'end_date', 'cricos_weeks', 'sort_order', 'cricos_code'],
  Documents: ['id', 'course_id', 'doc_type', 'file_name', 'drive_file_id', 'drive_view_link', 'uploaded_at', 'case_id', 's3_key', 'mime_type', 'drive_sync_status', 'synced_at', 'retry_count', 'last_error']
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
    else if (body.action === 'insertDocumentRecord') result = { document: insertDocumentRecord(body) };
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

// S3-primary pipeline: the file already went straight to S3 from the
// backend (fast, synchronous) -- this just records the Documents row with
// no Drive involvement at all. drive_sync_worker.py fills in drive_file_id/
// drive_view_link later, once it's actually synced. Same locked
// check-then-insert duplicate guard as uploadCourseDocument/
// uploadCaseDocument, just without the slow Drive upload in between the two
// checks.
function insertDocumentRecord(body) {
  var isCourseDoc = !!body.courseId;
  var key = isCourseDoc ? 'course_id' : 'case_id';
  var keyValue = isCourseDoc ? body.courseId : body.caseId;

  function checkDuplicate() {
    var existing = getRows('Documents').filter(function (d) {
      return String(d[key]) === String(keyValue) && d.doc_type === body.docType;
    });
    if (existing.length) throw new Error('DUPLICATE_DOCUMENT');
  }

  withLock(checkDuplicate);

  return withLock(function () {
    checkDuplicate();
    return appendRecord('Documents', {
      course_id: isCourseDoc ? String(body.courseId) : '',
      case_id: isCourseDoc ? '' : String(body.caseId),
      doc_type: body.docType,
      file_name: body.fileName,
      s3_key: body.s3Key,
      mime_type: body.mimeType,
      drive_file_id: '',
      drive_view_link: '',
      drive_sync_status: 'pending',
      synced_at: '',
      retry_count: 0,
      last_error: '',
      uploaded_at: body.uploadedAt
    });
  });
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
