    const DONGGE_TIMELINE_MODE_KEY = '8038-dongge-timeline-mode-v1';
    const DONGGE_BATCH_POLICY_KEY = '8038-dongge-batch-policy-v2';
    const DONGGE_CORE_ORDER_KEY = '8038-dongge-core-order-v1';
    const legacyCoreDisplayCache = loadLegacyCoreDisplayModes();
    const taskCoreDisplayCache = new Map();
    const legacyCoreRangeCache = loadLegacyCoreRanges();
    const taskCoreRangeCache = new Map();
    const materialCropModal = document.getElementById('material-crop-modal');
    const materialCropVideo = document.getElementById('material-crop-video');
    const materialCropSelection = document.getElementById('material-crop-selection');
    let activeMaterialCrop = null;
    let materialCropPlayEnd = null;
    let materialCropAnimationFrame = null;

    function loadDonggeTimelineMode(storageKey = DONGGE_TIMELINE_MODE_KEY) {
      try {
        return localStorage.getItem(storageKey) === 'native' ? 'native' : 'core_audio';
      } catch (_) {
        return 'core_audio';
      }
    }

    function saveDonggeTimelineMode(value, storageKey = DONGGE_TIMELINE_MODE_KEY) {
      const normalized = value === 'native' ? 'native' : 'core_audio';
      try {
        localStorage.setItem(storageKey, normalized);
      } catch (_) {}
      return normalized;
    }

    function normalizeDonggeBatchPolicy(value) {
      return value === 'per_core' || value === 'first_core_then_shuffle' ? value : 'rotate_cores';
    }
    window.normalizeDonggeBatchPolicy = normalizeDonggeBatchPolicy;

    function loadDonggeBatchPolicy(storageKey = DONGGE_BATCH_POLICY_KEY) {
      try {
        return normalizeDonggeBatchPolicy(localStorage.getItem(storageKey));
      } catch (_) {
        return 'rotate_cores';
      }
    }

    function saveDonggeBatchPolicy(value, storageKey = DONGGE_BATCH_POLICY_KEY) {
      const normalized = normalizeDonggeBatchPolicy(value);
      try {
        localStorage.setItem(storageKey, normalized);
      } catch (_) {}
      return normalized;
    }
    window.saveDonggeBatchPolicy = saveDonggeBatchPolicy;

    function saveLegacyCoreOrder(list, storageKey = DONGGE_CORE_ORDER_KEY) {
      if (!list) return;
      const names = [...list.children]
        .filter((el) => el.classList.contains('pool-file-row'))
        .map((row) => row.querySelector('input[name="files"]')?.value || '')
        .filter(Boolean);
      try {
        localStorage.setItem(storageKey, JSON.stringify(names));
      } catch (_) {}
    }

    function restoreLegacyCoreOrder(storageKey = DONGGE_CORE_ORDER_KEY) {
      let saved = null;
      try {
        saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
      } catch (_) {}
      if (!Array.isArray(saved) || !saved.length) return;
      const rank = new Map();
      saved.forEach((name, idx) => {
        if (typeof name === 'string' && name && !rank.has(name)) rank.set(name, idx);
      });
      document.querySelectorAll('.pool-cleanup[data-pool="核心素材"] .pool-file-list').forEach((list) => {
        const rows = [...list.children].filter((el) => el.classList.contains('pool-file-row'));
        if (rows.length < 2) return;
        rows.sort((a, b) => {
          const nameA = a.querySelector('input[name="files"]')?.value || '';
          const nameB = b.querySelector('input[name="files"]')?.value || '';
          const idxA = rank.has(nameA) ? rank.get(nameA) : Number.MAX_SAFE_INTEGER;
          const idxB = rank.has(nameB) ? rank.get(nameB) : Number.MAX_SAFE_INTEGER;
          return idxA - idxB;
        });
        rows.forEach((row) => list.appendChild(row));
      });
    }

    function syncDonggeMirrorControls() {
      for (const id of ['dongge-timeline-mode', 'dongge-cut', 'batch-picture-policy']) {
        const master = document.getElementById(id);
        if (!master) continue;
        document.querySelectorAll(`[data-sync-for="${id}"]`).forEach((mirror) => {
          if (mirror.value !== master.value) mirror.value = master.value;
        });
      }
    }
    window.syncDonggeMirrorControls = syncDonggeMirrorControls;

    function loadLegacyCoreRanges(storageKey = '8038-dongge-core-ranges-v1') {
      try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
        return new Map(Array.isArray(saved) ? saved.filter((entry) => Array.isArray(entry) && typeof entry[0] === 'string' && entry[1] && typeof entry[1] === 'object') : []);
      } catch (_) {
        return new Map();
      }
    }

    function shuffleRangeForFile(file, cache) {
      const name = String(file?.name || '');
      const duration = Number(file?.duration_seconds) || 0;
      const raw = cache.get(name) || { start: '0', end: '' };
      const startText = String(raw.start ?? '0').trim();
      const endText = String(raw.end ?? '').trim();
      const start = Number(startText);
      const end = endText === '' ? duration : Number(endText);
      const valid = startText !== '' && Number.isFinite(start) && Number.isFinite(end)
        && duration > 0 && start >= 0 && start < end && end <= duration;
      return {
        valid, start, end, duration: valid ? end - start : 0,
        payload: start === 0 && endText === '' ? null : [start, endText === '' ? null : end],
        error: valid ? '' : `“${name}”的范围无效：需满足 0 ≤ 起点 < 终点 ≤ ${duration.toFixed(3)} 秒。`,
      };
    }

    function createShuffleRangeControls(file, cache, onChange, locked = false, formId = '', options = {}) {
      const name = String(file?.name || '');
      const duration = Number(file?.duration_seconds) || 0;
      const raw = cache.get(name) || { start: '0', end: '' };
      const panel = document.createElement('div');
      panel.className = 'shuffle-range-controls';
      if (options.previewUrl) {
        const preview = document.createElement('button');
        preview.type = 'button'; preview.className = 'material-preview-button';
        preview.textContent = '预览／裁剪'; preview.disabled = locked;
        preview.addEventListener('click', (event) => {
          event.preventDefault(); event.stopPropagation();
          openMaterialCropPreview(file, cache, () => {
            const current = cache.get(name) || { start: '0', end: '' };
            startInput.value = String(current.start ?? '0');
            endInput.value = String(current.end ?? '');
            updateMessage(); onChange();
          }, options);
        });
        panel.appendChild(preview);
      }
      const makeInput = (labelText, value, placeholder) => {
        const label = document.createElement('label');
        label.textContent = labelText;
        const input = document.createElement('input');
        input.type = 'number'; input.min = '0'; input.max = String(duration); input.step = 'any';
        input.value = value; input.placeholder = placeholder; input.disabled = locked;
        if (formId) input.setAttribute('form', formId);
        input.setAttribute('aria-label', `${name}：${labelText}`);
        label.appendChild(input); panel.appendChild(label);
        return input;
      };
      const startInput = makeInput('开始（秒）', String(raw.start ?? '0'), '0');
      const endInput = makeInput('结束（秒）', String(raw.end ?? ''), `留空到结尾 ${duration.toFixed(2)}`);
      const reset = document.createElement('button');
      reset.type = 'button'; reset.textContent = '恢复全长'; reset.disabled = locked;
      panel.appendChild(reset);
      const message = document.createElement('div');
      message.className = 'shuffle-range-message'; message.setAttribute('aria-live', 'polite');
      panel.appendChild(message);
      const updateMessage = () => {
        const range = shuffleRangeForFile(file, cache);
        const audioDur = coreAudioDuration(file);
        const tailAudio = Math.max(0, audioDur - range.end);
        message.textContent = range.valid
          ? (options.keepAudio
            ? ((range.start > 0.005 || range.end < duration - 0.02)
              ? `开头原样保留 ${range.start.toFixed(2)}–${range.end.toFixed(2)} 秒（共 ${range.duration.toFixed(2)} 秒，画面＋同步原声不动） · 终点后剩余 ${tailAudio.toFixed(2)} 秒原声自动分配给后面的打乱素材`
              : `整条核心 ${duration.toFixed(2)} 秒（画面＋原声完全不动）放开头 · 打乱部分自动拉取后续核心素材原声接着播`)
            : `选取画面 ${range.duration.toFixed(2)} 秒 · 原片 ${duration.toFixed(2)} 秒 · 最终成片强制静音`)
          : range.error;
        message.classList.toggle('invalid', !range.valid);
        endInput.setCustomValidity(range.valid ? '' : range.error);
      };
      const update = () => {
        cache.set(name, { start: startInput.value, end: endInput.value });
        updateMessage(); onChange();
      };
      startInput.addEventListener('input', update);
      endInput.addEventListener('input', update);
      reset.addEventListener('click', () => {
        startInput.value = '0'; endInput.value = ''; cache.delete(name);
        updateMessage(); onChange();
      });
      updateMessage();
      return panel;
    }

    function stopMaterialCropRangePlayback() {
      if (materialCropAnimationFrame !== null) cancelAnimationFrame(materialCropAnimationFrame);
      materialCropAnimationFrame = null;
      materialCropPlayEnd = null;
    }

    function monitorMaterialCropRangePlayback() {
      materialCropAnimationFrame = null;
      if (!activeMaterialCrop || materialCropPlayEnd === null || !materialCropVideo) return;
      if (materialCropVideo.currentTime >= materialCropPlayEnd) {
        const end = materialCropPlayEnd;
        stopMaterialCropRangePlayback();
        materialCropVideo.pause();
        materialCropVideo.currentTime = end;
        return;
      }
      materialCropAnimationFrame = requestAnimationFrame(monitorMaterialCropRangePlayback);
    }

    function setMaterialCropModalCropButtonsVisible(visible) {
      const setStartBtn = document.getElementById('material-crop-set-start');
      const setEndBtn = document.getElementById('material-crop-set-end');
      const playBtn = document.getElementById('material-crop-play');
      if (setStartBtn) setStartBtn.hidden = !visible;
      if (setEndBtn) setEndBtn.hidden = !visible;
      if (playBtn) playBtn.hidden = !visible;
      if (playBtn?.parentElement?.classList.contains('material-crop-actions')) {
        playBtn.parentElement.hidden = !visible;
      }
    }

    function updateMaterialCropSelection(message = '') {
      if (!activeMaterialCrop || !materialCropSelection) return;
      if (activeMaterialCrop.previewOnly) {
        const dur = Number(activeMaterialCrop.file?.duration_seconds) || 0;
        materialCropSelection.textContent = message || `原片时长 ${dur.toFixed(2)} 秒 · 不作开头画面，完整原声 ${coreAudioDuration(activeMaterialCrop.file).toFixed(2)} 秒自动拉出接在前面核心音频之后配给打乱素材`;
        return;
      }
      const range = shuffleRangeForFile(activeMaterialCrop.file, activeMaterialCrop.cache);
      if (message || !range.valid) {
        materialCropSelection.textContent = message || range.error;
        return;
      }
      if (activeMaterialCrop.keepAudio) {
        materialCropSelection.textContent = `开头保留 ${range.start.toFixed(2)}–${range.end.toFixed(2)} 秒（${range.duration.toFixed(2)} 秒声画不动） · 剩余 ${Math.max(0, coreAudioDuration(activeMaterialCrop.file) - range.end).toFixed(2)} 秒原声自动分给打乱素材`;
        return;
      }
      if (activeMaterialCrop.videoOnly) {
        materialCropSelection.textContent = `开头原样保留 ${range.start.toFixed(2)}–${range.end.toFixed(2)} 秒画面（共 ${range.duration.toFixed(2)} 秒） · 不用此条原声，由其他核心音频供声`;
        return;
      }
      materialCropSelection.textContent = `画面起点 ${range.start.toFixed(2)} 秒 · 终点 ${range.end.toFixed(2)} 秒 · 选取 ${range.duration.toFixed(2)} 秒 · 成片强制静音`;
    }

    function closeMaterialCropPreview() {
      if (materialCropModal?.open) materialCropModal.close();
      stopMaterialCropRangePlayback();
      if (materialCropVideo) {
        materialCropVideo.pause();
        materialCropVideo.removeAttribute('src');
        materialCropVideo.load();
      }
      activeMaterialCrop = null;
    }

    function openMaterialCropPreview(file, cache, onChange, options) {
      if (!materialCropModal || !materialCropVideo) return;
      materialCropVideo.pause();
      stopMaterialCropRangePlayback();
      setMaterialCropModalCropButtonsVisible(true);
      activeMaterialCrop = {
        file,
        cache,
        onChange,
        keepAudio: Boolean(options.keepAudio),
        videoOnly: Boolean(options.videoOnly),
        previewOnly: false,
        scope: options.scope,
      };
      document.getElementById('material-crop-title').textContent = `${String(file.name || '素材')} · 预览／裁剪`;
      document.getElementById('material-crop-help').textContent = options.keepAudio
        ? '拖动进度条设为起点和终点：所选片段（画面＋对应同步原声）100% 原样不动放在成片开头；终点之后的画面不要，终点之后的剩余原声自动分配给后面的打乱素材，不够时自动拉取下一条核心原声接着播。'
        : options.videoOnly
        ? '拖动进度条，分别点「设为这条素材起点」和「设为这条素材终点」；所选片段画面原样保留在成片开头，不用此条原声，由其他核心音频供声。'
        : '拖动进度条，分别设为画面起点和终点；打乱素材预览默认静音，最终成片强制静音。';
      materialCropVideo.muted = !options.keepAudio;
      materialCropVideo.src = String(options.previewUrl);
      materialCropVideo.load();
      if (!materialCropModal.open) materialCropModal.showModal();
      updateMaterialCropSelection();
    }

    function openMaterialAudioPreview(file, options) {
      if (!materialCropModal || !materialCropVideo || !options?.previewUrl) return;
      materialCropVideo.pause();
      stopMaterialCropRangePlayback();
      setMaterialCropModalCropButtonsVisible(false);
      activeMaterialCrop = {
        file,
        cache: null,
        onChange: null,
        keepAudio: true,
        videoOnly: false,
        previewOnly: true,
        scope: options.scope,
      };
      document.getElementById('material-crop-title').textContent = `${String(file?.name || '核心素材')} · 试听原声`;
      document.getElementById('material-crop-help').textContent = '当前设为「仅完整音频」：不作开头画面，完整原声自动拉出接在前面核心音频之后配给打乱素材。如需使用并截取这条画面放开头，请将其使用方式改为「画面＋完整音频」或「仅裁剪画面」。';
      materialCropVideo.muted = false;
      materialCropVideo.src = String(options.previewUrl);
      materialCropVideo.load();
      if (!materialCropModal.open) materialCropModal.showModal();
      updateMaterialCropSelection();
    }

    function setMaterialCropBoundary(boundary) {
      if (!activeMaterialCrop || activeMaterialCrop.previewOnly || !materialCropVideo) return;
      const { file, cache, onChange } = activeMaterialCrop;
      const duration = Number(file.duration_seconds) || 0;
      const current = Math.min(duration, Math.max(0, Math.round(Number(materialCropVideo.currentTime) * 100) / 100));
      const raw = { ...(cache.get(String(file.name)) || { start: '0', end: '' }) };
      raw[boundary] = String(current);
      const candidate = new Map([[String(file.name), raw]]);
      const range = shuffleRangeForFile(file, candidate);
      if (!range.valid) {
        updateMaterialCropSelection(boundary === 'start' ? '起点需早于终点；请先移动进度条，或先设置较后的终点。' : '终点需晚于起点；请先移动进度条，或先设置较前的起点。');
        return;
      }
      materialCropVideo.pause(); stopMaterialCropRangePlayback();
      cache.set(String(file.name), raw);
      onChange(); updateMaterialCropSelection();
    }

    document.getElementById('material-crop-close')?.addEventListener('click', closeMaterialCropPreview);
    materialCropModal?.addEventListener('cancel', (event) => { event.preventDefault(); event.stopPropagation(); closeMaterialCropPreview(); });
    materialCropModal?.addEventListener('close', () => {
      if (activeMaterialCrop) closeMaterialCropPreview();
    });
    document.getElementById('material-crop-set-start')?.addEventListener('click', () => setMaterialCropBoundary('start'));
    document.getElementById('material-crop-set-end')?.addEventListener('click', () => setMaterialCropBoundary('end'));
    document.getElementById('material-crop-play')?.addEventListener('click', () => {
      if (!activeMaterialCrop || activeMaterialCrop.previewOnly) return;
      const range = shuffleRangeForFile(activeMaterialCrop.file, activeMaterialCrop.cache);
      if (!range.valid) { updateMaterialCropSelection(range.error); return; }
      stopMaterialCropRangePlayback();
      materialCropVideo.currentTime = range.start;
      materialCropPlayEnd = range.end;
      materialCropAnimationFrame = requestAnimationFrame(monitorMaterialCropRangePlayback);
      materialCropVideo.play().catch(() => {
        stopMaterialCropRangePlayback();
        updateMaterialCropSelection('视频暂时无法播放，请等待加载后重试。');
      });
    });
    materialCropVideo?.addEventListener('loadedmetadata', () => {
      if (!activeMaterialCrop) return;
      if (activeMaterialCrop.previewOnly) {
        materialCropVideo.currentTime = 0;
        return;
      }
      const range = shuffleRangeForFile(activeMaterialCrop.file, activeMaterialCrop.cache);
      if (range.valid) materialCropVideo.currentTime = range.start;
    });
    materialCropVideo?.addEventListener('timeupdate', () => {
      if (materialCropPlayEnd !== null && materialCropVideo.currentTime >= materialCropPlayEnd) {
        const end = materialCropPlayEnd;
        stopMaterialCropRangePlayback();
        materialCropVideo.pause();
        materialCropVideo.currentTime = end;
      }
    });
    materialCropVideo?.addEventListener('error', () => {
      if (activeMaterialCrop) updateMaterialCropSelection('预览加载失败，请确认素材仍在，或关闭后重新打开。');
    });


    function coreAudioDuration(file) {
      const raw = Object.prototype.hasOwnProperty.call(file || {}, 'audio_duration_seconds')
        ? file.audio_duration_seconds : file?.duration_seconds;
      const value = Number(raw);
      return Number.isFinite(value) && value > 0 ? value : 0;
    }

    function selectedMaterialMetadata(item) {
      return {
        name: item.value,
        duration_seconds: Number(item.dataset.duration) || 0,
        audio_duration_seconds: Number(item.dataset.audioDuration ?? item.dataset.duration) || 0,
      };
    }

    function normalizedCoreDisplayMode(value) {
      return value === 'audio_only' || value === 'video_only' ? value : 'video_audio';
    }

    function loadLegacyCoreDisplayModes(storageKey = '8038-dongge-core-modes-v1') {
      try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
        return new Map(Array.isArray(saved) ? saved.filter((entry) => Array.isArray(entry)
          && typeof entry[0] === 'string' && ['audio_only', 'video_audio', 'video_only'].includes(entry[1])) : []);
      } catch (_) {
        return new Map();
      }
    }

    function selectedShuffleFiles(pool = '打乱素材池') {
      const files = [];
      document.querySelectorAll('.pool-cleanup').forEach((form) => {
        if (form.dataset.pool !== pool) return;
        form.querySelectorAll('input[name="files"]:checked').forEach((item) => {
          files.push(selectedMaterialMetadata(item));
        });
      });
      return files;
    }

    function batchPicturePolicy(originalAudio, count, task = false) {
      if (!originalAudio) return null;
      const raw = normalizeDonggeBatchPolicy(
        document.getElementById((task ? 'task-' : '') + 'batch-picture-policy')?.value,
      );
      return raw === 'first_core_then_shuffle' && Number(count) <= 1 ? 'rotate_cores' : raw;
    }

    function effectiveCoreModeAt(index, rawMode, policy, count) {
      if (policy === 'first_core_then_shuffle' && Number(count) > 1) {
        return index === 0 ? 'video_audio' : 'audio_only';
      }
      return normalizedCoreDisplayMode(rawMode);
    }

    function createCoreOrderBar({ index, total, mode = 'video_audio', locked = false, onMakeFirst, onMoveUp, onMoveDown }) {
      const bar = document.createElement('div');
      bar.className = 'core-order-bar';
      const badge = document.createElement('span');
      const modeClass = mode === 'audio_only' ? ' is-audio-only' : mode === 'video_only' ? ' is-video-only' : index === 0 ? ' is-primary' : '';
      badge.className = `core-order-badge${modeClass}`;
      const modeDesc = mode === 'audio_only'
        ? '仅完整原声供打乱'
        : mode === 'video_only'
        ? '仅开头画面'
        : '开头声画不动＋余音配打乱';
      badge.textContent = `第 ${index + 1} 条核心 · ${modeDesc}`;
      bar.appendChild(badge);
      if (total > 1) {
        const actions = document.createElement('div');
        actions.className = 'core-order-actions';
        const makeBtn = (text, ariaLabel, boundaryDisabled, handler, extraClass = '') => {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = `core-order-btn${extraClass ? ` ${extraClass}` : ''}`;
          btn.textContent = text;
          btn.setAttribute('aria-label', ariaLabel);
          btn.dataset.boundaryDisabled = boundaryDisabled ? 'true' : 'false';
          btn.disabled = Boolean(locked || boundaryDisabled);
          btn.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            if (!btn.disabled && typeof handler === 'function') handler();
          });
          return btn;
        };
        if (index >= 1) {
          actions.appendChild(makeBtn('设为第1条', '设为第 1 条核心', false, onMakeFirst, 'make-first'));
        }
        actions.appendChild(makeBtn('↑ 上移', '上移这条核心', index === 0, onMoveUp));
        actions.appendChild(makeBtn('↓ 下移', '下移这条核心', index >= total - 1, onMoveDown));
        bar.appendChild(actions);
      }
      return bar;
    }
    window.createCoreOrderBar = createCoreOrderBar;

    function createCoreItemControls(file, {
      index = 0,
      modeCache,
      rangeCache,
      policy = 'rotate_cores',
      count = 1,
      locked = false,
      formId = '',
      scope = 'legacy',
      previewUrl = '',
      onRangeChange,
      onModeChange,
    } = {}) {
      const name = String(file?.name || '');
      const duration = Math.max(0, Number(file?.duration_seconds) || 0);
      const forcedFirstOnly = policy === 'first_core_then_shuffle' && Number(count) > 1;
      const effectiveMode = effectiveCoreModeAt(index, modeCache.get(name), policy, count);
      const usesPicture = effectiveMode === 'video_audio' || effectiveMode === 'video_only';
      const keepAudio = effectiveMode === 'video_audio';

      const panel = document.createElement('div');
      panel.className = 'shuffle-range-controls core-item-controls';

      const modeLabel = document.createElement('label');
      modeLabel.className = 'core-mode-field';
      modeLabel.textContent = '这条核心的使用方式';
      const select = document.createElement('select');
      select.dataset.coreDisplayName = name;
      select.setAttribute('aria-label', `${name}：这条核心的使用方式`);
      if (formId) select.setAttribute('form', formId);
      [
        ['video_audio', '画面＋完整音频（开头保留段声画不动＋剩余原声配打乱 · 裂变轮换）'],
        ['audio_only', '仅完整音频（不作开头画面 · 完整原声接续配打乱）'],
        ['video_only', '仅裁剪画面（开头原样保留画面 · 不用此条原声）'],
      ].forEach(([value, text]) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = text;
        select.appendChild(option);
      });
      select.value = effectiveMode;
      select.disabled = locked;
      modeLabel.appendChild(select);
      panel.appendChild(modeLabel);

      select.addEventListener('change', () => {
        const chosen = normalizedCoreDisplayMode(select.value);
        modeCache.set(name, chosen);
        let switchedPolicy = false;
        const policyEl = document.getElementById((scope === 'task' ? 'task-' : '') + 'batch-picture-policy');
        if (forcedFirstOnly || policyEl?.value === 'first_core_then_shuffle') {
          switchedPolicy = true;
          if (scope === 'task') {
            if (policyEl) policyEl.value = 'rotate_cores';
          } else {
            if (policyEl) policyEl.value = 'rotate_cores';
            saveDonggeBatchPolicy('rotate_cores');
            syncDonggeMirrorControls();
          }
        }
        if (typeof onModeChange === 'function') onModeChange(switchedPolicy);
      });

      if (usesPicture) {
        const raw = rangeCache.get(name) || { start: '0', end: '' };
        if (previewUrl) {
          const preview = document.createElement('button');
          preview.type = 'button';
          preview.className = 'material-preview-button';
          preview.textContent = '预览／裁剪';
          preview.disabled = locked;
          preview.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            openMaterialCropPreview(file, rangeCache, () => {
              const current = rangeCache.get(name) || { start: '0', end: '' };
              startInput.value = String(current.start ?? '0');
              endInput.value = String(current.end ?? '');
              updateMessage();
              if (typeof onRangeChange === 'function') onRangeChange();
            }, { keepAudio, videoOnly: effectiveMode === 'video_only', scope, previewUrl });
          });
          panel.appendChild(preview);
        }
        const makeInput = (labelText, value, placeholder) => {
          const label = document.createElement('label');
          label.textContent = labelText;
          const input = document.createElement('input');
          input.type = 'number';
          input.min = '0';
          input.max = String(duration);
          input.step = 'any';
          input.value = value;
          input.placeholder = placeholder;
          input.disabled = locked;
          if (formId) input.setAttribute('form', formId);
          input.setAttribute('aria-label', `${name}：${labelText}`);
          label.appendChild(input);
          panel.appendChild(label);
          return input;
        };
        const startInput = makeInput('开始（秒）', String(raw.start ?? '0'), '0');
        const endInput = makeInput('结束（秒）', String(raw.end ?? ''), `留空到结尾 ${duration.toFixed(2)}`);
        const reset = document.createElement('button');
        reset.type = 'button';
        reset.textContent = '恢复全长';
        reset.disabled = locked;
        panel.appendChild(reset);

        const message = document.createElement('div');
        message.className = 'shuffle-range-message';
        message.setAttribute('aria-live', 'polite');
        panel.appendChild(message);

        const updateMessage = () => {
          const range = shuffleRangeForFile(file, rangeCache);
          const audioDur = coreAudioDuration(file);
          const missingAudio = keepAudio && audioDur <= 0;
          const totalFrames = Math.max(1, Math.ceil(duration * 30 - 1e-3));
          const exactStart = range.valid && range.start > 0
            ? Math.min(totalFrames - 1, Math.max(0, Math.round(range.start * 30))) / 30
            : 0;
          const startBeyondAudio = keepAudio && range.valid && !missingAudio && (audioDur - exactStart <= 0);
          if (!range.valid) {
            message.textContent = range.error;
          } else if (missingAudio) {
            message.textContent = `核心素材「${name}」没有可用原声音轨，请改为仅裁剪画面或更换素材。`;
          } else if (startBeyondAudio) {
            message.textContent = `核心素材「${name}」所选起点已超出原声音轨长度，请调小开始秒数。`;
          } else if (keepAudio) {
            const tailAudio = Math.max(0, audioDur - range.end);
            if (range.start > 0.005 || range.end < duration - 0.02) {
              message.textContent = `开头原样保留 ${range.start.toFixed(2)}–${range.end.toFixed(2)} 秒（共 ${range.duration.toFixed(2)} 秒，画面＋同步原声不动） · 终点后剩余 ${tailAudio.toFixed(2)} 秒原声自动分配给后面的打乱素材`;
            } else {
              message.textContent = `整条核心 ${duration.toFixed(2)} 秒（画面＋原声完全不动）放开头 · 打乱部分自动拉取后续核心素材原声接着播`;
            }
          } else {
            message.textContent = `开头原样保留 ${range.start.toFixed(2)}–${range.end.toFixed(2)} 秒画面（共 ${range.duration.toFixed(2)} 秒） · 不用此条原声，由其他核心音频供声`;
          }
          message.classList.toggle('invalid', !range.valid || missingAudio || startBeyondAudio);
          endInput.setCustomValidity(!range.valid ? range.error : startBeyondAudio ? message.textContent : '');
        };
        const update = () => {
          rangeCache.set(name, { start: startInput.value, end: endInput.value });
          updateMessage();
          if (typeof onRangeChange === 'function') onRangeChange();
        };
        startInput.addEventListener('input', update);
        endInput.addEventListener('input', update);
        reset.addEventListener('click', () => {
          startInput.value = '0';
          endInput.value = '';
          rangeCache.delete(name);
          updateMessage();
          if (typeof onRangeChange === 'function') onRangeChange();
        });
        updateMessage();
      } else {
        if (previewUrl) {
          const preview = document.createElement('button');
          preview.type = 'button';
          preview.className = 'material-preview-button';
          preview.textContent = '试听原声';
          preview.disabled = locked;
          preview.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            openMaterialAudioPreview(file, { scope, previewUrl });
          });
          panel.appendChild(preview);
        }
        const message = document.createElement('div');
        message.className = 'shuffle-range-message';
        message.setAttribute('aria-live', 'polite');
        const audioDur = coreAudioDuration(file);
        if (audioDur <= 0) {
          message.textContent = `核心素材「${name}」没有可用原声音轨，请改为仅裁剪画面或更换素材。`;
          message.classList.add('invalid');
        } else {
          message.textContent = `不作开头画面 · 完整原声 ${coreAudioDuration(file).toFixed(2)} 秒自动拉出接在前面核心音频之后配给打乱素材`;
        }
        panel.appendChild(message);
      }
      return panel;
    }
    window.createCoreItemControls = createCoreItemControls;

    function reorderLegacyCoreRow(list, row, checkedRows, checkedIndex, action) {
      if (!list || !row) return;
      if (action === 'first' && checkedIndex > 0) {
        list.prepend(row);
      } else if (action === 'up' && checkedIndex > 0) {
        const prevCheckedRow = checkedRows[checkedIndex - 1];
        if (prevCheckedRow) list.insertBefore(row, prevCheckedRow);
      } else if (action === 'down' && checkedIndex >= 0 && checkedIndex < checkedRows.length - 1) {
        const nextCheckedRow = checkedRows[checkedIndex + 1];
        if (nextCheckedRow) nextCheckedRow.after(row);
      } else {
        return;
      }
      saveLegacyCoreOrder(list);
      if (typeof saveMaterialSelections === 'function') saveMaterialSelections();
      if (typeof refreshDurationRules === 'function') refreshDurationRules();
    }

    function renderLegacyCoreDisplayModes(original, policy) {
      const selectedCores = selectedShuffleFiles('核心素材');
      const count = Number(fissionCountInput?.value || 1);
      if (activeMaterialCrop?.scope === 'legacy') {
        const activeIndex = selectedCores.findIndex((f) => f.name === activeMaterialCrop.file.name);
        const activeMode = activeIndex >= 0
          ? effectiveCoreModeAt(activeIndex, legacyCoreDisplayCache.get(activeMaterialCrop.file.name), policy, count)
          : null;
        const shouldClose = !original
          || activeIndex < 0
          || (!activeMaterialCrop.previewOnly && activeMode === 'audio_only');
        if (shouldClose) closeMaterialCropPreview();
      }
      document.querySelectorAll('.pool-cleanup').forEach((form) => {
        if (form.dataset.pool !== '核心素材') return;
        const list = form.querySelector('.pool-file-list');
        const checkedItems = [...form.querySelectorAll('input[name="files"]:checked')];
        const checkedRows = checkedItems.map((el) => el.closest('.pool-file-row')).filter(Boolean);
        const checkedTotal = checkedItems.length;
        form.querySelectorAll('input[name="files"]').forEach((item) => {
          const row = item.closest('.pool-file-row');
          if (!row) return;
          const enabled = original && item.checked && !item.disabled && !runSubmissionPending;
          const checkedIndex = item.checked ? checkedItems.indexOf(item) : -1;
          const effectiveMode = checkedIndex >= 0
            ? effectiveCoreModeAt(checkedIndex, legacyCoreDisplayCache.get(item.value), policy, count)
            : 'video_audio';
          let orderBar = row.querySelector('.core-order-bar');
          if (!original || !item.checked) {
            if (orderBar) orderBar.remove();
            row.classList.remove('is-dongge-core-checked', 'is-primary-core');
          } else {
            row.classList.add('is-dongge-core-checked');
            row.classList.toggle('is-primary-core', checkedIndex === 0);
            const orderSig = `${checkedIndex}:${checkedTotal}:${effectiveMode}:${enabled ? 1 : 0}`;
            if (!orderBar || orderBar.dataset.orderSig !== orderSig) {
              const nextBar = createCoreOrderBar({
                index: checkedIndex,
                total: checkedTotal,
                mode: effectiveMode,
                locked: !enabled,
                onMakeFirst: () => reorderLegacyCoreRow(list, row, checkedRows, checkedIndex, 'first'),
                onMoveUp: () => reorderLegacyCoreRow(list, row, checkedRows, checkedIndex, 'up'),
                onMoveDown: () => reorderLegacyCoreRow(list, row, checkedRows, checkedIndex, 'down'),
              });
              nextBar.dataset.orderSig = orderSig;
              if (orderBar) orderBar.replaceWith(nextBar);
              else {
                const existingPanel = row.querySelector('.shuffle-range-controls');
                if (existingPanel) row.insertBefore(nextBar, existingPanel);
                else row.appendChild(nextBar);
              }
            }
          }

          const forcedTag = policy === 'first_core_then_shuffle' && count > 1 ? 'forced' : 'free';
          const kind = `${effectiveMode}:${forcedTag}`;
          let panel = row.querySelector('.shuffle-range-controls');
          if (panel && panel.dataset.coreControlKind !== kind) { panel.remove(); panel = null; }
          if (!panel && item.checked && original) {
            const file = selectedMaterialMetadata(item);
            const previewUrl = `/material-video/${encodeURIComponent('核心素材')}/${encodeURIComponent(item.value)}`;
            panel = createCoreItemControls(file, {
              index: checkedIndex,
              modeCache: legacyCoreDisplayCache,
              rangeCache: legacyCoreRangeCache,
              policy,
              count,
              locked: item.disabled || runSubmissionPending,
              formId: 'run-form',
              scope: 'legacy',
              previewUrl,
              onRangeChange: () => {
                try { localStorage.setItem('8038-dongge-core-ranges-v1', JSON.stringify([...legacyCoreRangeCache])); } catch (_) {}
                refreshDurationRules();
              },
              onModeChange: () => {
                try { localStorage.setItem('8038-dongge-core-modes-v1', JSON.stringify([...legacyCoreDisplayCache])); } catch (_) {}
                refreshDurationRules();
              },
            });
            panel.dataset.coreControlKind = kind;
            row.appendChild(panel);
          }
          if (panel) {
            panel.hidden = !original || !item.checked;
            panel.querySelectorAll('input, select, button').forEach((control) => { control.disabled = !enabled; });
          }
        });
      });
    }

    function shuffleDurationError(sourceSeconds, usable, used, holes, cut, variants) {
      if (variants > 1 && !sourceSeconds) return '批量制作需要勾选打乱素材来验收 40% 相似度；只制作核心时请选择单条。';
      if (!sourceSeconds || !cut.valid) return '';
      if (holes <= 0) return '打乱画面为 0 秒：当前成片已排满保留的核心画面。按当前设置导出纯核心成片时，请取消勾选打乱素材。';
      if (usable <= 0) return '打乱素材没有完整的一刀，请缩短刀长或补充素材。';
      if (variants > 1 && used < 0.5 - 1/30) return '批量制作时，每条至少需要 0.50 秒打乱画面才能验收相似度。';
      return '';
    }

    function cutSecondsSetting(input) {
      const value = Number(input?.value ?? 3);
      return { value, valid: Number.isFinite(value) && value >= 1 && value <= 10 && Math.abs(value * 10 - Math.round(value * 10)) < 0.00001 };
    }

    function fixedShuffleDuration(files, cut) {
      if (!Number.isFinite(cut) || cut < 1 || cut > 10) return 0;
      const cutFrames = Math.max(1, Math.round(cut * 30));
      return files.reduce((total, file) => {
        const frames = Math.max(0, Math.floor((Number(file.duration_seconds) || 0) * 30 + 0.001));
        return total + Math.floor(frames / cutFrames) * cutFrames / 30;
      }, 0);
    }


    function usesCoreAudioTimeline(scope) {
      const select = document.getElementById((scope === 'task' ? 'task-' : '') + 'dongge-timeline-mode');
      return currentEditionMode() === 'dongge' && select.value === 'core_audio';
    }

    function donggeRules(scope) {
      const task = scope === 'task';
      const files = pool => task ? taskPoolFiles(pool) : selectedShuffleFiles(pool);
      const cores = files('核心素材');
      const shuffles = files('打乱素材池');
      const duration = file => Number(file?.duration_seconds) || 0;
      const total = list => list.reduce((sum, file) => sum + duration(file), 0);
      const count = Math.max(1, Number((task ? taskFissionCount : fissionCountInput)?.value || 1));
      const multiplier = Number((task ? taskDurationMultiplier : durationMultiplierInput)?.value || 4.5);
      const prefix = task ? 'task-' : '';
      const cut = cutSecondsSetting(document.getElementById(prefix + 'dongge-cut'));
      const policy = batchPicturePolicy(true, count, task) || 'rotate_cores';
      const rangeCache = task ? taskCoreRangeCache : legacyCoreRangeCache;
      const modeCache = task ? taskCoreDisplayCache : legacyCoreDisplayCache;
      const stock = fixedShuffleDuration(shuffles, cut.value);
      const opening = duration(files('开头视频')[0]);

      const entries = cores.map((file, index) => {
        const name = String(file?.name || '');
        const mode = effectiveCoreModeAt(index, modeCache.get(name), policy, count);
        const usesPicture = mode === 'video_audio' || mode === 'video_only';
        const usesAudio = mode === 'video_audio' || mode === 'audio_only';
        const range = usesPicture ? shuffleRangeForFile(file, rangeCache) : null;
        return {
          index,
          file,
          name,
          mode,
          usesPicture,
          usesAudio,
          range,
          fullDuration: Math.max(0, duration(file)),
          audioDuration: coreAudioDuration(file),
        };
      });

      const pictureCores = entries.filter((item) => item.usesPicture);
      const audioCores = entries.filter((item) => item.usesAudio);
      const audioOnlyCount = entries.filter((item) => item.mode === 'audio_only').length;
      const videoOnlyCount = entries.filter((item) => item.mode === 'video_only').length;
      const invalidPic = pictureCores.find((item) => !item.range?.valid);
      const rangeError = invalidPic ? invalidPic.range.error : '';
      const silentAudioCore = audioCores.find((item) => item.audioDuration <= 0);
      const silentAudioError = silentAudioCore
        ? `核心素材「${silentAudioCore.name}」没有可用原声音轨，请改为仅裁剪画面或更换素材。`
        : '';
      const fullAudioSum = audioCores.reduce((sum, item) => sum + Math.max(0, item.audioDuration), 0);
      const coreRanges = entries.map((item) => (item.usesPicture && item.range?.valid ? [item.range.start, item.range.end] : null));
      const coreDisplayModes = entries.map((item) => item.mode);
      const primaryCoreDuration = Math.max(0, duration(cores[0]));

      const computeLeadAudio = (lead) => {
        const totalFrames = Math.max(1, Math.ceil(lead.fullDuration * 30 - 1e-3));
        const exactStart = lead.range.start > 0
          ? Math.min(totalFrames - 1, Math.max(0, Math.round(lead.range.start * 30))) / 30
          : 0;
        if (lead.mode === 'video_audio') {
          const leadUsable = coreAudioDuration(lead.file) - exactStart;
          return {
            exactStart,
            leadUsable,
            leadStartError: leadUsable <= 0
              ? `核心素材「${lead.file.name}」所选起点已超出原声音轨长度，请调小开始秒数。`
              : '',
            audioAvailable: fullAudioSum - exactStart,
          };
        }
        return {
          exactStart,
          leadUsable: null,
          leadStartError: '',
          audioAvailable: fullAudioSum,
        };
      };

      let activeVariants = [];
      let basis = 0;
      let minBasis = 0;
      let maxBasis = 0;
      let target = 0;
      let minTarget = 0;
      let maxTarget = 0;
      let coreBudget = 0;
      let used = 0;
      let minShuffle = 0;
      let maxShuffle = 0;
      let stockError = '';

      if (pictureCores.length > 0 && !rangeError) {
        if (policy === 'rotate_cores' || policy === 'first_core_then_shuffle') {
          const pool = policy === 'first_core_then_shuffle' ? [pictureCores[0]] : pictureCores;
          activeVariants = Array.from({ length: count }, (_, idx) => {
            const lead = pool[idx % pool.length];
            const b = lead.range.duration;
            const t = b * multiplier;
            const s = Math.max(0, t - b);
            return {
              variant: idx + 1,
              coreIndex: lead.index,
              name: lead.name,
              basis: b,
              target: t,
              shuffle: s,
              ...computeLeadAudio(lead),
            };
          });
          basis = activeVariants[0].basis;
          minBasis = Math.min(...activeVariants.map((x) => x.basis));
          maxBasis = Math.max(...activeVariants.map((x) => x.basis));
          target = activeVariants[0].target;
          minTarget = Math.min(...activeVariants.map((x) => x.target));
          maxTarget = Math.max(...activeVariants.map((x) => x.target));
          coreBudget = basis;
          minShuffle = Math.min(...activeVariants.map((x) => x.shuffle));
          maxShuffle = Math.max(...activeVariants.map((x) => x.shuffle));
          used = activeVariants[0].shuffle;

          if (maxShuffle > 1e-6 && !shuffles.length) {
            stockError = '目标时长大于核心裁剪画面，请勾选打乱素材填充后续画面。';
          } else if (count > 1 && !shuffles.length) {
            stockError = '批量制作需要勾选打乱素材来验收 40% 相似度；只制作核心时请选择单条。';
          } else if (count > 1 && minShuffle < 0.5 - 1 / 30) {
            stockError = '批量裂变每条至少需 0.50 秒打乱画面（请将倍率调至 > 1.0）。';
          } else if (maxShuffle > 1e-6 && cut.valid && stock <= 0) {
            stockError = '打乱素材没有完整的一刀，请缩短刀长或补充打乱素材。';
          } else if (maxShuffle <= 1e-6 && shuffles.length > 0) {
            stockError = '打乱画面为 0 秒：当前成片已排满保留的核心画面。按当前设置导出纯核心成片时，请取消勾选打乱素材。';
          }
        } else {
          // policy === 'per_core'
          const lead = pictureCores[0];
          const leadAudio = computeLeadAudio(lead);
          basis = lead.range.duration;
          minBasis = maxBasis = basis;
          target = basis * multiplier;
          minTarget = maxTarget = target;
          let position = 0;
          let keptTotal = 0;
          let audioOnlyDuration = 0;
          let firstPictureHoles = 0;
          entries.forEach((item, idx) => {
            const visible = Math.max(0, Math.min(item.fullDuration, target - position));
            position += item.fullDuration;
            if (item.usesPicture) {
              const kept = idx === lead.index
                ? Math.min(item.range.duration, target)
                : Math.min(item.range.duration, visible);
              keptTotal += kept;
              if (idx === lead.index) {
                firstPictureHoles = Math.max(0, visible - kept);
              }
            } else {
              audioOnlyDuration += visible;
            }
          });
          coreBudget = Math.min(target, keptTotal);
          const holes = Math.max(0, target - coreBudget);
          used = Math.min(firstPictureHoles, stock) + (stock > 0 ? Math.max(0, holes - firstPictureHoles) : 0);
          minShuffle = maxShuffle = used;
          activeVariants = Array.from({ length: count }, (_, idx) => ({
            variant: idx + 1,
            coreIndex: lead.index,
            name: lead.name,
            basis,
            target,
            shuffle: used,
            ...leadAudio,
          }));
          if (holes > 1e-6 && audioOnlyDuration > 1e-6 && !shuffles.length && count > 1) {
            stockError = '批量制作需要勾选打乱素材来验收 40% 相似度；只制作核心时请选择单条。';
          } else {
            stockError = shuffleDurationError(total(shuffles), stock, used, holes, cut, count);
          }
        }
      }

      const minVariantAudio = activeVariants.length
        ? Math.max(0, Math.min(...activeVariants.map((x) => x.audioAvailable)))
        : fullAudioSum;
      const maxVariantAudio = activeVariants.length
        ? Math.max(0, Math.max(...activeVariants.map((x) => x.audioAvailable)))
        : fullAudioSum;
      const audioAvailable = minVariantAudio;

      const startOverflowVariant = activeVariants.find((av) => av.leadStartError);
      const leadStartAudioError = startOverflowVariant ? startOverflowVariant.leadStartError : '';
      let worst = null;
      if (!leadStartAudioError) {
        for (const av of activeVariants) {
          const deficit = av.target - av.audioAvailable;
          if (deficit > 1e-6 && (!worst || deficit > (worst.target - worst.audioAvailable) + 1e-9)) {
            worst = av;
          }
        }
      }

      const audioPass = pictureCores.length > 0
        && audioCores.length > 0
        && !rangeError
        && !silentAudioError
        && !leadStartAudioError
        && !worst;
      const audioGateError = !pictureCores.length || !audioCores.length || rangeError || silentAudioError
        ? ''
        : leadStartAudioError
        || (worst
          ? `不能制作（核心原声不足）：第 ${worst.variant} 条成片目标需 ${worst.target.toFixed(2)} 秒，可用连续原声共 ${worst.audioAvailable.toFixed(2)} 秒（差 ${(worst.target - worst.audioAvailable).toFixed(2)} 秒）。请调低倍率、缩短核心截取时长或增加核心素材。`
          : '');

      const error = !cores.length
        ? '请先选择核心视频。'
        : !pictureCores.length
        ? '请至少将 1 条核心素材设为「画面＋完整音频」或「仅裁剪画面」作为主画面。'
        : !audioCores.length
        ? '请至少保留 1 条带音频的核心素材（「画面＋完整音频」或「仅完整音频」）提供原声。'
        : rangeError
        || silentAudioError
        || audioGateError
        || (!cut.valid ? '打乱刀长须为 1–10 秒，最多一位小数。' : '')
        || stockError;

      return {
        edition: 'dongge',
        editMode: 'original_audio_random',
        fissionCount: count,
        coreCount: cores.length,
        pictureCoreCount: pictureCores.length,
        audioCoreCount: audioCores.length,
        audioOnlyCount,
        videoOnlyCount,
        activeVariants,
        core: total(cores),
        coreSource: total(cores),
        primaryCoreDuration,
        coreRanges,
        coreDisplayModes,
        coreEffective: basis,
        minBasis,
        maxBasis,
        coreBudget,
        fullAudioSum,
        minVariantAudio,
        maxVariantAudio,
        audioAvailable,
        target,
        minTarget,
        maxTarget,
        opening,
        total: target + opening,
        shuffle: total(shuffles),
        shuffleBudget: used,
        minShuffle,
        maxShuffle,
        minimumShuffle: stock,
        sliceSec: cut.value,
        cutSeconds: cut.value,
        multiplier,
        batchPicturePolicy: policy,
        audioPass,
        audioGateError,
        targetPass: !rangeError && !silentAudioError && pictureCores.length > 0 && audioCores.length > 0 && basis > 0 && audioPass,
        shufflePass: cut.valid && !stockError,
        voiceError: error,
        valid: !error && maxTarget > 0,
      };
    }

    function donggeSummary(rule) {
      const openingNote = rule.opening ? `另加开头 ${rule.opening.toFixed(2)} 秒。` : '';
      const audioSpan = rule.maxVariantAudio !== undefined && Math.abs(rule.maxVariantAudio - rule.minVariantAudio) >= 0.01
        ? `${rule.minVariantAudio.toFixed(2)}–${rule.maxVariantAudio.toFixed(2)}`
        : rule.audioAvailable.toFixed(2);
      if (rule.batchPicturePolicy === 'rotate_cores' && rule.fissionCount > 1 && rule.pictureCoreCount > 1) {
        const basisSpan = Math.abs(rule.maxBasis - rule.minBasis) >= 0.01
          ? `${rule.minBasis.toFixed(2)}–${rule.maxBasis.toFixed(2)}`
          : rule.coreEffective.toFixed(2);
        const targetSpan = Math.abs(rule.maxTarget - rule.minTarget) >= 0.01
          ? `${rule.minTarget.toFixed(2)}–${rule.maxTarget.toFixed(2)}`
          : rule.target.toFixed(2);
        const shuffleSpan = Math.abs(rule.maxShuffle - rule.minShuffle) >= 0.01
          ? `${rule.minShuffle.toFixed(2)}–${rule.maxShuffle.toFixed(2)}`
          : rule.shuffleBudget.toFixed(2);
        return `共 ${rule.fissionCount} 条成片轮换 ${rule.pictureCoreCount} 条开头核心（开头保留 ${basisSpan} 秒声画不动 × ${rule.multiplier.toFixed(1)} = 主体 ${targetSpan} 秒，打乱 ${shuffleSpan} 秒）；开头剩余原声＋后续核心原声连续可用 ${audioSpan} 秒。${openingNote}`;
      }
      return `开头核心保留 ${rule.coreEffective.toFixed(2)} 秒（声画不动） × ${rule.multiplier.toFixed(1)} = 主体 ${rule.target.toFixed(2)} 秒；打乱画面 ${rule.shuffleBudget.toFixed(2)} 秒，开头剩余原声＋后续核心原声连续可用 ${audioSpan} 秒。${openingNote}`;
    }

    function renderDonggeRotationPreview(container, coreAudio, rule) {
      if (!container) return;
      if (!coreAudio || !rule) {
        container.hidden = true;
        container.replaceChildren();
        return;
      }
      container.hidden = false;
      container.replaceChildren();
      container.classList.remove('is-error', 'is-warn');

      const addLine = (text, cls = '') => {
        const p = document.createElement('p');
        p.className = `dongge-rotation-line${cls ? ` ${cls}` : ''}`;
        p.textContent = text;
        container.appendChild(p);
      };

      if (!rule.coreCount) {
        addLine('🎬 请先勾选或添加核心视频：所选核心片段（画面＋同步原声）原样不动放成片开头，终点后剩余原声自动配给打乱素材，不够时自动拉取后续核心原声接着播。');
        return;
      }

      const extraTags = [];
      if (rule.audioOnlyCount > 0) extraTags.push(`仅完整音频供打乱：${rule.audioOnlyCount} 条`);
      if (rule.videoOnlyCount > 0) extraTags.push(`仅开头画面：${rule.videoOnlyCount} 条`);
      const extraSuffix = extraTags.length ? ` ｜ ${extraTags.join(' ｜ ')}` : '';

      if (rule.pictureCoreCount > 0 && rule.activeVariants?.length) {
        if (rule.batchPicturePolicy === 'rotate_cores') {
          const planStr = rule.activeVariants
            .map((av) => `第${av.variant}条开头「${av.name}」(${av.basis.toFixed(2)}s)`)
            .join(' → ');
          addLine(`🎬 开头核心轮换（每条核心保留段声画原样不动）：${planStr}${extraSuffix}`);
        } else if (rule.batchPicturePolicy === 'first_core_then_shuffle') {
          const lead = rule.activeVariants[0];
          addLine(`🎬 固定开头核心（保留段声画原样不动）：全部 ${rule.fissionCount} 条成片统一以第1条「${lead.name}」(${lead.basis.toFixed(2)}s) 开头${rule.coreCount > 1 ? ` ｜ 其余 ${rule.coreCount - 1} 条完整原声接续配给打乱素材` : ''}`);
        } else {
          addLine(`🎬 单条成片内按序保留全部核心画面：共 ${rule.pictureCoreCount} 条画面核心${extraSuffix}`);
        }
      }

      const hardBlock = !rule.pictureCoreCount || !rule.audioCoreCount || Boolean(rule.audioGateError) || (!rule.audioPass && Boolean(rule.voiceError));
      if (hardBlock) {
        container.classList.add('is-error');
        addLine(`⛔ ${rule.audioGateError || rule.voiceError}`, 'is-error');
        return;
      }

      const minVariantAudio = Number.isFinite(rule.minVariantAudio) ? rule.minVariantAudio : rule.audioAvailable;
      addLine(
        `🔊 原声分配与接续：开头核心剩余原声先配给打乱素材，不够自动拉取后续核心原声接着播（可用连续原声 ${minVariantAudio.toFixed(2)} 秒 ≥ 成片最长 ${rule.maxTarget.toFixed(2)} 秒）`,
        'is-ok',
      );
      if (rule.voiceError) {
        container.classList.add('is-warn');
        addLine(`⚠️ ${rule.voiceError}`, 'is-warn');
      }
    }

    function refreshDonggeEditors(scope, rule) {
      const prefix = scope === 'task' ? 'task-' : '';
      const policyInput = document.getElementById(prefix + 'batch-picture-policy');
      const settingsBox = policyInput?.closest('.dongge-settings');
      const coreAudio = usesCoreAudioTimeline(scope);
      document.getElementById(prefix + 'dongge-cut').closest('label').hidden = !coreAudio;
      if (settingsBox) {
        const effectsScope = settingsBox.querySelector('.dongge-effects-scope');
        if (effectsScope) effectsScope.hidden = !coreAudio;
        renderDonggeRotationPreview(settingsBox.querySelector('[data-dongge-rotation-preview]'), coreAudio, rule);
      }
      if (policyInput) policyInput.closest('label').hidden = !coreAudio;
      if (scope !== 'task') {
        syncDonggeMirrorControls();
        document.querySelectorAll('.dongge-settings.in-pools').forEach((bar) => {
          const cutMirror = bar.querySelector('[data-sync-for="dongge-cut"]')?.closest('label');
          const policyMirror = bar.querySelector('[data-sync-for="batch-picture-policy"]')?.closest('label');
          const effectsScope = bar.querySelector('.dongge-effects-scope');
          if (cutMirror) cutMirror.hidden = !coreAudio;
          if (policyMirror) policyMirror.hidden = !coreAudio;
          if (effectsScope) effectsScope.hidden = !coreAudio;
          renderDonggeRotationPreview(bar.querySelector('[data-dongge-rotation-preview]'), coreAudio, rule);
        });
      }
      window.bottomOverlays?.refresh(scope, rule);
      window.manualShuffleMasks?.refresh(scope);
    }

    function collectDongge(scope, data, rule) {
      const coreAudio = usesCoreAudioTimeline(scope);
      data.set('dongge_timeline_mode', coreAudio ? 'core_audio' : 'native');
      if (scope === 'task') {
        data.set('core_order', JSON.stringify(taskPoolFiles('核心素材').map((f) => String(f.name || ''))));
      }
      if (coreAudio) {
        data.set('edit_mode', 'original_audio_random');
        data.set('cut_seconds', String(rule.cutSeconds));
        data.set('duration_multiplier', String(rule.multiplier));
        data.set('batch_picture_policy', rule.batchPicturePolicy || 'rotate_cores');
        data.set('core_ranges', JSON.stringify(rule.coreRanges));
        data.set('core_display_modes', JSON.stringify(rule.coreDisplayModes));
        data.delete('price_filter');
      }
      data.delete('subtitle_masks');
      window.bottomOverlays.collect(scope, data);
      window.manualShuffleMasks.collect(scope, data);
    }

    {
      const savedTimelineMode = loadDonggeTimelineMode();
      const savedBatchPolicy = loadDonggeBatchPolicy();
      for (const prefix of ['', 'task-']) {
        const tm = document.getElementById(prefix + 'dongge-timeline-mode');
        const pol = document.getElementById(prefix + 'batch-picture-policy');
        if (tm) tm.value = savedTimelineMode;
        if (pol) pol.value = savedBatchPolicy;
      }
      restoreLegacyCoreOrder();
      syncDonggeMirrorControls();
    }

    for (const prefix of ['', 'task-']) {
      const tm = document.getElementById(prefix + 'dongge-timeline-mode');
      const cut = document.getElementById(prefix + 'dongge-cut');
      const policy = document.getElementById(prefix + 'batch-picture-policy');
      tm?.addEventListener('change', () => {
        if (!prefix) {
          saveDonggeTimelineMode(tm.value);
          syncDonggeMirrorControls();
        }
        setEditionMode('dongge', { persist: false });
      });
      const refresh = () => prefix ? renderTaskDraft({ updateStatus: true }) : refreshDurationRules();
      cut?.addEventListener('input', () => {
        if (!prefix) syncDonggeMirrorControls();
        refresh();
      });
      policy?.addEventListener('change', () => {
        if (!prefix) {
          saveDonggeBatchPolicy(policy.value);
          syncDonggeMirrorControls();
        }
        refresh();
      });
    }

    document.querySelectorAll('[data-sync-for="dongge-timeline-mode"]').forEach((mirror) => {
      mirror.addEventListener('change', () => {
        const master = document.getElementById('dongge-timeline-mode');
        const normalized = saveDonggeTimelineMode(mirror.value);
        if (master) master.value = normalized;
        syncDonggeMirrorControls();
        setEditionMode('dongge', { persist: false });
      });
    });
    document.querySelectorAll('[data-sync-for="dongge-cut"]').forEach((mirror) => {
      mirror.addEventListener('input', () => {
        const master = document.getElementById('dongge-cut');
        if (master) master.value = mirror.value;
        syncDonggeMirrorControls();
        refreshDurationRules();
      });
    });
    document.querySelectorAll('[data-sync-for="batch-picture-policy"]').forEach((mirror) => {
      mirror.addEventListener('change', () => {
        const master = document.getElementById('batch-picture-policy');
        const normalized = saveDonggeBatchPolicy(mirror.value);
        if (master) master.value = normalized;
        syncDonggeMirrorControls();
        refreshDurationRules();
      });
    });

    if (typeof currentEditionMode === 'function' && currentEditionMode() === 'dongge' && typeof setEditionMode === 'function') {
      setEditionMode('dongge', { persist: false, refreshUI: true });
    }

    function appendShuffleScores(tables, status, scores, prefix) {
      tables.querySelector('[data-shuffle-scores]')?.remove();
      const dual = scores.pairs.every(pair => Number.isFinite(pair.source_similarity_percent) && Number.isFinite(pair.rendered_similarity_percent));
      const limit = scores.threshold_percent;
      if (dual) {
        const maxSource = Math.max(...scores.pairs.map(pair => pair.source_similarity_percent));
        const maxRendered = Math.max(...scores.pairs.map(pair => pair.rendered_similarity_percent));
        status.textContent = `${prefix} · ${scores.pairs.length} 组同批对比。打乱原画最高 ${maxSource.toFixed(2)}%，成片打乱画面最高 ${maxRendered.toFixed(2)}%，两项上限均为 ${limit}%。`;
        const table = document.createElement('table');
        table.setAttribute('aria-label', '同批各条打乱画面相似度');
        table.dataset.shuffleScores = '';
        const head = table.createTHead().insertRow();
        for (const label of ['同批成片对比', '打乱原画（贴图、遮挡前）', '成片打乱画面']) {
          const cell = document.createElement('th');
          cell.scope = 'col';
          cell.textContent = label;
          head.appendChild(cell);
        }
        const body = table.createTBody();
        for (const pair of scores.pairs) {
          const row = body.insertRow();
          row.insertCell().textContent = `第 ${pair.first}、${pair.second} 条`;
          row.insertCell().textContent = pair.source_similarity_percent.toFixed(2) + '%';
          row.insertCell().textContent = pair.rendered_similarity_percent.toFixed(2) + '%';
        }
        tables.prepend(table);
        return;
      }
      status.textContent = `${prefix} · ${scores.pairs.length} 组，成片打乱画面最高 ${scores.max_similarity_percent.toFixed(2)}%（上限 ${limit}%）。此旧报告仅有成片画面分数。`;
      const pairs = document.createElement('p');
      pairs.dataset.shuffleScores = '';
      pairs.textContent = scores.pairs.map(p => `第 ${p.first}、${p.second} 条：${p.similarity_percent.toFixed(2)}%`).join('　');
      tables.prepend(pairs);
    }

    function shuffleHistorySummary(check) {
      if (!Number.isInteger(check?.history_videos_checked) || check.history_videos_checked < 0) return '';
      const count = `已核对历史成片 ${check.history_videos_checked} 条`;
      if (!check.history_videos_checked) return count;
      const source = check.max_history_source_similarity_percent;
      const rendered = check.max_history_rendered_similarity_percent;
      if (!Number.isFinite(source) || !Number.isFinite(rendered)) return count;
      const bound = Array.isArray(check.history_pairs) && check.history_pairs.some(pair => pair?.bounded)
        ? '（含旧记录保守上界）' : '';
      return `${count} · 打乱原画最高 ${source.toFixed(2)}%，成片打乱画面最高 ${rendered.toFixed(2)}%${bound}`;
    }

    async function showShuffleCuts(taskId, taskName) {
      const dialog = document.getElementById('shuffle-cuts-dialog');
      dialog.dataset.taskId = taskId;
      dialog.querySelector('h2').textContent = taskName + ' · 打乱镜头明细';
      const status = dialog.querySelector('[data-shuffle-status]');
      const tables = dialog.querySelector('[data-shuffle-tables]');
      tables.replaceChildren();
      status.textContent = '正在读取成片镜头记录…';
      if (!dialog.open) dialog.showModal();
      try {
        const response = await fetch('/queue/compliance/' + encodeURIComponent(taskId), {cache:'no-store'});
        if (!response.ok) throw new Error();
        const report = await response.json();
        if (dialog.dataset.taskId !== taskId || !dialog.open) return;
        if (report.run_id !== taskId || !Array.isArray(report.variants)) throw new Error();
        let total = 0;
        let compared = 0;
        report.variants.forEach((variant, index) => {
          const shots = variant.shots.filter(s => s.role === 'shuffle' && !s.fixed_cut_part_offset_frames);
          total += shots.length;
          if (variant.shuffle_visual_verification?.earlier_variants_checked === index &&
              shots.length && variant.shuffle_visual_verification.cuts_checked === shots.length) compared++;
          const title = document.createElement('h3');
          title.textContent = `第 ${index + 1} 条 · ${shots.length} 个打乱镜头`;
          tables.appendChild(title);
          const history = shuffleHistorySummary(variant.shuffle_visual_verification);
          if (history) {
            const detail = document.createElement('p');
            detail.textContent = history;
            tables.appendChild(detail);
          }
          if (!shots.length) return;
          const table = document.createElement('table');
          const head = table.createTHead().insertRow();
          ['成片时间（秒）','打乱素材','原片时间（秒）'].forEach(label => {
            const th = document.createElement('th');
            th.scope = 'col';
            th.textContent = label;
            head.appendChild(th);
          });
          const body = table.createTBody();
          const range = (start, length) => start.toFixed(2) + '–' + (start + length).toFixed(2);
          for (const shot of shots) {
            const length = (shot.fixed_cut_group_frame_count || shot.frame_count) / variant.fps;
            const file = shot.source_path.split(String.fromCharCode(92)).pop().split('/').pop();
            const row = body.insertRow();
            for (const value of [range(shot.timeline_start_seconds, length), file, range(shot.source_start_seconds, length)]) {
              row.insertCell().textContent = value;
            }
          }
          tables.appendChild(table);
        });
        if (report.variants.length === 1) {
          const singleHistory = shuffleHistorySummary(report.variants[0]?.shuffle_visual_verification);
          status.textContent = total > 0
            ? `已读取单条成片共 ${total} 个打乱镜头${singleHistory ? ' · ' + singleHistory : ''}。`
            : '本条为纯核心成片，未使用打乱镜头。';
        } else {
          status.textContent = compared > 1 && compared === report.variants.length
            ? `${compared} 条成片已完成整段打乱画面对比，共 ${total} 刀。`
            : `已读取 ${total} 个打乱镜头的剪辑记录。`;
          if (Array.isArray(report.pairs) && report.pairs.length && Number.isFinite(report.max_similarity_percent)) {
            appendShuffleScores(tables, status, report, `${compared} 条成片已有两两相似度记录`);
          } else {
            status.textContent += ' 该报告未记录两两相似度百分比。';
          }
        }
        if (report.history_comparison_policy === 'track_scores_only') {
          const policy = document.createElement('p');
          policy.textContent = '整段打乱轨的原画和导出画面分别与历史成片比较，相似度均须 ≤40%。';
          tables.prepend(policy);
        }
        if (report.variants.length > 1 && total > 0) {
            const button = queueActionElement('复核本批成片 40% 相似度', async () => {
              button.disabled = true;
              status.textContent = 'FFmpeg 正在从每条成片的打乱时段抽帧…';
              try {
                const result = await fetch('/queue/shuffle-similarity/' + encodeURIComponent(taskId), {
                  method:'POST', body:new URLSearchParams({csrf_token:csrfToken})
                });
                const scores = await result.json();
                if (!dialog.open || dialog.dataset.taskId !== taskId || !button.isConnected) return;
                if (!result.ok) throw new Error(scores.message || '成片暂时无法复核。');
                button.remove();
                appendShuffleScores(tables, status, scores, scores.max_similarity_percent <= scores.threshold_percent ? '本批成片复核通过' : '本批成片复核超限');
              } catch (error) {
                if (!button.isConnected) return;
                button.disabled = false;
                status.textContent = error.message;
              }
            });
            tables.prepend(button);
        }
      } catch (_) {
        if (dialog.dataset.taskId !== taskId) return;
        tables.replaceChildren();
        status.textContent = '这条任务的镜头报告暂时无法读取。';
      }
    }
