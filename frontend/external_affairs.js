/* ── 대외협력(대관) 패널 ─────────────────────────────────────────────
   기존 app.js 의 state 객체를 확장하지 않는다. 전용 상태를 따로 둔다.
   URL 파라미터도 ea_ 접두사를 붙여 기존 page 파라미터와 충돌하지 않게 한다.
   ------------------------------------------------------------------ */
(function () {
  'use strict';

  var EA_PAGE_SIZE = 9;
  var SUBTABS = [
    { key: 'notice', label: '입법·행정예고', types: 'legislation,admin_notice' },
    { key: 'bill', label: '국회 의안', types: 'bill' },
    { key: 'policy', label: '정책 동향', news: '/api/ea/policy-news' },
    { key: 'trade', label: '통상 환경', news: '/api/ea/trade-news' }
  ];
  var IMPACT_LABEL = { high: '영향 높음', medium: '영향 보통', low: '영향 낮음', none: '영향 없음' };

  var eaState = {
    open: false, loaded: false, sub: 'notice',
    agency: '', impact: '', status: '', due: '', q: '', group: '',
    sort: 'deadline',   // deadline(마감임박순, 기본) | recent(최신순) | impact(영향도순)
    page: 1, loading: false
  };

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== null && text !== undefined) { n.textContent = text; }
    return n;
  }

  function getJSON(path) {
    return fetch(path, { headers: { Accept: 'application/json' } }).then(function (r) {
      if (!r.ok) { throw new Error(r.status + ' ' + path); }
      return r.json();
    });
  }

  function fmtDate(v) { return (v || '').slice(0, 10); }

  function link(text, href, cls) {
    var a = el('a', cls || null, text);
    a.href = href || '#';
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    return a;
  }

  /* ── URL 동기화 (ea_ 접두사 — 기존 파라미터와 충돌 없음) ── */
  var EA_KEYS = ['ea_sub', 'ea_agency', 'ea_group', 'ea_impact', 'ea_status', 'ea_due',
    'ea_q', 'ea_sort', 'ea_page'];

  function readUrl() {
    var p = new URLSearchParams(location.search);
    eaState.sub = p.get('ea_sub') || 'notice';
    eaState.agency = p.get('ea_agency') || '';
    eaState.group = p.get('ea_group') || '';
    eaState.impact = p.get('ea_impact') || '';
    eaState.status = p.get('ea_status') || '';
    eaState.due = p.get('ea_due') || '';
    eaState.q = p.get('ea_q') || '';
    eaState.sort = p.get('ea_sort') || 'deadline';
    eaState.page = Math.max(1, parseInt(p.get('ea_page') || '1', 10) || 1);
  }

  function writeUrl() {
    var p = new URLSearchParams(location.search);
    EA_KEYS.forEach(function (k) { p.delete(k); });
    if (eaState.open) {
      if (eaState.sub !== 'notice') { p.set('ea_sub', eaState.sub); }
      if (eaState.agency) { p.set('ea_agency', eaState.agency); }
      if (eaState.group) { p.set('ea_group', eaState.group); }
      if (eaState.impact) { p.set('ea_impact', eaState.impact); }
      if (eaState.status) { p.set('ea_status', eaState.status); }
      if (eaState.due) { p.set('ea_due', eaState.due); }
      if (eaState.q) { p.set('ea_q', eaState.q); }
      if (eaState.sort && eaState.sort !== 'deadline') { p.set('ea_sort', eaState.sort); }
      if (eaState.page > 1) { p.set('ea_page', String(eaState.page)); }
    }
    var qs = p.toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
  }

  /* ── 마감 임박 배너 (D-7 이내가 있을 때만) ── */
  function renderAlert(stats) {
    var box = $('eaAlert');
    var urgent = (stats && stats.urgent) || [];
    if (!urgent.length) { box.hidden = true; box.replaceChildren(); return; }
    var head = el('div');
    head.append(el('b', null, '⚠ 의견제출 마감 임박 ' + stats.urgent_total + '건'));
    var ul = el('ul');
    urgent.forEach(function (it) {
      var li = el('li');
      var d = it.d_day;
      li.append(el('b', null, '[' + (d >= 0 ? 'D-' + d : '마감') + '] '));
      li.append(link(it.title, it.opinion_url || it.url));
      ul.append(li);
    });
    box.replaceChildren(head, ul);
    box.hidden = false;
  }

  /* ── D-day 뱃지 ── */
  function ddayBadge(it) {
    var box = el('div', 'ea-dday');
    var d = it.d_day;
    if (d === null || d === undefined) {
      box.classList.add('is-closed');
      box.append(el('b', null, '—'), el('span', null, it.status || '기한없음'));
      return box;
    }
    if (d < 0) { box.classList.add('is-closed'); }
    else if (d <= 3) { box.classList.add('is-urgent'); }
    else if (d <= 7) { box.classList.add('is-soon'); }
    else { box.classList.add('is-open'); }
    box.append(el('b', null, d < 0 ? '마감' : 'D-' + d));
    box.append(el('span', null, d < 0 ? fmtDate(it.notice_end) : '까지'));
    return box;
  }

  /* ── 항목 카드 (기존 포토카드 재사용 안 함 — 정보 밀도 우선) ── */
  function buildCard(it) {
    var card = el('article', 'ea-card');
    card.append(ddayBadge(it));

    var body = el('div');
    var h = el('h3', 'ea-card-title');
    h.append(link(it.title, it.url));
    body.append(h);

    var bits = [];
    if (it.agency) { bits.push(it.agency); }
    if (it.law_name) { bits.push(it.law_name); }
    if (it.notice_start || it.notice_end) {
      bits.push(fmtDate(it.notice_start) + ' ~ ' + (fmtDate(it.notice_end) || '미정'));
    }
    if (it.status) { bits.push(it.status); }
    body.append(el('p', 'ea-card-sub', bits.join(' · ')));

    var tags = el('div', 'ea-tags');
    // 규제 대상 사업으로 판정한 관련 그룹사 — 회사명이 원문에 없어도 붙는다.
    (it.group_companies || []).forEach(function (g) {
      tags.append(el('span', 'ea-tag is-group', g));
    });
    if (it.category) { tags.append(el('span', 'ea-tag is-cat', it.category)); }
    if (it.impact_level) {
      tags.append(el('span', 'ea-tag ea-impact-' + it.impact_level,
        IMPACT_LABEL[it.impact_level] || it.impact_level));
    }
    (it.affected_areas || []).forEach(function (x) { tags.append(el('span', 'ea-tag', x)); });
    if (tags.childNodes.length) { body.append(tags); }

    if (it.summary) { body.append(el('p', 'ea-summary', it.summary)); }
    if (it.impact_rationale) {
      var r = el('div', 'ea-rationale');
      r.append(el('b', null, '근거 '));
      r.append(document.createTextNode(it.impact_rationale));
      body.append(r);
    }
    if (it.suggested_action) {
      var s = el('div', 'ea-rationale');
      s.append(el('b', null, '대응(초안) '));
      s.append(document.createTextNode(it.suggested_action));
      body.append(s);
    }

    var acts = el('div', 'ea-actions');
    acts.append(link('원문', it.url));
    if (it.opinion_url) { acts.append(link('의견 제출', it.opinion_url, 'is-primary')); }
    (it.attachment_urls || []).forEach(function (u, i) {
      acts.append(link('첨부 ' + (i + 1), u));
    });
    body.append(acts);

    card.append(body);
    return card;
  }

  /* ── 정책/통상 뉴스 카드 (S4·S5 재사용 — 기존 수집 결과 읽기 전용) ── */
  function buildNewsCard(n) {
    var card = el('article', 'ea-card');
    var badge = el('div', 'ea-dday is-open');
    badge.append(el('b', null, String(n.score)), el('span', null, '중요도'));
    card.append(badge);

    var body = el('div');
    var h = el('h3', 'ea-card-title');
    h.append(link(n.title, n.url));
    body.append(h);
    body.append(el('p', 'ea-card-sub',
      [n.press, fmtDate(n.published_at)].filter(Boolean).join(' · ')));
    if (n.summary) { body.append(el('p', 'ea-summary', n.summary)); }
    card.append(body);
    return card;
  }

  /* ── 서브탭 ── */
  function currentSub() {
    for (var i = 0; i < SUBTABS.length; i += 1) {
      if (SUBTABS[i].key === eaState.sub) { return SUBTABS[i]; }
    }
    return SUBTABS[0];
  }

  function renderSubtabs() {
    var box = $('eaSubtabs');
    box.replaceChildren.apply(box, SUBTABS.map(function (t) {
      var b = el('button', 'ea-subtab', t.label);
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(t.key === eaState.sub));
      b.addEventListener('click', function () {
        if (eaState.sub === t.key) { return; }
        eaState.sub = t.key;
        eaState.page = 1;
        renderSubtabs();
        $('eaFilters').hidden = !!t.news;   // 뉴스 재사용 탭은 전용 필터를 쓰지 않는다
        // 부처 목록은 탭마다 다르다(정부 부처 ↔ 국회 상임위) — 다시 받아서 채운다.
        if (t.news) { load(); } else { loadFilters().then(load); }
      });
      return b;
    }));
  }

  /* ── 필터 ── */
  function fillSelect(sel, options, value, placeholder) {
    sel.replaceChildren();
    if (placeholder !== null) {          // null 이면 '전체' 항목 없이 항상 값이 있는 셀렉트
      var o0 = el('option', null, placeholder);
      o0.value = '';
      sel.append(o0);
    }
    options.forEach(function (o) {
      var opt = el('option', null, o.label);
      opt.value = o.key;
      sel.append(opt);
    });
    sel.value = value || '';
  }

  function loadFilters() {
    // 서브탭을 함께 넘긴다 — 부처 목록·건수를 그 탭 기준으로 받기 위해서다.
    // (국회 의안 탭에는 상임위, 입법·행정예고 탭에는 정부 부처가 온다)
    var sub = currentSub();
    var qs = sub.types ? '?item_type=' + encodeURIComponent(sub.types) : '';
    return getJSON('/api/ea/filters' + qs).then(function (d) {
      fillSelect($('eaSort'), d.sorts || [
        { key: 'deadline', label: '마감 임박순' }, { key: 'recent', label: '최신순' },
        { key: 'impact', label: '영향도순' }
      ], eaState.sort || 'deadline', null);
      var agencies = d.agencies || [];
      // 탭이 바뀌어 지금 고른 부처가 목록에 없으면 해제한다(0건만 나오는 것을 막는다).
      if (eaState.agency && !agencies.some(function (a) { return a.key === eaState.agency; })) {
        eaState.agency = '';
      }
      fillSelect($('eaAgency'), agencies, eaState.agency, '부처 전체');
      fillSelect($('eaGroup'), d.groups || [], eaState.group, '그룹사 전체');
      fillSelect($('eaImpact'), d.impacts || [], eaState.impact, '영향도 전체');
      fillSelect($('eaStatus'), (d.statuses || []).map(function (s) {
        return { key: s, label: s };
      }), eaState.status, '상태 전체');
      fillSelect($('eaDue'), d.dues || [], eaState.due, '마감 전체');
    }).catch(function () { /* 필터는 없어도 목록은 보여준다 */ });
  }

  /* ── 페이지네이션 ── */
  function renderPager(total) {
    var nav = $('eaPager');
    var pages = Math.max(1, Math.ceil(total / EA_PAGE_SIZE));
    if (total === 0 || pages <= 1) { nav.hidden = true; nav.replaceChildren(); return; }
    nav.hidden = false;
    function btn(label, target, opt) {
      var b = el('button', opt.current ? 'is-current' : null, label);
      b.type = 'button';
      if (opt.disabled || opt.current) { b.disabled = true; }
      else {
        b.addEventListener('click', function () {
          eaState.page = target;
          load();
          $('eaPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      }
      return b;
    }
    var page = eaState.page;
    var items = [btn('‹', page - 1, { disabled: page <= 1 })];
    var near = [1, pages, page, page - 1, page + 1, page - 2, page + 2];
    var shown = near.filter(function (n, i) {
      return n >= 1 && n <= pages && near.indexOf(n) === i;
    }).sort(function (a, b) { return a - b; });
    var prev = 0;
    shown.forEach(function (n) {
      if (n - prev > 1) { items.push(el('span', 'ea-tag', '…')); }
      items.push(btn(String(n), n, { current: n === page }));
      prev = n;
    });
    items.push(btn('›', page + 1, { disabled: page >= pages }));
    nav.replaceChildren.apply(nav, items);
  }

  /* ── 목록 로드 ── */
  function showState(cls, text) {
    $('eaList').replaceChildren(el('p', 'ea-state' + (cls ? ' ' + cls : ''), text));
    $('eaPager').hidden = true;
    $('eaCount').textContent = '';
  }

  function load() {
    if (eaState.loading) { return; }
    eaState.loading = true;
    writeUrl();

    var sub = currentSub();
    $('eaList').replaceChildren.apply($('eaList'), [0, 1, 2].map(function () {
      return el('div', 'ea-skeleton');
    }));

    var done = function () { eaState.loading = false; };

    if (sub.news) {
      getJSON(sub.news + '?limit=30').then(function (d) {
        var items = d.items || [];
        $('eaCount').textContent = items.length + '건';
        renderPager(0);
        if (!items.length) {
          showState('', '최근 30일 내 해당 기사가 없습니다.');
          return;
        }
        $('eaList').replaceChildren.apply($('eaList'), items.map(buildNewsCard));
      }).catch(function () {
        showState('is-error', '기사를 불러오지 못했습니다.');
      }).finally(done);
      return;
    }

    var p = new URLSearchParams();
    p.set('item_type', sub.types);
    if (eaState.agency) { p.set('agency', eaState.agency); }
    if (eaState.group) { p.set('group', eaState.group); }
    if (eaState.impact) { p.set('impact', eaState.impact); }
    if (eaState.status) { p.set('status', eaState.status); }
    if (eaState.due) { p.set('due', eaState.due); }
    if (eaState.q) { p.set('q', eaState.q); }
    if (eaState.sort && eaState.sort !== 'deadline') { p.set('sort', eaState.sort); }
    p.set('page', String(eaState.page));
    p.set('size', String(EA_PAGE_SIZE));

    getJSON('/api/ea/items?' + p.toString()).then(function (d) {
      var total = d.total || 0;
      var pages = Math.max(1, Math.ceil(total / EA_PAGE_SIZE));
      if (eaState.page > pages) { eaState.page = pages; eaState.loading = false; load(); return; }
      $('eaCount').textContent = total.toLocaleString('ko-KR') + '건';
      renderPager(total);
      if (!total) {
        showState('', '조건에 맞는 항목이 없습니다. 수집은 매일 09시·15시에 실행됩니다.');
        return;
      }
      $('eaList').replaceChildren.apply($('eaList'), (d.items || []).map(buildCard));
    }).catch(function () {
      showState('is-error', '목록을 불러오지 못했습니다.');
    }).finally(done);
  }

  function loadStats() {
    return getJSON('/api/ea/stats').then(function (s) {
      renderAlert(s);
      var msg = '수집 ' + s.total + '건 · 예고중 ' + s.open + '건 · 분석 ' + s.analyzed + '건';
      var rest = s.sources_rest || {};
      var api = Object.keys(rest).filter(function (k) { return rest[k]; });
      msg += api.length ? ' · API: ' + api.join(',') : ' · 수집: 크롤링';
      $('eaMeta').textContent = msg;
    }).catch(function () {
      $('eaMeta').textContent = '현황을 불러오지 못했습니다.';
    });
  }

  /* ── 패널 열고 닫기 (기존 탭과 배타적) ── */
  function setOpen(on) {
    eaState.open = on;
    $('eaTab').setAttribute('aria-pressed', String(on));
    $('eaTab').textContent = on ? '← 전체 기사' : '🏛 대외협력';
    $('eaPanel').hidden = !on;
    var filters = document.querySelector('.filters');
    if (filters) { filters.hidden = on; }
    var grid = $('grid');
    if (grid) { grid.hidden = on; }
    var more = document.querySelector('.more-wrap');
    if (more) { more.hidden = on; }
    var urlAdd = document.querySelector('.url-add');
    if (urlAdd) { urlAdd.hidden = on; }
    var stats = $('stats');
    if (stats) { stats.hidden = on; }
    writeUrl();
  }

  function toggle() {
    // 다른 탭이 열려 있으면 먼저 닫는다 (배타 전환)
    if (!eaState.open && window.pfmCloseOtherViews) { window.pfmCloseOtherViews(); }
    setOpen(!eaState.open);
    if (eaState.open) {
      window.scrollTo({ top: 0, behavior: 'smooth' });
      if (!eaState.loaded) {
        eaState.loaded = true;
        renderSubtabs();
        $('eaFilters').hidden = !!currentSub().news;
        loadFilters().then(load);
        loadStats();
      }
    } else if (window.pfmRefreshList) {
      window.pfmRefreshList();
    }
  }

  /* app.js 가 다른 탭을 열 때 이걸 불러 대외협력을 닫는다 */
  window.pfmCloseEaView = function () { if (eaState.open) { setOpen(false); } };
  window.pfmEaIsOpen = function () { return eaState.open; };

  function init() {
    if (!$('eaTab') || !$('eaPanel')) { return; }
    readUrl();
    $('eaTab').addEventListener('click', toggle);

    [['eaSort', 'sort'], ['eaAgency', 'agency'], ['eaGroup', 'group'],
     ['eaImpact', 'impact'], ['eaStatus', 'status'], ['eaDue', 'due']].forEach(function (pair) {
      $(pair[0]).addEventListener('change', function (e) {
        eaState[pair[1]] = e.target.value;
        eaState.page = 1;
        load();
      });
    });

    var timer;
    $('eaSearch').addEventListener('input', function (e) {
      clearTimeout(timer);
      var v = e.target.value.trim();
      timer = setTimeout(function () {
        eaState.q = v;
        eaState.page = 1;
        load();
      }, 300);
    });

    // ?ea_sub= 등이 URL 에 있으면 새로고침 후에도 패널을 연 상태로 복원한다
    var p = new URLSearchParams(location.search);
    if (EA_KEYS.some(function (k) { return p.has(k); })) { toggle(); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
