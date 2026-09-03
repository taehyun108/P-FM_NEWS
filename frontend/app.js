/* =====================================================================
   P-FM NEWS — 프론트엔드 (PRD F6)

   원칙
     1. 이 파일은 backend API 만 호출한다. 외부 API·DB 에 직접 접근하지 않는다.
     2. API 키를 이 파일에 두지 않는다. (§6 보안)
     3. 필터는 같은 그룹 안 OR / 다른 그룹 사이 AND. (F6.1a)
     4. 칩은 렌더링 직전 한 번 더 중복 제거한다. (F6.2b)
   ===================================================================== */

const API = '';                 // 같은 오리진에서 서빙된다
const PAGE_SIZE = 20;

/* 선택 상태 — 기간만 단일 선택, 나머지는 다중 선택 */
const state = {
  group: new Set(),
  cat: new Set(),
  press: new Set(),
  period: 'all',
  q: '',
  page: 1,
  total: 0,
  loading: false,
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

/* ── 유틸 ───────────────────────────────────────────────────────── */

/** 칩 중복 제거용 정규화 키. 백엔드 normalize_chip 과 같은 규칙이다.
 *
 *  주의: JS 의 \W 는 ASCII 기준이라 한글까지 지운다(파이썬 \W 는 유니코드 인식).
 *  그대로 쓰면 순수 한글 칩의 키가 빈 문자열이 되어 통째로 버려진다.
 *  유니코드 문자·숫자만 남기도록 \p{L}\p{N} 을 쓴다. */
function chipKey(text) {
  return String(text || '').normalize('NFKC').replace(/[^\p{L}\p{N}]+/gu, '').toLowerCase();
}

/** 정규화 키 기준 중복 제거. 저장 단계에서 걸러도 표시 단계에서 한 번 더 막는다. */
function dedupeChips(items, exclude = []) {
  const blocked = new Set(exclude.map(chipKey).filter(Boolean));
  const seen = new Set();
  const out = [];
  for (const item of items || []) {
    const key = chipKey(item);
    if (!key || seen.has(key) || blocked.has(key)) continue;
    seen.add(key);
    out.push(String(item).trim());
  }
  return out;
}

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const diffMin = Math.floor((Date.now() - d.getTime()) / 60000);
  if (diffMin < 1) return '방금';
  if (diffMin < 60) return `${diffMin}분 전`;
  if (diffMin < 60 * 24) return `${Math.floor(diffMin / 60)}시간 전`;
  const p = (n) => String(n).padStart(2, '0');
  return `${String(d.getFullYear()).slice(2)}.${p(d.getMonth() + 1)}.${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function formatPrice(value, kind) {
  const digits = kind === 'fx' ? 2 : 0;
  return Number(value).toLocaleString('ko-KR', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

async function getJSON(path) {
  const res = await fetch(API + path, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
}

/* ── URL 쿼리스트링 동기화 (F6.1a) ──────────────────────────────── */

function readStateFromURL() {
  const p = new URLSearchParams(location.search);
  const load = (key, target) => {
    const raw = p.get(key);
    if (raw) raw.split(',').filter(Boolean).forEach((v) => target.add(v));
  };
  load('group', state.group);
  load('cat', state.cat);
  load('press', state.press);
  state.period = p.get('period') || 'all';
  state.q = p.get('q') || '';
}

function writeStateToURL() {
  const p = new URLSearchParams();
  if (state.group.size) p.set('group', [...state.group].join(','));
  if (state.cat.size) p.set('cat', [...state.cat].join(','));
  if (state.press.size) p.set('press', [...state.press].join(','));
  if (state.period !== 'all') p.set('period', state.period);
  if (state.q) p.set('q', state.q);
  const qs = p.toString();
  history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
}

/* ── 시세 티커 (F9.3) ───────────────────────────────────────────── */

async function loadQuotes() {
  const list = $('tickerList');
  try {
    const data = await getJSON('/api/quotes');
    if (!data.items.length) {
      list.replaceChildren(el('li', 'ticker-empty', '시세 데이터가 아직 없습니다'));
      return;
    }
    list.replaceChildren(...data.items.map((q) => {
      const li = el('li');
      li.append(el('span', 'tk-label', q.label));
      li.append(el('span', 'tk-price', formatPrice(q.price, q.kind)));

      const rate = q.change_rate;
      if (rate != null) {
        const dir = rate > 0 ? 'up' : rate < 0 ? 'down' : 'flat';
        const mark = rate > 0 ? '▲' : rate < 0 ? '▼' : '−';
        li.append(el('span', `tk-rate ${dir}`, `${mark} ${Math.abs(rate).toFixed(2)}%`));
      }
      // 15분 이상 낡은 값은 '지연'으로 표시한다. 값 자체는 지우지 않는다. (F9.2)
      if (q.stale) li.append(el('span', 'tk-stale', '지연'));
      return li;
    }));

    const newest = data.items
      .map((q) => q.fetched_at).filter(Boolean).sort().pop();
    $('tickerTime').textContent = newest ? `갱신 ${formatDate(newest)}` : '';
  } catch (err) {
    // 조회 실패 시 화면을 비우지 않는다. 직전 값이 그대로 남는다. (F9.2)
    console.warn('시세 조회 실패', err);
    if (!list.children.length || list.querySelector('.ticker-empty')) {
      list.replaceChildren(el('li', 'ticker-empty', '시세를 불러오지 못했습니다'));
    }
  }
}

/* ── 통계 바 ────────────────────────────────────────────────────── */

async function loadStats() {
  try {
    const s = await getJSON('/api/stats');
    const cards = [
      ['전체 기사', s.total.toLocaleString('ko-KR'), false],
      ['오늘 수집', s.today.toLocaleString('ko-KR'), false],
      ['최근 수집', s.last_collected_at ? formatDate(s.last_collected_at) : '—', true],
      ['분석 대기', s.analysis_pending.toLocaleString('ko-KR'), false],
      ['발송 실패', s.notify_failed.toLocaleString('ko-KR'), false],
    ];
    $('stats').replaceChildren(...cards.map(([label, value, small]) => {
      const box = el('div', 'stat');
      box.append(el('div', 'stat-label', label));
      box.append(el('div', `stat-value${small ? ' small' : ''}`, value));
      return box;
    }));
  } catch (err) {
    console.warn('통계 조회 실패', err);
  }
}

/* ── 필터 UI (F6.1a) ────────────────────────────────────────────── */

function renderChipGroup(container, values, selected, onToggle, single = false) {
  container.replaceChildren(...values.map((v) => {
    const key = typeof v === 'string' ? v : v.key;
    const label = typeof v === 'string' ? v : v.label;
    const btn = el('button', 'chip', label);
    btn.type = 'button';
    btn.setAttribute('aria-pressed', String(single ? selected === key : selected.has(key)));
    btn.addEventListener('click', () => onToggle(key));
    return btn;
  }));
}

async function loadFilters() {
  let data;
  try {
    data = await getJSON('/api/filters');
  } catch (err) {
    console.warn('필터 조회 실패', err);
    return;
  }

  renderChipGroup($('periodChips'), data.periods, state.period, (key) => {
    // 기간은 구간이 배타적이므로 단일 선택이다.
    state.period = key;
    refresh(true);
  }, true);

  const multi = [
    ['groupChips', data.groups, state.group],
    ['catChips', data.categories, state.cat],
    ['pressChips', data.presses, state.press],
  ];
  for (const [id, values, set] of multi) {
    renderChipGroup($(id), values, set, (key) => {
      // 재클릭하면 해제된다. (F6.1a)
      if (set.has(key)) set.delete(key); else set.add(key);
      refresh(true);
    });
  }
}

function syncChipStates() {
  const sync = (id, selected, single = false) => {
    for (const btn of $(id).children) {
      const label = btn.textContent;
      const on = single ? false : selected.has(label);
      btn.setAttribute('aria-pressed', String(on));
    }
  };
  sync('groupChips', state.group);
  sync('catChips', state.cat);
  sync('pressChips', state.press);
  // 기간은 key 와 label 이 다르므로 인덱스로 맞춘다.
  const periodKeys = ['today', '7d', '30d', 'all'];
  [...$('periodChips').children].forEach((btn, i) => {
    btn.setAttribute('aria-pressed', String(periodKeys[i] === state.period));
  });
}

/* ── 카드 렌더링 (F6.2) ─────────────────────────────────────────── */

function buildCard(item) {
  const card = el('article', 'card');

  /* 상단 메타 */
  const top = el('div', 'card-top');
  top.append(el('span', 'card-date', formatDate(item.published_at)));

  const right = el('div', 'card-top-right');
  if (item.swot) right.append(buildSwotBadge(item.swot));
  if (item.press_name) right.append(el('span', 'press-badge', item.press_name));

  // ↗ 버튼 — 이 기사 요약을 텔레그램으로 전송한다. (원문은 제목·썸네일 클릭)
  const send = el('button', 'icon-btn', '↗');
  send.type = 'button';
  send.title = '텔레그램으로 요약 전송';
  send.addEventListener('click', () => shareToTelegram(item.id, send));
  right.append(send);

  const star = el('button', 'icon-btn', '☆');
  star.type = 'button';
  star.title = '즐겨찾기';
  star.addEventListener('click', () => {
    const on = star.classList.toggle('on');
    star.textContent = on ? '★' : '☆';
    saveFavorite(item.id, on);
    if (!on && favView) card.remove();   // 즐겨찾기 화면에서 해제하면 즉시 제거
  });
  if (isFavorite(item.id)) { star.classList.add('on'); star.textContent = '★'; }
  right.append(star);
  top.append(right);
  card.append(top);

  /* 썸네일 — 클릭하면 원문. 없거나 로딩 실패 시 그룹사 로고 배지로 대체한다 */
  if (item.thumbnail_url) {
    const thumb = el('div', 'card-thumb');
    const img = el('img');
    img.src = item.thumbnail_url;
    img.alt = '';
    img.loading = 'lazy';
    img.decoding = 'async';   // 디코딩을 메인 스레드에서 떼어내 스크롤 끊김을 줄인다
    img.addEventListener('error', () => thumb.replaceWith(buildLogoThumb(item)), { once: true });
    if (item.url) {
      const a = el('a');
      a.href = item.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.title = '원문 열기';
      a.append(img);
      thumb.append(a);
    } else {
      thumb.append(img);
    }
    card.append(thumb);
  } else {
    card.append(buildLogoThumb(item));
  }

  /* 제목 */
  const h3 = el('h3', 'card-title');
  if (item.url) {
    const a = el('a', null, item.title);
    a.href = item.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    h3.append(a);
  } else {
    h3.textContent = item.title;
  }
  card.append(h3);

  /* 요약 — '[언론사, 기자]' 머리표 + 본문 (F4.1) */
  if (item.summary_text) {
    const p = el('p', 'card-summary');
    if (item.summary_header) {
      p.append(el('span', 'summary-head', item.summary_header + ' '));
    }
    p.append(document.createTextNode(item.summary_text));
    card.append(p);
  }

  /* 포스코 관점 — 요약과 분리해 표시한다 (F4.1) */
  if (item.perspective_text) {
    const box = el('div', 'card-perspective');
    box.append(el('strong', null, '포스코 관점: '));
    box.append(document.createTextNode(item.perspective_text));
    card.append(box);
  }

  /* 기자 · 출처 */
  const meta = el('div', 'card-meta');
  if (item.author) meta.append(el('span', null, `✎ ${item.author}`));
  meta.append(el('span', 'card-source', 'SUPABASE'));
  card.append(meta);

  /* 칩 행 — 그룹사(남색) → 감성·중요도 → 카테고리 → 키워드. 각각 독립 칩. (F6.2b) */
  const chips = el('div', 'card-chips');
  const groups = dedupeChips(item.group_companies);
  // 그룹사 필터가 켜져 있으면 그 그룹사를 앞에 세운다 (필터 결과와 칩 순서 일치).
  const activeGroupKeys = new Set([...state.group].map(chipKey));
  if (activeGroupKeys.size) {
    groups.sort((a, b) =>
      (activeGroupKeys.has(chipKey(b)) ? 1 : 0) - (activeGroupKeys.has(chipKey(a)) ? 1 : 0));
  }
  const categories = dedupeChips(item.categories || []);
  const keywords = dedupeChips(item.keywords || []);

  // ① 그룹사(회사명) — 감성·점수와 섞지 않고 별도 칩으로 낸다.
  groups.forEach((g) => chips.append(el('span', 'tag tag-group', g)));

  // ② 감성 · 중요도 — 회사명 없이 독립.
  const cls = item.sentiment === '긍정' ? 'pos' : item.sentiment === '부정' ? 'neg' : 'neu';
  const senti = item.sentiment || '중립';
  chips.append(el('span', `tag tag-senti ${cls}`, `${senti} · ${item.importance_score}`));

  if (item.is_backfill) chips.append(el('span', 'tag tag-backfill', '지연 수집'));
  // 아직 LLM 분석 전이면 요약·키워드가 없다. 빈 카드처럼 보이지 않게 상태를 알린다.
  if (!item.summary_text) chips.append(el('span', 'tag tag-pending', '분석 대기'));
  // ③ 카테고리 → ④ 키워드
  categories.forEach((c) => chips.append(el('span', 'tag tag-cat', c)));
  keywords.forEach((k) => chips.append(el('span', 'tag tag-key', k)));
  card.append(chips);

  return card;
}

/* ── 텔레그램 공유 ─────────────────────────────────────────────── */

async function shareToTelegram(articleId, btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const res = await fetch(`${API}/api/articles/${articleId}/telegram`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    btn.textContent = data.ok ? '✓' : '✗';
    btn.title = data.ok ? '전송했습니다' : (data.error || '전송 실패');
  } catch {
    btn.textContent = '✗';
    btn.title = '서버에 연결하지 못했습니다';
  }
  setTimeout(() => {
    btn.textContent = original;
    btn.title = '텔레그램으로 요약 전송';
    btn.disabled = false;
  }, 2500);
}

/* ── 마스터 패널 ───────────────────────────────────────────────── */

let masterKeywords = [];
let policyKeywords = [];
let policyRequired = [];
let thTimer;

function masterAuth() {
  try {
    const a = JSON.parse(localStorage.getItem('pfm.master') || 'null');
    if (a && a.token && a.exp > Date.now()) return a.token;
  } catch { /* noop */ }
  return null;
}
function saveMasterAuth(token, ttlHours) {
  try {
    localStorage.setItem('pfm.master',
      JSON.stringify({ token, exp: Date.now() + (ttlHours || 24) * 3600 * 1000 }));
  } catch { /* noop */ }
}
function clearMasterAuth() { try { localStorage.removeItem('pfm.master'); } catch { /* noop */ } }

async function masterFetch(path, opts = {}) {
  const res = await fetch(API + path, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      'X-Master-Token': masterAuth() || '',
      ...(opts.headers || {}),
    },
  });
  if (res.status === 401) { clearMasterAuth(); showMasterLogin(); throw new Error('unauthorized'); }
  return res;
}

function masterMsg(kind, text) {
  const m = $('masterMsg');
  m.className = `url-add-msg ${kind}`;
  m.textContent = text;
  m.hidden = false;
  setTimeout(() => { m.hidden = true; }, 3000);
}

function openMaster() {
  $('masterModal').hidden = false;
  if (masterAuth()) showMasterPanel(); else showMasterLogin();
}
function closeMaster() { $('masterModal').hidden = true; }

function showMasterLogin() {
  $('masterLogin').hidden = false;
  $('masterPanel').hidden = true;
  $('masterPw').value = '';
  $('masterLoginMsg').hidden = true;
}
async function showMasterPanel() {
  $('masterLogin').hidden = true;
  $('masterPanel').hidden = false;
  await loadMasterSettings();
}

async function loadMasterSettings() {
  try {
    const d = await (await masterFetch('/api/master/settings')).json();
    $('thRange').value = d.threshold;
    $('thVal').textContent = d.threshold;
    $('thRec').textContent = d.recommended_min;
    $('webPwNow').textContent = d.web_password || '(미설정)';
    $('notifyPolicy').checked = !!d.notify_policy;
    masterKeywords = d.keywords || [];
    policyKeywords = d.policy_keywords || [];
    policyRequired = d.policy_required || [];
    renderKwList();
    renderPolicyKwList();
    renderPolicyReqList();
  } catch (e) {
    if (e.message !== 'unauthorized') masterMsg('err', '설정을 불러오지 못했습니다.');
  }
}

/** 칩 목록 렌더 — 삭제 버튼은 arr 에서 빼고 save 콜백을 부른다. */
function renderChipEditor(container, arr, save) {
  $(container).replaceChildren(...arr.map((kw) => {
    const chip = el('span', 'kw-chip', kw);
    const x = el('button', null, '×');
    x.type = 'button';
    x.addEventListener('click', () => save(arr.filter((k) => k !== kw)));
    chip.append(x);
    return chip;
  }));
}

function renderKwList() { renderChipEditor('kwList', masterKeywords, (next) => { masterKeywords = next; renderKwList(); saveKw('keywords', masterKeywords); }); }
function renderPolicyKwList() { renderChipEditor('policyKwList', policyKeywords, (next) => { policyKeywords = next; renderPolicyKwList(); saveKw('policy_keywords', policyKeywords); }); }
function renderPolicyReqList() { renderChipEditor('policyReqList', policyRequired, (next) => { policyRequired = next; renderPolicyReqList(); saveKw('policy_required', policyRequired); }); }

async function saveKw(field, arr) {
  try {
    await masterFetch('/api/master/settings',
      { method: 'POST', body: JSON.stringify({ [field]: arr }) });
    masterMsg('ok', '저장했습니다.');
  } catch (e) {
    if (e.message !== 'unauthorized') masterMsg('err', '저장에 실패했습니다.');
  }
}

function initMaster() {
  $('masterBtn').addEventListener('click', openMaster);
  $('masterClose').addEventListener('click', closeMaster);
  $('masterModal').addEventListener('click', (e) => {
    if (e.target === $('masterModal')) closeMaster();
  });

  $('masterLogin').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const res = await fetch(API + '/api/master/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: $('masterPw').value }),
      });
      const d = await res.json();
      if (!d.ok) { $('masterLoginMsg').textContent = d.error || '로그인 실패'; $('masterLoginMsg').hidden = false; return; }
      saveMasterAuth(d.token, d.ttl_hours);
      showMasterPanel();
    } catch {
      $('masterLoginMsg').textContent = '서버에 연결하지 못했습니다.';
      $('masterLoginMsg').hidden = false;
    }
  });

  $('thRange').addEventListener('input', (e) => {
    $('thVal').textContent = e.target.value;
    clearTimeout(thTimer);
    thTimer = setTimeout(async () => {
      try {
        await masterFetch('/api/master/settings',
          { method: 'POST', body: JSON.stringify({ threshold: Number(e.target.value) }) });
        masterMsg('ok', `임계값 ${e.target.value} 저장`);
      } catch (err) { if (err.message !== 'unauthorized') masterMsg('err', '저장 실패'); }
    }, 400);
  });

  $('notifyPolicy').addEventListener('change', async (e) => {
    try {
      await masterFetch('/api/master/settings',
        { method: 'POST', body: JSON.stringify({ notify_policy: e.target.checked }) });
      masterMsg('ok', e.target.checked ? '정책브리핑 기사 알림 켬' : '정책브리핑 기사 알림 끔');
    } catch (err) { if (err.message !== 'unauthorized') masterMsg('err', '저장 실패'); }
  });

  const wireKwAdd = (btnId, inputId, arrGetter, arrSetter, render, field) => {
    const add = () => {
      const v = $(inputId).value.trim();
      if (v && !arrGetter().includes(v)) {
        arrSetter([...arrGetter(), v]);
        $(inputId).value = '';
        render();
        saveKw(field, arrGetter());
      }
    };
    $(btnId).addEventListener('click', add);
    $(inputId).addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
  };
  wireKwAdd('kwAdd', 'kwNew', () => masterKeywords, (a) => { masterKeywords = a; }, renderKwList, 'keywords');
  wireKwAdd('policyKwAdd', 'policyKwNew', () => policyKeywords, (a) => { policyKeywords = a; }, renderPolicyKwList, 'policy_keywords');
  wireKwAdd('policyReqAdd', 'policyReqNew', () => policyRequired, (a) => { policyRequired = a; }, renderPolicyReqList, 'policy_required');

  $('pwMasterForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const d = await (await masterFetch('/api/master/password', {
        method: 'POST',
        body: JSON.stringify({
          target: 'master',
          current_password: $('pwMasterCur').value,
          new_password: $('pwMasterNew').value,
        }),
      })).json();
      if (d.ok) { masterMsg('ok', '마스터 비밀번호를 변경했습니다.'); e.target.reset(); }
      else masterMsg('err', d.error || '변경 실패');
    } catch (err) { if (err.message !== 'unauthorized') masterMsg('err', '변경 실패'); }
  });

  $('pwWebForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const d = await (await masterFetch('/api/master/password', {
        method: 'POST',
        body: JSON.stringify({ target: 'web', new_password: $('pwWebNew').value }),
      })).json();
      if (d.ok) {
        masterMsg('ok', '웹페이지 비밀번호를 저장했습니다.');
        e.target.reset();
        loadMasterSettings();       // 현재 비밀번호 표시 갱신
      } else masterMsg('err', d.error || '변경 실패');
    } catch (err) { if (err.message !== 'unauthorized') masterMsg('err', '변경 실패'); }
  });
}

/* ── 로고 썸네일 (사진이 없을 때 그룹사 로고 배지로 대체) ───────── */

/* 그룹사별 배경색. 각사 브랜드 톤에 맞춘 근사값이다. */
const GROUP_BRAND = {
  '포스코홀딩스':     '#0b3a86',
  '포스코퓨처엠':     '#16337A',
  '포스코DX':         '#0057a8',
  '포스코인터내셔널': '#0e2a5e',
  '포스코이앤씨':     '#0f7b52',
  '포스코':           '#0b3a86',
};

function buildLogoThumb(item) {
  const groups = dedupeChips(item.group_companies);
  const name = groups[0] || 'POSCO';
  const wrap = el('div', 'card-thumb card-thumb-logo');
  wrap.style.background = GROUP_BRAND[name] || '#0b3a86';

  const badge = el('div', 'logo-badge');
  badge.append(el('span', 'logo-mark', '✦'));
  badge.append(el('span', 'logo-name', name));
  wrap.append(badge);
  return wrap;
}

/* ── SWOT 배지 및 툴팁 (F6.2a) ──────────────────────────────────── */

const SWOT_LABELS = { s: '강점 S', w: '약점 W', o: '기회 O', t: '위협 T' };

function buildSwotBadge(swot) {
  const badge = el('button', 'swot-badge');
  badge.type = 'button';
  badge.setAttribute('aria-label', `SWOT 종합 점수 ${swot.total}. 상세 보기`);
  badge.append(el('span', null, 'SWOT'));
  badge.append(el('b', null, String(swot.total)));

  // 호버 · 드래그 · 키보드 포커스 · 모바일 탭 모두에서 열린다.
  const show = () => showSwotTip(badge, swot);
  badge.addEventListener('mouseenter', show);
  badge.addEventListener('mousemove', show);
  badge.addEventListener('focus', show);
  badge.addEventListener('mouseleave', hideSwotTip);
  badge.addEventListener('blur', hideSwotTip);
  badge.addEventListener('click', (e) => {
    e.preventDefault();
    if ($('swotTip').hidden) show(); else hideSwotTip();
  });
  return badge;
}

function showSwotTip(anchor, swot) {
  const tip = $('swotTip');
  tip.replaceChildren();
  tip.append(el('h4', null, `SWOT 종합 ${swot.total} / 100`));

  const dl = el('dl');
  for (const key of ['s', 'w', 'o', 't']) {
    const node = swot[key] || { score: 0, text: '해당 없음' };
    const dt = el('dt', null, SWOT_LABELS[key] + ' ');
    dt.append(el('span', null, String(node.score)));
    dl.append(dt);
    dl.append(el('dd', null, node.text || '해당 없음'));
  }
  tip.append(dl);
  tip.hidden = false;

  // 뷰포트를 벗어나면 반대편으로 뒤집는다.
  const rect = anchor.getBoundingClientRect();
  const tipRect = tip.getBoundingClientRect();
  let left = rect.left + window.scrollX;
  let top = rect.bottom + window.scrollY + 8;
  if (left + tipRect.width > window.innerWidth - 12) {
    left = Math.max(12, window.innerWidth - tipRect.width - 12);
  }
  if (rect.bottom + tipRect.height + 20 > window.innerHeight) {
    top = rect.top + window.scrollY - tipRect.height - 8;
  }
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function hideSwotTip() { $('swotTip').hidden = true; }

document.addEventListener('scroll', hideSwotTip, { passive: true });

/* ── 즐겨찾기 (브라우저별 로컬 저장) ────────────────────────────── */

function favorites() {
  try { return new Set(JSON.parse(localStorage.getItem('pfm.fav') || '[]')); }
  catch { return new Set(); }
}
function isFavorite(id) { return favorites().has(id); }
function saveFavorite(id, on) {
  try {
    const set = favorites();
    if (on) set.add(id); else set.delete(id);
    localStorage.setItem('pfm.fav', JSON.stringify([...set]));
  } catch { /* 사생활 보호 모드 등에서 실패할 수 있다. 무시한다. */ }
}

/* ── 즐겨찾기 보기 ─────────────────────────────────────────────── */

let favView = false;

function toggleFavView() {
  favView = !favView;
  $('favTab').setAttribute('aria-pressed', String(favView));
  $('favTab').textContent = favView ? '← 전체 기사' : '★ 즐겨찾기';
  document.querySelector('.filters').hidden = favView;
  if (favView) renderFavorites(); else refresh(true);
}

async function renderFavorites() {
  const ids = [...favorites()];
  $('resultCount').textContent = `${ids.length}건`;
  $('loadMore').hidden = true;
  $('emptyMsg').hidden = true;
  if (!ids.length) {
    $('grid').replaceChildren(el('p', 'empty', '즐겨찾기한 기사가 없습니다.'));
    return;
  }
  $('grid').replaceChildren(...Array.from(
    { length: Math.min(ids.length, 3) }, () => el('div', 'skeleton')));
  const cards = await Promise.all(
    ids.map((id) => getJSON(`/api/articles/${id}`).catch(() => null)));
  const nodes = cards.filter(Boolean).map(buildCard);
  $('grid').replaceChildren(...(nodes.length
    ? nodes : [el('p', 'empty', '즐겨찾기 기사를 불러오지 못했습니다.')]));
}

/* ── 목록 로드 ──────────────────────────────────────────────────── */

function buildQuery() {
  const p = new URLSearchParams();
  if (state.group.size) p.set('group', [...state.group].join(','));
  if (state.cat.size) p.set('cat', [...state.cat].join(','));
  if (state.press.size) p.set('press', [...state.press].join(','));
  if (state.period !== 'all') p.set('period', state.period);
  if (state.q) p.set('q', state.q);
  p.set('page', String(state.page));
  p.set('size', String(PAGE_SIZE));
  return p.toString();
}

async function refresh(reset) {
  // 즐겨찾기 화면일 때는 일반 목록이 덮어쓰지 않게 막는다.
  // (필터를 숨기는 것만으로는 방어가 약하다 — 단축키·코드 변경에 취약)
  if (favView || state.loading) return;
  state.loading = true;
  if (reset) {
    state.page = 1;
    $('grid').replaceChildren(...Array.from({ length: 3 }, () => el('div', 'skeleton')));
  }
  writeStateToURL();
  syncChipStates();

  try {
    const data = await getJSON('/api/articles?' + buildQuery());
    state.total = data.total;
    const cards = data.items.map(buildCard);
    if (reset) $('grid').replaceChildren(...cards);
    else $('grid').append(...cards);

    $('resultCount').textContent = `${data.total.toLocaleString('ko-KR')}건`;
    $('emptyMsg').hidden = data.total !== 0;
    $('loadMore').hidden = state.page * PAGE_SIZE >= data.total;
    updateActiveFilterCount();
  } catch (err) {
    console.error('기사 조회 실패', err);
    $('grid').replaceChildren(el('p', 'empty', '기사를 불러오지 못했습니다.'));
  } finally {
    state.loading = false;
  }
}

/* ── 필터 접기/펼치기 ──────────────────────────────────────────── */

function activeFilterCount() {
  return state.group.size + state.cat.size + state.press.size
    + (state.period !== 'all' ? 1 : 0) + (state.q ? 1 : 0);
}

const PERIOD_LABEL = { today: '오늘', '7d': '7일', '30d': '30일', all: '전체' };

function updateActiveFilterCount() {
  const n = activeFilterCount();
  const badge = $('activeFilterCount');
  badge.textContent = `필터 ${n}`;
  badge.hidden = n === 0;

  // 걸린 필터를 "기간: 7일 · 그룹사: A, B · 카테고리: 전체 …" 형태로 요약한다.
  const box = $('filterSummary');
  box.replaceChildren();
  if (n === 0) { box.hidden = true; return; }
  const seg = (label, val) => {
    if (box.childNodes.length) box.append(document.createTextNode('   ·   '));
    box.append(el('b', null, `${label}: `));
    box.append(document.createTextNode(val));
  };
  seg('기간', PERIOD_LABEL[state.period] || '전체');
  seg('그룹사', state.group.size ? [...state.group].join(', ') : '전체');
  seg('카테고리', state.cat.size ? [...state.cat].join(', ') : '전체');
  seg('언론사', state.press.size ? [...state.press].join(', ') : '전체');
  if (state.q) seg('검색', state.q);
  box.hidden = false;
}

function setFilterCollapsed(collapsed) {
  $('filterBody').hidden = collapsed;
  $('filterToggle').setAttribute('aria-expanded', String(!collapsed));
  try { localStorage.setItem('pfm.filterCollapsed', collapsed ? '1' : '0'); } catch { /* noop */ }
}

/* ── 수동 URL 등록 (F8) ────────────────────────────────────────── */

function urlMsg(kind, text) {
  const m = $('urlAddMsg');
  m.className = `url-add-msg ${kind}`;
  m.textContent = text;
  m.hidden = false;
}

async function submitUrl(e) {
  e.preventDefault();
  const input = $('urlAddInput');
  const btn = $('urlAddBtn');
  const url = input.value.trim();
  if (!url) return;

  btn.disabled = true;
  urlMsg('info', '기사를 분석하고 있습니다… (10~20초 걸릴 수 있어요)');

  try {
    const res = await fetch(API + '/api/analyze-url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();

    if (!data.ok) {
      urlMsg('err', data.error || '분석에 실패했습니다.');
      return;
    }
    if (data.already) {
      urlMsg('info', '이미 등록된 기사입니다. 아래 카드를 확인하세요.');
    } else {
      urlMsg('ok', '카드를 만들었습니다. 목록 맨 위에도 추가됩니다.');
      input.value = '';
    }
    $('urlAddResult').replaceChildren(buildCard(data.card));
    // 새로 만든 카드를 기존 목록 맨 앞에도 끼워 넣는다.
    if (!data.already) $('grid').prepend(buildCard(data.card));
  } catch (err) {
    console.error('URL 분석 실패', err);
    urlMsg('err', '서버에 연결하지 못했습니다.');
  } finally {
    btn.disabled = false;
  }
}

/* ── 초기화 ─────────────────────────────────────────────────────── */

function init() {
  readStateFromURL();
  $('searchInput').value = state.q;

  // 마지막으로 접었는지 기억한다. 활성 필터가 있으면 펼친 상태로 시작한다.
  let collapsed = false;
  try { collapsed = localStorage.getItem('pfm.filterCollapsed') === '1'; } catch { /* noop */ }
  if (activeFilterCount() > 0) collapsed = false;
  setFilterCollapsed(collapsed);
  updateActiveFilterCount();

  $('filterToggle').addEventListener('click', () => {
    setFilterCollapsed(!$('filterBody').hidden);   // 보이는 중이면 접고, 접혀 있으면 편다
  });

  let timer;
  $('searchInput').addEventListener('input', (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.q = e.target.value.trim(); refresh(true); }, 300);
  });

  $('clearAll').addEventListener('click', () => {
    state.group.clear(); state.cat.clear(); state.press.clear();
    state.period = 'all'; state.q = '';
    $('searchInput').value = '';
    refresh(true);
  });

  $('loadMore').addEventListener('click', () => { state.page += 1; refresh(false); });

  $('favTab').addEventListener('click', toggleFavView);
  initMaster();

  // 헤더 Telegram 버튼 — 봇 대화방 주소를 받아 링크를 채운다(실패하면 숨김).
  getJSON('/api/telegram-link').then((d) => {
    if (!d.ok || !d.url) return;
    const a = $('tgLink');
    a.href = d.url;
    a.textContent = d.kind === 'channel' ? '✈ Telegram 채널' : '✈ Telegram';
    a.hidden = false;
  }).catch(() => { /* 텔레그램 미설정 — 버튼은 숨긴 채로 둔다 */ });

  $('urlAddForm').addEventListener('submit', submitUrl);

  loadFilters().then(() => refresh(true));
  loadStats();
  loadQuotes();

  // 시세는 60초, 통계는 5분 주기로 갱신한다. (F9.2)
  setInterval(loadQuotes, 60_000);
  setInterval(loadStats, 300_000);
}

document.addEventListener('DOMContentLoaded', init);
