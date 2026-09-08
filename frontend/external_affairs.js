/* ── 대외협력(대관) — 독립 화면(/ea) ─────────────────────────────────
   즐겨찾기 탭과 같은 방식: 버튼을 누르면 대외협력 화면으로 전환되고
   (버튼 라벨이 '← 전체 기사'로 바뀐다), 다시 누르면 뉴스로 돌아온다.
   필터는 메인 뉴스 필터(.filters)와 같은 접기식 칩 패널이되 대외협력 전용이다.
   app.js 의 state 는 건드리지 않는다. URL 은 경로(/ea) + ea_ 쿼리.
   ------------------------------------------------------------------ */
(function () {
  'use strict';

  var EA_PAGE_SIZE = 9;
  var EA_PATH = '/ea';

  // 카테고리(예전의 상단 탭) — 이제 필터의 '카테고리' 칩 행이다. 단일 선택.
  //  kind 'items' : /api/ea/items          kind 'news' : /api/ea/{news}(기사 재사용)
  var CATS = [
    { key: 'notice',   label: '입법·행정예고', kind: 'items', types: 'legislation,admin_notice',
      sort: 'deadline', hasDeadline: true },
    { key: 'bill',     label: '국회 의안',     kind: 'items', types: 'bill',
      sort: 'deadline', hasDeadline: true },
    { key: 'policy',   label: '정책 동향',     kind: 'news',  news: '/api/ea/policy-news' },
    { key: 'trade',    label: '통상 환경',     kind: 'news',  news: '/api/ea/trade-news' },
    { key: 'ministry', label: '부처별 동향',   kind: 'items', types: 'ministry_news',
      sort: 'recent', hasDeadline: false }
  ];
  var IMPACT_LABEL = { high: '영향 높음', medium: '영향 보통', low: '영향 낮음', none: '영향 없음' };

  var eaState = {
    open: false, loaded: false, cat: 'notice',
    agency: new Set(), impact: new Set(), group: new Set(),   // 중복 선택 (칩)
    status: '', due: '', sort: 'deadline', q: '',              // 단일 (드롭다운/검색)
    page: 1, loading: false, filterCollapsed: false,
    newsAgencies: []   // policy·trade 에서 로드한 항목으로 채운 기관 칩 목록
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

  function currentCat() {
    for (var i = 0; i < CATS.length; i += 1) {
      if (CATS[i].key === eaState.cat) { return CATS[i]; }
    }
    return CATS[0];
  }

  var csv = function (set) { return Array.from(set).join(','); };

  /* ── 칩 렌더 (메인 .chip 스타일 재사용) ── */
  function renderChips(container, options, selected, onToggle, single) {
    if (!container) { return; }
    container.replaceChildren.apply(container, options.map(function (o) {
      var key = typeof o === 'string' ? o : o.key;
      var label = typeof o === 'string' ? o : o.label;
      var b = el('button', 'chip', label);
      b.type = 'button';
      b.setAttribute('aria-pressed',
        String(single ? selected === key : selected.has(key)));
      b.addEventListener('click', function () { onToggle(key); });
      return b;
    }));
  }

  /* ── URL: 경로(/ea) + ea_ 접두사 쿼리 ── */
  var EA_KEYS = ['ea_cat', 'ea_agency', 'ea_group', 'ea_impact', 'ea_status', 'ea_due',
    'ea_q', 'ea_sort', 'ea_page'];

  function readUrl() {
    var p = new URLSearchParams(location.search);
    eaState.cat = p.get('ea_cat') || 'notice';
    eaState.agency = new Set((p.get('ea_agency') || '').split(',').filter(Boolean));
    eaState.group = new Set((p.get('ea_group') || '').split(',').filter(Boolean));
    eaState.impact = new Set((p.get('ea_impact') || '').split(',').filter(Boolean));
    eaState.status = p.get('ea_status') || '';
    eaState.due = p.get('ea_due') || '';
    eaState.q = p.get('ea_q') || '';
    eaState.sort = p.get('ea_sort') || currentCat().sort || 'deadline';
    eaState.page = Math.max(1, parseInt(p.get('ea_page') || '1', 10) || 1);
  }

  function writeUrl(push) {
    var p = new URLSearchParams(location.search);
    EA_KEYS.forEach(function (k) { p.delete(k); });
    var path = location.pathname;
    if (eaState.open) {
      path = EA_PATH;
      if (eaState.cat !== 'notice') { p.set('ea_cat', eaState.cat); }
      if (eaState.agency.size) { p.set('ea_agency', csv(eaState.agency)); }
      if (eaState.group.size) { p.set('ea_group', csv(eaState.group)); }
      if (eaState.impact.size) { p.set('ea_impact', csv(eaState.impact)); }
      if (eaState.status) { p.set('ea_status', eaState.status); }
      if (eaState.due) { p.set('ea_due', eaState.due); }
      if (eaState.q) { p.set('ea_q', eaState.q); }
      if (eaState.sort && eaState.sort !== currentCat().sort) { p.set('ea_sort', eaState.sort); }
      if (eaState.page > 1) { p.set('ea_page', String(eaState.page)); }
    } else if (path === EA_PATH) {
      path = '/';
    }
    var qs = p.toString();
    var url = path + (qs ? '?' + qs : '');
    if (push) { history.pushState({ ea: eaState.open }, '', url); }
    else { history.replaceState({ ea: eaState.open }, '', url); }
  }

  function activeFilterCount() {
    return eaState.agency.size + eaState.impact.size + eaState.group.size
      + (eaState.status ? 1 : 0) + (eaState.due ? 1 : 0) + (eaState.q ? 1 : 0);
  }

  /* ── 마감 임박 배너 (D-7 이내가 있을 때만) ── */
  function renderAlert(stats) {
    var box = $('eaAlert');
    var urgent = (stats && stats.urgent) || [];
    if (!urgent.length || !currentCat().hasDeadline) {
      box.hidden = true; box.replaceChildren(); return;
    }
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

  /* ── 항목 카드 (예고·부처동향) ── */
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

  /* ── 정책/통상 뉴스 카드 ── */
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
      [n.agency || n.press, fmtDate(n.published_at)].filter(Boolean).join(' · ')));
    if (n.summary) { body.append(el('p', 'ea-summary', n.summary)); }
    card.append(body);
    return card;
  }

  /* ── 필터: 카테고리 칩 + 카테고리별 행 노출 ── */
  function renderCatChips() {
    renderChips($('eaCatChips'), CATS, eaState.cat, function (key) {
      if (eaState.cat === key) { return; }
      eaState.cat = key;
      eaState.page = 1;
      eaState.agency.clear(); eaState.impact.clear(); eaState.group.clear();
      eaState.status = ''; eaState.due = '';
      eaState.sort = currentCat().sort || 'deadline';
      renderCatChips();
      applyRowVisibility();
      loadFilters().then(load);
      loadStats();
    }, true);
  }

  function applyRowVisibility() {
    var c = currentCat();
    var isNews = c.kind === 'news';
    $('eaAgencyRow').hidden = false;                     // 기관은 모든 카테고리
    $('eaImpactRow').hidden = isNews;                    // 영향도·그룹사는 예고·부처동향만
    $('eaGroupRow').hidden = isNews;
    $('eaMiscRow').hidden = isNews;                      // 정렬·상태·마감은 items 만
    $('eaSort').hidden = isNews;
    $('eaStatus').hidden = isNews || !c.hasDeadline;
    $('eaDue').hidden = isNews || !c.hasDeadline;
  }

  function fillSelect(sel, options, value, placeholder) {
    sel.replaceChildren();
    if (placeholder !== null) {
      var o0 = el('option', null, placeholder); o0.value = ''; sel.append(o0);
    }
    options.forEach(function (o) {
      var opt = el('option', null, o.label); opt.value = o.key; sel.append(opt);
    });
    sel.value = value || '';
  }

  function loadFilters() {
    var c = currentCat();
    return getJSON('/api/ea/filters?category=' + encodeURIComponent(c.key)).then(function (d) {
      fillSelect($('eaSort'), d.sorts || [], eaState.sort || c.sort || 'deadline', null);
      fillSelect($('eaStatus'), (d.statuses || []).map(function (s) {
        return { key: s, label: s };
      }), eaState.status, '상태 전체');
      fillSelect($('eaDue'), d.dues || [], eaState.due, '마감 전체');

      var agencies = c.kind === 'news'
        ? eaState.newsAgencies.map(function (a) { return { key: a, label: a }; })
        : (d.agencies || []);
      // 카테고리가 바뀌어 선택된 기관이 목록에 없으면 해제
      Array.from(eaState.agency).forEach(function (k) {
        if (!agencies.some(function (a) { return a.key === k; })) { eaState.agency.delete(k); }
      });
      renderChips($('eaAgencyChips'), agencies, eaState.agency, function (key) {
        toggleSet(eaState.agency, key); load();
      });
      renderChips($('eaImpactChips'), d.impacts || [], eaState.impact, function (key) {
        toggleSet(eaState.impact, key); load();
      });
      renderChips($('eaGroupChips'), d.groups || [], eaState.group, function (key) {
        toggleSet(eaState.group, key); load();
      });
      syncFilterHead();
    }).catch(function () { /* 필터는 없어도 목록은 보여준다 */ });
  }

  function toggleSet(set, key) {
    if (set.has(key)) { set.delete(key); } else { set.add(key); }
    eaState.page = 1;
  }

  function syncFilterHead() {
    var n = activeFilterCount();
    var badge = $('eaActiveCount');
    if (n) { badge.textContent = n + '개 적용'; badge.hidden = false; }
    else { badge.hidden = true; }
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
    $('eaCount').textContent = '—';
  }

  function load() {
    if (eaState.loading) { return; }
    eaState.loading = true;
    writeUrl();
    syncFilterHead();

    var c = currentCat();
    $('eaList').replaceChildren.apply($('eaList'), [0, 1, 2].map(function () {
      return el('div', 'ea-skeleton');
    }));
    var done = function () { eaState.loading = false; };

    if (c.kind === 'news') {
      var np = new URLSearchParams();
      np.set('limit', '80');
      if (eaState.agency.size) { np.set('agency', csv(eaState.agency)); }
      getJSON(c.news + '?' + np.toString()).then(function (d) {
        var items = d.items || [];
        if (!eaState.agency.size) {
          var set = {};
          items.forEach(function (x) { if (x.agency) { set[x.agency] = 1; } });
          eaState.newsAgencies = Object.keys(set).sort();
          renderChips($('eaAgencyChips'),
            eaState.newsAgencies.map(function (a) { return { key: a, label: a }; }),
            eaState.agency, function (key) { toggleSet(eaState.agency, key); load(); });
        }
        var start = (eaState.page - 1) * EA_PAGE_SIZE;
        $('eaCount').textContent = items.length.toLocaleString('ko-KR') + '건';
        renderPager(items.length);
        if (!items.length) { showState('', '최근 45일 내 해당 자료가 없습니다.'); return; }
        $('eaList').replaceChildren.apply($('eaList'),
          items.slice(start, start + EA_PAGE_SIZE).map(buildNewsCard));
      }).catch(function () {
        showState('is-error', '자료를 불러오지 못했습니다.');
      }).finally(done);
      return;
    }

    var p = new URLSearchParams();
    p.set('item_type', c.types);
    if (eaState.agency.size) { p.set('agency', csv(eaState.agency)); }
    if (eaState.group.size) { p.set('group', csv(eaState.group)); }
    if (eaState.impact.size) { p.set('impact', csv(eaState.impact)); }
    if (eaState.status) { p.set('status', eaState.status); }
    if (eaState.due) { p.set('due', eaState.due); }
    if (eaState.q) { p.set('q', eaState.q); }
    p.set('sort', eaState.sort || c.sort || 'deadline');
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

  function clearAll() {
    eaState.agency.clear(); eaState.impact.clear(); eaState.group.clear();
    eaState.status = ''; eaState.due = ''; eaState.q = '';
    eaState.page = 1;
    $('eaSearch').value = '';
    loadFilters().then(load);
  }

  /* ── 화면 열고 닫기 (즐겨찾기 탭과 동일한 방식) ── */
  function setOpen(on, opts) {
    opts = opts || {};
    eaState.open = on;
    var tab = $('eaTab');
    tab.setAttribute('aria-pressed', String(on));
    tab.textContent = on ? '← 전체 기사' : '🏛 대외협력';
    $('eaPanel').hidden = !on;
    ['.filters:not(.ea-filter-panel)', '#grid', '.more-wrap', '.url-add', '#stats']
      .forEach(function (sel) {
        var n = document.querySelector(sel);
        if (n) { n.hidden = on; }
      });
    if (!opts.silent) { writeUrl(opts.push); }
    if (on) {
      eaState.loaded = true;
      renderCatChips();
      applyRowVisibility();
      loadFilters().then(load);
      loadStats();
    }
  }

  function openEa() {
    if (window.pfmCloseOtherViews) { window.pfmCloseOtherViews(); }
    setOpen(true, { push: true });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function closeEa() {
    setOpen(false, { push: true });
    if (window.pfmRefreshList) { window.pfmRefreshList(); }
  }

  function toggle() {
    if (eaState.open) { closeEa(); } else { openEa(); }
  }

  window.pfmCloseEaView = function () {
    if (eaState.open) { setOpen(false, { push: false }); }
  };
  window.pfmEaIsOpen = function () { return eaState.open; };

  function init() {
    if (!$('eaTab') || !$('eaPanel')) { return; }
    readUrl();
    $('eaTab').addEventListener('click', toggle);

    $('eaFilterToggle').addEventListener('click', function () {
      eaState.filterCollapsed = !eaState.filterCollapsed;
      $('eaFilterToggle').setAttribute('aria-expanded', String(!eaState.filterCollapsed));
      $('eaFilterBody').hidden = eaState.filterCollapsed;
    });
    $('eaClearAll').addEventListener('click', clearAll);

    [['eaSort', 'sort'], ['eaStatus', 'status'], ['eaDue', 'due']].forEach(function (pair) {
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
        eaState.q = v; eaState.page = 1; load();
      }, 300);
    });

    window.addEventListener('popstate', function () {
      var wantEa = location.pathname === EA_PATH;
      if (wantEa && !eaState.open) { readUrl(); setOpen(true, { silent: true }); }
      else if (!wantEa && eaState.open) {
        setOpen(false, { silent: true });
        if (window.pfmRefreshList) { window.pfmRefreshList(); }
      }
    });

    var p = new URLSearchParams(location.search);
    if (location.pathname === EA_PATH || EA_KEYS.some(function (k) { return p.has(k); })) {
      if (window.pfmCloseOtherViews) { window.pfmCloseOtherViews(); }
      $('eaSearch').value = eaState.q;
      setOpen(true, { silent: true });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
