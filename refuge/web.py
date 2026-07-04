"""The single-page upload site served to client machines. No external assets."""

PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Refuge - Emergency File Rescue</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #101418;
         color: #e6e9ec; min-height: 100vh; padding: 24px; }
  .wrap { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin-bottom: 4px; }
  h1 span { color: #4fc3f7; }
  .sub { color: #8a939c; margin-bottom: 20px; font-size: .95rem; }
  .card { background: #1a2027; border: 1px solid #2a323c; border-radius: 10px;
          padding: 18px; margin-bottom: 16px; }
  label { display: block; font-size: .85rem; color: #8a939c; margin-bottom: 6px; }
  input[type=text] { width: 100%; padding: 10px 12px; border-radius: 6px;
          border: 1px solid #2a323c; background: #10151a; color: #e6e9ec;
          font-size: 1rem; }
  #authcode { text-transform: uppercase; letter-spacing: 3px;
          font-family: 'Consolas', monospace; max-width: 200px; }
  .check { display: flex; align-items: center; gap: 8px; color: #b9c2cb;
          font-size: .9rem; margin-top: 12px; }
  .check input { width: auto; }
  #drop { border: 2px dashed #3a4552; border-radius: 10px; padding: 40px 20px;
          text-align: center; cursor: pointer; transition: all .15s; }
  #drop.hover { border-color: #4fc3f7; background: #16202a; }
  #drop .big { font-size: 1.15rem; margin-bottom: 6px; }
  #drop .small { color: #8a939c; font-size: .85rem; }
  #fileInput { display: none; }
  .item { display: flex; align-items: center; gap: 12px; padding: 10px 4px;
          border-bottom: 1px solid #232b34; font-size: .92rem; }
  .item:last-child { border-bottom: none; }
  .item .name { flex: 1; overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; }
  .item .size { color: #8a939c; white-space: nowrap; }
  .item .state { width: 92px; text-align: right; white-space: nowrap; }
  .bar { height: 5px; background: #232b34; border-radius: 3px; overflow: hidden;
          margin-top: 5px; }
  .bar i { display: block; height: 100%; width: 0; background: #4fc3f7;
          transition: width .2s; }
  .ok { color: #66d179; } .err { color: #ef6a6a; } .busy { color: #4fc3f7; }
  .totals { margin-top: 10px; font-size: .88rem; color: #8a939c; }
  h2 { font-size: 1rem; margin-bottom: 4px; color: #b9c2cb; }
  .hint { font-size: .8rem; color: #6c757d; margin-bottom: 10px; }
  .received { font-size: .88rem; color: #8a939c; max-height: 260px; overflow-y: auto; }
  .rrow { display: flex; align-items: center; gap: 10px; padding: 6px 0;
          border-bottom: 1px solid #202830; }
  .rrow .rname { flex: 1; overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; color: #4fc3f7; text-decoration: none; }
  .rrow .rname:hover { text-decoration: underline; }
  .rrow .rsize { color: #8a939c; white-space: nowrap; }
  button { background: #2a323c; color: #e6e9ec; border: 1px solid #3a4552;
          border-radius: 6px; padding: 5px 12px; font-size: .82rem; cursor: pointer; }
  button:hover { background: #37414d; }
  button.del { color: #ef8a8a; border-color: #5a3236; padding: 4px 10px; }
  button.del:hover { background: #3a2226; }
  #delmsg { font-size: .84rem; margin-top: 8px; min-height: 1.1em; }
</style>
</head>
<body>
<div class="wrap">
  <h1>&#128737;&#65039; <span>Refuge</span> Emergency File Rescue</h1>
  <div class="sub">Drop files below to copy them to the rescue drive. Large files are fine.</div>

  <div class="card">
    <label for="machine">Source machine / label (optional &mdash; files are grouped into a folder with this name)</label>
    <input type="text" id="machine" placeholder="e.g. ACME-DC01" maxlength="80">
  </div>

  <div class="card">
    <div id="drop">
      <div class="big">Drop files here or click to choose</div>
      <div class="small">Multiple files supported &middot; uploads start immediately</div>
      <input type="file" id="fileInput" multiple>
    </div>
    <div id="list"></div>
    <div class="totals" id="totals"></div>
  </div>

  <div class="card">
    <h2>Delete / overwrite authorization</h2>
    <div class="hint">Deleting or overwriting a file that already exists on the
      rescue drive requires the 6-character code shown <strong>on the rescue
      machine's screen</strong>. This protects saved files even if this
      computer is compromised. The code changes after each use.</div>
    <label for="authcode">Authorization code</label>
    <input type="text" id="authcode" maxlength="6" placeholder="ABC234" autocomplete="off">
    <label class="check"><input type="checkbox" id="overwrite">
      Overwrite files that already exist (instead of saving a numbered copy)</label>
    <div id="delmsg"></div>
  </div>

  <div class="card">
    <h2>Files already rescued to this drive</h2>
    <div class="hint">Click a name to download it back. Deleting requires the
      authorization code above.</div>
    <div class="received" id="received">Loading&hellip;</div>
  </div>
</div>

<script>
(function () {
  var drop = document.getElementById('drop');
  var input = document.getElementById('fileInput');
  var list = document.getElementById('list');
  var totals = document.getElementById('totals');
  var delmsg = document.getElementById('delmsg');
  var queue = [];
  var uploading = false;
  var doneCount = 0, doneBytes = 0;

  function fmt(bytes) {
    var units = ['B','KB','MB','GB','TB'], i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return bytes.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
  }

  function code() { return document.getElementById('authcode').value.trim().toUpperCase(); }

  drop.addEventListener('click', function () { input.click(); });
  input.addEventListener('change', function () { enqueue(input.files); input.value = ''; });
  ['dragenter','dragover'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('hover'); });
  });
  ['dragleave','drop'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('hover'); });
  });
  drop.addEventListener('drop', function (e) { enqueue(e.dataTransfer.files); });

  function enqueue(files) {
    for (var i = 0; i < files.length; i++) {
      var row = document.createElement('div');
      row.className = 'item';
      row.innerHTML = '<div class="name">' + esc(files[i].name) +
        '<div class="bar"><i></i></div></div>' +
        '<div class="size">' + fmt(files[i].size) + '</div>' +
        '<div class="state">queued</div>';
      list.appendChild(row);
      queue.push({ file: files[i], row: row });
    }
    pump();
  }

  function esc(s) {
    return s.replace(/[&<>"]/g, function (c) {
      return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c];
    });
  }

  function pump() {
    if (uploading || queue.length === 0) return;
    uploading = true;
    var job = queue.shift();
    var bar = job.row.querySelector('.bar i');
    var state = job.row.querySelector('.state');
    state.textContent = 'uploading';
    state.className = 'state busy';

    var overwrite = document.getElementById('overwrite').checked;
    var form = new FormData();
    form.append('machine', document.getElementById('machine').value);
    if (overwrite) { form.append('overwrite', '1'); form.append('code', code()); }
    form.append('file', job.file, job.file.name);

    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        var pct = Math.round(e.loaded / e.total * 100);
        bar.style.width = pct + '%';
        state.textContent = pct + '%';
      }
    };
    xhr.onload = function () {
      if (xhr.status === 200) {
        bar.style.width = '100%';
        state.textContent = 'saved ✓';
        state.className = 'state ok';
        doneCount++; doneBytes += job.file.size;
        if (overwrite) { document.getElementById('authcode').value = ''; }
        refreshReceived();
      } else {
        state.textContent = 'failed';
        state.className = 'state err';
      }
      finishOne();
    };
    xhr.onerror = function () {
      state.textContent = 'failed';
      state.className = 'state err';
      finishOne();
    };
    xhr.send(form);
  }

  function finishOne() {
    uploading = false;
    totals.textContent = doneCount + ' file(s) rescued, ' + fmt(doneBytes) + ' total this session.';
    pump();
  }

  window.refugeDelete = function (name) {
    if (!code()) { showDel('Enter the authorization code from the rescue machine first.', true); return; }
    if (!confirm('Delete "' + name + '" from the rescue drive? This cannot be undone.')) return;
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/delete');
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onload = function () {
      var msg = '';
      try { msg = JSON.parse(xhr.responseText).error || ''; } catch (e) {}
      if (xhr.status === 200) {
        showDel('Deleted. The authorization code has changed - read the new one on the rescue machine.', false);
        document.getElementById('authcode').value = '';
        refreshReceived();
      } else {
        showDel(msg || ('Delete failed (' + xhr.status + ').'), true);
      }
    };
    xhr.onerror = function () { showDel('Could not reach the server.', true); };
    xhr.send(JSON.stringify({ name: name, code: code() }));
  };

  function showDel(text, isErr) {
    delmsg.textContent = text;
    delmsg.className = isErr ? 'err' : 'ok';
  }

  function refreshReceived() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/files');
    xhr.onload = function () {
      if (xhr.status !== 200) return;
      var files = JSON.parse(xhr.responseText);
      var box = document.getElementById('received');
      if (files.length === 0) { box.textContent = 'Nothing yet.'; return; }
      box.innerHTML = '';
      files.forEach(function (f) {
        var href = '/download/' + f.name.split('/').map(encodeURIComponent).join('/');
        var row = document.createElement('div');
        row.className = 'rrow';
        row.innerHTML = '<a class="rname" href="' + href + '" download>' +
          esc(f.name) + '</a>' +
          '<span class="rsize">' + fmt(f.size) + '</span>' +
          '<button class="del">Delete</button>';
        row.querySelector('button').addEventListener('click', function () {
          window.refugeDelete(f.name);
        });
        box.appendChild(row);
      });
    };
    xhr.send();
  }
  refreshReceived();
  setInterval(refreshReceived, 5000);
})();
</script>
</body>
</html>
"""
