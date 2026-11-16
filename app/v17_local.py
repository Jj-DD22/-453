import hmac
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import Blueprint, jsonify, render_template_string, request, send_file


VIDEO_EXTENSIONS = {".mp4", ".mov"}
STICKER_EXTENSIONS = {".png"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MASK_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
VIDEO_COUNTS = {1, 3, 4, 5}
RUNNING_STATUSES = {"queued", "processing"}


V17_PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>V17 旧版剪辑</title>
  <style>
    :root {
      color-scheme: dark;
      --bg:#080b10;
      --panel:#11161d;
      --panel-2:#171e27;
      --line:#27313d;
      --text:#f5f7fa;
      --muted:#9aa7b5;
      --green:#73dc8c;
      --cyan:#5fd6e7;
      --amber:#f0c56a;
      --red:#ff7f8f;
    }
    * { box-sizing:border-box; }
    [hidden] { display:none !important; }
    body {
      margin:0;
      min-height:100vh;
      background:var(--bg);
      color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    }
    header {
      position:sticky;
      top:0;
      z-index:5;
      min-height:62px;
      padding:0 24px;
      border-bottom:1px solid var(--line);
      background:rgba(8,11,16,.94);
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:16px;
    }
    .brand { display:flex; align-items:center; gap:10px; color:var(--text); text-decoration:none; }
    .brand-mark {
      width:34px;
      height:34px;
      border-radius:7px;
      display:grid;
      place-items:center;
      background:var(--green);
      color:#0a150d;
      font-weight:900;
    }
    .brand-copy strong { display:block; font-size:15px; }
    .brand-copy span { display:block; margin-top:2px; color:var(--muted); font-size:11px; }
    .header-actions { display:flex; align-items:center; gap:9px; }
    .local-state { color:var(--green); font-size:12px; }
    .button, button {
      min-height:38px;
      border:1px solid transparent;
      border-radius:6px;
      padding:8px 13px;
      background:var(--green);
      color:#09130c;
      font:inherit;
      font-weight:750;
      text-decoration:none;
      cursor:pointer;
      display:inline-flex;
      align-items:center;
      justify-content:center;
      gap:7px;
    }
    .button.secondary {
      border-color:var(--line);
      background:var(--panel-2);
      color:var(--text);
    }
    button:disabled { opacity:.48; cursor:not-allowed; }
    main {
      width:min(1180px,calc(100vw - 32px));
      margin:0 auto;
      padding:24px 0 34px;
      display:grid;
      grid-template-columns:minmax(0,1fr) 360px;
      gap:16px;
      align-items:start;
    }
    .panel {
      border:1px solid var(--line);
      border-radius:8px;
      background:var(--panel);
      overflow:hidden;
    }
    .panel-head { padding:17px 18px 13px; border-bottom:1px solid var(--line); }
    .panel-head h1, .panel-head h2 { margin:0; font-size:17px; }
    .panel-head p { margin:6px 0 0; color:var(--muted); font-size:12px; line-height:1.5; }
    .form-section { padding:17px 18px; border-bottom:1px solid var(--line); }
    .form-section:last-child { border-bottom:0; }
    .section-title { margin:0 0 12px; color:#d9e0e8; font-size:12px; font-weight:800; }
    label { display:block; color:var(--muted); font-size:12px; }
    input[type=password], select {
      width:100%;
      min-height:42px;
      margin-top:7px;
      border:1px solid var(--line);
      border-radius:6px;
      padding:8px 10px;
      background:#0b0f14;
      color:var(--text);
      font:inherit;
      outline:none;
    }
    input:focus, select:focus { border-color:var(--cyan); box-shadow:0 0 0 3px rgba(95,214,231,.1); }
    .count-options { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:7px; }
    .count-option { margin:0; cursor:pointer; }
    .count-option input { position:absolute; opacity:0; pointer-events:none; }
    .count-option span {
      min-height:40px;
      border:1px solid var(--line);
      border-radius:6px;
      background:#0b0f14;
      color:var(--muted);
      display:grid;
      place-items:center;
      font-weight:750;
    }
    .count-option input:checked + span {
      border-color:var(--green);
      background:rgba(115,220,140,.1);
      color:#dff9e5;
    }
    .upload-list { display:grid; gap:9px; }
    .upload-row {
      min-height:76px;
      margin:0;
      border:1px dashed #354251;
      border-radius:7px;
      padding:11px 12px;
      background:#0c1117;
      display:flex;
      align-items:center;
      gap:12px;
      cursor:pointer;
    }
    .upload-row:hover { border-color:var(--cyan); }
    .upload-row.has-files { border-style:solid; border-color:var(--green); background:rgba(115,220,140,.055); }
    .upload-icon {
      width:38px;
      height:38px;
      flex:0 0 auto;
      border:1px solid var(--line);
      border-radius:7px;
      background:var(--panel-2);
      color:var(--cyan);
      display:grid;
      place-items:center;
      font-size:20px;
    }
    .upload-copy { min-width:0; flex:1; }
    .upload-copy strong { display:block; color:var(--text); font-size:13px; }
    .upload-copy span {
      display:block;
      margin-top:4px;
      color:var(--muted);
      font-size:11px;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
    }
    .upload-row input { position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
    .submit-row { display:flex; align-items:center; gap:12px; }
    .submit-row button { flex:1; }
    .charge-note { color:var(--muted); font-size:11px; white-space:nowrap; }
    .status-panel { position:sticky; top:78px; }
    .status-body { padding:18px; }
    .status-line { display:flex; align-items:center; justify-content:space-between; gap:10px; }
    .status-line span { color:var(--muted); font-size:12px; }
    .status-line strong { color:var(--green); font-size:13px; }
    .progress {
      height:9px;
      margin:12px 0 14px;
      border-radius:999px;
      overflow:hidden;
      background:#080b10;
      border:1px solid #202832;
    }
    .progress i {
      display:block;
      width:0;
      height:100%;
      background:var(--green);
      transition:width .25s ease;
    }
    .message { min-height:40px; color:#d5dde6; font-size:12px; line-height:1.6; }
    .message.error { color:var(--red); }
    .message.warn { color:var(--amber); }
    .download { width:100%; margin-top:13px; background:var(--cyan); color:#071317; }
    pre {
      height:250px;
      margin:14px 0 0;
      border:1px solid var(--line);
      border-radius:6px;
      padding:10px;
      background:#080b10;
      color:#9fb0c0;
      font:11px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;
      white-space:pre-wrap;
      overflow:auto;
    }
    .notice {
      display:none;
      margin-bottom:12px;
      border:1px solid var(--line);
      border-radius:7px;
      padding:10px 12px;
      background:var(--panel);
      color:var(--text);
      font-size:12px;
    }
    .notice.show { display:block; }
    .notice.error { border-color:rgba(255,127,143,.45); color:#ffd1d7; }
    .notice.ok { border-color:rgba(115,220,140,.45); color:#dff9e5; }
    @media (max-width:900px) {
      header { padding:0 14px; }
      .local-state { display:none; }
      main { grid-template-columns:1fr; width:min(100% - 24px,720px); padding-top:14px; }
      .status-panel { position:static; }
    }
    @media (max-width:520px) {
      .brand-copy span { display:none; }
      .count-options { grid-template-columns:repeat(2,minmax(0,1fr)); }
      .charge-note { white-space:normal; }
    }
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/v17">
      <span class="brand-mark">17</span>
      <span class="brand-copy"><strong>V17 旧版剪辑</strong><span>内置本地引擎</span></span>
    </a>
    <div class="header-actions">
      <span class="local-state">本机运行 · 不连接 115</span>
      <a class="button secondary" href="/#launch">返回 V16</a>
    </div>
  </header>
  <main>
    <div>
      <div id="notice" class="notice" role="status"></div>
      <form id="v17-form" class="panel" action="/v17/run" method="post" enctype="multipart/form-data">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <div class="panel-head">
          <h1>旧版批量出片</h1>
          <p>核心素材完整拼接，追加优质素材，使用固定四角贴纸。</p>
        </div>
        <div class="form-section">
          <div class="section-title">计费与数量</div>
          <label>API Key
            <input id="v17-api-key" type="password" name="customer_key" required autocomplete="off" maxlength="512" placeholder="请输入客户 API Key">
          </label>
          <div style="height:12px"></div>
          <div class="count-options" aria-label="生成数量">
            {% for count in (1, 3, 4, 5) %}
            <label class="count-option">
              <input type="radio" name="video_count" value="{{ count }}" {% if count == 1 %}checked{% endif %}>
              <span>{{ count }} 条</span>
            </label>
            {% endfor %}
          </div>
        </div>
        <div class="form-section">
          <div class="section-title">素材</div>
          <div class="upload-list">
            <label class="upload-row">
              <span class="upload-icon">+</span>
              <span class="upload-copy"><strong>核心素材</strong><span data-empty="选择 MP4 / MOV，至少一个">选择 MP4 / MOV，至少一个</span></span>
              <input type="file" name="core_files" accept=".mp4,.mov" multiple required>
            </label>
            <label class="upload-row">
              <span class="upload-icon">+</span>
              <span class="upload-copy"><strong>优质素材池</strong><span data-empty="选择 MP4 / MOV">选择 MP4 / MOV</span></span>
              <input type="file" name="qual_files" accept=".mp4,.mov" multiple>
            </label>
            <label class="upload-row">
              <span class="upload-icon">+</span>
              <span class="upload-copy"><strong>贴纸素材池</strong><span data-empty="选择 PNG，自动放置四角">选择 PNG，自动放置四角</span></span>
              <input type="file" name="sticker_files" accept=".png" multiple>
            </label>
          </div>
        </div>
        <div class="form-section submit-row">
          <button id="v17-submit" type="submit">开始生成</button>
          <span class="charge-note">成功后按成品数量扣次</span>
        </div>
      </form>
    </div>
    <section class="panel status-panel">
      <div class="panel-head"><h2>任务状态</h2></div>
      <div class="status-body">
        <div class="status-line"><span id="v17-stage">等待提交</span><strong id="v17-progress-text">0%</strong></div>
        <div class="progress"><i id="v17-progress"></i></div>
        <div id="v17-message" class="message">选择素材后开始生成。</div>
        <a id="v17-download" class="button download" href="#" hidden>下载成品</a>
        <pre id="v17-log">暂无运行日志</pre>
      </div>
    </section>
  </main>
  <script>
    const form = document.getElementById('v17-form');
    const submitButton = document.getElementById('v17-submit');
    const notice = document.getElementById('notice');
    const stage = document.getElementById('v17-stage');
    const progressText = document.getElementById('v17-progress-text');
    const progressBar = document.getElementById('v17-progress');
    const message = document.getElementById('v17-message');
    const log = document.getElementById('v17-log');
    const download = document.getElementById('v17-download');
    const apiKeyInput = document.getElementById('v17-api-key');
    const apiKeyStorage = 'v17-local-customer-key';
    const taskStorage = 'v17-local-task-id';
    let pollTimer = null;

    try {
      const savedKey = localStorage.getItem(apiKeyStorage) || '';
      if (savedKey) apiKeyInput.value = savedKey;
    } catch (_) {}

    document.querySelectorAll('.upload-row input[type=file]').forEach((input) => {
      const row = input.closest('.upload-row');
      const copy = row.querySelector('.upload-copy span');
      const sync = () => {
        const files = [...input.files];
        row.classList.toggle('has-files', files.length > 0);
        copy.textContent = files.length
          ? (files.length === 1 ? files[0].name : `已选择 ${files.length} 个文件`)
          : copy.dataset.empty;
      };
      input.addEventListener('change', sync);
    });

    function showNotice(text, level) {
      notice.textContent = text || '';
      notice.className = `notice show ${level || ''}`;
      window.setTimeout(() => { notice.className = 'notice'; }, 5000);
    }

    function renderState(data) {
      const value = Math.max(0, Math.min(100, Number(data.progress) || 0));
      stage.textContent = data.stage || data.status || '等待提交';
      progressText.textContent = `${value}%`;
      progressBar.style.width = `${value}%`;
      message.textContent = data.warning
        ? `${data.message || ''} ${data.warning}`.trim()
        : (data.message || '');
      message.className = `message ${data.status === 'error' ? 'error' : data.warning ? 'warn' : ''}`;
      log.textContent = Array.isArray(data.log) && data.log.length ? data.log.join('\n') : '暂无运行日志';
      log.scrollTop = log.scrollHeight;
      const finished = data.status === 'done' || data.status === 'error';
      submitButton.disabled = !finished && ['queued', 'processing'].includes(data.status);
      submitButton.textContent = submitButton.disabled ? '正在生成' : '开始生成';
      if (data.download_ready && data.task_id) {
        download.href = `/v17/download?task_id=${encodeURIComponent(data.task_id)}`;
        download.textContent = data.file_type === 'zip' ? '下载批量成品包' : '下载成品视频';
        download.hidden = false;
      } else {
        download.hidden = true;
      }
    }

    async function pollTask(taskId) {
      if (pollTimer) window.clearTimeout(pollTimer);
      try {
        const response = await fetch(`/v17/status?task_id=${encodeURIComponent(taskId)}`, {cache:'no-store'});
        if (response.status === 404) {
          try { localStorage.removeItem(taskStorage); } catch (_) {}
          return;
        }
        if (!response.ok) throw new Error('任务状态读取失败');
        const data = await response.json();
        renderState(data);
        if (['queued', 'processing'].includes(data.status)) {
          pollTimer = window.setTimeout(() => pollTask(taskId), 1200);
        }
      } catch (_) {
        pollTimer = window.setTimeout(() => pollTask(taskId), 2500);
      }
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (submitButton.disabled) return;
      submitButton.disabled = true;
      submitButton.textContent = '正在提交';
      download.hidden = true;
      try {
        localStorage.setItem(apiKeyStorage, apiKeyInput.value.trim());
      } catch (_) {}
      try {
        const response = await fetch(form.action, {
          method:'POST',
          body:new FormData(form),
          headers:{'Accept':'application/json','X-Requested-With':'fetch'},
        });
        const data = await response.json().catch(() => ({message:'服务器返回内容无法读取。'}));
        if (!response.ok) throw new Error(data.message || '提交失败。');
        try { localStorage.setItem(taskStorage, data.task_id); } catch (_) {}
        showNotice(data.message || '任务已提交。', 'ok');
        renderState(data);
        pollTask(data.task_id);
      } catch (error) {
        submitButton.disabled = false;
        submitButton.textContent = '开始生成';
        showNotice(error.message || '提交失败，请重试。', 'error');
      }
    });

    try {
      const savedTask = localStorage.getItem(taskStorage);
      if (savedTask) pollTask(savedTask);
    } catch (_) {}
  </script>
</body>
</html>
"""

V17_ORIGINAL_PAGE = Path(__file__).with_name("v17_original.html")


def _safe_filename(filename):
    name = Path((filename or "").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    if not name:
        return f"素材_{uuid.uuid4().hex[:8]}"
    encoded = name.encode("utf-8", errors="ignore")
    if len(encoded) <= 180:
        return name
    suffix = Path(name).suffix
    stem = Path(name).stem
    while len((stem + suffix).encode("utf-8", errors="ignore")) > 180 and stem:
        stem = stem[:-1]
    return (stem or f"素材_{uuid.uuid4().hex[:8]}") + suffix


def _unique_path(folder, filename):
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    source = Path(filename)
    for index in range(2, 10000):
        candidate = folder / f"{source.stem}_{index}{source.suffix}"
        if not candidate.exists():
            return candidate
    raise OSError("同名素材过多，请整理后重试。")


def create_v17_blueprint(
    *,
    data_dir,
    code_dir,
    csrf_token,
    get_customer,
    debit_customer,
    report_videos,
    report_failure,
    report_erp,
):
    blueprint = Blueprint("v17_local", __name__)
    task_root = Path(data_dir) / ".v17_local_tasks"
    runner_path = Path(code_dir) / "run_v17_core.py"
    task_root.mkdir(parents=True, exist_ok=True)
    tasks = {}
    tasks_lock = threading.RLock()

    def public_task(task):
        output_path = Path(task.get("output_path") or "")
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "stage": task.get("stage", ""),
            "message": task.get("message", ""),
            "warning": task.get("warning", ""),
            "progress": task.get("progress", 0),
            "log": list(task.get("log", []))[-300:],
            "download_ready": task.get("status") == "done" and output_path.is_file(),
            "file_type": task.get("file_type", "mp4"),
        }

    def append_log(task_id, line):
        clean = str(line or "").strip()
        if not clean:
            return
        with tasks_lock:
            task = tasks.get(task_id)
            if not task:
                return
            task["log"].append(clean)
            task["log"] = task["log"][-300:]
        try:
            with (task_root / task_id / "V17任务日志.txt").open(
                "a", encoding="utf-8"
            ) as log_file:
                log_file.write(clean + "\n")
        except OSError:
            pass

    def cleanup_old_tasks():
        with tasks_lock:
            completed = sorted(
                (
                    task for task in tasks.values()
                    if task.get("status") not in RUNNING_STATUSES
                ),
                key=lambda item: item.get("created_at", 0),
                reverse=True,
            )
            stale = completed[3:]
            for task in stale:
                tasks.pop(task["task_id"], None)
        for task in stale:
            shutil.rmtree(task_root / task["task_id"], ignore_errors=True)

    def fail_task(task_id, detail):
        with tasks_lock:
            task = tasks.get(task_id)
            if not task:
                return
            log_tail = list(task.get("log", []))[-8:]
            visible_detail = "\n".join(log_tail).strip() or str(detail).strip()
            task.update({
                "status": "error",
                "stage": "制作失败",
                "message": visible_detail[-1800:] or "V17 制作失败。",
                "progress": min(99, int(task.get("progress", 0))),
                "finished_at": time.time(),
            })

    def run_task(task_id):
        with tasks_lock:
            task = tasks[task_id]
            task.update({
                "status": "processing",
                "stage": "正在准备 V17 引擎",
                "message": "正在检查素材并启动本地剪辑。",
                "progress": 6,
            })
            count = task["video_count"]
            customer = dict(task["customer"])
        task_dir = task_root / task_id
        outputs = []
        try:
            if not runner_path.is_file():
                raise FileNotFoundError("安装包缺少 V17 本地剪辑核心。")
            for index in range(1, count + 1):
                output_path = task_dir / f"成品_V17_第{index}条.mp4"
                with tasks_lock:
                    task = tasks[task_id]
                    task.update({
                        "stage": f"正在生成第 {index}/{count} 条",
                        "message": "核心素材和优质素材正在本机合成。",
                        "progress": 8 + int(((index - 1) / count) * 78),
                    })
                env = os.environ.copy()
                env["TASK_WORKSPACE"] = str(task_dir)
                env["BASE_DIR"] = str(task_dir)
                env["CUSTOM_OUT_FILE"] = str(output_path)
                process = subprocess.Popen(
                    [sys.executable, str(runner_path)],
                    cwd=str(task_dir),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if process.stdout:
                    for line in process.stdout:
                        append_log(task_id, line)
                returncode = process.wait()
                if returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
                    raise RuntimeError(f"第 {index} 条生成失败，退出码 {returncode}。")
                outputs.append(output_path)
                with tasks_lock:
                    tasks[task_id]["progress"] = 8 + int((index / count) * 78)

            if count == 1:
                final_output = outputs[0]
                file_type = "mp4"
            else:
                final_output = task_dir / f"批量成品_V17_{count}条.zip"
                with zipfile.ZipFile(final_output, "w", compression=zipfile.ZIP_STORED) as archive:
                    for output_path in outputs:
                        archive.write(output_path, output_path.name)
                file_type = "zip"

            warning_parts = []
            debit_confirmed = False
            try:
                debit_customer(
                    customer["id"],
                    amount=-count,
                    reason=f"local_v17_success_{count}_videos",
                )
                debit_confirmed = True
                append_log(task_id, f"客户“{customer['name']}”已成功扣费 {count} 次。")
            except Exception as exc:
                warning_parts.append(f"扣费结果待核对：{exc}")
                append_log(task_id, warning_parts[-1])

            try:
                report_videos(
                    customer["id"],
                    task_id,
                    [
                        {
                            "name": output_path.name,
                            "url": f"/v17/download?task_id={task_id}&file={index}",
                            "local_task_id": task_id,
                        }
                        for index, output_path in enumerate(outputs, start=1)
                    ],
                )
                append_log(task_id, "V17 成品记录已上报 35 后台。")
            except Exception as exc:
                prefix = "已扣费" if debit_confirmed else "成片已生成"
                warning_parts.append(f"{prefix}，但成品记录上报失败：{exc}")
                append_log(task_id, warning_parts[-1])

            try:
                report_erp(
                    customer["id"],
                    customer["name"],
                    task_id,
                    page_tab="快手自动化剪辑",
                )
                append_log(task_id, "V17 使用记录已上报 ERP。")
            except Exception as exc:
                append_log(task_id, f"ERP 使用记录跳过：{exc}")

            with tasks_lock:
                tasks[task_id].update({
                    "status": "done",
                    "stage": "制作完成",
                    "message": f"已生成 {count} 条 V17 成品。",
                    "warning": "；".join(warning_parts),
                    "progress": 100,
                    "output_path": str(final_output),
                    "output_files": [str(path) for path in outputs],
                    "file_type": file_type,
                    "finished_at": time.time(),
                })
            append_log(task_id, "V17 本地任务完成。")
        except Exception as exc:
            detail = str(exc)
            append_log(task_id, detail)
            fail_task(task_id, detail)
            try:
                report_failure(customer["id"], task_id, detail)
            except Exception:
                pass

    def save_uploads(task_dir, field_name, folder_name, extensions):
        saved = []
        folder = task_dir / folder_name
        for upload in request.files.getlist(field_name):
            if not upload or not upload.filename:
                continue
            filename = _safe_filename(upload.filename)
            if Path(filename).suffix.lower() not in extensions:
                raise ValueError(f"{folder_name}包含不支持的文件格式。")
            folder.mkdir(parents=True, exist_ok=True)
            destination = _unique_path(folder, filename)
            upload.save(destination)
            saved.append(destination)
        return saved

    @blueprint.get("/v17")
    def index():
        page = V17_ORIGINAL_PAGE.read_text(encoding="utf-8")
        return render_template_string(page, csrf_token=csrf_token)

    @blueprint.post("/v17/run")
    def start():
        submitted_token = request.form.get("csrf_token", "")
        if not hmac.compare_digest(submitted_token, csrf_token):
            return jsonify(status="error", message="页面校验已失效，请刷新后重试。"), 403
        with tasks_lock:
            if any(task.get("status") in RUNNING_STATUSES for task in tasks.values()):
                return jsonify(status="error", message="已有 V17 任务正在运行，请完成后再提交。"), 409
        try:
            count = int(request.form.get("video_count", "1"))
        except (TypeError, ValueError):
            count = 0
        if count not in VIDEO_COUNTS:
            return jsonify(status="error", message="生成数量无效。"), 400
        try:
            customer = get_customer(request.form.get("customer_key", ""))
        except Exception as exc:
            return jsonify(status="error", message=str(exc)), 401
        if int(customer.get("balance", 0)) < count:
            return jsonify(
                status="error",
                message=f"余额不足：计划生成 {count} 条，当前剩余 {customer.get('balance', 0)} 次。",
            ), 402

        task_id = uuid.uuid4().hex
        task_dir = task_root / task_id
        task_dir.mkdir(parents=True, exist_ok=False)
        try:
            core_files = save_uploads(task_dir, "core_files", "核心素材", VIDEO_EXTENSIONS)
            save_uploads(task_dir, "qual_files", "优质素材池", VIDEO_EXTENSIONS)
            save_uploads(task_dir, "dedup_files", "去重素材池", VIDEO_EXTENSIONS)
            save_uploads(task_dir, "mask_files", "蒙版素材", MASK_EXTENSIONS)
            save_uploads(task_dir, "sticker_files", "贴纸素材池", STICKER_EXTENSIONS)
            save_uploads(task_dir, "image_files", "图片素材池", IMAGE_EXTENSIONS)
            if not core_files:
                raise ValueError("请至少选择一个核心素材视频。")
        except (OSError, ValueError) as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            return jsonify(status="error", message=str(exc)), 400

        task = {
            "task_id": task_id,
            "status": "queued",
            "stage": "任务已提交",
            "message": "正在等待 V17 本地引擎。",
            "warning": "",
            "progress": 2,
            "log": [],
            "video_count": count,
            "customer": customer,
            "created_at": time.time(),
            "output_path": "",
            "output_files": [],
            "file_type": "mp4",
        }
        with tasks_lock:
            tasks[task_id] = task
        cleanup_old_tasks()
        thread = threading.Thread(target=run_task, args=(task_id,), daemon=True)
        try:
            thread.start()
        except (RuntimeError, OSError) as exc:
            fail_task(task_id, f"V17 任务启动失败：{exc}")
            return jsonify(public_task(tasks[task_id])), 500
        return jsonify(public_task(task)), 202

    @blueprint.get("/v17/status")
    def status():
        task_id = request.args.get("task_id", "")
        with tasks_lock:
            task = tasks.get(task_id)
            if not task:
                return jsonify(status="error", message="没有找到这个 V17 任务。"), 404
            payload = public_task(task)
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @blueprint.get("/v17/download")
    def download():
        task_id = request.args.get("task_id", "")
        with tasks_lock:
            task = tasks.get(task_id)
            if not task or task.get("status") != "done":
                return jsonify(status="error", message="成品尚未生成。"), 404
            requested_file = request.args.get("file", "")
            if requested_file:
                try:
                    index = int(requested_file) - 1
                    output_path = Path(task["output_files"][index])
                except (ValueError, IndexError):
                    return jsonify(status="error", message="成品序号无效。"), 404
            else:
                output_path = Path(task["output_path"])
        if not output_path.is_file() or task_root not in output_path.parents:
            return jsonify(status="error", message="成品文件不存在。"), 404
        return send_file(output_path, as_attachment=True, download_name=output_path.name)

    return blueprint
