/* =====================================================================
   P-FM NEWS — 프론트엔드 (PRD F6)

   원칙
     1. 이 파일은 backend API 만 호출한다. 외부 API·DB 에 직접 접근하지 않는다.
     2. API 키를 이 파일에 두지 않는다. (§6 보안)
     3. 필터는 같은 그룹 안 OR / 다른 그룹 사이 AND. (F6.1a)
     4. 칩은 렌더링 직전 한 번 더 중복 제거한다. (F6.2b)
   ===================================================================== */

const API = '';                 // 같은 오리진에서 서빙된다
const PAGE_SIZE = 9;             // 포토카드 페이지당 개수 (3열 × 3행)

/* 선택 상태 — 기간만 단일 선택, 나머지는 다중 선택 */
const state = {
  group: new Set(),
  cat: new Set(),
  press: new Set(),
  period: 'all',
  sort: 'recent',   // 'recent'(발행일 최신순, 기본) | 'score'(직접 등록·중요도순)
  q: '',
  // 상세 검색 — 입력된 칸끼리 AND. 키는 API 파라미터(s_title 등)와 같다.
  adv: { s_title: '', s_body: '', s_press: '', s_author: '' },
  page: 1,
  total: 0,
  loading: false,
};

/* 상세 검색 칸 — [상태 키, 입력칸 id, 요약 라벨] */
const ADV_FIELDS = [
  ['s_title', 'advTitle', '제목'],
  ['s_body', 'advBody', '본문'],
  ['s_press', 'advPress', '언론사'],
  ['s_author', 'advAuthor', '기자'],
];
const advActive = () => ADV_FIELDS.filter(([k]) => state.adv[k]);

/* 정렬 옵션 — 백엔드 /api/articles?sort= 값과 라벨. */
const SORT_OPTIONS = [
  { key: 'recent', label: '최신순' },
  { key: 'score', label: '중요도순' },
];

const $ = (id) => document.getElementById(id);
const EXCERPT_FOLD_CHARS = 200;  // 포스코퓨처엠 언급 발췌가 이보다 길면 접어서 보여 준다
const PFM_MENTION_RE = /(포스코\s*퓨처엠|POSCO\s*(?:FUTURE\s*M|퓨처엠)|퓨처엠)/i;  // 발췌에서 강조할 언급
const PEOPLE_FOLD_LINES = 10;   // 인사·부고 카드는 이 줄 수까지만 접힌 채로 보여 준다

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

async function getJSON(path, signal) {
  const res = await fetch(API + path, { headers: { Accept: 'application/json' }, signal });
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
  state.sort = p.get('sort') === 'score' ? 'score' : 'recent';
  state.q = p.get('q') || '';
  ADV_FIELDS.forEach(([k]) => { state.adv[k] = p.get(k) || ''; });
  state.page = Math.max(1, parseInt(p.get('page') || '1', 10) || 1);
}

function writeStateToURL() {
  const p = new URLSearchParams();
  if (state.group.size) p.set('group', [...state.group].join(','));
  if (state.cat.size) p.set('cat', [...state.cat].join(','));
  if (state.press.size) p.set('press', [...state.press].join(','));
  if (state.period !== 'all') p.set('period', state.period);
  if (state.sort !== 'recent') p.set('sort', state.sort);
  if (state.q) p.set('q', state.q);
  advActive().forEach(([k]) => p.set(k, state.adv[k]));
  if (state.page > 1) p.set('page', String(state.page));
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
    // detail: 눌렀을 때 열어 볼 목록의 종류 (0건이면 볼 게 없으니 버튼으로 만들지 않는다)
    const cards = [
      ['전체 기사', s.total.toLocaleString('ko-KR'), false, null],
      ['오늘 수집', s.today.toLocaleString('ko-KR'), false, null],
      ['최근 수집', s.last_collected_at ? formatDate(s.last_collected_at) : '—', true, null],
      ['분석 대기', s.analysis_pending.toLocaleString('ko-KR'), false,
        s.analysis_pending > 0 ? 'analysis' : null],
      ['발송 실패', s.notify_failed.toLocaleString('ko-KR'), false,
        s.notify_failed > 0 ? 'notify' : null],
      // 봇으로 나간 메시지 전문 — 0건이어도 눌러서 확인할 수 있게 항상 버튼으로 둔다.
      ['발송 로그', (s.telegram_log_total ?? 0).toLocaleString('ko-KR'), false, 'tglog'],
    ];
    $('stats').replaceChildren(...cards.map(([label, value, small, detail]) => {
      const box = el(detail ? 'button' : 'div', 'stat');
      if (detail) {
        box.type = 'button';
        box.title = `${label} ${value}건 — 눌러서 목록 보기`;
        box.addEventListener('click', () => openStatDetail(detail));
      }
      box.append(el('div', 'stat-label', label));
      box.append(el('div', `stat-value${small ? ' small' : ''}`, value));
      return box;
    }));
  } catch (err) {
    console.warn('통계 조회 실패', err);
  }
}

/* ── 통계 상세 (발송 실패 · 분석 대기) ──────────────────────────────
   숫자만 보여 주면 '무엇이 왜 그런지'를 알 수가 없다. 카드를 누르면
   해당 기사와 실패 원인을 그대로 펼쳐 준다. */

function stripTags(s) { return (s || '').replace(/<[^>]+>/g, ''); }

/* 발송 이유 문구 → 알약에 넣을 짧은 분류. 전체 문구는 카드 본문에 따로 나온다. */
function tglogShortKind(kind) {
  const k = kind || '기타';
  if (k.startsWith('URL 등록')) return 'URL 등록';
  if (k.includes('항상발송 키워드')) return '항상발송 키워드';
  if (k.includes('무조건 발송 점수')) return '무조건 점수';
  if (k.includes('임계값')) return '중요도 임계값';
  return k;   // '봇 응답' · '직접 전송' · '연결 테스트' · '자동 알림' 등 이미 짧음
}

const STAT_DETAIL = {
  notify: {
    title: '발송 실패',
    api: '/api/stats/notify-failed',
    desc: '텔레그램으로 보내지 못한 기사입니다. 카드 위 붉은 칸이 텔레그램이 돌려준 실제 원인입니다.',
    cards: true,
    render: (it) => {
      const wrap = el('div', 'detail-card');
      const f = it.fail || {};
      const banner = el('div', 'detail-card-fail');
      banner.append(el('b', null, '발송 실패: '));
      banner.append(document.createTextNode(f.error || '(원인이 기록되지 않았습니다)'));
      const sub = [f.created_at ? formatDate(f.created_at) : null,
                  `재시도 ${f.retry_count ?? 0}회`,
                  f.stuck ? '재시도 중단됨' : null].filter(Boolean);
      banner.append(el('div', 'detail-card-fail-sub', sub.join(' · ')));
      wrap.append(banner);
      wrap.append(buildCard(it));
      return wrap;
    },
  },
  analysis: {
    title: '분석 대기',
    api: '/api/stats/analysis-pending',
    desc: '본문은 받아 왔는데 요약·분석이 끝나지 않은 기사입니다. 다음 분석 주기에 처리됩니다.',
    cards: true,
    render: (it) => {
      const wrap = el('div', 'detail-card');
      const p = it.pending || {};
      const bits = [p.collected_at ? `수집 ${formatDate(p.collected_at)}` : null,
                    p.body_len != null ? `본문 ${p.body_len.toLocaleString('ko-KR')}자` : null,
                    p.summary_source || null].filter(Boolean);
      wrap.append(el('div', 'detail-card-note',
                     '분석 대기 중' + (bits.length ? ' · ' + bits.join(' · ') : '')));
      wrap.append(buildCard(it));
      return wrap;
    },
  },
  tglog: {
    title: '텔레그램 발송 로그',
    api: '/api/stats/telegram-log',
    desc: '봇으로 실제 나간 메시지입니다. "발송 이유"가 이 기사를 왜 보냈는지 — '
        + '항상발송 키워드 매칭인지, 중요도 임계값 초과인지 — 알려줍니다. 최근 100건.',
    render: (it) => {
      const box = el('div', `tglog-item${it.ok ? '' : ' tglog-item--fail'}`);
      const head = el('div', 'tglog-head');
      const kind = it.kind || '기타';
      // 긴 발송 이유 문구는 짧은 분류만 알약으로, 전체 문구는 아래 줄에 보여 준다.
      head.append(el('span', 'tglog-kind', tglogShortKind(kind)));
      head.append(el('span', `tglog-state tglog-state--${it.ok ? 'ok' : 'fail'}`, it.ok ? '성공' : '실패'));
      if (it.created_at) head.append(el('span', 'tglog-time', formatDate(it.created_at)));
      if (it.chat_id) head.append(el('span', 'tglog-chat', it.chat_id));
      box.append(head);
      const why = el('div', 'tglog-why');
      why.append(el('b', null, '발송 이유: '));
      why.append(document.createTextNode(kind));
      box.append(why);
      box.append(el('div', 'tglog-text', stripTags(it.text)));
      if (!it.ok && it.error) {
        const e = el('div', 'tglog-err');
        e.append(el('b', null, '원인: '));
        e.append(document.createTextNode(it.error));
        box.append(e);
      }
      return box;
    },
  },
};

async function openStatDetail(kind) {
  const spec = STAT_DETAIL[kind];
  if (!spec) return;
  $('detailTitle').textContent = spec.title;
  $('detailDesc').textContent = spec.desc;
  const list = $('detailList');
  list.classList.toggle('detail-list--cards', !!spec.cards);
  list.replaceChildren(el('div', 'detail-empty', '불러오는 중…'));
  $('detailModal').hidden = false;
  try {
    const data = await getJSON(spec.api);
    const items = data.items || [];
    list.replaceChildren(
      ...(items.length ? items.map(spec.render)
                       : [el('div', 'detail-empty', '해당하는 항목이 없습니다.')]));
  } catch (err) {
    list.replaceChildren(
      el('div', 'detail-empty', '목록을 불러오지 못했습니다: ' + err.message));
  }
}

function closeStatDetail() { $('detailModal').hidden = true; }

/* ── 필터 UI (F6.1a) ────────────────────────────────────────────── */

/* 언론사는 370곳이 넘어 전부 펼치면 필터가 화면을 다 먹는다. 기본은 접어 둔다. */
const PRESS_CHIP_LIMIT = 24;
let pressChipsExpanded = false;

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
  renderFilterChips(data);
}

function renderFilterChips(data) {
  renderChipGroup($('periodChips'), data.periods, state.period, (key) => {
    // 기간은 구간이 배타적이므로 단일 선택이다.
    state.period = key;
    refresh(true);
  }, true);

  renderChipGroup($('sortChips'), SORT_OPTIONS, state.sort, (key) => {
    // 정렬은 최신순/중요도순 중 하나만 — 단일 선택.
    state.sort = key;
    refresh(true);
  }, true);

  const multi = [
    ['groupChips', data.groups, state.group, 0],
    ['catChips', data.categories, state.cat, 0],
    // 언론사는 370곳이 넘는다. 전부 펼치면 필터만으로 화면을 채워 기사 카드가
    // 스크롤 밖으로 밀린다. 선택된 것 + 상위 일부만 보이고 나머지는 접는다.
    ['pressChips', data.presses, state.press, PRESS_CHIP_LIMIT],
  ];
  for (const [id, values, set, cap] of multi) {
    const onToggle = (key) => {
      // 재클릭하면 해제된다. (F6.1a)
      if (set.has(key)) set.delete(key); else set.add(key);
      refresh(true);
    };
    if (cap && values.length > cap && !pressChipsExpanded) {
      // 선택된 언론사는 접혀도 항상 보여야 해제할 수 있다.
      const picked = values.filter((v) => set.has(v));
      const rest = values.filter((v) => !set.has(v));
      const shown = [...picked, ...rest].slice(0, Math.max(cap, picked.length));
      renderChipGroup($(id), shown, set, onToggle);
      const more = el('button', 'chip chip-more', `+${values.length - shown.length}개 더`);
      more.type = 'button';
      more.addEventListener('click', () => { pressChipsExpanded = true; renderFilterChips(data); });
      $(id).append(more);
    } else {
      renderChipGroup($(id), values, set, onToggle);
      if (cap && values.length > cap) {
        const less = el('button', 'chip chip-more', '접기');
        less.type = 'button';
        less.addEventListener('click', () => { pressChipsExpanded = false; renderFilterChips(data); });
        $(id).append(less);
      }
    }
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
  // 정렬 칩도 인덱스로 맞춘다 (key ≠ label).
  [...$('sortChips').children].forEach((btn, i) => {
    btn.setAttribute('aria-pressed', String((SORT_OPTIONS[i] || {}).key === state.sort));
  });
}

/* ── 카드 렌더링 (F6.2) ─────────────────────────────────────────── */

/* 인사·부고 카드 본문 — 'ㆍ이름 · 직책 (소속·구분)' 줄에서 이름을 굵게, 출처 주소(http…)는 링크로 만든다. */
const URL_IN_TEXT_RE = /(https?:\/\/[^\s)]+)/g;
function appendLinked(parent, text) {
  text.split(URL_IN_TEXT_RE).forEach((chunk, i) => {
    if (i % 2 === 1) {
      const a = el('a', 'people-src', '출처');
      a.href = chunk; a.target = '_blank'; a.rel = 'noopener noreferrer';
      parent.append(a);
    } else if (chunk) {
      parent.append(document.createTextNode(chunk));
    }
  });
}
function renderPeopleLines(p, text) {
  const lines = String(text).split('\n');
  lines.forEach((ln, i) => {
    const m = /^ㆍ([^·(]+?)( · | \(|$)(.*)$/.exec(ln);
    const row = el('span', 'people-line');
    if (m && m[1].length <= 12) {
      row.append('ㆍ', el('b', 'people-name', m[1].trim()));
      appendLinked(row, ln.slice(1 + m[1].length));
    } else {
      appendLinked(row, ln);
    }
    p.append(row);
    if (i < lines.length - 1) p.append('\n');
  });
}

function buildCard(item) {
  const card = el('article', 'card');

  /* 상단 메타 */
  const top = el('div', 'card-top');
  top.append(el('span', 'card-date', formatDate(item.published_at)));

  const right = el('div', 'card-top-right');
  // 사용자가 URL 로 직접 등록한 기사임을 표시한다 (자동 수집분과 구분).
  if (item.manual) {
    const badge = el('span', 'manual-badge', '✍ 직접 등록');
    badge.title = '내가 URL 로 직접 등록한 기사입니다';
    right.append(badge);
  }
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
  /* 영어 기사는 제목을 한글로 번역해 저장하므로, 영어 원제는 작게 덧붙인다 */
  if (item.title_original) card.append(el('p', 'card-orig', `원제: ${item.title_original}`));

  /* 요약 — '[언론사, 기자]' 머리표 + 본문 (F4.1) */
  if (item.summary_text) {
    /* 인사·부고는 사람별 줄바꿈 구조라 개행을 살린다 */
    const isPeople = (item.categories || []).includes('인사·부고');
    const p = el('p', isPeople ? 'card-summary card-summary--people' : 'card-summary');
    if (item.summary_header) {
      p.append(el('span', 'summary-head', item.summary_header + (isPeople ? '\n' : ' ')));
    }
    if (isPeople) renderPeopleLines(p, item.summary_text);
    else p.append(document.createTextNode(item.summary_text));
    card.append(p);
    /* 인사가 수십 명이면(검사 인사 등) 카드가 너무 길어지므로 처음 10줄만 보이고 펼친다. 내용은 전부 있다. */
    const lineCount = item.summary_text.split('\n').filter((l) => l.trim()).length;
    if (isPeople && lineCount > PEOPLE_FOLD_LINES) {
      p.classList.add('is-folded');
      const more = el('button', 'people-more', `전체 ${lineCount}줄 펼치기 ▾`);
      more.type = 'button';
      more.addEventListener('click', () => {
        const folded = p.classList.toggle('is-folded');
        more.textContent = folded ? `전체 ${lineCount}줄 펼치기 ▾` : '접기 ▴';
      });
      card.append(more);
    }
  }

  /* 포스코퓨처엠 언급 발췌 — 본문 원문에서 언급 문장+앞뒤 문맥(약 4줄).
     언급이 없는 기사는 이 영역을 아예 그리지 않는다(대체 문구 없음). */
  if (item.pfm_excerpt) {
    const box = el('div', 'card-excerpt');
    box.append(el('strong', null, '포스코퓨처엠 언급'));
    const body = el('div', 'card-excerpt-text');
    const paras = item.pfm_excerpt.split('\n').filter((t) => t.trim());
    const hit = paras.map((t) => PFM_MENTION_RE.test(t));
    const first = Math.max(0, hit.indexOf(true));
    /* 문단 하나를 그리되 포스코퓨처엠 언급은 <mark>로 강조한다 */
    const paint = (t, idx) => {
      const d = el('div', 'excerpt-para');
      d.dataset.idx = idx;
      let last = 0; let m;
      const re = new RegExp(PFM_MENTION_RE.source, 'gi');
      while ((m = re.exec(t))) {
        if (m.index > last) d.append(document.createTextNode(t.slice(last, m.index)));
        d.append(el('mark', 'pfm-mark', m[0]));
        last = m.index + m[0].length;
      }
      if (last < t.length) d.append(document.createTextNode(t.slice(last)));
      return d;
    };
    paras.forEach((t, i) => body.append(paint(t, i)));
    box.append(body);
    /* 길면 '언급이 든 문단'만 남기고 접는다 — 언급이 가려지는 일이 없도록 앞뒤 문단만 숨긴다 */
    if (paras.length > 1 && item.pfm_excerpt.length > EXCERPT_FOLD_CHARS) {
      const apply = (folded) => {
        body.querySelectorAll('.excerpt-para').forEach((d) => {
          d.hidden = folded && Number(d.dataset.idx) !== first;
        });
        body.classList.toggle('is-folded', folded);
      };
      apply(true);
      const more = el('button', 'excerpt-more', '앞뒤 맥락 더보기 ▾');
      more.type = 'button';
      let folded = true;
      more.addEventListener('click', () => {
        folded = !folded;
        apply(folded);
        more.textContent = folded ? '앞뒤 맥락 더보기 ▾' : '접기 ▴';
      });
      box.append(more);
    }
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
  const sentiChip = el('span', `tag tag-senti ${cls}`, `${senti} · ${item.importance_score}`);
  attachSentiTip(sentiChip, item);   // 마우스를 올리면 '왜 긍정인지 · 왜 N점인지' 설명
  chips.append(sentiChip);

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
  if (!masterAuth()) { showMasterLogin(); return; }
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const res = await masterFetch(`/api/articles/${articleId}/telegram`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    btn.textContent = data.ok ? '✓' : '✗';
    btn.title = data.ok ? '전송했습니다' : (data.error || '전송 실패');
  } catch (err) {
    btn.textContent = '✗';
    btn.title = err.message === 'unauthorized' ? '마스터 인증이 필요합니다' : '서버에 연결하지 못했습니다';
  }
  setTimeout(() => {
    btn.textContent = original;
    btn.title = '텔레그램으로 요약 전송';
    btn.disabled = false;
  }, 2500);
}

/* ── 마스터 패널 ───────────────────────────────────────────────── */

let masterKeywords = [];
let excludeKeywords = [];
let policyKeywords = [];
let policyRequired = [];
let policyExclude = [];
let tradeKeywords = [];
let tradeRequired = [];
let tradeExclude = [];
let weeklyTo = [];       // 주간 레포트 수신자 이메일 목록
let scoreItems = [];       // 중요도 기본 항목 [{key,label,points,enabled}]
let scoreCustomItems = []; // 중요도 사용자 추가 항목 [{id,label,keywords,scope,points}]
let scoreCustomMax = 30;
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
function closeMaster() { $('masterModal').hidden = true; afterMasterLogin = null; }

// 로그인이 필요해 멈춘 동작(예: URL 등록). 로그인에 성공하면 이어서 실행한다.
let afterMasterLogin = null;

function showMasterLogin() {
  // 예전엔 창을 열지 않고 내부 상태만 바꿔서, 로그인 전에 '등록'을 누르면 아무 반응이 없었다.
  $('masterModal').hidden = false;
  $('masterLogin').hidden = false;
  $('masterPanel').hidden = true;
  $('masterPw').value = '';
  $('masterLoginMsg').hidden = true;
}
async function showMasterPanel() {
  $('masterLogin').hidden = true;
  $('masterPanel').hidden = false;
  await loadMasterSettings();
  loadCollectKeywords();
}

/* ── 수집 키워드 관리 (마스터 패널 8번, 2026-09-15) ───────────────────
   알림 필터 키워드(kwList 등)와는 완전히 별개 — 이건 구글·네이버 검색 자체를
   무엇으로 돌릴지 정하는 목록(keyword_sets)이다. */
let collectKwCategories = [];

async function loadCollectKeywords() {
  try {
    const d = await (await masterFetch('/api/master/keywords')).json();
    if (!d.ok) return;
    collectKwCategories = d.categories || [];
    const sel = $('kwCollectCategory');
    sel.replaceChildren(...collectKwCategories.map((c) => {
      const opt = document.createElement('option');
      opt.value = c; opt.textContent = c;
      return opt;
    }));
    renderCollectKeywordGroups(d.items || [], d.always_category);
  } catch (e) {
    if (e.message !== 'unauthorized') {
      $('kwCollectGroups').replaceChildren(el('p', 'score-eg', '불러오지 못했습니다.'));
    }
  }
}

function renderCollectKeywordGroups(items, alwaysCategory) {
  const byCategory = new Map(collectKwCategories.map((c) => [c, []]));
  items.forEach((it) => {
    if (!byCategory.has(it.category)) byCategory.set(it.category, []);
    byCategory.get(it.category).push(it);
  });
  const groups = [...byCategory.entries()].map(([category, list]) => {
    const onCount = list.filter((it) => it.enabled).length;
    const head = el('div', 'kw-group-head');
    head.append(
      document.createTextNode(category === alwaysCategory ? `${category} (매 회차 조회)` : category),
      el('span', 'kw-count', ` — 켜짐 ${onCount}/${list.length}`),
    );
    const listEl = el('div', 'kw-list');
    listEl.replaceChildren(...list.map((it) => {
      const chip = el('span', `kw-chip kw-chip--toggle${it.enabled ? '' : ' kw-chip--off'}`);
      chip.type = 'button';
      chip.title = it.enabled ? '클릭하면 끕니다(수집 중단)' : '클릭하면 켭니다(수집 재개)';
      chip.append(document.createTextNode(it.keyword));
      chip.addEventListener('click', (e) => {
        if (e.target.closest('button.kw-del')) return;
        toggleCollectKeyword(it);
      });
      const del = el('button', 'kw-del', '×');
      del.type = 'button';
      del.title = '완전히 삭제';
      del.addEventListener('click', (e) => { e.stopPropagation(); deleteCollectKeyword(it); });
      chip.append(del);
      return chip;
    }));
    const wrap = document.createDocumentFragment();
    wrap.append(head, listEl);
    return wrap;
  });
  $('kwCollectGroups').replaceChildren(...groups.length ? groups : [el('p', 'score-eg', '등록된 키워드가 없습니다.')]);
}

async function toggleCollectKeyword(item) {
  try {
    await masterFetch(`/api/master/keywords/${item.id}/toggle`,
      { method: 'POST', body: JSON.stringify({ enabled: !item.enabled }) });
    loadCollectKeywords();
  } catch (e) { if (e.message !== 'unauthorized') masterMsg('err', '변경에 실패했습니다.'); }
}

async function deleteCollectKeyword(item) {
  if (!confirm(`"${item.keyword}" 키워드를 완전히 삭제할까요? (되돌릴 수 없습니다)`)) return;
  try {
    await masterFetch(`/api/master/keywords/${item.id}`, { method: 'DELETE' });
    loadCollectKeywords();
  } catch (e) { if (e.message !== 'unauthorized') masterMsg('err', '삭제에 실패했습니다.'); }
}

async function addCollectKeyword() {
  const category = $('kwCollectCategory').value;
  const input = $('kwCollectNew');
  const keyword = input.value.trim();
  const msg = $('kwCollectMsg');
  if (!keyword) return;
  try {
    const res = await masterFetch('/api/master/keywords',
      { method: 'POST', body: JSON.stringify({ category, keyword }) });
    const d = await res.json();
    if (!d.ok) {
      msg.textContent = d.error || '추가에 실패했습니다.'; msg.className = 'url-add-msg err'; msg.hidden = false;
      return;
    }
    input.value = '';
    msg.hidden = true;
    loadCollectKeywords();
  } catch (e) {
    if (e.message !== 'unauthorized') {
      msg.textContent = '추가에 실패했습니다.'; msg.className = 'url-add-msg err'; msg.hidden = false;
    }
  }
}

// 야간 억제 표시 — 시간대 창 문구와 최소 점수 상태
function renderNightWindow() {
  const s = $('nightStart').value, e = $('nightEnd').value;
  const tz = $('nightTz').dataset.tz || '서울시간';
  $('nightWindow').textContent = `${s}시 ~ ${e}시`;
  $('nightTz').textContent = `(${tz})`;
}
function renderNightMin(v) {
  $('nightMinVal').textContent = v > 100 ? '전면 차단' : v;
  $('nightMinState').textContent = v > 100
    ? "— '무조건 받을 키워드'만 발송"
    : (v > 0 ? '점 이상만 발송' : '— 야간에도 전부 발송');
}
function applyNightSettings(d) {
  $('nightStart').value = d.night_start ?? 23;
  $('nightEnd').value = d.night_end ?? 7;
  $('nightTz').dataset.tz = `${d.night_tz || 'UTC+9'} 서울시간`;
  const nm = Number(d.night_min_score ?? 80);
  $('nightMinRange').value = nm;
  renderNightWindow();
  renderNightMin(nm);
}

async function loadMasterSettings() {
  try {
    const d = await (await masterFetch('/api/master/settings')).json();
    $('telegramEnabled').checked = !!d.telegram_enabled;
    $('thRange').value = d.threshold;
    $('thVal').textContent = d.threshold;
    $('thRec').textContent = d.recommended_min;
    applyNightSettings(d);
    $('webPwNow').textContent = d.web_password || '(미설정)';
    $('webLockEnabled').checked = !!d.web_lock_enabled;
    $('masterLockEnabled').checked = d.master_lock_enabled !== false;
    $('alwaysKwBypassNight').checked = d.always_kw_bypass_night !== false;
    $('notifyPolicy').checked = !!d.notify_policy;
    $('notifyTrade').checked = !!d.notify_trade;
    masterKeywords = d.keywords || [];
    excludeKeywords = d.exclude_keywords || [];
    policyKeywords = d.policy_keywords || [];
    policyRequired = d.policy_required || [];
    policyExclude = d.policy_exclude || [];
    tradeKeywords = d.trade_keywords || [];
    tradeRequired = d.trade_required || [];
    tradeExclude = d.trade_exclude || [];
    weeklyTo = d.weekly_to || [];
    const hint = $('weeklyToHint');
    if (hint) {
      hint.textContent = d.weekly_smtp_ready
        ? '월요일 아침 이 주소들로 발송됩니다. (비우면 .env WEEKLY_REPORT_TO 사용)'
        : '⚠ .env 에 SMTP_USER · SMTP_APP_PASSWORD 를 채워야 실제 발송됩니다.';
    }
    renderKwList();
    renderExcludeList();
    renderPolicyKwList();
    renderPolicyReqList();
    renderPolicyExList();
    renderTradeKwList();
    renderTradeReqList();
    renderTradeExList();
    renderWeeklyToList();
    renderScoreRules(d.score_rules);
  } catch (e) {
    if (e.message !== 'unauthorized') masterMsg('err', '설정을 불러오지 못했습니다.');
  }
}

/** 중요도 점수 산정 규칙 — /api/master/settings 의 score_rules 를 불러와 편집 상태로 둔다. */
function renderScoreRules(rules) {
  if (!rules) return;
  scoreItems = (rules.items || []).map((it) => ({ ...it }));
  scoreCustomItems = (rules.custom_items || []).map((it) => ({ ...it }));
  scoreCustomMax = rules.custom_max || 30;
  renderScoreItems();
  renderScoreCustomItems();
  $('scoreEg').innerHTML =
    '예) "<b>포스코퓨처엠</b> 양극재 3만톤 증설"(조선일보) = 50(제목) + 10(주요 언론사) = <b>60점</b>';
  const n = rules.night || {};
  const nb = (n.min_score ?? 80) > 100;
  const kw = $('alwaysKwBypassNight') && $('alwaysKwBypassNight').checked
    ? "'무조건 받을 키워드'(4번)" : '';
  $('scoreNight').innerHTML =
    `야간(${n.start ?? 23}시~${n.end ?? 7}시, ${n.tz ?? 'UTC+9'} 서울시간)에는 ` +
    (nb
      ? (kw ? `<b>${kw}에 걸린 기사만</b> 나가고, 나머지는 전부 아침에 발송됩니다.`
            : `<b>모든 기사가 아침까지 대기</b>합니다 (야간 완전 무음).`)
      : `<b>${n.min_score ?? 80}점 이상</b>${kw ? ` 또는 ${kw}에 걸린 기사만` : ''} 즉시 나가고, 나머지는 아침에 발송됩니다.`);
}

/** 기본 중요도 항목 — 점수 입력·사용여부 체크박스. 판정 로직이 코드에 있어 완전 삭제는
 * 안 되지만, '사용' 체크를 끄면 점수 기여가 0이 돼 사실상 삭제와 같다. */
function renderScoreItems() {
  const ul = $('scoreRules');
  if (!ul) return;
  ul.replaceChildren(...scoreItems.map((it) => {
    const li = el('li', it.enabled === false ? 'sr-disabled' : '');
    li.append(el('span', 'sr-label', it.label));

    const enableLbl = el('label', 'sr-enable');
    const enableCk = el('input');
    enableCk.type = 'checkbox';
    enableCk.checked = it.enabled !== false;
    enableCk.addEventListener('change', () => {
      it.enabled = enableCk.checked;
      li.classList.toggle('sr-disabled', !it.enabled);
    });
    enableLbl.append(enableCk, document.createTextNode('사용'));
    li.append(enableLbl);

    const pointsInput = el('input', 'sr-points');
    pointsInput.type = 'number';
    pointsInput.min = '-100';
    pointsInput.max = '100';
    pointsInput.value = String(it.points);
    pointsInput.addEventListener('change', () => {
      const v = parseInt(pointsInput.value, 10);
      it.points = Number.isFinite(v) ? Math.max(-100, Math.min(100, v)) : it.points;
      pointsInput.value = String(it.points);
    });
    li.append(pointsInput);
    return li;
  }));
}

/** 사용자 추가 항목 — 키워드 기반. 이름·키워드·범위·점수를 자유롭게 추가·수정·삭제한다. */
function renderScoreCustomItems() {
  const ul = $('scoreCustomList');
  if (!ul) return;
  ul.replaceChildren(...scoreCustomItems.map((it) => {
    const li = el('li', 'sc-row');

    const labelInput = el('input', 'sc-label');
    labelInput.type = 'text';
    labelInput.placeholder = '이름';
    labelInput.maxLength = 60;
    labelInput.value = it.label || '';
    labelInput.addEventListener('change', () => { it.label = labelInput.value.trim(); });
    li.append(labelInput);

    const kwInput = el('input', 'sc-kw');
    kwInput.type = 'text';
    kwInput.placeholder = '키워드(콤마로 구분)';
    kwInput.value = (it.keywords || []).join(', ');
    kwInput.addEventListener('change', () => {
      it.keywords = kwInput.value.split(',').map((k) => k.trim()).filter(Boolean);
    });
    li.append(kwInput);

    const scopeSel = el('select', 'sc-scope');
    [['title_or_body', '제목+본문'], ['title', '제목만']].forEach(([v, label]) => {
      const opt = el('option', null, label);
      opt.value = v;
      if ((it.scope || 'title_or_body') === v) opt.selected = true;
      scopeSel.append(opt);
    });
    scopeSel.addEventListener('change', () => { it.scope = scopeSel.value; });
    li.append(scopeSel);

    const pointsInput = el('input', 'sr-points sc-points');
    pointsInput.type = 'number';
    pointsInput.min = '-100';
    pointsInput.max = '100';
    pointsInput.value = String(it.points ?? 0);
    pointsInput.addEventListener('change', () => {
      const v = parseInt(pointsInput.value, 10);
      it.points = Number.isFinite(v) ? Math.max(-100, Math.min(100, v)) : (it.points || 0);
      pointsInput.value = String(it.points);
    });
    li.append(pointsInput);

    const del = el('button', 'sc-del', '×');
    del.type = 'button';
    del.addEventListener('click', () => {
      scoreCustomItems = scoreCustomItems.filter((x) => x !== it);
      renderScoreCustomItems();
    });
    li.append(del);
    return li;
  }));
}

function addScoreCustomItem() {
  if (scoreCustomItems.length >= scoreCustomMax) {
    masterMsg('err', `추가 항목은 최대 ${scoreCustomMax}개까지입니다.`);
    return;
  }
  scoreCustomItems.push({ id: '', label: '', keywords: [], scope: 'title_or_body', points: 10 });
  renderScoreCustomItems();
}

async function saveScoreRules() {
  const msg = $('scoreSaveMsg');
  const invalid = scoreCustomItems.find((it) => !it.label || !it.keywords || !it.keywords.length);
  if (invalid) {
    if (msg) { msg.textContent = '추가 항목은 이름과 키워드가 최소 1개 필요합니다.'; msg.className = 'err'; }
    return;
  }
  try {
    const res = await masterFetch('/api/master/settings', {
      method: 'POST',
      body: JSON.stringify({ score_items: scoreItems, score_custom_items: scoreCustomItems }),
    });
    const d = await res.json();
    if (!d.ok) {
      if (msg) { msg.textContent = d.error || '저장에 실패했습니다.'; msg.className = 'err'; }
      return;
    }
    if (msg) { msg.textContent = '저장했습니다.'; msg.className = 'ok'; }
    loadMasterSettings();
  } catch (e) {
    if (e.message !== 'unauthorized' && msg) { msg.textContent = '저장에 실패했습니다.'; msg.className = 'err'; }
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
function renderExcludeList() { renderChipEditor('excludeList', excludeKeywords, (next) => { excludeKeywords = next; renderExcludeList(); saveKw('exclude_keywords', excludeKeywords); }); }
function renderPolicyExList() { renderChipEditor('policyExList', policyExclude, (next) => { policyExclude = next; renderPolicyExList(); saveKw('policy_exclude', policyExclude); }); }
function renderTradeExList() { renderChipEditor('tradeExList', tradeExclude, (next) => { tradeExclude = next; renderTradeExList(); saveKw('trade_exclude', tradeExclude); }); }
function renderPolicyKwList() { renderChipEditor('policyKwList', policyKeywords, (next) => { policyKeywords = next; renderPolicyKwList(); saveKw('policy_keywords', policyKeywords); }); }
function renderPolicyReqList() { renderChipEditor('policyReqList', policyRequired, (next) => { policyRequired = next; renderPolicyReqList(); saveKw('policy_required', policyRequired); }); }
function renderTradeKwList() { renderChipEditor('tradeKwList', tradeKeywords, (next) => { tradeKeywords = next; renderTradeKwList(); saveKw('trade_keywords', tradeKeywords); }); }
function renderTradeReqList() { renderChipEditor('tradeReqList', tradeRequired, (next) => { tradeRequired = next; renderTradeReqList(); saveKw('trade_required', tradeRequired); }); }
function renderWeeklyToList() { renderChipEditor('weeklyToList', weeklyTo, (next) => { weeklyTo = next; renderWeeklyToList(); saveKw('weekly_to', weeklyTo); }); }

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

  // 통계 상세 모달 — 닫기 버튼 · 바깥 클릭 · Esc
  $('detailClose').addEventListener('click', closeStatDetail);
  $('detailModal').addEventListener('click', (e) => {
    if (e.target === $('detailModal')) closeStatDetail();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('detailModal').hidden) closeStatDetail();
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
      const next = afterMasterLogin;
      afterMasterLogin = null;
      if (next) { closeMaster(); next(); } else showMasterPanel();
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

  $('telegramEnabled').addEventListener('change', async (e) => {
    try {
      await masterFetch('/api/master/settings',
        { method: 'POST', body: JSON.stringify({ telegram_enabled: e.target.checked }) });
      masterMsg('ok', `텔레그램 발송 ${e.target.checked ? '켬' : '끔'}`);
    } catch (err) { if (err.message !== 'unauthorized') masterMsg('err', '저장 실패'); }
  });

  $('webLockEnabled').addEventListener('change', async (e) => {
    try {
      const res = await masterFetch('/api/master/settings',
        { method: 'POST', body: JSON.stringify({ web_lock_enabled: e.target.checked }) });
      const d = await res.json();
      if (!d.ok) { e.target.checked = !e.target.checked; masterMsg('err', d.error || '저장 실패'); return; }
      masterMsg('ok', e.target.checked ? '웹 접속 비밀번호 사용 켬' : '웹 접속 비밀번호 사용 끔 (누구나 접속 가능)');
    } catch (err) {
      if (err.message !== 'unauthorized') { e.target.checked = !e.target.checked; masterMsg('err', '저장 실패'); }
    }
  });

  $('masterLockEnabled').addEventListener('change', async (e) => {
    if (e.target.checked) {
      try {
        const res = await masterFetch('/api/master/lock',
          { method: 'POST', body: JSON.stringify({ enabled: true }) });
        const d = await res.json();
        if (!d.ok) { e.target.checked = false; masterMsg('err', d.error || '저장 실패'); return; }
        masterMsg('ok', '마스터 비밀번호 사용 켬');
      } catch (err) { if (err.message !== 'unauthorized') { e.target.checked = false; masterMsg('err', '저장 실패'); } }
      return;
    }
    // 끄는 건 위험하므로 체크는 도로 켜 두고, 현재 비밀번호 확인을 먼저 받는다.
    e.target.checked = true;
    $('masterLockOffConfirm').hidden = false;
    $('masterLockOffPw').value = '';
    $('masterLockOffPw').focus();
  });

  $('masterLockOffCancel').addEventListener('click', () => {
    $('masterLockOffConfirm').hidden = true;
    $('masterLockOffPw').value = '';
  });

  $('masterLockOffApply').addEventListener('click', async () => {
    const pw = $('masterLockOffPw').value;
    try {
      const res = await masterFetch('/api/master/lock',
        { method: 'POST', body: JSON.stringify({ enabled: false, current_password: pw }) });
      const d = await res.json();
      if (!d.ok) { masterMsg('err', d.error || '끄기 실패'); return; }
      $('masterLockEnabled').checked = false;
      $('masterLockOffConfirm').hidden = true;
      $('masterLockOffPw').value = '';
      masterMsg('ok', '마스터 비밀번호 사용 끔');
    } catch (err) { if (err.message !== 'unauthorized') masterMsg('err', '끄기 실패'); }
  });

  $('scoreCustomAdd').addEventListener('click', addScoreCustomItem);
  $('scoreSaveBtn').addEventListener('click', saveScoreRules);

  // 야간 억제 — 시각 2개(number) + 최소 점수(range). run_state 에 저장(마스터 우선).
  let nightTimer;
  const nightSave = (payload, okMsg) => {
    clearTimeout(nightTimer);
    nightTimer = setTimeout(async () => {
      try {
        await masterFetch('/api/master/settings', { method: 'POST', body: JSON.stringify(payload) });
        masterMsg('ok', okMsg);
      } catch (err) { if (err.message !== 'unauthorized') masterMsg('err', '저장 실패'); }
    }, 500);
  };
  const clampHour = (v) => Math.max(0, Math.min(23, parseInt(v, 10) || 0));
  $('nightStart').addEventListener('change', (e) => {
    e.target.value = clampHour(e.target.value);
    renderNightWindow();
    nightSave({ night_start: Number(e.target.value) }, `야간 시작 ${e.target.value}시`);
  });
  $('nightEnd').addEventListener('change', (e) => {
    e.target.value = clampHour(e.target.value);
    renderNightWindow();
    nightSave({ night_end: Number(e.target.value) }, `야간 종료 ${e.target.value}시`);
  });
  $('nightMinRange').addEventListener('input', (e) => {
    renderNightMin(Number(e.target.value));
    nightSave({ night_min_score: Number(e.target.value) },
      Number(e.target.value) > 100 ? '야간 전면 차단' : `야간 최소 점수 ${e.target.value}`);
  });


  const wireToggle = (id, field, label) => $(id).addEventListener('change', async (e) => {
    try {
      await masterFetch('/api/master/settings',
        { method: 'POST', body: JSON.stringify({ [field]: e.target.checked }) });
      masterMsg('ok', `${label} 알림 ${e.target.checked ? '켬' : '끔'}`);
    } catch (err) { if (err.message !== 'unauthorized') masterMsg('err', '저장 실패'); }
  });
  wireToggle('notifyPolicy', 'notify_policy', '정책브리핑 기사');
  wireToggle('notifyTrade', 'notify_trade', '글로벌 통상환경 기사');

  $('alwaysKwBypassNight').addEventListener('change', async (e) => {
    try {
      await masterFetch('/api/master/settings',
        { method: 'POST', body: JSON.stringify({ always_kw_bypass_night: e.target.checked }) });
      masterMsg('ok', `무조건 받을 키워드 야간 발송 ${e.target.checked ? '켬' : '끔'}`);
      loadMasterSettings();   // 2번의 야간 안내 문구 갱신
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
  wireKwAdd('excludeAdd', 'excludeNew', () => excludeKeywords, (a) => { excludeKeywords = a; }, renderExcludeList, 'exclude_keywords');
  wireKwAdd('policyKwAdd', 'policyKwNew', () => policyKeywords, (a) => { policyKeywords = a; }, renderPolicyKwList, 'policy_keywords');
  wireKwAdd('policyReqAdd', 'policyReqNew', () => policyRequired, (a) => { policyRequired = a; }, renderPolicyReqList, 'policy_required');
  wireKwAdd('policyExAdd', 'policyExNew', () => policyExclude, (a) => { policyExclude = a; }, renderPolicyExList, 'policy_exclude');
  wireKwAdd('tradeKwAdd', 'tradeKwNew', () => tradeKeywords, (a) => { tradeKeywords = a; }, renderTradeKwList, 'trade_keywords');
  wireKwAdd('tradeReqAdd', 'tradeReqNew', () => tradeRequired, (a) => { tradeRequired = a; }, renderTradeReqList, 'trade_required');
  wireKwAdd('tradeExAdd', 'tradeExNew', () => tradeExclude, (a) => { tradeExclude = a; }, renderTradeExList, 'trade_exclude');
  wireKwAdd('weeklyToAdd', 'weeklyToNew', () => weeklyTo, (a) => { weeklyTo = a; }, renderWeeklyToList, 'weekly_to');

  $('kwCollectAdd').addEventListener('click', addCollectKeyword);
  $('kwCollectNew').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addCollectKeyword(); }
  });

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
  '배터리협회':       '#1f7a8c',
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

/* ── 말풍선 공통 동작 (SWOT 배지 · 감성/점수 칩) ─────────────────────────
   마우스: 올리면 열고 벗어나면 닫는다.
   터치(스마트폰): 탭하면 열고, 같은 곳을 다시 탭하거나 바깥을 탭하면 닫는다.
   예전엔 터치도 mouseenter 로 한 번 열리고 곧바로 이어지는 click 이 '열려 있으면 닫기'라
   열렸다가 즉시 닫혀 모바일에서는 말풍선이 보이지 않았다(2026-10-01 제보). */
let openTipAnchor = null;     // 지금 말풍선을 연 요소

function closeTip() {
  openTipAnchor = null;
  hideSwotTip();
}

function bindTip(anchor, show) {
  const open = () => { openTipAnchor = anchor; show(); };
  // 마우스 포인터일 때만 호버로 연다. 터치가 만드는 호환용 mouseenter 는 pointerType 이 'touch' 다.
  anchor.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse') open(); });
  anchor.addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse' && openTipAnchor !== anchor) open(); });
  anchor.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse' && openTipAnchor === anchor) closeTip(); });
  // 키보드로 포커스가 왔을 때만(탭으로 생긴 포커스는 제외) 연다.
  anchor.addEventListener('focus', () => { if (anchor.matches(':focus-visible')) open(); });
  anchor.addEventListener('blur', () => { if (openTipAnchor === anchor) closeTip(); });
  anchor.addEventListener('click', (e) => {
    e.preventDefault();
    const isOpen = openTipAnchor === anchor && !$('swotTip').hidden;
    if (!isOpen) { open(); return; }
    // 이미 열려 있는데 눌렀다: 마우스는 호버로 열린 상태라 그대로 두고, 터치(두 번째 탭)만 닫는다.
    if (lastPointerType === 'touch') closeTip();
  });
}

let lastPointerType = 'mouse';
document.addEventListener('pointerdown', (e) => {
  lastPointerType = e.pointerType || 'mouse';
  // 말풍선 밖을 누르면 닫는다(스마트폰에서 닫는 유일한 방법이므로 꼭 필요하다).
  const tip = $('swotTip');
  if (!tip.hidden && !tip.contains(e.target) && !(openTipAnchor && openTipAnchor.contains(e.target))) closeTip();
}, true);

/* ── SWOT 배지 및 툴팁 (F6.2a) ──────────────────────────────────── */

const SWOT_LABELS = { s: '강점 S', w: '약점 W', o: '기회 O', t: '위협 T' };

function buildSwotBadge(swot) {
  const badge = el('button', 'swot-badge');
  badge.type = 'button';
  badge.setAttribute('aria-label', `SWOT 종합 점수 ${swot.total}. 상세 보기`);
  badge.append(el('span', null, 'SWOT'));
  badge.append(el('b', null, String(swot.total)));

  // 마우스 호버 · 키보드 포커스 · 모바일 탭 모두에서 열린다.
  bindTip(badge, () => showSwotTip(badge, swot));
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

/* ── 감성·중요도 칩 툴팁 ('긍정 · 22' — 왜 긍정인지, 왜 22점인지) ─────── */

const scoreDetailCache = new Map();   // 기사 id → 서버 응답. 같은 카드를 다시 올려도 재요청하지 않는다.
let sentiTipToken = 0;                // 마우스를 빨리 옮길 때 늦게 온 옛 응답이 덮어쓰지 않게 한다.

const SENTI_CRITERIA = '감성은 주가 호재·악재가 아니라 포스코 그룹의 대외협력 대응 필요성 기준으로 AI가 판단합니다.';

function attachSentiTip(chip, item) {
  chip.tabIndex = 0;
  chip.setAttribute('role', 'button');
  chip.setAttribute('aria-label', `${item.sentiment || '중립'} ${item.importance_score}점. 이유 보기`);
  chip.classList.add('has-tip');
  bindTip(chip, () => showSentiTip(chip, item));
}

function placeTip(anchor, tip) {
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

function fillSentiTip(tip, item, d) {
  tip.replaceChildren();
  const senti = d.sentiment || item.sentiment || '중립';
  tip.append(el('h4', null, `${senti} · ${d.score}점`));

  const dl = el('dl', 'senti-dl');
  dl.append(el('dt', null, `왜 ${senti}인가요?`));
  dl.append(el('dd', null, d.sentiment_reason
    ? `AI 판단 근거: ${d.sentiment_reason}`
    : 'AI 근거를 지금 만들지 못했습니다(잠시 후 다시 올려 보세요). 아래 기준으로 분류됩니다.'));
  dl.append(el('dd', 'tip-sub', SENTI_CRITERIA));

  dl.append(el('dt', null, `왜 ${d.score}점인가요? (중요도 0~100)`));
  if (!d.items.length) {
    dl.append(el('dd', null, '해당되는 가산 항목이 없어 0점입니다.'));
  } else {
    d.items.forEach((it) => {
      dl.append(el('dd', 'tip-row', `${it.points > 0 ? '+' : ''}${it.points}  ${it.label}`));
    });
    dl.append(el('dd', 'tip-sum', `합계 ${d.computed}점 (0~100으로 제한)`));
  }
  (d.notes || []).forEach((n) => dl.append(el('dd', 'tip-sub', n)));
  dl.append(el('dd', 'tip-sub', '점수 기준 항목과 점수는 마스터 패널 ‘중요도 점수’ 설정을 따릅니다.'));
  tip.append(dl);
}

async function showSentiTip(chip, item) {
  const tip = $('swotTip');
  const token = ++sentiTipToken;
  tip.replaceChildren(el('h4', null, `${item.sentiment || '중립'} · ${item.importance_score}점`),
    el('p', 'tip-sub', '이유를 불러오는 중… (처음 열 때는 AI가 근거를 만드느라 몇 초 걸립니다)'));
  tip.hidden = false;
  placeTip(chip, tip);
  let d = scoreDetailCache.get(item.id);
  if (!d) {
    try {
      const res = await fetch(`${API}/api/articles/${encodeURIComponent(item.id)}/score-detail`);
      d = await res.json();
      if (!d.ok) throw new Error(d.error || 'fail');
      if (d.sentiment_reason) scoreDetailCache.set(item.id, d);   // 근거가 비었으면 다음에 다시 만들어 본다
    } catch {
      if (token === sentiTipToken) {
        tip.replaceChildren(el('h4', null, `${item.sentiment || '중립'} · ${item.importance_score}점`),
          el('p', 'tip-sub', '설명을 불러오지 못했습니다.'));
      }
      return;
    }
  }
  if (token !== sentiTipToken || tip.hidden) return;   // 그 사이 다른 칩으로 옮겼거나 닫았다
  fillSentiTip(tip, item, d);
  placeTip(chip, tip);
}

document.addEventListener('scroll', closeTip, { passive: true });

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
  if (weeklyView && !favView) toggleWeeklyView();   // 주간동향 화면이면 먼저 닫는다
  if (pressView && !favView) togglePressView();     // 언론사 화면이면 먼저 닫는다
  if (window.pfmCloseEaView) window.pfmCloseEaView();   // 대외협력 화면이면 먼저 닫는다
  favView = !favView;
  $('favTab').setAttribute('aria-pressed', String(favView));
  $('favTab').textContent = favView ? '← 전체 기사' : '★ 즐겨찾기';
  $('mainFilters').hidden = favView;
  if (favView) renderFavorites(); else refresh(true);
}

async function renderFavorites() {
  const ids = [...favorites()];
  $('resultCount').textContent = `${ids.length}건`;
  $('pagination').hidden = true;
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

/* ── 언론사 보기 — 포스코퓨처엠 보도 언론사별 집계 ─────────────────── */

let pressView = false;
let pressLoaded = false;
const TONE_CLASS = { 긍정: 'pos', 중립: 'neu', 부정: 'neg' };

function togglePressView() {
  if (favView) toggleFavView();
  if (weeklyView && !pressView) toggleWeeklyView();
  if (window.pfmCloseEaView) window.pfmCloseEaView();
  pressView = !pressView;
  $('pressTab').setAttribute('aria-pressed', String(pressView));
  $('pressTab').textContent = pressView ? '← 전체 기사' : '📰 언론사';
  $('pressPanel').hidden = !pressView;
  $('mainFilters').hidden = pressView;
  $('grid').hidden = pressView;
  document.querySelector('.more-wrap').hidden = pressView;
  document.querySelector('.url-add').hidden = pressView;
  $('stats').hidden = pressView;
  if (pressView) {
    window.scrollTo({ top: 0, behavior: 'smooth' });
    if (!pressLoaded) { pressLoaded = true; loadPressStats(); }
  } else {
    refresh(true);
  }
}

async function loadPressStats() {
  const body = $('pressBody');
  body.replaceChildren(el('p', 'empty', '불러오는 중…'));
  let d;
  try {
    d = await getJSON('/api/press-stats');
  } catch {
    pressLoaded = false;   // 다음에 탭을 열면 다시 시도
    $('pressMeta').textContent = '집계를 불러오지 못했습니다.';
    body.replaceChildren();
    return;
  }
  const gen = d.generated_at ? new Date(d.generated_at).toLocaleString('ko-KR') : '';
  const w = d.windows || { week: 7, month: 30, year: 365 };
  $('pressMeta').textContent =
    `최근 ${w.year}일 포스코퓨처엠 기사 ${d.article_count || 0}건 · 언론사 ${(d.items || []).length}곳 · `
    + `주간=${w.week}일 · 월간=${w.month}일 · 연간=${w.year}일 · 집계 ${gen}`;
  if (!(d.items || []).length) {
    body.replaceChildren(el('p', 'empty', '최근 1년간 포스코퓨처엠 기사가 없습니다.'));
    return;
  }
  body.replaceChildren(buildPressTable(d.items, w));
}

function buildPressTable(items, windows) {
  const wrap = el('div', 'press-table-wrap');
  const table = el('table', 'press-table');
  const thead = el('thead');
  const h1 = el('tr');
  [['순위', { rowSpan: 2 }], ['언론사', { rowSpan: 2 }], ['언급 횟수', { colSpan: 3 }], ['논조', { colSpan: 4 }],
    ['기자 (건수) · 색=논조', { rowSpan: 2 }]].forEach(([t, span]) => {
    h1.append(Object.assign(el('th', null, t), span));
  });
  const h2 = el('tr');
  ['주간', '월간', '연간', '긍정', '중립', '부정', '미판정'].forEach((t) => h2.append(el('th', 'num', t)));
  thead.append(h1, h2);
  const tbody = el('tbody');
  items.forEach((it) => {
    const tr = el('tr', 'press-row');
    tr.tabIndex = 0;
    tr.setAttribute('aria-expanded', 'false');
    // 순위는 별도 칸으로 맨 왼쪽에 둔다(언급 건수 많은 순 — 연간 → 월간 → 주간).
    tr.append(el('td', 'press-rank-cell', it.rank ? String(it.rank) : '—'));
    // 언론사 이름 색: 긍정·부정 차이가 (긍정+부정)의 60% 를 넘으면 파랑·빨강, 그 외 검정, 판정 없음 회색(백엔드 press_tone_color)
    const nameTone = it.color ? `name-${TONE_CLASS[it.color]}` : 'name-none';
    const name = el('td', `press-name ${nameTone}`);
    const tt = it.tone || {};
    name.title = `논조 긍정 ${tt['긍정'] || 0} · 중립 ${tt['중립'] || 0} · 부정 ${tt['부정'] || 0} — 긍정·부정 차이가 60%를 넘으면 파랑·빨강`;
    name.append(el('span', 'press-caret', '▸'), document.createTextNode(' ' + it.press));
    tr.append(name);
    // data-label — 모바일에서 표를 카드로 쌓을 때 각 숫자 위에 '주간·월간…' 라벨로 쓴다
    // 숫자 칸은 누르면 그 조건(기간·논조)에 맞는 기사만 아래에 펼친다 — data-win / data-tone
    const countCells = [];
    [['주간', it.week, 'week'], ['월간', it.month, 'month'], ['연간', it.year, 'year']].forEach(([lb, n, win]) => {
      const td = el('td', 'num clickable', String(n));
      td.dataset.label = lb;
      td.title = `${lb} 기사만 보기`;
      countCells.push([td, { win }]);
      tr.append(td);
    });
    ['긍정', '중립', '부정'].forEach((k) => {
      const n = (it.tone || {})[k] || 0;
      const td = el('td', `num clickable tone-${TONE_CLASS[k]}${n ? '' : ' zero'}`, String(n));
      td.dataset.label = k;
      td.title = `논조 ${k} 기사만 보기`;
      countCells.push([td, { tone: k }]);
      tr.append(td);
    });
    const unrated = el('td', `num clickable${((it.tone || {})['미판정'] || 0) ? '' : ' zero'}`,
      String((it.tone || {})['미판정'] || 0));
    unrated.dataset.label = '미판정';
    unrated.title = '미판정 기사만 보기';
    countCells.push([unrated, { tone: '미판정' }]);
    tr.append(unrated);

    const detail = el('tr', 'press-detail');
    detail.hidden = true;
    const cell = el('td');
    cell.colSpan = 11;
    detail.append(cell);
    // 언론사 기사 목록 — 필터(기간·논조)를 바꿀 때마다 같은 칸에서 다시 그린다
    // 언론사를 펼치면 맨 위에 '이 언론사는 포스코퓨처엠을 전반적으로 이렇게 평가한다' 한 줄(AI 요약, 처음 한 번만 불러온다)
    const overview = el('div', 'press-overview');
    let overviewLoaded = false;
    const loadOverview = async () => {
      if (overviewLoaded) return;
      overviewLoaded = true;
      overview.replaceChildren(el('b', 'press-ov-label', '전반적 평가'), el('span', 'press-ov-text', '기사들을 읽고 요약하는 중…'));
      try {
        const res = await fetch(`${API}/api/press-stats/overview?press=${encodeURIComponent(it.press)}`);
        const d = await res.json();
        if (!d.ok) throw new Error(d.error || 'fail');
        overview.replaceChildren(
          el('b', 'press-ov-label', '전반적 평가'),
          el('span', 'press-ov-text', d.overview),
          el('span', 'press-ov-src', d.source === 'ai' ? `AI 요약 · 기사 ${d.articles}건 기준` : '논조 분포 기준(AI 요약을 지금 만들지 못함)'));
        if (d.source !== 'ai') overviewLoaded = false;     // 다음에 열 때 AI 요약을 다시 시도한다
      } catch {
        overviewLoaded = false;
        overview.replaceChildren(el('b', 'press-ov-label', '전반적 평가'), el('span', 'press-ov-text', '불러오지 못했습니다.'));
      }
    };
    const render = (filter) => {
      cell.replaceChildren(overview,
        buildPressArticles(it, { filter: filter || {}, windows, onFilter: render }));
      loadOverview();
    };
    const showAll = () => render({});   // 언론사 전체 기사
    const setOpen = (open) => {
      detail.hidden = !open;
      tr.setAttribute('aria-expanded', String(open));
      name.firstChild.textContent = open ? '▾' : '▸';
    };
    const toggle = () => {
      const open = detail.hidden;
      setOpen(open);
      if (open) showAll();     // 기자 화면에서 접었다 펼치면 전체 기사로 돌아온다
    };
    // 기자 이름 — 논조 색(긍정=파랑·중립=검정·부정=빨강). 누르면 그 기자의 기사만 보여준다.
    const repCell = el('td', 'press-reporters');
    (it.reporters || []).forEach((r) => {
      const cls = TONE_CLASS[r.color] || 'none';
      const chip = el('button', `rep-chip rep-${cls}`, `${r.name}(${r.count})`);
      chip.type = 'button';
      const t = r.tone || {};
      chip.title = `긍정 ${t['긍정'] || 0} · 중립 ${t['중립'] || 0} · 부정 ${t['부정'] || 0}`
        + ` · 미판정 ${t['미판정'] || 0} — 누르면 이 기자의 기사만 봅니다`;
      chip.addEventListener('click', (ev) => {
        ev.stopPropagation();   // 언론사 행 펼침 토글과 분리
        setOpen(true);
        cell.replaceChildren(buildReporterView(it, r, showAll));
      });
      repCell.append(chip, document.createTextNode(' '));
    });
    if (!(it.reporters || []).length) repCell.textContent = '—';
    tr.append(repCell);
    countCells.forEach(([td, filter]) => td.addEventListener('click', (ev) => {
      ev.stopPropagation();        // 행 전체 펼침 토글과 분리
      setOpen(true);
      render(filter);
    }));
    tr.addEventListener('click', toggle);
    tr.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
    tbody.append(tr, detail);
  });
  table.append(thead, tbody);
  wrap.append(table);
  return wrap;
}

/* 기자 1명의 기사 — 포스코퓨처엠 기사(집계 데이터) + 그 기자가 쓴 인사·부고(목록 API) */
function buildReporterView(it, rep, onBack) {
  const box = el('div', 'rep-view');
  const back = el('button', 'rep-back', `← ${it.press} 전체 기사`);
  back.type = 'button';
  back.addEventListener('click', (ev) => { ev.stopPropagation(); onBack(); });
  box.append(back, el('h4', 'rep-title', `${rep.name} 기자 · 포스코퓨처엠 기사 ${rep.count}건`));
  const mine = (it.articles || []).filter((a) => (a.authors || []).includes(rep.name));
  box.append(buildPressArticles({ articles: mine, year: mine.length }));

  box.append(el('h4', 'rep-title', '인사·부고'));
  const people = el('ul', 'press-articles');
  people.append(el('li', 'press-more', '불러오는 중…'));
  box.append(people);
  const qs = new URLSearchParams({
    cat: '인사·부고', s_author: rep.name, press: it.press, size: '50',
  });
  getJSON(`/api/articles?${qs}`).then((d) => {
    people.replaceChildren();
    (d.items || []).forEach((c) => {
      const li = el('li');
      li.append(el('span', 'press-date', (c.published_at || '').slice(0, 10)));
      const link = el('a', null, c.title);
      link.href = c.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      li.append(link);
      people.append(li);
    });
    if (!(d.items || []).length) people.append(el('li', 'press-more', '이 기자의 인사·부고 기사가 없습니다.'));
  }).catch(() => {
    people.replaceChildren(el('li', 'press-more', '불러오지 못했습니다.'));
  });
  return box;
}

const PRESS_WIN_LABEL = { week: '주간', month: '월간', year: '연간' };
const PRESS_TONES = ['긍정', '중립', '부정', '미판정'];

/* opts 가 있으면 위쪽에 필터 칩(기간·논조)을 달고 조건에 맞는 기사만 보여 준다. opts 없으면 전체(기자 보기용). */
function buildPressArticles(it, opts) {
  const all = it.articles || [];
  const f = (opts && opts.filter) || {};
  let rows = all;
  if (opts) {
    const days = f.win && opts.windows ? opts.windows[f.win] : 0;
    const cutoff = days ? Date.now() - days * 86400000 : 0;
    rows = all.filter((a) => (!cutoff || (a.published_at && new Date(a.published_at).getTime() >= cutoff))
      && (!f.tone || (f.tone === '미판정' ? !a.tone : a.tone === f.tone)));
  }
  const wrap = el('div', 'press-list');
  if (opts) {
    const bar = el('div', 'press-filterbar');
    const mk = (label, active, apply) => {
      const b = el('button', `press-fchip${active ? ' on' : ''}`, label);
      b.type = 'button';
      b.addEventListener('click', (ev) => { ev.stopPropagation(); opts.onFilter(apply()); });
      return b;
    };
    bar.append(el('span', 'press-flabel', '기간'));
    bar.append(mk('전체', !f.win, () => ({ ...f, win: undefined })));
    Object.entries(PRESS_WIN_LABEL).forEach(([k, lb]) => bar.append(mk(lb, f.win === k, () => ({ ...f, win: k }))));
    bar.append(el('span', 'press-flabel', '논조'));
    bar.append(mk('전체', !f.tone, () => ({ ...f, tone: undefined })));
    PRESS_TONES.forEach((t) => bar.append(mk(t, f.tone === t, () => ({ ...f, tone: t }))));
    wrap.append(bar);
    const cond = [f.win ? PRESS_WIN_LABEL[f.win] : '', f.tone || ''].filter(Boolean).join(' · ') || '전체';
    wrap.append(el('p', 'press-fcount', `${it.press} · ${cond} — ${rows.length}건`));
  }
  const list = el('ul', 'press-articles');
  if (opts && !rows.length) list.append(el('li', 'press-more', '조건에 맞는 기사가 없습니다.'));
  rows.forEach((a) => {
    const li = el('li');
    li.append(el('span', 'press-date', (a.published_at || '').slice(0, 10)));
    const tone = a.tone || '미판정';
    const badge = el('span', `press-tone tone-${TONE_CLASS[tone] || 'none'}`, tone);
    if (a.tone_reason) badge.title = a.tone_reason;
    li.append(badge);
    if (a.url) {
      const link = el('a', null, a.title);
      link.href = a.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      li.append(link);
    } else {
      li.append(el('span', null, a.title));
    }
    if (a.author) li.append(el('span', 'press-author', a.author));
    if (a.tone_reason) li.append(el('p', 'press-reason', `근거: ${a.tone_reason}`));
    list.append(li);
  });
  if (it.articles && it.year > it.articles.length) {
    list.append(el('li', 'press-more', `최근 ${it.articles.length}건만 표시합니다.`));
  }
  wrap.append(list);
  return wrap;
}

/* ── 주간동향 보기 ────────────────────────────────────────────── */

let weeklyView = false;
let weeklyLoaded = false;
let weeklyCur = null;       // 현재 화면에 표시 중인 레포트 { id, ... }
let weeklyRecipients = [];  // 마스터 패널 수신자 목록 (참고 표시용)

function toggleWeeklyView() {
  if (favView) toggleFavView();            // 즐겨찾기 화면이면 먼저 닫는다
  if (pressView && !weeklyView) togglePressView();   // 언론사 화면이면 먼저 닫는다
  if (window.pfmCloseEaView) window.pfmCloseEaView();   // 대외협력 화면이면 먼저 닫는다
  weeklyView = !weeklyView;
  $('weeklyTab').setAttribute('aria-pressed', String(weeklyView));
  $('weeklyTab').textContent = weeklyView ? '← 전체 기사' : '📈 주간동향';
  $('weeklyPanel').hidden = !weeklyView;
  $('mainFilters').hidden = weeklyView;
  $('grid').hidden = weeklyView;
  document.querySelector('.more-wrap').hidden = weeklyView;
  document.querySelector('.url-add').hidden = weeklyView;
  $('stats').hidden = weeklyView;
  if (weeklyView) {
    window.scrollTo({ top: 0, behavior: 'smooth' });
    if (!weeklyLoaded) { weeklyLoaded = true; loadWeekly(); loadWeeklyHistory(); }
  } else {
    refresh(true);
  }
}

async function loadWeekly(id) {
  $('weeklyBody').innerHTML = '<p class="empty">불러오는 중…</p>';
  let d;
  try {
    d = await getJSON('/api/weekly' + (id ? `?id=${encodeURIComponent(id)}` : ''));
  } catch {
    $('weeklyMeta').textContent = '레포트를 불러오지 못했습니다.';
    $('weeklyBody').innerHTML = '';
    return;
  }
  $('weeklyGenBtn').hidden = false;   // 생성 버튼은 항상 노출(누르면 마스터 인증)
  weeklyRecipients = d.recipients || [];
  if (!d.report) {
    weeklyCur = null;
    $('weeklyMailBtn').hidden = true;
    $('weeklyMeta').textContent = d.enabled
      ? '아직 생성된 레포트가 없습니다. 월요일 아침에 자동 생성되거나, 지금 새로 생성할 수 있습니다.'
      : '주간 레포트가 비활성 상태입니다 (.env WEEKLY_REPORT_ENABLED).';
    $('weeklyBody').innerHTML = '';
    return;
  }
  const r = d.report;
  weeklyCur = r;
  const fmt = (iso) => (iso || '').slice(0, 10);
  const gen = r.generated_at ? new Date(r.generated_at).toLocaleString('ko-KR') : '';
  const sent = r.sent_at ? ` · 발송됨 ${new Date(r.sent_at).toLocaleString('ko-KR')}`
    : (r.send_error ? ` · 발송 실패(${r.send_error})` : ' · 미발송');
  const cnt = (r.payload && r.payload.article_count) || 0;
  $('weeklyMeta').textContent =
    `집계 ${fmt(r.period_start)} ~ ${fmt(r.period_end)} · 대상 ${cnt}건 · 생성 ${gen}${sent}`;
  $('weeklyBody').innerHTML = r.html || '';
  $('weeklyMailBtn').hidden = !d.smtp_ready;
  $('weeklyMailBtn').textContent = d.recipients && d.recipients.length
    ? `메일로 보내기 (${d.recipients.length}명)` : '메일로 보내기';
}

async function loadWeeklyHistory() {
  let items = [];
  try { items = (await getJSON('/api/weekly/history')).items || []; } catch { return; }
  const sel = $('weeklyPick');
  if (items.length < 2) { sel.hidden = true; return; }
  sel.innerHTML = '';
  items.forEach((it, i) => {
    const s = (it.period_start || '').slice(0, 10);   // 연도 포함
    const e = (it.period_end || '').slice(0, 10);
    const o = el('option', '', `${s} ~ ${e}${i === 0 ? ' (최신)' : ''}`);
    o.value = it.id;
    sel.appendChild(o);
  });
  sel.hidden = false;
  sel.onchange = () => loadWeekly(sel.value);
}

async function generateWeekly() {
  if (!masterAuth()) { showMasterLogin(); return; }
  const btn = $('weeklyGenBtn');
  btn.disabled = true;
  btn.textContent = '생성 중… (1~2분)';
  try {
    const res = await masterFetch('/api/weekly/generate', { method: 'POST' });
    const d = await res.json();
    if (!d.ok) throw new Error(d.error || '생성 실패');
    await loadWeekly();
    await loadWeeklyHistory();
  } catch (e) {
    if (e.message !== 'unauthorized') alert('생성 실패: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '지금 새로 생성';
  }
}

async function sendWeeklyMail() {
  if (!weeklyCur) return;
  if (!masterAuth()) { showMasterLogin(); return; }
  if (!weeklyRecipients.length &&
      !confirm('수신자 목록이 비어 있습니다. 마스터 패널에서 수신자를 추가하세요.')) return;
  const to = weeklyRecipients.length ? `\n\n수신: ${weeklyRecipients.join(', ')}` : '';
  if (!confirm('이 레포트를 이메일로 보낼까요?' + to)) return;
  const btn = $('weeklyMailBtn');
  btn.disabled = true;
  btn.textContent = '보내는 중…';
  try {
    const res = await masterFetch(`/api/weekly/${weeklyCur.id}/send`, { method: 'POST' });
    const d = await res.json();
    if (!d.ok) throw new Error(d.error || '발송 실패');
    alert('발송 완료: ' + (d.sent_to || []).join(', '));
    await loadWeekly(weeklyCur.id);
  } catch (e) {
    if (e.message !== 'unauthorized') alert('발송 실패: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '메일로 보내기';
  }
}

/* ── 목록 로드 ──────────────────────────────────────────────────── */

function buildQuery() {
  const p = new URLSearchParams();
  if (state.group.size) p.set('group', [...state.group].join(','));
  if (state.cat.size) p.set('cat', [...state.cat].join(','));
  if (state.press.size) p.set('press', [...state.press].join(','));
  if (state.period !== 'all') p.set('period', state.period);
  if (state.sort !== 'recent') p.set('sort', state.sort);
  if (state.q) p.set('q', state.q);
  advActive().forEach(([k]) => p.set(k, state.adv[k]));
  p.set('page', String(state.page));
  p.set('size', String(PAGE_SIZE));
  return p.toString();
}

// 목록 요청은 '마지막 요청이 이긴다'. 예전엔 요청 중에 들어온 새 요청을 버려서,
// 한글 입력 도중('김철') 먼저 나간 검색 결과가 최종 검색어('김철수') 대신 화면에 남았다
// (2026-09-29 사용자 지적: 기자명을 검색했는데 다른 기자가 나옴). 이제 새 요청이 오면
// 이전 요청을 취소하고, 늦게 도착한 옛 응답은 번호(seq)로 걸러 버린다.
let refreshSeq = 0;
let refreshAbort = null;

async function refresh(reset) {
  // 즐겨찾기 화면일 때는 일반 목록이 덮어쓰지 않게 막는다.
  // (필터를 숨기는 것만으로는 방어가 약하다 — 단축키·코드 변경에 취약)
  if (favView || weeklyView || pressView) return;
  const seq = ++refreshSeq;
  if (refreshAbort) refreshAbort.abort();
  refreshAbort = new AbortController();
  const signal = refreshAbort.signal;
  state.loading = true;
  if (reset) state.page = 1;
  $('grid').replaceChildren(...Array.from({ length: 6 }, () => el('div', 'skeleton')));
  writeStateToURL();
  syncChipStates();

  try {
    const data = await getJSON('/api/articles?' + buildQuery(), signal);
    if (seq !== refreshSeq) return;   // 더 새 요청이 나갔다 — 이 응답은 버린다
    state.total = data.total;
    // 필터가 좁아져 현재 페이지가 범위를 벗어나면 마지막 페이지로 되돌린다.
    const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
    if (state.page > pages) {
      state.page = pages;
      state.loading = false;
      return refresh(false);
    }
    $('grid').replaceChildren(...data.items.map(buildCard));
    $('resultCount').textContent = `${data.total.toLocaleString('ko-KR')}건`;
    // 상세 검색 칸 옆 '결과 N건 보기 ↓' — 폰에서 결과가 어디 있는지 바로 알 수 있게
    const jump = $('advJump');
    if (jump) {
      const searching = !!state.q || advActive().length > 0;
      jump.hidden = !searching;
      jump.textContent = `검색 결과 ${data.total.toLocaleString('ko-KR')}건 보기 ↓`;
    }
    $('emptyMsg').hidden = data.total !== 0;
    renderPagination(state.page, data.total);
    updateActiveFilterCount();
  } catch (err) {
    if (seq !== refreshSeq || err.name === 'AbortError') return;   // 취소된 옛 요청
    console.error('기사 조회 실패', err);
    $('grid').replaceChildren(el('p', 'empty', '기사를 불러오지 못했습니다.'));
    renderPagination(1, 0);
  } finally {
    if (seq === refreshSeq) state.loading = false;
  }
}

/* ── 페이지네이션 ──────────────────────────────────────────────── */

function gotoPage(n) {
  if (n === state.page) return;
  state.page = n;
  refresh(false);
  const grid = document.querySelector('.grid');
  const top = grid.getBoundingClientRect().top + window.scrollY - 90;
  window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
}

function renderPagination(page, total) {
  const nav = $('pagination');
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (total === 0 || pages <= 1) { nav.hidden = true; nav.replaceChildren(); return; }
  nav.hidden = false;

  const btn = (label, target, opt = {}) => {
    const b = el('button', 'pager-btn' + (opt.current ? ' is-current' : ''), label);
    b.type = 'button';
    if (opt.disabled || opt.current) b.disabled = true;
    else b.addEventListener('click', () => gotoPage(target));
    if (opt.aria) b.setAttribute('aria-label', opt.aria);
    return b;
  };

  const items = [btn('‹', page - 1, { disabled: page <= 1, aria: '이전 페이지' })];
  const near = new Set([1, pages, page, page - 1, page + 1, page - 2, page + 2]);
  const shown = [...near].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
  let prev = 0;
  for (const n of shown) {
    if (n - prev > 1) items.push(el('span', 'pager-gap', '…'));
    items.push(btn(String(n), n, { current: n === page }));
    prev = n;
  }
  items.push(btn('›', page + 1, { disabled: page >= pages, aria: '다음 페이지' }));
  nav.replaceChildren(...items);
}

/* ── 필터 접기/펼치기 ──────────────────────────────────────────── */

function activeFilterCount() {
  return state.group.size + state.cat.size + state.press.size
    + (state.period !== 'all' ? 1 : 0) + (state.q ? 1 : 0) + advActive().length;
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
  advActive().forEach(([k, , label]) => seg(`${label} 검색`, state.adv[k]));
  box.hidden = false;
}

function setAdvOpen(open) {
  $('advSearch').hidden = !open;
  $('advToggle').setAttribute('aria-expanded', String(open));
  $('advToggle').textContent = open ? '상세 검색 ▴' : '상세 검색 ▾';
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

let pendingDraft = null;   // 미리보기 중인 draft 기사 {id, card}

function clearUrlPreview() {
  pendingDraft = null;
  $('urlAddConfirm').hidden = true;
  $('urlAddCheck').hidden = true;
  $('urlAddResult').replaceChildren();
}

async function submitUrl(e) {
  e.preventDefault();
  const input = $('urlAddInput');
  const btn = $('urlAddBtn');
  const url = input.value.trim();
  if (!url) return;

  btn.disabled = true;
  clearUrlPreview();
  urlMsg('info', '기사를 분석하고 있습니다… (10~20초 걸릴 수 있어요)');

  try {
    const res = await fetch(API + '/api/analyze-url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();

    if (!data.ok) { urlMsg('err', data.error || '분석에 실패했습니다.'); return; }

    $('urlAddResult').replaceChildren(buildCard(data.card));
    // 붙여넣은 URL 이 의도한 기사가 맞는지 제목으로 재확인시킨다 (엉뚱한 링크 등록 방지).
    const chk = $('urlAddCheck');
    chk.textContent = `📄 이 기사가 맞습니까? — 「${data.card.title || '제목 없음'}」 · ${data.card.press_name || '언론사 미상'}`;
    chk.hidden = false;
    if (data.already) {
      urlMsg('info', '이미 등록된 기사입니다. 아래 카드를 확인하세요.');
    } else if (data.draft_id) {
      pendingDraft = { id: data.draft_id, card: data.card };
      $('urlAddConfirm').hidden = false;
      urlMsg('info', '미리보기를 만들었습니다. 확인 후 등록하세요.');
    }
  } catch (err) {
    console.error('URL 분석 실패', err);
    urlMsg('err', '서버에 연결하지 못했습니다.');
  } finally {
    btn.disabled = false;
  }
}

async function confirmDraft() {
  if (!pendingDraft) return;
  if (!masterAuth()) {
    urlMsg('info', '등록은 관리자 로그인이 필요합니다. 로그인하면 이어서 등록됩니다.');
    afterMasterLogin = confirmDraft;
    showMasterLogin();
    return;
  }
  try {
    const res = await masterFetch(`/api/articles/${pendingDraft.id}/confirm`, { method: 'POST' });
    if (!(await res.json()).ok) throw new Error();
    $('grid').prepend(buildCard(pendingDraft.card));   // 목록 맨 위에 추가
    $('urlAddInput').value = '';
    urlMsg('ok', '목록에 등록했습니다.');
    clearUrlPreview();
  } catch (err) {
    if (err.message === 'unauthorized') {
      afterMasterLogin = confirmDraft;   // 토큰 만료 — 다시 로그인하면 이어서 등록
      urlMsg('info', '로그인이 만료되었습니다. 다시 로그인하면 이어서 등록됩니다.');
    } else {
      urlMsg('err', '등록에 실패했습니다.');
    }
  }
}

async function discardDraft() {
  if (!pendingDraft) return;
  if (!masterAuth()) {
    // 로그인 없이는 서버 초안을 지울 수 없다(24시간 뒤 자동 정리) — 화면 미리보기만 닫는다.
    urlMsg('info', '등록하지 않았습니다.');
    clearUrlPreview();
    return;
  }
  try {
    await masterFetch(`/api/articles/${pendingDraft.id}/discard`, { method: 'POST' });
  } catch { /* 실패해도 24시간 뒤 자동 정리된다 */ }
  urlMsg('info', '등록하지 않았습니다.');
  clearUrlPreview();
}

/* ── 초기화 ─────────────────────────────────────────────────────── */

function init() {
  readStateFromURL();
  $('searchInput').value = state.q;
  ADV_FIELDS.forEach(([k, id]) => { $(id).value = state.adv[k]; });
  // 상세 검색 칸에 값이 있으면(공유 링크 등) 펼친 채로 시작한다.
  setAdvOpen(advActive().length > 0);
  $('advToggle').addEventListener('click', () => setAdvOpen($('advSearch').hidden));

  // 카드 사진 ON/OFF — 이 브라우저에 기억한다(기본 ON). 목록을 다시 그릴 필요 없이 CSS 클래스만 바꾼다.
  const setThumbs = (on, save) => {
    document.body.classList.toggle('hide-thumbs', !on);
    $('thumbToggle').setAttribute('aria-pressed', String(on));
    $('thumbToggle').textContent = on ? '사진 ON' : '사진 OFF';
    if (save) { try { localStorage.setItem('pfm.thumbs', on ? '1' : '0'); } catch { /* noop */ } }
  };
  let thumbsOn = true;
  try { thumbsOn = localStorage.getItem('pfm.thumbs') !== '0'; } catch { /* noop */ }
  setThumbs(thumbsOn, false);
  $('thumbToggle').addEventListener('click', () => {
    setThumbs($('thumbToggle').getAttribute('aria-pressed') !== 'true', true);
  });

  // 마지막으로 접었는지 기억한다. 활성 필터가 있으면 펼친 상태로 시작한다.
  let collapsed = false;
  try { collapsed = localStorage.getItem('pfm.filterCollapsed') === '1'; } catch { /* noop */ }
  if (activeFilterCount() > 0) collapsed = false;
  setFilterCollapsed(collapsed);
  updateActiveFilterCount();

  $('filterToggle').addEventListener('click', () => {
    setFilterCollapsed(!$('filterBody').hidden);   // 보이는 중이면 접고, 접혀 있으면 편다
  });

  // 검색칸들은 디바운스 타이머 하나를 같이 쓰므로, 타이머가 터질 때 '바뀐 칸'만이 아니라
  // 모든 칸을 다시 읽는다 — 안 그러면 빠르게 두 칸을 입력할 때 앞 칸 조건이 사라진다.
  let timer;
  const applySearch = () => {
    clearTimeout(timer);
    state.q = $('searchInput').value.trim();
    ADV_FIELDS.forEach(([k, id]) => { state.adv[k] = $(id).value.trim(); });
    updateActiveFilterCount();
    refresh(true);
  };
  // 폰: 필터 패널이 길어 결과 목록이 화면 아래로 밀려 있다 — 검색을 확정(Enter·검색 버튼)하면
  // 키보드를 닫고 결과 첫 줄로 스크롤한다.
  const searchAndShow = () => {
    applySearch();
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    $('grid').scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const debounced = () => {
    clearTimeout(timer);
    timer = setTimeout(applySearch, 300);
  };
  ['searchInput', ...ADV_FIELDS.map(([, id]) => id)].forEach((id) => {
    const input = $(id);
    input.addEventListener('input', debounced);
    // 일부 모바일 키보드(한글 조합)는 조합이 끝날 때·지우기 버튼에서 input 이벤트가 빠지는 경우가 있어 함께 듣는다.
    input.addEventListener('compositionend', debounced);
    input.addEventListener('change', debounced);
    input.addEventListener('search', debounced);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); searchAndShow(); }
    });
  });
  $('advGo').addEventListener('click', searchAndShow);
  $('advJump').addEventListener('click', () => $('grid').scrollIntoView({ behavior: 'smooth', block: 'start' }));

  const goHome = () => {
    if (weeklyView) toggleWeeklyView();     // 주간동향 화면이면 전체 목록으로
    if (pressView) togglePressView();       // 언론사 화면이면 전체 목록으로
    if (favView) toggleFavView();          // 즐겨찾기 화면이면 전체 목록으로
    if (window.pfmCloseEaView) window.pfmCloseEaView();   // 대외협력 화면이면 전체 목록으로
    state.group.clear(); state.cat.clear(); state.press.clear();
    state.period = 'all'; state.q = '';
    $('searchInput').value = '';
    ADV_FIELDS.forEach(([k, id]) => { state.adv[k] = ''; $(id).value = ''; });
    window.scrollTo({ top: 0, behavior: 'smooth' });
    refresh(true);
  };
  $('clearAll').addEventListener('click', goHome);
  $('brandHome').addEventListener('click', (e) => { e.preventDefault(); goHome(); });


  $('favTab').addEventListener('click', toggleFavView);
  $('weeklyTab').addEventListener('click', toggleWeeklyView);
  $('pressTab').addEventListener('click', togglePressView);
  window.pfmCloseOtherViews = () => {
    if (favView) toggleFavView();
    if (weeklyView) toggleWeeklyView();
    if (pressView) togglePressView();
  };
  window.pfmRefreshList = () => refresh(true);
  $('weeklyGenBtn').addEventListener('click', generateWeekly);
  $('weeklyMailBtn').addEventListener('click', sendWeeklyMail);
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
  $('urlConfirmBtn').addEventListener('click', confirmDraft);
  $('urlDiscardBtn').addEventListener('click', discardDraft);

  // 첫 로드는 URL 의 page 를 살리기 위해 reset 하지 않는다.
  loadFilters().then(() => refresh(false));
  loadStats();
  loadQuotes();

  // 시세는 60초, 통계는 5분 주기로 갱신한다. (F9.2)
  setInterval(loadQuotes, 60_000);
  setInterval(loadStats, 300_000);
}

document.addEventListener('DOMContentLoaded', init);
