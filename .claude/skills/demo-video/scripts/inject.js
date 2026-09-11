// ─────────────────────────────────────────────────────────────────────────
// 브라우저 페이지 안에서 실행되는 스크립트다 (Node 쪽이 아니다!).
// context.addInitScript() 로 등록되며, 페이지가 새 문서를 로드할 때마다
// "그 문서 안의 첫 스크립트보다도 먼저" 다시 실행된다.
//
// Playwright 로 녹화한 영상에는 OS 마우스 커서가 찍히지 않으므로,
// 커서·자막·하이라이트·챕터 카드를 전부 DOM 요소로 직접 그려서 화면에 보이게 한다.
// Node 쪽 러너(run_scenario.js)는 이 파일이 만들어 둔 window.__demo 함수들을
// page.evaluate() 로 호출해서 연출을 제어한다.
// ─────────────────────────────────────────────────────────────────────────
(function () {
  const state = { x: 60, y: 60 };
  let cursorEl, rippleEl, captionEl, spotlightEl, chapterEl, chapterTitleEl, chapterSubEl;

  function injectStyles() {
    const style = document.createElement('style');
    style.setAttribute('data-demo-video', 'true');
    style.textContent = `
      #__demo-cursor{position:fixed;left:0;top:0;width:30px;height:30px;margin:-6px 0 0 -4px;
        pointer-events:none;z-index:2147483647;will-change:transform;
        filter:drop-shadow(0 2px 4px rgba(0,0,0,.5));}
      #__demo-ripple{position:fixed;left:0;top:0;width:18px;height:18px;margin:-9px 0 0 -9px;
        pointer-events:none;z-index:2147483646;border-radius:50%;
        border:3px solid rgba(255,89,60,.95);background:rgba(255,89,60,.28);opacity:0;}
      #__demo-ripple.play{animation:__demoRipple .55s cubic-bezier(.2,.6,.3,1);}
      @keyframes __demoRipple{
        0%{opacity:1;transform:scale(.3);}
        100%{opacity:0;transform:scale(3.4);}
      }
      #__demo-caption{position:fixed;left:50%;bottom:56px;transform:translateX(-50%);
        max-width:82vw;padding:14px 28px;border-radius:12px;background:rgba(10,12,20,.82);
        color:#fff;font:600 26px/1.45 -apple-system,"Malgun Gothic","맑은 고딕",sans-serif;
        text-align:center;z-index:2147483645;opacity:0;transition:opacity .25s ease;
        pointer-events:none;box-shadow:0 10px 28px rgba(0,0,0,.35);}
      #__demo-caption.show{opacity:1;}
      #__demo-spotlight{position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;
        z-index:2147483644;border:3px solid #ffd23f;border-radius:12px;
        box-shadow:0 0 0 9999px rgba(0,0,0,.55);opacity:0;
        transition:opacity .25s ease,left .35s ease,top .35s ease,width .35s ease,height .35s ease;}
      #__demo-spotlight.show{opacity:1;}
      #__demo-chapter{position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;
        align-items:center;justify-content:center;background:linear-gradient(135deg,#0e1b3a,#16337a);
        opacity:0;transition:opacity .4s ease;pointer-events:none;text-align:center;}
      #__demo-chapter.show{opacity:1;}
      #__demo-chapter h1{color:#fff;margin:0 48px;
        font:800 60px/1.35 -apple-system,"Malgun Gothic","맑은 고딕",sans-serif;}
      #__demo-chapter p{color:#c7d3ee;margin:18px 48px 0;
        font:400 26px/1.5 -apple-system,"Malgun Gothic","맑은 고딕",sans-serif;}
    `;
    (document.head || document.documentElement).appendChild(style);
  }

  function buildElements() {
    cursorEl = document.createElement('div');
    cursorEl.id = '__demo-cursor';
    // 화살표 모양 커서(SVG). 그림자는 CSS filter 로 준다.
    cursorEl.innerHTML =
      '<svg width="30" height="30" viewBox="0 0 30 30">' +
      '<path d="M4 2 L26 15 L15.5 17.5 L20 27 L15.5 29 L11 19 L4 24 Z" ' +
      'fill="#ffffff" stroke="#1b1f2a" stroke-width="1.6" stroke-linejoin="round"/></svg>';

    rippleEl = document.createElement('div');
    rippleEl.id = '__demo-ripple';

    captionEl = document.createElement('div');
    captionEl.id = '__demo-caption';

    spotlightEl = document.createElement('div');
    spotlightEl.id = '__demo-spotlight';

    chapterEl = document.createElement('div');
    chapterEl.id = '__demo-chapter';
    chapterTitleEl = document.createElement('h1');
    chapterSubEl = document.createElement('p');
    chapterEl.appendChild(chapterTitleEl);
    chapterEl.appendChild(chapterSubEl);

    const root = document.body;
    root.appendChild(spotlightEl);
    root.appendChild(chapterEl);
    root.appendChild(captionEl);
    root.appendChild(rippleEl);
    root.appendChild(cursorEl); // 커서를 맨 위에 둔다(z-index 로도 보장되지만 DOM 순서도 맞춘다)

    setCursorPos(state.x, state.y);
  }

  function setCursorPos(x, y) {
    state.x = x;
    state.y = y;
    if (cursorEl) cursorEl.style.transform = `translate(${x}px, ${y}px)`;
  }

  function easeInOutQuad(t) {
    return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
  }

  // 시작점→끝점을 직선이 아니라 2차 베지어 곡선으로 부드럽게 이동한다.
  // 컨트롤 포인트를 이동 경로의 수직 방향으로 무작위로 밀어서, 매번 다른
  // 자연스러운 곡선이 나오게 한다(사람이 마우스를 움직이는 느낌).
  function moveCursorTo(x, y, duration) {
    duration = Math.max(1, duration || 600);
    return new Promise((resolve) => {
      const sx = state.x, sy = state.y;
      const dx = x - sx, dy = y - sy;
      const dist = Math.hypot(dx, dy) || 1;
      const bow = Math.min(140, dist * 0.3) * (Math.random() < 0.5 ? -1 : 1);
      const nx = -dy / dist, ny = dx / dist; // 이동 방향에 수직인 단위 벡터
      const cx = sx + dx * 0.5 + nx * bow;
      const cy = sy + dy * 0.5 + ny * bow;
      const t0 = performance.now();
      function frame(now) {
        const t = Math.min(1, (now - t0) / duration);
        const e = easeInOutQuad(t);
        const ix = (1 - e) * (1 - e) * sx + 2 * (1 - e) * e * cx + e * e * x;
        const iy = (1 - e) * (1 - e) * sy + 2 * (1 - e) * e * cy + e * e * y;
        setCursorPos(ix, iy);
        if (t < 1) requestAnimationFrame(frame);
        else resolve();
      }
      requestAnimationFrame(frame);
    });
  }

  function clickRipple(x, y) {
    rippleEl.style.left = x + 'px';
    rippleEl.style.top = y + 'px';
    rippleEl.classList.remove('play');
    void rippleEl.offsetWidth; // 강제 리플로우: 연속 클릭에서도 애니메이션이 매번 재생되게 한다.
    rippleEl.classList.add('play');
  }

  function setCaption(text) {
    captionEl.textContent = text || '';
    captionEl.classList.toggle('show', !!text);
  }

  function showSpotlight(rect, pad) {
    pad = pad == null ? 10 : pad;
    spotlightEl.style.left = (rect.x - pad) + 'px';
    spotlightEl.style.top = (rect.y - pad) + 'px';
    spotlightEl.style.width = (rect.width + pad * 2) + 'px';
    spotlightEl.style.height = (rect.height + pad * 2) + 'px';
    spotlightEl.classList.add('show');
  }

  function hideSpotlight() {
    spotlightEl.classList.remove('show');
  }

  function showChapter(title, subtitle) {
    chapterTitleEl.textContent = title || '';
    chapterSubEl.textContent = subtitle || '';
    chapterSubEl.style.display = subtitle ? 'block' : 'none';
    chapterEl.classList.add('show');
  }

  function hideChapter() {
    chapterEl.classList.remove('show');
  }

  window.__demo = {
    ready: false,
    setCursor: setCursorPos,
    moveCursorTo,
    clickRipple,
    setCaption,
    clearCaption: () => setCaption(''),
    showSpotlight,
    hideSpotlight,
    showChapter,
    hideChapter,
  };

  // document.body 가 생기기 전(addInitScript 는 파싱 시작 전에 실행됨)이면
  // MutationObserver 로 body 가 생기는 순간을 기다렸다가 UI 를 만든다.
  function waitForBody(cb) {
    if (document.body) { cb(); return; }
    const target = document.documentElement || document;
    const obs = new MutationObserver(() => {
      if (document.body) { obs.disconnect(); cb(); }
    });
    obs.observe(target, { childList: true, subtree: true });
  }

  waitForBody(() => {
    injectStyles();
    buildElements();
    window.__demo.ready = true;
  });
})();
