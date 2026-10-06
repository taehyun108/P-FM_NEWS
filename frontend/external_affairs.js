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
    { key: 'policy', label: '정책 동향', kind: 'items', types: 'policy_press',
      sort: 'priority', hasDeadline: false, layout: 'policy' },
    { key: 'notice', label: '입법·행정예고', kind: 'items', types: 'legislation,admin_notice',
      sort: 'deadline', hasDeadline: true, layout: 'notice' },    // 의견제출 마감(D-day) 중심
    { key: 'bill',   label: '국회 의안', kind: 'items', types: 'bill',
      sort: 'priority', hasDeadline: false, layout: 'bill' },
    { key: 'trade',  label: '통상 환경', kind: 'items', types: 'trade_webzine',
      sort: 'priority', hasDeadline: false, layout: 'trade' },   // 산업통상부 월간 통상(tongsangnews.kr)
    { key: 'grant',  label: '공모·수요조사', kind: 'items', types: 'grant_notice',
      sort: 'deadline', hasDeadline: true, layout: 'grant' },     // 산업부·기후부 사업공고 + IRIS
    { key: 'calendar', label: '일정', kind: 'calendar', layout: 'calendar' }   // 모든 마감일을 달력으로
  ];
  // 우선순위 4단계 — 기준은 화면의 '기준 보기'와 같은 말이다(백엔드 ea_priority 규칙).
  var PRIORITIES = ['긴급', '중요', '관심', '일반'];
  var PRIORITY_CLASS = { '긴급': 'is-urgent', '중요': 'is-important', '관심': 'is-interest', '일반': 'is-normal' };

  var eaState = {
    open: false, loaded: false, cat: 'policy',
    agency: new Set(), priority: new Set(), group: new Set(),   // 중복 선택 (칩)
    status: '', due: '', sort: 'priority', q: '',              // 단일 (드롭다운/검색)
    page: 1, loading: false, filterCollapsed: false,
    month: '', day: ''     // 일정 화면: 보고 있는 달(YYYY-MM)·선택한 날(YYYY-MM-DD)
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
      b.addEventListener('click', function () {
        onToggle(key);
        if (!single) { b.setAttribute('aria-pressed', String(selected.has(key))); }   // 선택 표시를 바로 반영
      });
      return b;
    }));
  }

  /* ── URL: 경로(/ea) + ea_ 접두사 쿼리 ── */
  var EA_KEYS = ['ea_cat', 'ea_agency', 'ea_group', 'ea_priority', 'ea_status', 'ea_due',
    'ea_q', 'ea_sort', 'ea_page'];

  function readUrl() {
    var p = new URLSearchParams(location.search);
    eaState.cat = ['notice', 'bill', 'trade', 'grant', 'calendar'].indexOf(p.get('ea_cat')) >= 0 ? p.get('ea_cat') : 'policy';
    eaState.agency = new Set((p.get('ea_agency') || '').split(',').filter(Boolean));
    eaState.group = new Set((p.get('ea_group') || '').split(',').filter(Boolean));
    eaState.priority = new Set((p.get('ea_priority') || '').split(',').filter(Boolean));
    eaState.status = p.get('ea_status') || '';
    eaState.due = p.get('ea_due') || '';
    eaState.q = p.get('ea_q') || '';
    eaState.sort = p.get('ea_sort') || currentCat().sort || 'priority';
    eaState.page = Math.max(1, parseInt(p.get('ea_page') || '1', 10) || 1);
  }

  function writeUrl(push) {
    var p = new URLSearchParams(location.search);
    EA_KEYS.forEach(function (k) { p.delete(k); });
    var path = location.pathname;
    if (eaState.open) {
      path = EA_PATH;
      if (eaState.cat !== 'policy') { p.set('ea_cat', eaState.cat); }
      if (eaState.agency.size) { p.set('ea_agency', csv(eaState.agency)); }
      if (eaState.group.size) { p.set('ea_group', csv(eaState.group)); }
      if (eaState.priority.size) { p.set('ea_priority', csv(eaState.priority)); }
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
    return eaState.agency.size + eaState.priority.size + eaState.group.size
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

  /* 텔레그램 전송 버튼 — 뉴스 카드의 '↗ 직접 전송'과 같은 direct-send 방식.
     endpoint 만 다르게 넘긴다(기사=/api/articles/.., 대외협력 항목=/api/ea/items/..). */
  function telegramButton(endpoint) {
    var btn = el('button', 'ea-tg-btn', '✈ 텔레그램 전송');
    btn.type = 'button';
    btn.addEventListener('click', function () {
      var original = btn.textContent;
      btn.disabled = true;
      btn.textContent = '전송 중…';
      fetch(endpoint, { method: 'POST' })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (d) {
          btn.textContent = d.ok ? '✓ 전송됨' : (d.error || '전송 실패');
        })
        .catch(function () { btn.textContent = '서버 연결 실패'; })
        .finally(function () {
          setTimeout(function () { btn.textContent = original; btn.disabled = false; }, 2500);
        });
    });
    return btn;
  }

  /* 마감 D-day 칩 — 3일 이내 빨강, 7일 이내 주황 */
  function ddayChip(it) {
    var d = it.d_day;
    if (d === null || d === undefined) { return el('span', 'ea-dday is-none', '기한 없음'); }
    if (d < 0) { return el('span', 'ea-dday is-closed', '마감'); }
    return el('span', 'ea-dday ' + (d <= 3 ? 'is-urgent' : d <= 7 ? 'is-soon' : 'is-open'),
      d === 0 ? 'D-DAY' : 'D-' + d);
  }

  /* ── 일정 캘린더 — 의견제출 마감일을 월간 달력 + 목록으로 ── */
  var WEEKDAYS = ['일', '월', '화', '수', '목', '금', '토'];

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function shiftMonth(ym, delta) {
    var y = parseInt(ym.slice(0, 4), 10), m = parseInt(ym.slice(5, 7), 10) - 1 + delta;
    y += Math.floor(m / 12); m = ((m % 12) + 12) % 12;
    return y + '-' + pad2(m + 1);
  }

  function agendaRow(e) {
    var li = el('li', 'ea-ag-item');
    var d = e.d_day;
    var chip = el('span', 'ea-dday ' + (d === null || d === undefined ? 'is-none' : d < 0 ? 'is-closed'
      : d <= 3 ? 'is-urgent' : d <= 7 ? 'is-soon' : 'is-open'),
      d === null || d === undefined ? '' : d < 0 ? '마감' : d === 0 ? 'D-DAY' : 'D-' + d);
    var top = el('div', 'ea-ag-top');
    top.append(chip, prioBadge(e), el('span', 'ea-ag-kind', e.kind), el('span', 'ea-ag-date', e.date));
    li.append(top);
    var t = el('div', 'ea-ag-title');
    t.append(link(e.title, e.url));
    li.append(t);
    var sub = [e.agency, e.status].filter(Boolean).join(' · ');
    if (sub) { li.append(el('div', 'ea-ag-sub', sub)); }
    if (e.opinion_url) { li.append(link('의견 제출', e.opinion_url, 'ea-ag-act')); }
    return li;
  }

  function renderCalendar(d) {
    var box = el('div', 'ea-cal');
    var ym = d.month;
    var head = el('div', 'ea-cal-head');
    var prev = el('button', 'ea-cal-nav', '‹'); prev.type = 'button'; prev.setAttribute('aria-label', '이전 달');
    var next = el('button', 'ea-cal-nav', '›'); next.type = 'button'; next.setAttribute('aria-label', '다음 달');
    var todayBtn = el('button', 'ea-cal-today', '오늘'); todayBtn.type = 'button';
    prev.addEventListener('click', function () { eaState.month = shiftMonth(ym, -1); eaState.day = ''; loadCalendar(); });
    next.addEventListener('click', function () { eaState.month = shiftMonth(ym, 1); eaState.day = ''; loadCalendar(); });
    todayBtn.addEventListener('click', function () { eaState.month = ''; eaState.day = ''; loadCalendar(); });
    head.append(prev, el('b', 'ea-cal-title', ym.slice(0, 4) + '년 ' + parseInt(ym.slice(5, 7), 10) + '월'), next, todayBtn);
    box.append(head);

    var byDay = {};
    d.events.forEach(function (e) { (byDay[e.date] = byDay[e.date] || []).push(e); });
    var grid = el('div', 'ea-cal-grid');
    WEEKDAYS.forEach(function (w, i) { grid.append(el('div', 'ea-cal-wd' + (i === 0 ? ' is-sun' : ''), w)); });
    var first = new Date(parseInt(ym.slice(0, 4), 10), parseInt(ym.slice(5, 7), 10) - 1, 1);
    var days = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
    for (var i = 0; i < first.getDay(); i += 1) { grid.append(el('div', 'ea-cal-cell is-empty')); }
    for (var day = 1; day <= days; day += 1) {
      var iso = ym + '-' + pad2(day);
      var evs = byDay[iso] || [];
      var cell = el('button', 'ea-cal-cell' + (iso === d.today ? ' is-today' : '') + (iso === eaState.day ? ' is-sel' : '')
        + (evs.length ? ' has-ev' : ''));
      cell.type = 'button';
      cell.append(el('span', 'ea-cal-num', String(day)));
      if (evs.length) {
        var dots = el('span', 'ea-cal-dots');
        evs.slice(0, 4).forEach(function (e) {
          dots.append(el('i', 'ea-cal-dot ' + (PRIORITY_CLASS[e.priority] || 'is-normal')));
        });
        if (evs.length > 4) { dots.append(el('em', null, '+' + (evs.length - 4))); }
        cell.append(dots, el('span', 'ea-cal-count', evs.length + '건'));
        cell.title = evs.map(function (e) { return '[' + e.priority + '] ' + e.title; }).join('\n');
      }
      (function (isoDay, n) {
        cell.addEventListener('click', function () {
          if (!n) { return; }
          eaState.day = eaState.day === isoDay ? '' : isoDay;
          renderCalendar(d);
        });
      }(iso, evs.length));
      grid.append(cell);
    }
    box.append(grid);

    var agenda = el('div', 'ea-agenda');
    var list = el('ul', 'ea-ag-list');
    var rows, title;
    if (eaState.day) {
      rows = byDay[eaState.day] || [];
      title = eaState.day + ' 마감 ' + rows.length + '건';
    } else if (ym === d.today.slice(0, 7) || !d.events.length) {
      rows = d.upcoming; title = '다가오는 마감 ' + rows.length + '건';
    } else {
      rows = d.events; title = ym.slice(5) + '월 마감 ' + rows.length + '건';
    }
    var ah = el('div', 'ea-ag-head');
    ah.append(el('b', null, title));
    if (eaState.day) {
      var clr = el('button', 'ea-fold-btn', '선택 해제'); clr.type = 'button';
      clr.addEventListener('click', function () { eaState.day = ''; renderCalendar(d); });
      ah.append(clr);
    }
    agenda.append(ah);
    if (!rows.length) { agenda.append(el('p', 'ea-state', '표시할 마감 일정이 없습니다.')); }
    rows.forEach(function (e) { list.append(agendaRow(e)); });
    agenda.append(list);
    box.append(agenda);
    $('eaList').replaceChildren(box);
    $('eaCount').textContent = d.events.length + '건 (이 달)';
  }

  function loadCalendar() {
    if (eaState.loading) { return; }
    eaState.loading = true;
    $('eaPager').hidden = true;
    getJSON('/api/ea/calendar' + (eaState.month ? '?month=' + eaState.month : '')).then(function (d) {
      eaState.month = d.month;
      renderCalendar(d);
    }).catch(function () {
      showState('is-error', '일정을 불러오지 못했습니다.');
    }).finally(function () { eaState.loading = false; });
  }

  /* ── 우선순위 배지 — 마우스를 올리거나(PC) 눌러서(모바일) '포스코퓨처엠에 미치는 영향'을 쉬운 말로 보여 준다 ── */
  // 등급별 쉬운 설명 (백엔드 ea_priority 규칙과 같은 기준)
  var PRIO_ELI5 = {
    '긴급': { head: '긴급 — 지금 바로 챙겨야 해요',
      easy: '우리 회사(포스코퓨처엠)가 하는 일과 직접 관련된 소식인데, 곧 시행되거나 결정되거나 마감이 다가오는 단계예요. ' +
            '비유하면 “우리 집 앞 도로 공사가 내일부터 시작돼요”라는 공지와 같아요. 미리 준비하지 않으면 바로 영향을 받을 수 있어요.' },
    '중요': { head: '중요 — 꼭 알아 두세요',
      easy: '우리 회사가 하는 일(배터리 소재·핵심광물·무역 등)과 직접 관련된 소식이에요. 다만 아직 “논의 중”이거나 “계획 단계”라 당장 시행되지는 않아요. ' +
            '비유하면 “우리 동네에 도로를 새로 낼 계획이 있대요”라는 소문이에요. 지금 급하진 않지만 어떻게 흘러가는지 지켜봐야 해요.' },
    '관심': { head: '관심 — 가볍게 지켜봐요',
      easy: '포스코퓨처엠이 직접 영향을 받는 소식은 아니고, 포스코 그룹의 다른 회사(철강·건설·에너지·무역 등)와 관련된 소식이에요. ' +
            '비유하면 “옆집에 공사가 있대요”처럼, 우리 집엔 당장 상관없지만 소문은 알아 두면 좋은 정도예요.' },
    '일반': { head: '일반 — 지금은 참고만 해요',
      easy: '지금 확인된 내용만으로는 포스코 그룹 사업과 직접 이어지는 곳을 찾지 못했어요. 그래서 우선순위를 가장 낮게 두었어요. ' +
            '비유하면 “다른 동네 소식”이에요. 나중에 내용이 바뀌면 등급도 바뀔 수 있어요.' }
  };
  // 제목에 들어 있으면 '우리 사업과 직접 연결'로 보는 말의 뜻 (어려운 용어 풀이)
  var KW_ELI5 = {
    '이차전지': '전기차·스마트폰에 들어가는 충전식 배터리예요. 포스코퓨처엠이 배터리 재료를 만들어 팔아요.',
    '2차전지': '전기차·스마트폰에 들어가는 충전식 배터리예요. 포스코퓨처엠이 배터리 재료를 만들어 팔아요.',
    '배터리': '충전해서 쓰는 전지예요. 포스코퓨처엠의 주력 고객 산업이에요.',
    '양극재': '배터리의 “+극” 재료로 배터리 값과 성능을 좌우해요. 포스코퓨처엠의 핵심 제품이에요.',
    '음극재': '배터리의 “−극” 재료예요. 포스코퓨처엠이 만드는 핵심 제품이에요.',
    '전구체': '양극재를 만들기 전 단계 재료예요. 정책이 바뀌면 원가와 공급에 바로 영향이 있어요.',
    '리튬': '배터리의 핵심 원료(금속)예요. 값이 오르내리면 우리 원가가 달라져요.',
    '니켈': '고성능 배터리에 쓰이는 원료(금속)예요. 값과 공급이 우리 원가를 좌우해요.',
    '코발트': '배터리 원료(금속)예요. 나는 곳이 한정돼 있어 공급 이슈가 잦아요.',
    '흑연': '배터리 음극재의 주원료예요. 특정 나라에 의존해서 수출 규제 영향이 커요.',
    '전해액': '배터리 안에서 전기가 오가게 하는 액체예요. 배터리 생태계 소식이에요.',
    '분리막': '배터리 안 +극과 −극이 닿지 않게 막는 얇은 막이에요. 배터리 생태계 소식이에요.',
    '핵심광물': '배터리·반도체 등에 꼭 필요한 귀한 광물이에요. 확보 정책이 우리 원료 조달과 직결돼요.',
    '희토류': '첨단 제품에 쓰이는 희귀 금속이에요. 공급 제한이 생기면 산업 전반이 흔들려요.',
    '소재부품장비': '제조에 꼭 필요한 재료·부품·장비를 뜻해요. 정부 지원 대상에 들면 혜택을 받을 수 있어요.',
    '소부장': '소재·부품·장비의 줄임말이에요. 정부 지원 대상에 들면 혜택을 받을 수 있어요.',
    '전기차': '배터리를 가장 많이 쓰는 고객 산업이에요. 전기차가 안 팔리면 우리 재료도 덜 팔려요.',
    '국가첨단전략산업': '정부가 “꼭 키울 산업”으로 정한 분야예요. 세금 혜택·지원이 걸려 있어요.',
    '포스코퓨처엠': '우리 회사 이름이 직접 나온 소식이에요.',
    '관세': '물건이 국경을 넘을 때 내는 세금이에요. 올라가면 수출 가격이 비싸져 팔기 어려워져요.',
    '반덤핑': '“싸게 팔아 시장을 망친다”며 상대국이 물리는 벌금성 관세예요. 수출에 직접 타격이에요.',
    '상계관세': '“정부 보조금으로 싸게 팔았다”며 물리는 관세예요. 수출에 직접 타격이에요.',
    '수출통제': '특정 물건을 나라 밖으로 못 팔게 막는 규제예요. 원료·제품 거래가 막힐 수 있어요.',
    '공급망': '재료가 만들어져 우리 손에 오고 제품으로 팔리는 전체 흐름이에요. 끊기면 생산이 멈춰요.',
    '원산지': '“이 물건이 어느 나라 것인가”를 가리는 기준이에요. 혜택(세금·보조금) 여부가 갈려요.',
    '대미투자': '미국에 하는 투자예요. 관세 협상과 엮여 우리 수출 조건에 영향을 줘요.',
    '무역협상': '나라끼리 관세·수출입 조건을 정하는 협상이에요. 결과가 우리 판매에 직접 영향을 줘요.'
  };
  // '곧 영향이 생기는 단계'로 보는 말의 뜻
  var STAGE_ELI5 = {
    '시행': '법·제도가 실제로 적용되기 시작한다는 뜻이에요.',
    '공포': '확정된 법을 나라가 공식 발표했다는 뜻이에요. 곧 시행돼요.',
    '의결': '회의에서 “이렇게 하자”고 결정했다는 뜻이에요.',
    '통과': '국회 등에서 표결을 통과했다는 뜻이에요. 거의 확정 단계예요.',
    '가결': '표결에서 찬성이 많아 통과됐다는 뜻이에요.',
    '확정': '내용이 최종으로 정해졌다는 뜻이에요.',
    '입법예고': '“법을 이렇게 바꿀 건데 의견 주세요”라고 미리 알리는 단계예요. 의견을 낼 수 있는 기간이 정해져 있어요.',
    '행정예고': '“규정을 이렇게 바꿀 건데 의견 주세요”라고 미리 알리는 단계예요. 의견을 낼 수 있는 기간이 정해져 있어요.',
    '의견수렴': '정부가 사람들 의견을 받는 중이에요. 기간이 끝나면 닫혀요.',
    '국무회의': '정부가 중요한 일을 결정하는 회의예요. 여기서 정해지면 곧 실행돼요.',
    '본회의': '국회의원 전체가 모여 최종 표결하는 회의예요.',
    '발효': '조약·규정의 효력이 실제로 생기는 때예요.',
    '부과': '세금·관세를 실제로 매긴다는 뜻이에요.',
    '개정안발의': '법을 바꾸자는 안이 국회에 올라왔다는 뜻이에요.'
  };
  var IMPACT_ELI5 = {
    high: '영향이 큰 편이에요 — 우리 매출·원가·규제에 직접 닿을 수 있어요.',
    medium: '영향이 중간 정도예요 — 일부 사업이나 제품에 닿을 수 있어요.',
    low: '영향이 작은 편이에요 — 간접적으로만 닿아요.',
    none: '우리 사업과 직접 이어지는 내용은 찾지 못했어요.'
  };

  var prioTipEl = null, prioTipOwner = null;
  function prioTip() {
    if (!prioTipEl) {
      prioTipEl = el('div', 'ea-prio-tip');
      prioTipEl.setAttribute('role', 'tooltip');
      prioTipEl.hidden = true;
      document.body.append(prioTipEl);
    }
    return prioTipEl;
  }
  function hidePrioTip() {
    if (prioTipEl) { prioTipEl.hidden = true; }
    if (prioTipOwner) { prioTipOwner.setAttribute('aria-expanded', 'false'); }
    prioTipOwner = null;
  }

  /* 항목 하나의 '왜 이 등급인가 + 포스코퓨처엠 영향'을 쉬운 말로 풀어 만든다 */
  function fillPrioTip(tip, it) {
    var info = PRIO_ELI5[it.priority] || PRIO_ELI5['일반'];
    var why = it.priority_reason || '';
    tip.replaceChildren();
    tip.append(el('h4', null, info.head));
    tip.append(el('p', 'ea-tip-easy', info.easy));

    // 왜 이 등급이에요?
    tip.append(el('h5', null, '왜 이 등급이에요?'));
    var ul = el('ul');
    var m;
    if ((m = why.match(/제목에 '([^']+)'/))) {
      ul.append(el('li', null, '제목에 “' + m[1] + '”가 있어요. ' + (KW_ELI5[m[1]] || '포스코퓨처엠 사업과 직접 이어지는 말이에요.')));
    }
    if (why.indexOf('포스코퓨처엠 관련 분야로 분류') >= 0) {
      ul.append(el('li', null, '글 내용이 포스코퓨처엠이 하는 일과 관련 있다고 분류됐어요.'));
    }
    if (why.indexOf("AI 영향도 '높음'") >= 0) {
      ul.append(el('li', null, 'AI가 글을 읽고 “우리에게 영향이 크다”고 판단했어요.'));
    }
    if ((m = why.match(/마감 D-(\d+)/))) {
      ul.append(el('li', null, '마감까지 ' + m[1] + '일 남았어요. 기한 안에 대응(의견 제출 등)해야 해요.'));
    }
    if ((m = why.match(/'([^']+)' 단계/))) {
      var stage = STAGE_ELI5[m[1].replace(/\s+/g, '')];
      ul.append(el('li', null, '“' + m[1] + '” 단계예요. ' + (stage || '곧 영향이 생기는 단계예요.')));
    }
    if (why.indexOf('시행·의결 등 임박 단계는 아직') >= 0) {
      ul.append(el('li', null, '아직 시행·결정 직전 단계는 아니에요. 그래서 “긴급”이 아니라 “중요”예요.'));
    }
    if (it.priority === '관심') {
      var og = (it.group_companies || []).filter(function (g) { return g !== '포스코퓨처엠'; });
      ul.append(el('li', null, '포스코퓨처엠에는 직접 영향이 없고, ' + (og.length ? og.join('·') + ' 사업과 관련 있어요.' : '그룹 다른 사업(철강·건설·에너지·무역 등)과 관련 있어요.')));
    }
    if (it.priority === '일반') {
      ul.append(el('li', null, '제목·내용에서 포스코 그룹 사업과 이어지는 말을 찾지 못했어요.'));
    }
    if (!ul.childNodes.length) { ul.append(el('li', null, why || '판단 근거가 아직 없어요.')); }
    tip.append(ul);

    // 포스코퓨처엠에는 어떤 영향이 있어요?
    tip.append(el('h5', null, '포스코퓨처엠에는 어떤 영향이 있어요?'));
    var ul2 = el('ul');
    if (it.impact_level && IMPACT_ELI5[it.impact_level]) {
      ul2.append(el('li', null, 'AI 평가: ' + IMPACT_ELI5[it.impact_level]));
    } else {
      ul2.append(el('li', null, 'AI가 아직 영향도를 평가하지 않았어요(분석 대기 중). 위 등급은 규칙으로만 정했어요.'));
    }
    if (it.impact_rationale) { ul2.append(el('li', null, '근거(원문 인용): ' + it.impact_rationale)); }
    if ((it.affected_areas || []).length) {
      ul2.append(el('li', null, '영향 받는 곳: ' + it.affected_areas.join(', ')));
    }
    if (it.suggested_action) { ul2.append(el('li', null, '이렇게 해 보세요: ' + it.suggested_action)); }
    tip.append(ul2);
    tip.append(el('p', 'ea-tip-note', '※ 등급은 AI 점수가 아니라 정해진 규칙으로 매깁니다. 중요한 결정은 꼭 원문을 확인하세요.'));
  }

  function showPrioTip(badge, it) {
    var tip = prioTip();
    fillPrioTip(tip, it);
    tip.hidden = false;
    if (prioTipOwner && prioTipOwner !== badge) { prioTipOwner.setAttribute('aria-expanded', 'false'); }
    prioTipOwner = badge;
    badge.setAttribute('aria-expanded', 'true');
    // 위치: 작은 화면은 화면 아래에 가로로 꽉 차게, 넓은 화면은 배지 아래
    if (window.innerWidth <= 640) {
      tip.style.left = '8px'; tip.style.right = '8px'; tip.style.top = 'auto'; tip.style.bottom = '8px';
      tip.classList.add('is-sheet');
    } else {
      tip.classList.remove('is-sheet');
      tip.style.right = 'auto'; tip.style.bottom = 'auto';
      var r = badge.getBoundingClientRect();
      var w = Math.min(380, window.innerWidth - 24);
      tip.style.width = w + 'px';
      var left = Math.max(12, Math.min(r.left, window.innerWidth - w - 12));
      tip.style.left = left + 'px';
      tip.style.top = (r.bottom + 8) + 'px';
      var th = tip.getBoundingClientRect().height;
      if (r.bottom + 8 + th > window.innerHeight - 8) {            // 아래 공간이 모자라면 위로
        tip.style.top = Math.max(8, r.top - th - 8) + 'px';
      }
    }
  }

  function prioBadge(it) {
    var b = el('button', 'ea-prio ' + (PRIORITY_CLASS[it.priority] || 'is-normal'), it.priority || '일반');
    b.type = 'button';
    b.setAttribute('aria-label', (it.priority || '일반') + ' 등급 — 눌러서 포스코퓨처엠에 미치는 영향 보기');
    b.setAttribute('aria-expanded', 'false');
    var hoverable = window.matchMedia && window.matchMedia('(hover: hover)').matches;
    if (hoverable) {                       // PC: 마우스를 올리면 열리고 벗어나면 닫힌다
      b.addEventListener('mouseenter', function () { showPrioTip(b, it); });
      b.addEventListener('mouseleave', function () { if (prioTipOwner === b) { hidePrioTip(); } });
      b.addEventListener('focus', function () { showPrioTip(b, it); });
      b.addEventListener('blur', function () { if (prioTipOwner === b) { hidePrioTip(); } });
    }
    b.addEventListener('click', function (ev) {   // 모바일(터치): 누르면 열고 다시 누르면 닫는다
      ev.stopPropagation();
      if (prioTipOwner === b && !hoverable) { hidePrioTip(); } else { showPrioTip(b, it); }
    });
    return b;
  }
  document.addEventListener('click', function (ev) {
    if (prioTipOwner && !(prioTipEl && prioTipEl.contains(ev.target))) { hidePrioTip(); }
  });
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') { hidePrioTip(); } });
  window.addEventListener('scroll', function () { if (prioTipEl && !prioTipEl.hidden && window.innerWidth > 640) { hidePrioTip(); } }, true);

  /* 긴 요약은 접어 둔다 — '주요 내용'은 끝까지 읽을 수 있어야 한다 */
  function foldable(textEl, maxLines) {
    var wrap = el('div', 'ea-fold');
    wrap.append(textEl);
    var text = textEl.textContent || '';
    if (text.length > 160) {
      textEl.classList.add('is-folded');
      textEl.style.setProperty('--fold-lines', String(maxLines));
      var more = el('button', 'ea-fold-btn', '더보기 ▾');
      more.type = 'button';
      more.addEventListener('click', function () {
        var folded = textEl.classList.toggle('is-folded');
        more.textContent = folded ? '더보기 ▾' : '접기 ▴';
      });
      wrap.append(more);
    }
    return wrap;
  }

  function party(p) { return p.party ? p.name + '(' + p.party + ')' : p.name; }

  /* ── 항목 카드 — 정책 동향: [등급] 제목 / 소관 / 주요 내용 · 국회 의안: [등급] 제목 / 발의 의원 전원 / 주요 내용 ── */
  function buildCard(it) {
    var layout = currentCat().layout;
    var isBill = layout === 'bill';
    var isTrade = layout === 'trade';
    var isGrant = layout === 'grant';
    var isNotice = layout === 'notice' || isGrant;
    var card = el('article', 'ea-card ea-card--flat');
    if (isTrade && it.thumbnail) {          // 통상 환경 — 포토카드(월간 통상 기사 대표 사진)
      var ph = el('a', 'ea-photo');
      ph.href = it.url; ph.target = '_blank'; ph.rel = 'noopener noreferrer';
      var img = el('img');
      img.src = it.thumbnail; img.alt = ''; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', function () { ph.remove(); });
      ph.append(img);
      card.append(ph);
    }
    var body = el('div', 'ea-card-body');

    var head = el('div', 'ea-card-head');
    head.append(prioBadge(it));
    if (isNotice) { head.append(ddayChip(it)); }
    head.append(el('span', 'ea-card-date', isNotice
      ? (isGrant ? ('공고 ' + fmtDate(it.notice_start) + (it.notice_end ? ' · 접수 마감 ' + fmtDate(it.notice_end) : ' · 접수 마감일 미확인'))
        : (fmtDate(it.notice_start) + ' ~ ' + (fmtDate(it.notice_end) || '미정')))
      : fmtDate(it.published_at || it.notice_start)));
    body.append(head);

    var h = el('h3', 'ea-card-title');
    h.append(link(it.title, it.url));
    body.append(h);

    if (isNotice) {
      var nb = [it.agency && ('소관: ' + it.agency),
        it.law_name && ((isGrant ? '담당·전문기관: ' : '') + it.law_name), it.status].filter(Boolean);
      body.append(el('p', 'ea-card-sub', nb.join(' · ')));
    } else if (isTrade) {
      // 기자명이 없는 정부 발행물 — 출처를 산업통상부(월간 통상)로 적는다
      var src = el('p', 'ea-card-sub');
      src.append(el('b', null, '출처: '), document.createTextNode((it.agency || '산업통상부') + ' · 월간 통상'
        + (it.category ? ' · ' + it.category : '')));
      body.append(src);
    } else if (!isBill) {
      // 기자명 자리에 '소관: 부처'
      var own = el('p', 'ea-card-sub');
      own.append(el('b', null, '소관: '), document.createTextNode(it.agency || '확인 불가'));
      body.append(own);
    } else {
      var ps = it.proposers || [];
      var who = el('div', 'ea-proposers');
      if (ps.length) {
        var rep = ps[0];
        who.append(el('b', null, '발의: '));
        who.append(document.createTextNode(party(rep) + (ps.length > 1 ? ' 외 ' + (ps.length - 1) + '인' : '')));
        if (ps.length > 1) {
          var all = el('p', 'ea-proposer-all is-folded', ps.map(party).join(', '));
          var tog = el('button', 'ea-fold-btn', '발의 의원 ' + ps.length + '명 전원 보기 ▾');
          tog.type = 'button';
          tog.addEventListener('click', function () {
            var f = all.classList.toggle('is-folded');
            tog.textContent = f ? '발의 의원 ' + ps.length + '명 전원 보기 ▾' : '접기 ▴';
          });
          who.append(tog, all);
        }
      } else {
        who.append(el('b', null, '발의: '));
        who.append(document.createTextNode('발의 의원 정보를 가져오지 못했습니다 — 의안정보시스템 원문에서 확인하세요'));
      }
      body.append(who);
      var sub = [it.agency && ('소관위: ' + it.agency), it.status].filter(Boolean).join(' · ');
      if (sub) { body.append(el('p', 'ea-card-sub', sub)); }
    }

    var tags = el('div', 'ea-tags');
    var seen = {};
    (it.group_companies || []).forEach(function (g) {
      if (g && !seen[g]) { seen[g] = 1; tags.append(el('span', 'ea-tag is-group', g)); }
    });
    if (tags.childNodes.length) { body.append(tags); }

    var sum = el('p', 'ea-summary', it.summary || '요약을 아직 만들지 못했습니다 — 원문에서 확인하세요');
    var sumBox = el('div', 'ea-summary-box');
    sumBox.append(el('b', 'ea-summary-label', '주요 내용'));
    sumBox.append(foldable(sum, 5));
    body.append(sumBox);
    if (it.priority_reason) {
      body.append(el('p', 'ea-prio-why', '분류 이유: ' + it.priority_reason));
    }

    var acts = el('div', 'ea-actions');
    acts.append(link(isBill ? '의안정보시스템 원문' : isTrade ? '월간 통상 원문' : isGrant ? '공고 원문' : isNotice ? '원문' : '정책브리핑 원문', it.url));
    if (isNotice && it.opinion_url) { acts.append(link('의견 제출', it.opinion_url, 'is-primary')); }
    if (isNotice) {
      (it.attachment_urls || []).forEach(function (u, i) { acts.append(link('첨부 ' + (i + 1), u)); });
    }
    acts.append(telegramButton('/api/ea/items/' + it.id + '/telegram'));
    body.append(acts);

    card.append(body);
    return card;
  }

  /* ── 필터: 카테고리 칩 + 카테고리별 행 노출 ── */
  function renderCatChips() {
    renderChips($('eaCatChips'), CATS, eaState.cat, function (key) {
      if (eaState.cat === key) { return; }
      eaState.cat = key;
      eaState.page = 1;
      eaState.agency.clear(); eaState.priority.clear(); eaState.group.clear();
      eaState.status = ''; eaState.due = '';
      eaState.sort = currentCat().sort || 'priority';
      renderCatChips();
      applyRowVisibility();
      loadFilters().then(load);
      loadStats();
    }, true);
  }

  function applyRowVisibility() {
    var c = currentCat();
    var cal = c.layout === 'calendar';
    $('eaSearch').closest('.filter-row').hidden = cal;
    $('eaAgencyRow').hidden = cal;
    $('eaPriorityRow').hidden = cal;
    $('eaGroupRow').hidden = cal;
    $('eaMiscRow').hidden = cal;
    $('eaSort').hidden = false;
    $('eaStatus').hidden = !(c.layout === 'bill' || c.layout === 'notice');   // 심사 단계·예고 상태
    $('eaDue').hidden = c.layout !== 'notice';                                  // 마감 D-7/14/30
    var lab = document.querySelector('#eaAgencyRow .filter-label');
    if (lab) { lab.firstChild.textContent = c.layout === 'bill' ? '소관위 ' : c.layout === 'trade' ? '출처 ' : '소관 부처 '; }
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
    if (c.layout === 'calendar') { return Promise.resolve(); }
    return getJSON('/api/ea/filters?category=' + encodeURIComponent(c.key)).then(function (d) {
      fillSelect($('eaSort'), d.sorts || [], eaState.sort || c.sort || 'deadline', null);
      fillSelect($('eaStatus'), (d.statuses || []).map(function (s) {
        return { key: s, label: s };
      }), eaState.status, '상태 전체');
      fillSelect($('eaDue'), d.dues || [], eaState.due, '마감 전체');

      var agencies = d.agencies || [];
      // 카테고리가 바뀌어 선택된 기관이 목록에 없으면 해제
      Array.from(eaState.agency).forEach(function (k) {
        if (!agencies.some(function (a) { return a.key === k; })) { eaState.agency.delete(k); }
      });
      renderChips($('eaAgencyChips'), agencies, eaState.agency, function (key) {
        toggleSet(eaState.agency, key); load();
      });
      renderChips($('eaPriorityChips'), d.priorities || PRIORITIES.map(function (k) { return { key: k, label: k }; }),
        eaState.priority, function (key) { toggleSet(eaState.priority, key); load(); });
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
    if (c.layout === 'calendar') { eaState.loading = false; loadCalendar(); return; }
    $('eaList').replaceChildren.apply($('eaList'), [0, 1, 2].map(function () {
      return el('div', 'ea-skeleton');
    }));
    var done = function () { eaState.loading = false; };

    var p = new URLSearchParams();
    p.set('item_type', c.types);
    if (eaState.agency.size) { p.set('agency', csv(eaState.agency)); }
    if (eaState.group.size) { p.set('group', csv(eaState.group)); }
    if (eaState.priority.size) { p.set('priority', csv(eaState.priority)); }
    if (eaState.status) { p.set('status', eaState.status); }
    if (eaState.due) { p.set('due', eaState.due); }
    if (eaState.q) { p.set('q', eaState.q); }
    p.set('sort', eaState.sort || c.sort || 'priority');
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
      $('eaMeta').textContent = '수집 ' + s.total + '건 · 분석 ' + s.analyzed + '건 · 매일 09시·15시 갱신';
    }).catch(function () {
      $('eaMeta').textContent = '현황을 불러오지 못했습니다.';
    });
  }

  function clearAll() {
    eaState.agency.clear(); eaState.priority.clear(); eaState.group.clear();
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
