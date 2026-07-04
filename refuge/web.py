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
  h2 { font-size: 1rem; margin-bottom: 10px; color: #b9c2cb; }
  .received { font-size: .88rem; color: #8a939c; max-height: 220px; overflow-y: auto; }
  .rrow { display: flex; align-items: center; gap: 10px; padding: 4px 0;
          border-bottom: 1px solid #202830; }
  .rrow .rname { flex: 1; overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; color: #4fc3f7; text-decoration: none; }
  .rrow .rname:hover { text-decoration: underline; }
  .rrow .rsize { white-space: nowrap; }
  .rdel { flex: none; width: 22px; height: 22px; line-height: 20px; padding: 0;
          border: 1px solid #3a4552; border-radius: 5px; background: none;
          color: #ef6a6a; cursor: pointer; font-size: .95rem; }
  .rdel:hover { background: #2a1418; border-color: #ef6a6a; }
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
    <h2>Files already rescued to this drive</h2>
    <div class="received" id="received">Loading&hellip;</div>
  </div>
</div>

<script>
(function () {
  var drop = document.getElementById('drop');
  var input = document.getElementById('fileInput');
  var list = document.getElementById('list');
  var totals = document.getElementById('totals');
  var queue = [];
  var uploading = false;
  var doneCount = 0, doneBytes = 0;

  function fmt(bytes) {
    var units = ['B','KB','MB','GB','TB'], i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return bytes.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
  }

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

    var form = new FormData();
    form.append('machine', document.getElementById('machine').value);
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

  function refreshReceived() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/files');
    xhr.onload = function () {
      if (xhr.status !== 200) return;
      var files = JSON.parse(xhr.responseText);
      var box = document.getElementById('received');
      if (files.length === 0) { box.textContent = 'Nothing yet.'; return; }
      box.innerHTML = files.map(function (f) {
        var href = '/download/' + f.name.split('/').map(encodeURIComponent).join('/');
        return '<div class="rrow" data-href="' + href + '" data-name="' + esc(f.name) + '">' +
          '<a class="rname" href="' + href + '" download>' + esc(f.name) + '</a>' +
          '<span class="rsize">' + fmt(f.size) + '</span>' +
          '<button class="rdel" title="Delete">&times;</button></div>';
      }).join('');
    };
    xhr.send();
  }

  document.getElementById('received').addEventListener('click', function (e) {
    var btn = e.target.closest('.rdel');
    if (!btn) return;
    var row = btn.closest('.rrow');
    var href = row.getAttribute('data-href');
    var name = row.getAttribute('data-name');
    if (!confirm('Delete "' + name + '"? This cannot be undone.')) return;
    btn.disabled = true;
    var xhr = new XMLHttpRequest();
    xhr.open('DELETE', href);
    xhr.onload = function () {
      if (xhr.status === 200) {
        refreshReceived();
      } else {
        alert('Could not delete file.');
        btn.disabled = false;
      }
    };
    xhr.onerror = function () {
      alert('Could not delete file.');
      btn.disabled = false;
    };
    xhr.send();
  });

  refreshReceived();
  setInterval(refreshReceived, 5000);
})();
</script>
</body>
</html>
"""
