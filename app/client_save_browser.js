(function (root) {
  'use strict';
  async function savePlan(plan, directory, fetchFile, progress = () => {}) {
    if (!plan || !Array.isArray(plan.files) || !plan.files.length ||
        !/^快手成片_[a-f0-9]{8}_[a-f0-9]{12}$/.test(plan.folder_name)) {
      throw new Error('保存清单无效，未确认清理。');
    }
    const names = new Set();
    for (const file of plan.files) {
      if (!file.name || /[\\/:*?"<>|\x00-\x1f]/.test(file.name) || ['.', '..'].includes(file.name) ||
          !Number.isSafeInteger(file.size) || file.size <= 0 || names.has(file.name.toLowerCase()) ||
          !/^\/queue\/[a-f0-9]{32}\/client-save\/[a-f0-9]{32}\/file\/[0-9]+$/.test(file.url)) {
        throw new Error('保存文件名称、大小或地址无效。');
      }
      names.add(file.name.toLowerCase());
    }
    // Never overwrite an existing directory or a user's earlier local files.
    try {
      await directory.getDirectoryHandle(plan.folder_name);
      throw new Error('本次保存目录已存在，请重新发起保存以创建新的目录。');
    } catch (error) {
      if (error.name !== 'NotFoundError') throw error;
    }
    const target = await directory.getDirectoryHandle(plan.folder_name, { create: true });
    const saved = [];
    for (const file of plan.files) {
      progress(saved.length, plan.files.length);
      const response = await fetchFile(file.url, { credentials: 'same-origin', cache: 'no-store' });
      const length = response.headers.get('Content-Length');
      if (response.status !== 200 || response.redirected || !response.body ||
          (length !== null && Number(length) !== file.size)) {
        await response.body?.cancel();
        throw new Error('文件响应不完整或登录已失效，服务器副本未确认清理。');
      }
      let handle;
      let writer;
      let reader;
      let closed = false;
      try {
        handle = await target.getFileHandle(file.name, { create: true });
        writer = await handle.createWritable();
        reader = response.body.getReader();
        let written = 0;
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          written += value.byteLength;
          if (written > file.size) throw new Error('文件大小与保存清单不一致。');
          await writer.write(value);
        }
        if (written !== file.size) throw new Error('文件下载中断，服务器副本未确认清理。');
        await writer.close();
        closed = true;
        const local = await handle.getFile();
        if (local.size !== file.size) throw new Error('本机文件大小校验失败，未确认清理。');
        saved.push({ index: file.index, size: local.size });
      } catch (error) {
        try { if (reader) await reader.cancel(); else await response.body.cancel(); } catch (_) {}
        try { if (writer && !closed) await writer.abort(); } catch (_) {}
        throw error;
      } finally {
        try { reader?.releaseLock(); } catch (_) {}
      }
    }
    progress(saved.length, plan.files.length);
    return saved;
  }
  // Compatibility for HTML already loaded by running 2.3.7 tenant processes.
  // This script is fetched with revalidation on refresh; no renderer restart
  // or queue mutation is needed to remove the old controls.
  function removeAutomaticSaveControls(document) {
    const panel = document.getElementById('client-auto-save-panel');
    const label = document.getElementById('task-save-location-label');
    const legacyAutoSave = Boolean(panel || label?.textContent.includes('先选择本机文件夹'));
    panel?.remove();
    document.querySelectorAll('[data-auto-save-choose], [data-auto-save-status], #client-auto-save-stop, #client-auto-save-include-pending')
      .forEach(element => element.remove());
    if (legacyAutoSave && label) {
      // Old refresh handlers hold this ID for automatic-directory descriptions.
      // Rename before their inline script runs so they cannot restore that text.
      label.id = 'task-manual-save-location-label';
      label.textContent = '制作完成后，请在任务卡片点击保存到当前电脑；保存位置由浏览器下载设置决定。下载并核对完成后，可确认清理服务器副本。';
    }
  }

  // Native attachments must not navigate the editing workspace. Never fetch
  // an entire video/ZIP into JS memory and never infer a local-save receipt
  // from an iframe load event. Attachment completion belongs to the browser.
  let downloadSequence = 0;
  function downloadNotice(message) {
    if (typeof root.showNotice === 'function') {
      root.showNotice(message, 'error');
      return;
    }
    let notice = root.document.getElementById('native-download-notice');
    if (!notice) {
      notice = root.document.createElement('div');
      notice.id = 'native-download-notice';
      notice.setAttribute('role', 'alert');
      notice.style.cssText = 'position:fixed;top:16px;right:16px;z-index:99999;max-width:440px;padding:16px;background:#fff4e5;color:#7c2d12;border:1px solid #ffb869;border-radius:8px';
      root.document.body.appendChild(notice);
    }
    notice.textContent = message;
  }

  function createNativeDownloadFrame(onFailure = downloadNotice) {
    const frame = root.document.createElement('iframe');
    frame.name = `native-download-${Date.now()}-${++downloadSequence}`;
    frame.hidden = true;
    frame.setAttribute('aria-hidden', 'true');
    // A returned error page cannot run scripts or navigate the parent. Keep
    // gateway frame-ancestors/X-Frame-Options unchanged, including auth pages.
    frame.setAttribute('sandbox', 'allow-downloads allow-same-origin');
    let finished = false;
    let fallback = null;
    let offeredFallback = false;
    // Capture the native request after the caller has assigned its target.
    // A second, explicit click can open a normal top-level download tab when
    // the browser hides the iframe response; never retry a large ZIP silently.
    root.setTimeout(() => {
      const form = Array.from(root.document.forms).find(item => item.target === frame.name);
      const link = Array.from(root.document.querySelectorAll('a[href]')).find(item => item.target === frame.name);
      if (form && new URL(form.action, root.location.href).origin === root.location.origin) {
        const saved = form.cloneNode(true);
        fallback = () => {
          const retry = saved.cloneNode(true);
          retry.removeAttribute('id'); retry.target = '_blank'; retry.setAttribute('rel', 'noopener');
          root.document.body.appendChild(retry); retry.submit();
          root.setTimeout(() => retry.remove(), 1000);
        };
      } else if (link && new URL(link.href, root.location.href).origin === root.location.origin) {
        const href = link.href;
        fallback = () => {
          const retry = root.document.createElement('a');
          retry.href = href; retry.target = '_blank'; retry.rel = 'noopener'; retry.dataset.nativeTopLevel = 'true';
          root.document.body.appendChild(retry); retry.click(); retry.remove();
        };
      }
    });
    const offerFallback = () => {
      if (offeredFallback || !fallback) return;
      offeredFallback = true;
      let notice = root.document.getElementById('native-download-notice');
      if (!notice) {
        notice = root.document.createElement('div'); notice.id = 'native-download-notice';
        notice.setAttribute('role', 'status');
        notice.style.cssText = 'position:fixed;top:16px;right:16px;z-index:99999;max-width:440px;padding:16px;background:#fff4e5;color:#7c2d12;border:1px solid #ffb869;border-radius:8px';
        root.document.body.appendChild(notice);
      }
      notice.textContent = '浏览器未返回可确认的下载状态。请先查看下载列表；若没有文件，可在新标签页重试，查看具体结果。';
      const button = root.document.createElement('button');
      button.type = 'button'; button.textContent = '在新标签页重试下载';
      button.style.cssText = 'display:block;margin-top:10px;padding:8px 12px;cursor:pointer';
      button.addEventListener('click', () => { fallback(); button.disabled = true; });
      notice?.appendChild(button);
    };
    frame.addEventListener('load', () => {
      if (finished) return;
      let message = '';
      // An opaque frame or a load event is not evidence of a failed download.
      // Chromium/WebViews can hide attachment responses from contentDocument;
      // removing their frame here can cancel a valid pending transfer.
      try {
        const document = frame.contentDocument;
        if (document?.URL === 'about:blank') return; // Initial load, not success.
        if (document) {
          const url = new URL(document.URL);
          if (/^\/auth(?:\/|$)/.test(url.pathname)) {
            message = '下载时登录已失效，请退出后重新扫码登录，再重试下载。工作页没有被跳走。';
          } else {
            // Render only bounded plain text, never server-returned HTML.
            const raw = (document.body?.textContent || '').trim().slice(0, 2048);
            try {
              const error = JSON.parse(raw);
              if (typeof error.error === 'string') message = `下载未开始：${error.error.slice(0, 180)}。工作页已保留。`;
            } catch (_) { /* Non-JSON error or a frame blocked by gateway. */ }
          }
        }
      } catch (_) { /* Opaque attachment state: preserve the transfer. */ }
      if (!message) { offerFallback(); return; }
      finished = true;
      onFailure(message);
      // A non-attachment response has finished. Successful attachment frames
      // stay alive until page unload; do not cancel slow ZIP preparation with
      // a timer or reuse its target when a different download is clicked.
      frame.remove();
    });
    root.document.body.appendChild(frame);
    return frame;
  }

  function installDownloadNavigationGuard(document) {
    const pending = new Map();
    let legacyBatchTemplate;
    document.addEventListener('click', event => {
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const batchButton = event.target.closest?.('#queue-download-selected');
      if (batchButton && !batchButton.disabled && batchButton.dataset.downloadPending !== 'true') {
        // Running tenant processes retain their old inline HTML template. On
        // refresh they do load this external script, so hand that old handler
        // a fresh frame through its existing ID lookup. Keep earlier frames
        // and their target names alive: a slow ZIP may still be preparing.
        if (legacyBatchTemplate === undefined) {
          legacyBatchTemplate = Array.from(document.scripts).some(script =>
            !script.src && /getElementById\(\s*(['"])queue-batch-download-frame\1\s*\)/.test(script.textContent));
        }
        if (legacyBatchTemplate) {
          document.getElementById('queue-batch-download-frame')?.removeAttribute('id');
          const frame = createNativeDownloadFrame();
          frame.id = 'queue-batch-download-frame';
        }
      }
      const link = event.target.closest?.('a[href]');
      if (!link || link.dataset.nativeTopLevel === 'true') return;
      let url;
      try { url = new URL(link.href, root.location.href); } catch (_) { return; }
      if (url.origin !== root.location.origin ||
          !/^\/(?:download|download-compliance|v17\/download|queue\/(?:download|compliance)\/[a-f0-9]{32}|download-fission\/[a-f0-9]{32}\/(?:bundle|[1-5])|queue\/batch-download\/file)$/.test(url.pathname)) return;
      if (pending.has(url.href) || link.dataset.downloadPending === 'true') {
        event.preventDefault();
        event.stopImmediatePropagation();
        downloadNotice('下载请求已发起，请查看浏览器下载列表，不要连续重复点击。');
        return;
      }
      const frame = createNativeDownloadFrame(message => {
        pending.delete(url.href);
        downloadNotice(message);
      });
      pending.set(url.href, frame.name);
      root.setTimeout(() => {
        if (pending.get(url.href) === frame.name) pending.delete(url.href);
      }, 10000);
      link.target = frame.name;
      link.removeAttribute('download'); // Content-Disposition supplies filename.
      // Preserve the native trusted click, cookies and streaming/resume path.
      // The existing button handler still runs (debounce + status message).
    }, true);
  }

  // Only explicit user-click saving is exposed. No automatic controller,
  // folder authorization, background polling, download, or cleanup ACK starts.

  function taskFailureAdvice(text) {
    if (/仍高于\s*40%/.test(text)) return '这些视频画面还是太像，系统已尝试多种打乱组合。建议补充不同场景、角度或动作的打乱素材，或减少本次裂变条数，再重试。改文件名或重复上传同一视频不会增加差异。';
    if (/完整结束点|完整话术.*结束点/.test(text)) return '当前组合在目标时长附近找不到完整结尾。可以稍微调整时长倍率，或补充有完整结尾、长短不同的打乱素材，再重试；系统会保留完整话术和动作。';
    const gap = text.match(/需要\s*([\d.]+)\s*秒，转场后仅有\s*([\d.]+)\s*秒/);
    if (gap && Number(gap[1]) - Number(gap[2]) <= 0.2) return '这次卡在转场时长校验，不代表你缺少大量素材。服务器已更新时长计算方式，可重试；若仍失败，请把任务编号发给管理员。';
    if (/可用素材不足|打乱.*不足以到达/.test(text)) return '当前选中的可用画面不够达到目标时长。建议补充打乱素材，或降低时长倍率后重试。';
    if (/渲染失败/.test(text)) return '制作程序没有完成这次任务，仅凭这条提示还不能确定原因。请保留任务编号交给管理员排查，不必反复提交相同任务。';
    return '';
  }
  function installTaskFailureAdvice(doc) {
    let queued = false;
    function update() {
      queued = false;
      doc.querySelectorAll('.queue-task').forEach(card => {
        const detail = Array.from(card.children).find(el => el.tagName === 'P' && !el.className);
        const advice = card.classList.contains('failed') && detail ? taskFailureAdvice(detail.textContent || '') : '';
        let note = card.querySelector('.task-failure-advice');
        if (!advice) { if (note) note.remove(); return; }
        if (!note) { note = doc.createElement('p'); note.className = 'task-failure-advice'; note.style.cssText = 'padding:10px;border-radius:8px;background:#fff3cd;color:#594000;line-height:1.6'; detail.insertAdjacentElement('afterend', note); }
        if (note.textContent !== advice) note.textContent = advice;
      });
    }
    function schedule() { if (!queued) { queued = true; root.setTimeout(update, 80); } }
    new root.MutationObserver(schedule).observe(doc.body, { childList: true, subtree: true, characterData: true });
    update();
  }

  const api = { savePlan, createNativeDownloadFrame, taskFailureAdvice, installTaskFailureAdvice };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else {
    root.V17ClientSave = api;
    if (root.document) {
      if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", () => installTaskFailureAdvice(root.document), { once: true });
      else installTaskFailureAdvice(root.document);
      installDownloadNavigationGuard(root.document);
      removeAutomaticSaveControls(root.document);
      if (root.document.readyState === 'loading') {
        root.document.addEventListener('DOMContentLoaded', () => removeAutomaticSaveControls(root.document), { once: true });
      }
    }
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);


