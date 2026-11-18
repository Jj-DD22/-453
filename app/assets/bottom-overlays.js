(() => {
  const panels = new Map();

  function pictureTiming(rule) {
    if (rule?.editMode !== 'original_audio_random') return {coreAudio:false, start:null, end:Infinity};
    const range = rule.coreRanges?.[0];
    if (!range) return {coreAudio:true, start:null, end:null};
    // The original-audio editor exports at 30 fps and rounds each crop boundary to the nearest even-tie frame.
    const frames = seconds => Math.max(1, Math.ceil(seconds * 30 - .001));
    const boundary = seconds => {
      const n = seconds * 30;
      const whole = Math.floor(n);
      return n - whole === .5 ? whole + whole % 2 : Math.round(n);
    };
    const total = frames(rule.primaryCoreDuration);
    const first = Math.min(total - 1, Math.max(0, boundary(range[0])));
    const last = range[0] === 0 && range[1] >= rule.primaryCoreDuration - 1e-6
      ? total : Math.min(total, Math.max(first + 1, boundary(range[1])));
    const opening = rule.opening > 0 ? frames(rule.opening) : 0;
    const bodyFrames = Math.max(last - first, Math.floor(rule.target * 30 + .001));
    return {coreAudio:true, start:(opening + last - first) / 30, end:(opening + bodyFrames) / 30};
  }

  function syncTiming(panel, rule) {
    const next = pictureTiming(rule);
    const old = panel.timing;
    if (old && old.coreAudio === next.coreAudio && old.start === next.start && old.end === next.end) return;
    panel.timing = next;
    for (const row of panel.rows) update(row);
    draw(panel);
  }

  function sources(scope) {
    const task = scope === 'task';
    const base = task ? '/task-drafts/' + encodeURIComponent(taskDraft?.id || '') + '/video-preview/' : '/material-video/';
    const pool = '打乱素材池';
    const files = task ? taskPoolFiles(pool) : selectedShuffleFiles(pool);
    return files.map(file => base + encodeURIComponent(pool) + '/' + encodeURIComponent(file.name) + '?frame=0');
  }

  function makeTile(image, sizePercent) {
    const baseScale = Math.min(360 * .82 / image.naturalWidth, 640 * .30 / image.naturalHeight);
    const scale = baseScale * sizePercent / 100;
    const tile = document.createElement('canvas');
    tile.width = Math.max(1, Math.round(image.naturalWidth * scale));
    tile.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const ctx = tile.getContext('2d');
    ctx.drawImage(image, 0, 0, tile.width, tile.height);
    const fade = ctx.createLinearGradient(0, 0, 0, Math.max(1, Math.round(tile.height * .10)));
    fade.addColorStop(0, 'transparent');
    fade.addColorStop(1, 'black');
    ctx.globalCompositeOperation = 'destination-in';
    ctx.fillStyle = fade;
    ctx.fillRect(0, 0, tile.width, tile.height);
    return tile;
  }

  function rowInterval(card, timing) {
    const anchor = card.querySelector('[data-anchor]')?.value === 'movie_clock' ? 'movie_clock' : 'first_core_end';
    const duration = card.querySelector('[data-duration]').valueAsNumber;
    if (anchor === 'first_core_end') {
      const start = Number.isFinite(timing?.start) ? timing.start : 0;
      const span = Number.isFinite(duration) && duration > 0 ? duration : 5;
      return {anchor, start, end: start + span};
    }
    const start = card.querySelector('[data-start]').valueAsNumber;
    if (!Number.isFinite(start + duration) || start < 0 || duration <= 0) return null;
    return {anchor, start, end: start + duration};
  }

  function draw(panel) {
    if (!panel.active) return;
    const canvas = panel.canvas;
    const ctx = canvas.getContext('2d');
    const {width, height} = canvas;
    if (panel.frame) {
      ctx.drawImage(panel.frame, 0, 0, width, height);
    } else {
      const gray = ctx.createLinearGradient(0, 0, width, height);
      gray.addColorStop(0, '#d5dae0');
      gray.addColorStop(1, '#47515d');
      ctx.fillStyle = gray;
      ctx.fillRect(0, 0, width, height);
    }
    const activeInterval = rowInterval(panel.active.card, panel.timing);
    const time = activeInterval ? activeInterval.start : null;
    panel.handle.hidden = true;
    for (const row of panel.rows) {
      const card = row.card;
      if (!row.tile || !card.querySelector('[data-enabled]').checked) continue;
      const interval = rowInterval(card, panel.timing);
      const isActiveFirstCore = row === panel.active && interval?.anchor === 'first_core_end';
      if (!isActiveFirstCore && (!interval || time === null || !(time >= interval.start && time < interval.end))) continue;
      ctx.globalAlpha = card.querySelector('[data-opacity]').valueAsNumber / 100;
      ctx.drawImage(row.tile, Math.round((width - row.tile.width) / 2), height - row.tile.height - Math.round(height * .025));
      if (row === panel.active) {
        panel.handle.style.bottom = ((row.tile.height + Math.round(height * .025)) / height * 100) + '%';
        panel.handle.hidden = false;
      }
    }
    ctx.globalAlpha = 1;
  }

  function refreshBackground(panel) {
    if (!panel.active) return;
    const urls = sources(panel.scope);
    if (panel.urls && urls.length === panel.urls.length && urls.every((url, i) => url === panel.urls[i])) return;
    clearTimeout(panel.timer);
    panel.urls = urls;
    panel.frame = null;
    panel.count.textContent = urls.length + ' 条';
    panel.caption.textContent = urls.length ? '正在读取打乱画面…' : '选入打乱视频后即可预览叠图。';
    draw(panel);
    if (!urls.length) return;

    // Only the visual example rotates. The submission contains no preview URL or clip id.
    const cache = new Map();
    function schedule(index) {
      if (urls.length < 2) return;
      panel.timer = setTimeout(() => {
        if (panel.urls !== urls) return;
        if (document.hidden || !panel.figure.getClientRects().length || panel.active.card.matches(':active')) {
          schedule(index);
        } else {
          showFrame(index);
        }
      }, 3200);
    }
    function showFrame(index) {
      const url = urls[index];
      function display(frame) {
        if (panel.urls !== urls) return;
        if (frame) {
          panel.frame = frame;
          panel.caption.textContent = urls.length > 1 ? '画面示例 ' + (index + 1) + ' / ' + urls.length : '打乱画面示例';
          draw(panel);
        } else if (!panel.frame) {
          panel.caption.textContent = '暂时无法读取打乱素材的参考帧。';
        }
        schedule((index + 1) % urls.length);
      }
      if (cache.has(url)) return display(cache.get(url));
      const frame = new Image();
      frame.onload = () => {
        if (panel.urls !== urls) return;
        if (cache.size >= 12) cache.delete(cache.keys().next().value);
        cache.set(url, frame);
        display(frame);
      };
      frame.onerror = () => display(null);
      frame.src = url;
    }
    showFrame(0);
  }

  function show(panel, row) {
    if (panel.active !== row) {
      panel.active = row;
      panel.figure.hidden = !row;
      (row?.card || panel.element).appendChild(panel.figure);
      for (const item of panel.rows) {
        const button = item.card.querySelector('[data-view-picture]');
        button.setAttribute('aria-pressed', String(item === row));
        button.textContent = item === row ? '预览中' : '查看预览';
      }
      if (row) refreshBackground(panel);
      else { clearTimeout(panel.timer); panel.frame = null; panel.urls = null; }
    }
    draw(panel);
  }

  function update(row) {
    const q = selector => row.card.querySelector(selector);
    const timing = row.panel.timing || {coreAudio:false, start:null, end:Infinity};
    const anchor = q('[data-anchor]')?.value === 'movie_clock' ? 'movie_clock' : 'first_core_end';
    const input = q('[data-start]');
    const startWrap = input.closest('label');
    const startLabel = q('[data-start-label]');
    if (anchor === 'first_core_end') {
      if (!input.readOnly) row.manualStart = input.value;
      input.readOnly = true;
      if (timing.coreAudio) {
        if (startWrap) startWrap.hidden = false;
        if (startLabel) startLabel.textContent = '开始（秒 · 随核心）';
        input.value = timing.start === null ? '' : String(Number(timing.start.toFixed(3)));
        input.placeholder = '等待核心选段';
        input.title = '跟随首条核心选段的成片末尾，计入本条开头视频。';
      } else {
        if (startWrap) startWrap.hidden = true;
        input.value = '0';
        input.placeholder = '';
        input.title = '';
      }
    } else {
      if (startWrap) startWrap.hidden = false;
      if (input.readOnly) {
        input.readOnly = false;
        input.value = row.manualStart ?? '0';
      } else {
        row.manualStart = input.value;
      }
      if (startLabel) startLabel.textContent = '开始（秒）';
      input.placeholder = '0';
      input.title = '';
    }
    const start = input.valueAsNumber;
    const duration = q('[data-duration]').valueAsNumber;
    const opacity = q('[data-opacity]').value;
    q('[data-opacity-value]').textContent = opacity + '%';
    const size = q('[data-size]').valueAsNumber;
    q('[data-size-value]').textContent = size + '%';
    const image = q('.overlay-name img');
    if (image.naturalWidth && size !== row.tilePercent) {
      row.tile = makeTile(image, size);
      row.tilePercent = size;
    }
    let message = '';
    if (anchor === 'first_core_end') {
      if (timing.coreAudio) {
        if (timing.start === null) {
          message = '选择有效的首条核心片段后，自动确定开始时间。';
        } else if (Number.isFinite(duration) && duration > 0) {
          const autoStart = Number(timing.start.toFixed(3));
          const autoEnd = Number(Math.min(timing.start + duration, timing.end).toFixed(3));
          message = autoEnd <= autoStart ? '首条核心片段已到片尾，本条没有叠图时段。'
            : `成片 ${autoStart}–${autoEnd} 秒 · 紧随首段核心，仅打乱画面显示`;
        }
      } else if (Number.isFinite(duration) && duration > 0) {
        message = `首段核心结束后自动从第 1 个打乱镜头起显示 ${duration} 秒（仅打乱画面，核心自动跳过）`;
      }
    } else if (Number.isFinite(start + duration) && start >= 0 && duration > 0) {
      message = `成片 ${start}–${(start + duration).toFixed(2)} 秒 · 仅打乱画面（遇核心自动跳过）`;
    }
    q('.overlay-timing').textContent = message;
    q('fieldset').disabled = !q('[data-enabled]').checked;
    row.card.classList.toggle('overlay-off', !q('[data-enabled]').checked);
  }

  function release(row) {
    URL.revokeObjectURL(row.url);
    row.card.remove();
  }

  function add(panel, file) {
    syncTiming(panel, panel.scope === 'task' ? taskCurrentRules() : currentDurationRules());
    const card = panel.element.querySelector('template').content.firstElementChild.cloneNode(true);
    const row = {panel, card, file, url: URL.createObjectURL(file)};
    const name = card.querySelector('.overlay-filename');
    name.textContent = name.title = file.name;
    const thumbnail = card.querySelector('img');
    thumbnail.onload = () => {
      update(row);
      draw(panel);
    };
    thumbnail.src = row.url;
    card.querySelector('[data-view-picture]').onclick = () => show(panel, row);
    card.querySelector('[data-remove]').setAttribute('aria-label', '移除 ' + file.name);
    card.querySelector('[data-remove]').onclick = () => {
      panel.rows.delete(row);
      if (panel.active === row) show(panel, panel.rows.values().next().value || null);
      release(row);
      draw(panel);
    };
    const edit = event => {
      if (panel.figure.contains(event.target)) return;
      update(row);
      show(panel, row);
    };
    card.addEventListener('input', edit);
    card.addEventListener('change', edit);
    panel.rows.add(row);
    panel.element.querySelector('[data-overlay-rows]').append(card);
    update(row);
    show(panel, row);
  }

  function bindResize(panel) {
    const handle = panel.handle;
    let drag = null;
    handle.addEventListener('pointerdown', event => {
      if (!event.isPrimary || event.button !== 0 || !panel.active?.tile) return;
      const input = panel.active.card.querySelector('[data-size]');
      if (input.disabled || input.matches(':disabled')) return;
      const renderedHeight = panel.active.tile.height * panel.canvas.getBoundingClientRect().height / panel.canvas.height;
      drag = {input, start: input.valueAsNumber, y: event.clientY, percentPerPixel: input.valueAsNumber / renderedHeight};
      handle.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    handle.addEventListener('pointermove', event => {
      if (!drag || !handle.hasPointerCapture(event.pointerId)) return;
      const size = Math.round(drag.start + (drag.y - event.clientY) * drag.percentPerPixel);
      const input = drag.input;
      input.value = String(Math.max(Number(input.min), Math.min(Number(input.max), size)));
      input.dispatchEvent(new Event('input', {bubbles:true}));
    });
    handle.addEventListener('lostpointercapture', () => { drag = null; });
  }

  for (const element of document.querySelectorAll('[data-bottom-panel]')) {
    const figure = element.querySelector('.overlay-preview');
    const panel = {element, rows: new Set(), scope: element.dataset.bottomPanel, figure,
      canvas: figure.querySelector('canvas'), caption: figure.querySelector('figcaption'),
      count: figure.querySelector('[data-shuffle-count]'), handle: figure.querySelector('.overlay-resize')};
    bindResize(panel);
    panels.set(element.dataset.bottomPanel, panel);
    const picker = element.querySelector('input[type=file]');
    element.querySelector('[data-add-picture]').onclick = () => picker.click();
    picker.onchange = () => {
      for (const file of picker.files) add(panel, file);
      picker.value = '';
    };
  }

  window.bottomOverlays = {
    refresh(scope, rule) {
      const panel = panels.get(scope);
      if (panel) {
        syncTiming(panel, rule);
        refreshBackground(panel);
      }
    },
    collect(scope, body) {
      const settings = [];
      for (const row of panels.get(scope)?.rows || []) {
        const q = selector => row.card.querySelector(selector);
        if (!q('[data-enabled]').checked) continue;
        const anchor = q('[data-anchor]')?.value === 'movie_clock' ? 'movie_clock' : 'first_core_end';
        const duration = q('[data-duration]').valueAsNumber;
        const opacity = q('[data-opacity]').valueAsNumber;
        const size_percent = q('[data-size]').valueAsNumber;
        if (anchor === 'first_core_end') {
          if (!Number.isFinite(duration) || duration <= 0) {
            throw Error('请检查「' + row.file.name + '」的显示时长（须大于 0 秒）。');
          }
          body.set('overlay_image_' + settings.length, row.file);
          settings.push({anchor: 'first_core_end', start: 0, duration, opacity, size_percent});
        } else {
          const start = q('[data-start]').valueAsNumber;
          if (!Number.isFinite(start + duration) || start < 0 || duration <= 0) {
            throw Error('请检查「' + row.file.name + '」的开始秒数和显示时长。');
          }
          body.set('overlay_image_' + settings.length, row.file);
          settings.push({anchor: 'movie_clock', start, duration, opacity, size_percent});
        }
      }
      if (settings.length) body.set('bottom_overlays', JSON.stringify(settings));
      else body.delete('bottom_overlays');
    },
    reset(scope) {
      const panel = panels.get(scope);
      if (panel) {
        show(panel, null);
        for (const row of panel.rows) release(row);
        panel.rows.clear();
      }
    }
  };
})();
