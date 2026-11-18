(() => {
  const clamp = (n, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, n));
  const clock = n => Math.floor(n / 60) + ':' + String(Math.floor(n % 60)).padStart(2, '0');
  const panels = new Map();
  const grips = [
    ['nw',0,0],['n',50,0],['ne',100,0],['e',100,50],
    ['se',100,100],['s',50,100],['sw',0,100],['w',0,50]
  ];
  function setup(element) {
    const scope = element.dataset.masksScope;
    const el = name => element.querySelector('[data-mask-' + name + ']');
    const enabled = el('enabled'), stage = el('stage'), video = stage.querySelector('video'), still = el('still');
    const source = el('source'), seek = el('seek'), play = el('play'), strength = el('strength'), styleSelect = el('style');
    const normalizeStyle = v => ['blur', 'delogo', 'dark'].includes(v) ? v : 'blur';
    const boxes = [];
    let selected = -1, drag = null, clipURL = '', fallback = false, fallbackTimer = 0, frameRequest = 0;
    const duration = () => Number.isFinite(video.duration) && !fallback ? video.duration : Number(source.selectedOptions[0]?.dataset.seconds || 0);
    const setTime = t => {
      const total = Math.max(0, duration());
      seek.max = String(total);
      seek.value = String(clamp(t, 0, total));
      el('time').textContent = clock(Number(seek.value)) + ' / ' + clock(total);
    };
    function pause() {
      video.pause();
      clearInterval(fallbackTimer);
      fallbackTimer = 0;
      play.textContent = '播放';
    }
    function showFrame() {
      if (!clipURL) return;
      const token = ++frameRequest;
      const image = new Image();
      image.onload = () => {
        if (token !== frameRequest || !fallback) return;
        still.src = image.src;
        still.hidden = false;
      };
      image.onerror = () => {
        if (token !== frameRequest) return;
        pause();
        el('hint').textContent = '暂时无法读取参考画面，请切换素材。';
      };
      image.src = clipURL + '?frame=' + Number(seek.value).toFixed(3);
    }
    function loadClip() {
      const file = source.value;
      const base = scope === 'task' ? '/task-drafts/'+encodeURIComponent(taskDraft?.id || '')+'/video-preview/' : '/material-video/';
      const next = enabled.checked && file ? base+encodeURIComponent('打乱素材池')+'/'+encodeURIComponent(file) : '';
      if (next === clipURL) return;
      pause(); clipURL = next; fallback = false; ++frameRequest;
      still.hidden = true; still.removeAttribute('src'); video.hidden = false;
      if (next) video.src = next;
      else video.removeAttribute('src');
      video.load(); setTime(0);
    }
    function scaleBlur() {
      const style = normalizeStyle(styleSelect?.value);
      stage.dataset.maskCurrentStyle = style;
      const val = clamp(Math.round(Number(strength.value) || 55), 1, 100);
      if (style === 'delogo') {
        strength.disabled = true;
        stage.style.setProperty('--mask-blur', '2.5px');
        strength.nextElementSibling.textContent = '自动融合';
      } else if (style === 'dark') {
        strength.disabled = false;
        stage.style.setProperty('--mask-blur', '2px');
        stage.style.setProperty('--mask-dark-alpha', (0.28 + 0.64 * (val / 100)).toFixed(3));
        strength.nextElementSibling.textContent = String(val);
      } else {
        strength.disabled = false;
        stage.style.setProperty('--mask-blur', Math.max(.1, stage.clientWidth * .0007 * val) + 'px');
        strength.nextElementSibling.textContent = String(val);
      }
    }
    function paint() {
      const list = el('boxes');
      list.replaceChildren();
      boxes.forEach(([l,t,r,b], i) => {
        const node = document.createElement('div');
        node.className = 'mask-box' + (i === selected ? ' active' : '');
        node.dataset.index = i;
        Object.assign(node.style, {left:l*100+'%',top:t*100+'%',width:(r-l)*100+'%',height:(b-t)*100+'%'});
        if (i === selected) {
          node.style.zIndex = 1;
          for (const [dir,x,y] of grips) {
            const grip = document.createElement('i');
            grip.dataset.grip = dir;
            Object.assign(grip.style,{left:x+'%',top:y+'%',cursor:dir+'-resize'});
            node.append(grip);
          }
        }
        list.append(node);
      });
      el('count').textContent = boxes.length+' / 8';
      el('remove').disabled = selected < 0;
      el('add').disabled = boxes.length >= 8;
      seek.disabled = play.disabled = !clipURL;
      el('hint').textContent = !clipURL ? '勾选或添加打乱素材后可在此预览参考画面；也可先拉框或一键加底部字幕框。' :
        '在空白处拖出框，拖框内可移动，拖边上的圆点可缩放。';
      scaleBlur();
    }
    function refresh() {
      el('editor').hidden = !enabled.checked;
      const files = enabled.checked ? (scope === 'task' ? taskPoolFiles('打乱素材池') : selectedShuffleFiles()) : [];
      const same = files.length === source.options.length && files.every((f,i) => f.name === source.options[i].value && String(f.duration_seconds || 0) === source.options[i].dataset.seconds);
      if (!same) {
        const previous = source.value;
        source.replaceChildren(...files.map(f => {
          const option = new Option(f.name, f.name);
          option.dataset.seconds = String(f.duration_seconds || 0);
          return option;
        }));
        if (files.some(f => f.name === previous)) source.value = previous;
      }
      loadClip();
      if (!drag) paint();
    }
    enabled.onchange = refresh;
    source.onchange = () => { loadClip(); paint(); };
    video.onloadedmetadata = () => setTime(0);
    video.ontimeupdate = () => { if (!fallback) setTime(video.currentTime); };
    video.onplay = () => { play.textContent = '暂停'; };
    video.onpause = () => { if (!fallbackTimer) play.textContent = '播放'; };
    function useStills() {
      if (!clipURL || !enabled.checked) return;
      fallback = true; video.hidden = true; setTime(Number(seek.value)); showFrame();
    }
    video.onerror = useStills;
    seek.oninput = () => {
      const at = Number(seek.value);
      setTime(at);
      if (fallback) showFrame(); else if (video.readyState >= 1) video.currentTime = at;
    };
    play.onclick = async () => {
      if (!clipURL) return;
      if (!video.paused || fallbackTimer) { pause(); return; }
      if (!fallback) {
        try { await video.play(); return; } catch { useStills(); }
      }
      if (Number(seek.value) >= duration()) setTime(0);
      play.textContent = '暂停';
      const tick = () => {
        const pos = Math.min(duration(), Number(seek.value)+.5);
        setTime(pos); showFrame();
        if (pos >= duration()) pause();
      };
      fallbackTimer = setInterval(tick, 500);
    };
    function point(event) {
      const rect = stage.getBoundingClientRect();
      return [clamp((event.clientX-rect.left)/rect.width), clamp((event.clientY-rect.top)/rect.height)];
    }
    stage.onpointerdown = event => {
      if (!event.isPrimary || event.button !== 0) return;
      const node = event.target.closest('.mask-box');
      if (!node && boxes.length >= 8) return;
      pause(); event.preventDefault(); stage.focus({preventScroll:true});
      const [x,y] = point(event);
      const prev = selected;
      if (node) selected = Number(node.dataset.index);
      else { selected = boxes.length; boxes.push([x,y,x,y]); }
      drag = {mode:node ? (event.target.dataset.grip || 'move') : 'new', x,y, previous:prev, original:boxes[selected].slice()};
      stage.setPointerCapture(event.pointerId); paint();
    };
    stage.onpointermove = event => {
      if (!drag || !stage.hasPointerCapture(event.pointerId)) return;
      const [x,y] = point(event), [l,t,r,b] = drag.original;
      const dx=x-drag.x, dy=y-drag.y, mode=drag.mode;
      if (mode === 'new') boxes[selected]=[Math.min(x,drag.x),Math.min(y,drag.y),Math.max(x,drag.x),Math.max(y,drag.y)];
      else if (mode === 'move') {
        const sx=clamp(dx,-l,1-r), sy=clamp(dy,-t,1-b);
        boxes[selected]=[l+sx,t+sy,r+sx,b+sy].map(v=>clamp(v));
      } else boxes[selected]=[mode.includes('w')?clamp(l+dx,0,r-.015):l, mode.includes('n')?clamp(t+dy,0,b-.01):t,
                              mode.includes('e')?clamp(r+dx,l+.015,1):r,mode.includes('s')?clamp(b+dy,t+.01,1):b];
      paint();
    };
    function finish(event,cancel) {
      if (!drag) return;
      const [l,t,r,b]=boxes[selected];
      if (drag.mode === 'new' && (cancel || r-l < .015 || b-t < .01)) {
        boxes.splice(selected,1); selected=drag.previous;
      } else if (cancel) boxes[selected]=drag.original;
      drag=null;
      if (stage.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
      paint();
    }
    stage.onpointerup = e => { if(drag && e.isPrimary) { stage.onpointermove(e); finish(e,false); } };
    stage.onpointercancel = e => finish(e,true);
    stage.onlostpointercapture = e => finish(e,true);
    el('add').onclick = () => {
      if (boxes.length >= 8) return;
      const top=.68-(boxes.length%5)*.075;
      boxes.push([.10,top,.90,top+.10]); selected=boxes.length-1; paint(); stage.focus();
    };
    el('remove').onclick = () => {
      if (selected < 0) return;
      boxes.splice(selected,1); selected=Math.min(selected,boxes.length-1); paint();
    };
    stage.onkeydown = e => {
      if (selected < 0) return;
      if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); el('remove').click(); return; }
      const delta={ArrowLeft:[-.005,0],ArrowRight:[.005,0],ArrowUp:[0,-.005],ArrowDown:[0,.005]}[e.key];
      if (!delta) return;
      e.preventDefault();
      const [l,t,r,b]=boxes[selected], x=clamp(delta[0],-l,1-r), y=clamp(delta[1],-t,1-b);
      boxes[selected]=[l+x,t+y,r+x,b+y].map(v=>clamp(v)); paint();
    };
    strength.oninput = scaleBlur;
    if (styleSelect) styleSelect.onchange = scaleBlur;
    new ResizeObserver(scaleBlur).observe(stage);
    const dialog = element.closest('dialog');
    if (dialog) dialog.addEventListener('close',pause);
    refresh();
    return {
      refresh,
      reset() {
        pause(); drag=null; enabled.checked=false; boxes.length=0; selected=-1; strength.value='55';
        if (styleSelect) styleSelect.value='blur';
        refresh();
      },
      getState() {
        return {
          enabled: Boolean(enabled.checked),
          style: normalizeStyle(styleSelect?.value),
          strength: clamp(Math.round(Number(strength.value) || 55), 1, 100),
          boxes: boxes.map(([l, t, r, b]) => [l, t, r, b]),
        };
      },
      setState(state) {
        pause(); drag=null;
        const s = state && typeof state === 'object' ? state : {};
        enabled.checked = Boolean(s.enabled);
        if (styleSelect) styleSelect.value = normalizeStyle(s.style);
        strength.value = String(clamp(Math.round(Number(s.strength) || 55), 1, 100));
        boxes.length = 0;
        if (Array.isArray(s.boxes)) {
          for (const box of s.boxes.slice(0, 8)) {
            if (Array.isArray(box) && box.length >= 4) {
              const [l, t, r, b] = box.map(v => clamp(Number(v) || 0));
              if (r - l >= .01 && b - t >= .01) boxes.push([l, t, r, b]);
            }
          }
        }
        selected = boxes.length - 1;
        refresh();
      },
      collect(body) {
        const hasShuffle = (scope === 'task' ? taskPoolFiles('打乱素材池') : selectedShuffleFiles()).length > 0 || source.options.length > 0;
        if (!enabled.checked || !boxes.length || !hasShuffle) {
          body.delete('shuffle_masks');
          return;
        }
        body.set('shuffle_masks', JSON.stringify({
          strength: clamp(Math.round(Number(strength.value) || 55), 1, 100),
          style: normalizeStyle(styleSelect?.value),
          boxes: boxes.map(([l, t, r, b]) => [l, t, r, b]),
        }));
      }
    };
  }
  for (const element of document.querySelectorAll('[data-masks-scope]')) panels.set(element.dataset.masksScope,setup(element));
  window.manualShuffleMasks = {
    refresh(scope) { panels.get(scope)?.refresh(); },
    reset(scope) { panels.get(scope)?.reset(); },
    getState(scope) {
      return panels.get(scope)?.getState() || {enabled:false, style:'blur', strength:55, boxes:[]};
    },
    setState(scope, state) { panels.get(scope)?.setState(state); },
    collect(scope,body) { panels.get(scope)?.collect(body); }
  };
})();
