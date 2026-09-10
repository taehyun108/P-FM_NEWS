"""대외협력(대관) 모듈 — 입법예고·행정예고·국회 의안.

설계 원칙 (기존 파이프라인 무영향이 1순위)
  · main.py 를 import 하지 않는다. 순환 참조를 만들지 않기 위해서다.
    공통 유틸(now_utc·iso·new_id·jload)은 3~5줄짜리라 여기에 복제한다 —
    기존 파일을 고치는 위험보다 코드 중복이 낫다.
  · DB 는 스레드 전용 커넥션을 새로 연다. Storage 클래스에 손대지 않는다.
  · 기존 수집 루프(300초)에 얹지 않는다. 독립 스레드 C 가 하루 2회만 돈다.
  · 텔레그램·notifications 는 읽지도 쓰지도 않는다. category 값만 채워 둔다.
  · 기존 url_ledger 는 읽기만 하고, 쓰기는 ea_url_ledger 에만 한다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

log = logging.getLogger("pfm.ea")

# ── 설정 (.env) ──────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))


def _env(name: str, default: str = "") -> str:
    """main.load_dotenv_file 이 이미 os.environ 에 넣어 둔 값을 읽는다."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    # 인라인 주석 제거 (기존 .env 표기 규약과 동일)
    text = raw.strip()
    if text and not text.startswith(('"', "'")):
        text = text.split("#", 1)[0].strip()
    return text or default


def _env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        val = int(_env(name, str(default)))
    except ValueError:
        return default
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


def _env_on(name: str, default: bool) -> bool:
    raw = _env(name, "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def app_tz() -> timezone:
    """운영 기준 시간대. main.load_config 와 같은 APP_TZ_OFFSET 을 읽는다(기본 KST).
    클라우드 서버는 대개 UTC 라 datetime.now() 를 그대로 쓰면 수집 시각이 9시간 어긋난다."""
    return timezone(timedelta(hours=_env_int("APP_TZ_OFFSET", 9, -12, 14)))


def now_local() -> datetime:
    return datetime.now(app_tz())


# 수집 시각(운영 기준 시간대 = 기본 KST 기준 시). 기본 09시·15시 — 하루 2회.
def schedule_hours() -> list[int]:
    raw = _env("EA_SCHEDULE_HOURS", "9,15")
    out: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if part.isdigit() and 0 <= int(part) <= 23:
            out.append(int(part))
    return sorted(set(out)) or [9, 15]


def ea_enabled() -> bool:
    return _env_on("EA_ENABLED", True)


def assembly_key() -> str:
    return _env("EA_ASSEMBLY_KEY")


def kotra_service_key() -> str:
    """data.go.kr 에서 발급한 KOTRA 해외시장뉴스 API 서비스키. 없으면 KOTRA 소스는 꺼진다."""
    return _env("EA_KOTRA_SERVICE_KEY")


EA_LLM_DAILY_LIMIT = lambda: _env_int("EA_LLM_DAILY_LIMIT", 50, 0)   # noqa: E731
ASSEMBLY_AGE = lambda: _env_int("EA_ASSEMBLY_AGE", 22, 1)            # noqa: E731

# 1회 수집에서 상세 조회(HTTP)·분석까지 갈 최대 건수. 기존 파이프라인의
# MAX_PROCESS_PER_RUN(12) 과 같은 취지 — 실행 시간을 예측 가능하게 묶는다.
EA_MAX_PROCESS_PER_RUN = 40
EA_HTTP_TIMEOUT = 20
EA_SEEN_CACHE_HOURS = 72


# ── 복제 유틸 (main.py 와 동일 동작. import 하지 않으려고 복제했다) ──
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def jload(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def parse_date(value: Any) -> str | None:
    """'2026-09-04' · '20260904' · '2026.09.04' 를 ISO date 문자열로."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(".", "-").replace("/", "-")
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def d_day(notice_end: str | None, today: date | None = None) -> int | None:
    """마감까지 남은 일수. 지났으면 음수, 마감일 없으면 None."""
    parsed = parse_date(notice_end)
    if not parsed:
        return None
    return (date.fromisoformat(parsed) - (today or datetime.now(KST).date())).days


# ── 전용 DB 접근 ─────────────────────────────────────────────────────
# Storage 클래스에 메서드를 추가하지 않는다. 여기서 스레드 전용 커넥션을
# 새로 열고 ea_* 테이블만 만진다. 기존 테이블은 읽기 전용으로만 조회한다.
class EaDB:
    def __init__(self, sqlite_path: str) -> None:
        self.path = sqlite_path
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30.0)
            c.row_factory = sqlite3.Row
            c.execute("pragma journal_mode = wal")
            c.execute("pragma busy_timeout = 5000")
            c.execute("pragma foreign_keys = on")
            self._local.conn = c
        return c

    def rows(self, sql: str, args: Sequence[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn().execute(sql, tuple(args)).fetchall()]

    def one(self, sql: str, args: Sequence[Any] = ()) -> dict | None:
        got = self.rows(sql, args)
        return got[0] if got else None

    def exec(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        c = self.conn()
        cur = c.execute(sql, tuple(args))
        c.commit()
        return cur

    # ── 부처 ──
    def seed_agencies(self, rows: Iterable[tuple[str, str, str]]) -> int:
        """(이름, 약칭, 구분) 을 넣는다. 구분(kind)은 'ministry' | 'committee'.

        이미 있는 행도 kind 는 갱신한다 — 컬럼을 뒤늦게 추가했기 때문이다.
        """
        n = 0
        for name, short, kind in rows:
            cur = self.exec(
                "insert or ignore into ea_agencies (id, name, short_name, kind, enabled)"
                " values (?,?,?,?,1)", (new_id(), name, short, kind))
            n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            self.exec("update ea_agencies set kind=? where name=? and ifnull(kind,'')<>?",
                      (kind, name, kind))
        return n

    def agencies(self, enabled_only: bool = True) -> list[dict]:
        sql = "select * from ea_agencies"
        if enabled_only:
            sql += " where enabled=1"
        return self.rows(sql + " order by name")

    def agency_id_by_name(self, name: str) -> str | None:
        if not name:
            return None
        row = self.one("select id from ea_agencies where name=? or short_name=?", (name, name))
        return row["id"] if row else None

    def relink_agencies(self) -> int:
        """agency_id 가 비어 있는 항목을 agency_raw ↔ ea_agencies.name 으로 다시 잇는다.

        정부조직 개편으로 부처명이 바뀌어 시드에 뒤늦게 추가된 경우, 이미 저장된
        과거 항목의 agency_id 를 채워 화면 필터·집계와 어긋나지 않게 한다.
        """
        return self.exec(
            "update ea_policy_items set agency_id = ("
            "  select id from ea_agencies where name = ea_policy_items.agency_raw)"
            " where agency_id is null and agency_raw is not null"
            "   and exists (select 1 from ea_agencies where name = ea_policy_items.agency_raw)"
        ).rowcount or 0

    # ── 게이트용 조회 ──
    def known_url_sources(self, candidates: Sequence[str]) -> set[str]:
        """G2 — ea_policy_items + ea_url_ledger 를 한 번에 대조한다."""
        if not candidates:
            return set()
        found: set[str] = set()
        chunk = 400
        for i in range(0, len(candidates), chunk):
            part = list(candidates[i:i + chunk])
            marks = ",".join("?" * len(part))
            for sql in (f"select url_source from ea_policy_items where url_source in ({marks})",
                        f"select url_source from ea_url_ledger where url_source in ({marks})"):
                found.update(r["url_source"] for r in self.rows(sql, part))
        return found

    def recent_url_sources(self, hours: int) -> set[str]:
        """G1 캐시 로드 — 최근 수집분. 성능 계층일 뿐 정확성에 관여하지 않는다."""
        cutoff = iso(now_utc() - timedelta(hours=hours))
        return {r["url_source"] for r in self.rows(
            "select url_source from ea_policy_items where collected_at >= ?", (cutoff,))}

    def upsert_ledger(self, url_source: str, reason: str) -> None:
        self.exec(
            "insert into ea_url_ledger (url_source, reason, first_seen, hit_count)"
            " values (?,?,?,1)"
            " on conflict(url_source) do update set hit_count = hit_count + 1",
            (url_source, reason, iso(now_utc())))

    # ── 항목 ──
    # ── 실행 상태 (수집 시각 등. 대외협력 전용 테이블) ──
    def get_run_state(self, key: str) -> str:
        row = self.one("select value from ea_run_state where key=?", (key,))
        return (row or {}).get("value") or ""

    def set_run_state(self, key: str, value: str) -> None:
        self.exec("insert into ea_run_state (key, value) values (?,?)"
                  " on conflict(key) do update set value=excluded.value", (key, value))

    def _ensure_cols(self) -> None:
        """스키마에 뒤늦게 추가한 컬럼·테이블을 보강한다(기존 DB 도 그대로 쓸 수 있게)."""
        self.exec("create table if not exists ea_run_state ("
                  " key TEXT primary key, value TEXT)")
        for table, col, ddl in (
            ("ea_policy_items", "agency_raw", "TEXT"),
            ("ea_policy_items", "group_companies", "TEXT"),   # JSON 배열. 규칙 기반 판정 결과
            ("ea_agencies", "kind", "TEXT"),                  # 'ministry' | 'committee'
        ):
            have = {r["name"] for r in self.rows(f"pragma table_info({table})")}
            if col not in have:
                try:
                    self.exec(f"alter table {table} add column {col} {ddl}")
                except sqlite3.OperationalError:
                    pass

    def insert_item(self, row: dict) -> bool:
        cols = ",".join(row)
        marks = ",".join("?" * len(row))
        try:
            self.exec(f"insert into ea_policy_items ({cols}) values ({marks})", list(row.values()))
            return True
        except sqlite3.IntegrityError:
            return False   # url_source UNIQUE — 경합 시 최종 방어선

    def unanalyzed_items(self, limit: int) -> list[dict]:
        return self.rows(
            "select p.* from ea_policy_items p"
            " left join ea_analyses a on a.policy_item_id = p.id"
            " where a.id is null"
            " order by (p.notice_end is null), p.notice_end asc limit ?", (limit,))

    def save_analysis(self, row: dict) -> None:
        cols = ",".join(row)
        marks = ",".join("?" * len(row))
        self.exec(f"insert into ea_analyses ({cols}) values ({marks})", list(row.values()))

    def analyses_today(self) -> int:
        return int((self.one(
            "select count(*) as n from ea_analyses where created_at >= ?",
            (iso(now_utc().replace(hour=0, minute=0, second=0, microsecond=0)),)) or {"n": 0})["n"])

    def stats(self) -> dict:
        def n(sql: str, args: Sequence[Any] = ()) -> int:
            return int((self.one(sql, args) or {"n": 0})["n"])
        today = datetime.now(KST).date().isoformat()
        return {
            "total": n("select count(*) as n from ea_policy_items"),
            "open": n("select count(*) as n from ea_policy_items"
                      " where notice_end is not null and notice_end >= ?", (today,)),
            "analyzed": n("select count(*) as n from ea_analyses"),
            "excluded": n("select count(*) as n from ea_url_ledger"),
        }

    # ── 조회(뷰) — 화면·주간레포트·마이그레이션이 공유 ──
    def agency_name_by_id(self, agency_id: str) -> str:
        if not agency_id:
            return ""
        row = self.one("select name from ea_agencies where id=?", (agency_id,))
        return (row or {}).get("name", "")

    def backfill_group_targets(self) -> list[dict]:
        """group_companies 가 비어 있는 항목 — id·title·law_name·category 만."""
        return self.rows("select id, title, law_name, category from ea_policy_items"
                         " where group_companies is null or group_companies in ('', '[]')")

    def update_item_groups(self, item_id: str, groups_json: str) -> None:
        self.exec("update ea_policy_items set group_companies=? where id=?",
                  (groups_json, item_id))

    def last_item_collected_at_raw(self) -> str | None:
        row = self.one("select max(collected_at) as t from ea_policy_items")
        return row.get("t") if row else None

    def ensure_ready(self) -> None:
        """뒤늦게 추가한 컬럼·테이블 보강. Supabase 는 schema.sql 이 이미 최신이라 _ensure_cols 로 위임."""
        self._ensure_cols()

    def query_items(self, *, item_type: str = "", agency: str = "", impact: str = "",
                    status: str = "", due: str = "", q: str = "", group: str = "",
                    sort: str = "deadline") -> list[dict]:
        """정렬은 sort 파라미터로 고른다. 기본은 마감일 오름차순. (SS8.4)

        정렬 자체는 SQL 이 아니라 _ea_sort_rows() 가 한다 - Supabase 백엔드와
        똑같은 함수를 써서 두 저장소의 정렬 결과가 어긋나지 않게 한다.
        """
        sql, args = _ITEM_SELECT + " where 1=1", []
        if group:
            parts = group.split(",")
            sql += " and (" + " or ".join(["p.group_companies like ?"] * len(parts)) + ")"
            args += [f'%"{g}"%' for g in parts]
        if item_type:
            marks = ",".join("?" * len(item_type.split(",")))
            sql += f" and p.item_type in ({marks})"; args += item_type.split(",")
        if agency:
            parts = agency.split(",")
            marks = ",".join("?" * len(parts))
            sql += f" and coalesce(g.name, p.agency_raw) in ({marks})"; args += parts
        if impact:
            marks = ",".join("?" * len(impact.split(",")))
            sql += f" and ifnull(a.impact_level,'') in ({marks})"; args += impact.split(",")
        if status:
            marks = ",".join("?" * len(status.split(",")))
            sql += f" and ifnull(p.status,'') in ({marks})"; args += status.split(",")
        if due in ("7", "14", "30"):
            today = datetime.now(KST).date()
            sql += " and p.notice_end is not null and p.notice_end >= ? and p.notice_end <= ?"
            args += [today.isoformat(), (today + timedelta(days=int(due))).isoformat()]
        if q:
            sql += (" and (p.title like ? or ifnull(p.law_name,'') like ?"
                   " or ifnull(a.summary,'') like ?)")
            args += [f"%{q}%"] * 3
        rows = self.rows(sql, args)
        return _ea_sort_rows(rows, sort)

    def filters_agency_counts(self, types: list[str]) -> dict[str, int]:
        where, args = "", []
        if types:
            where = f" where p.item_type in ({','.join('?' * len(types))})"
            args = list(types)
        return {r["v"]: r["n"] for r in self.rows(
            "select coalesce(g.name, p.agency_raw) as v, count(*) as n"
            " from ea_policy_items p left join ea_agencies g on g.id=p.agency_id"
            + where + " group by v", args) if r["v"]}

    def filters_group_counts(self, types: list[str]) -> dict[str, int]:
        where, args = "", []
        if types:
            where = f" where p.item_type in ({','.join('?' * len(types))})"
            args = list(types)
        gcounts: dict[str, int] = {}
        for r in self.rows("select group_companies as v from ea_policy_items p" + where, args):
            for g in jload(r.get("v"), []):
                gcounts[g] = gcounts.get(g, 0) + 1
        return gcounts

    def filters_statuses(self, types: list[str]) -> list[str]:
        if types:
            where = f" where p.item_type in ({','.join('?' * len(types))})"
            rows = self.rows("select distinct p.status as v from ea_policy_items p" + where
                             + " order by v", types)
        else:
            rows = self.rows("select distinct status as v from ea_policy_items order by v")
        return [r["v"] for r in rows if r["v"]]

    def item_detail(self, item_id: str) -> dict | None:
        return self.one(_ITEM_SELECT + " where p.id=?", (item_id,))

    def recent_news_items_for_weekly(self, ea_types: Sequence[str], limit: int = 120) -> list[dict]:
        if not ea_types:
            return []
        marks = ",".join("?" * len(ea_types))
        return self.rows(
            _ITEM_SELECT + f" where p.item_type in ({marks})"
            " order by coalesce(p.notice_start, substr(p.collected_at,1,10)) desc limit ?",
            list(ea_types) + [limit])


def _ea_pick_service_key(k1: str, k2: str) -> str:
    """main._pick_supabase_service_key 와 동일한 로직을 독립 구현한다(main.py 미임포트 원칙)."""
    import base64
    for k in (k1, k2):
        if not k:
            continue
        try:
            payload = k.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            if json.loads(base64.urlsafe_b64decode(payload)).get("role") == "service_role":
                return k
        except Exception:
            continue
    return k1 or k2


def _ea_sort_rows(rows: list[dict], sort: str) -> list[dict]:
    """query_items 공통 정렬. EaDB(SQLite)와 EaSupabaseDB 양쪽이 이 함수 하나로 정렬해
    두 백엔드의 정렬 결과가 어긋나지 않는다.

    - deadline(기본): 마감일 있는 항목 먼저, 마감일 오름차순, 같은 마감일은 수집 최신순
    - recent: 공고 시작일(없으면 수집일) 내림차순
    - impact: deadline 순서를 기본으로 두고 영향도 등급으로 다시 정렬(안정 정렬)
    """
    rows = list(rows)
    rows.sort(key=lambda r: r.get("collected_at") or "", reverse=True)
    if sort == "recent":
        rows.sort(key=lambda r: r.get("notice_start") or (r.get("collected_at") or "")[:10],
                  reverse=True)
    else:
        rows.sort(key=lambda r: r.get("notice_end") or "9999-99-99")
    if sort == "impact":
        rows.sort(key=lambda r: _IMPACT_RANK.get(r.get("impact_level") or "", 0), reverse=True)
    return rows


class EaSupabaseDB:
    """EaDB 와 같은 메서드 인터페이스를 Supabase(PostgREST)로 구현한다.

    DB_BACKEND=supabase 일 때 make_ea_db() 가 이걸 돌려준다. 대외협력 데이터는
    수백 건 규모라 JOIN·집계를 SQL 에 맡기지 않고 관련 테이블을 통째로 읽어
    파이썬에서 처리한다(PostgREST 는 임의 JOIN 을 지원하지 않는다).
    main.py 를 import 하지 않는다는 이 파일의 설계 원칙을 그대로 지킨다 -
    Supabase 클라이언트를 독립적으로 만든다(SupabaseStorage 코드와 일부 중복되지만
    의도된 것이다).
    """

    def __init__(self) -> None:
        from supabase import create_client
        url = _env("SUPABASE_URL")
        key = _ea_pick_service_key(_env("SUPABASE_SERVICE_ROLE_KEY"), _env("SUPABASE_ANON_KEY"))
        if not url or not key:
            raise RuntimeError("대외협력: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 가 필요합니다.")
        self.db = create_client(url, key)

    def _t(self, name: str):
        return self.db.table(name)

    # ── 부처 ──
    def seed_agencies(self, rows: Iterable[tuple[str, str, str]]) -> int:
        n = 0
        for name, short, kind in rows:
            existing = self._t("ea_agencies").select("id,kind").eq("name", name).execute().data
            if existing:
                if (existing[0].get("kind") or "") != kind:
                    self._t("ea_agencies").update({"kind": kind}).eq("name", name).execute()
                continue
            self._t("ea_agencies").insert(
                {"id": str(uuid.uuid4()), "name": name, "short_name": short,
                 "kind": kind, "enabled": True}).execute()
            n += 1
        return n

    def agencies(self, enabled_only: bool = True) -> list[dict]:
        q = self._t("ea_agencies").select("*")
        if enabled_only:
            q = q.eq("enabled", True)
        return q.order("name").execute().data

    def agency_id_by_name(self, name: str) -> str | None:
        if not name:
            return None
        rows = (self._t("ea_agencies").select("id")
               .or_(f"name.eq.{name},short_name.eq.{name}").limit(1).execute().data)
        return rows[0]["id"] if rows else None

    def agency_name_by_id(self, agency_id: str) -> str:
        if not agency_id:
            return ""
        rows = self._t("ea_agencies").select("name").eq("id", agency_id).limit(1).execute().data
        return rows[0]["name"] if rows else ""

    def relink_agencies(self) -> int:
        agencies = {a["name"]: a["id"] for a in self._t("ea_agencies").select("id,name").execute().data}
        if not agencies:
            return 0
        targets = (self._t("ea_policy_items").select("id,agency_raw")
                  .is_("agency_id", "null").not_.is_("agency_raw", "null").execute().data)
        n = 0
        for r in targets:
            aid = agencies.get(r.get("agency_raw") or "")
            if aid:
                self._t("ea_policy_items").update({"agency_id": aid}).eq("id", r["id"]).execute()
                n += 1
        return n

    # ── 게이트용 조회 ──
    def known_url_sources(self, candidates: Sequence[str]) -> set[str]:
        if not candidates:
            return set()
        found: set[str] = set()
        chunk = 200
        cands = list(candidates)
        for i in range(0, len(cands), chunk):
            part = cands[i:i + chunk]
            for table in ("ea_policy_items", "ea_url_ledger"):
                rows = self._t(table).select("url_source").in_("url_source", part).execute().data
                found.update(r["url_source"] for r in rows)
        return found

    def recent_url_sources(self, hours: int) -> set[str]:
        cutoff = iso(now_utc() - timedelta(hours=hours))
        rows = self._t("ea_policy_items").select("url_source").gte("collected_at", cutoff).execute().data
        return {r["url_source"] for r in rows}

    def upsert_ledger(self, url_source: str, reason: str) -> None:
        existing = (self._t("ea_url_ledger").select("hit_count")
                   .eq("url_source", url_source).limit(1).execute().data)
        if existing:
            self._t("ea_url_ledger").update(
                {"hit_count": int(existing[0].get("hit_count") or 0) + 1}
            ).eq("url_source", url_source).execute()
        else:
            self._t("ea_url_ledger").insert(
                {"url_source": url_source, "reason": reason, "first_seen": iso(now_utc()),
                 "hit_count": 1}).execute()

    # ── 실행 상태 ──
    def get_run_state(self, key: str) -> str:
        rows = self._t("ea_run_state").select("value").eq("key", key).limit(1).execute().data
        return (rows[0].get("value") or "") if rows else ""

    def set_run_state(self, key: str, value: str) -> None:
        self._t("ea_run_state").upsert({"key": key, "value": value}, on_conflict="key").execute()

    def ensure_ready(self) -> None:
        pass

    # ── 항목 ──
    def insert_item(self, row: dict) -> bool:
        try:
            self._t("ea_policy_items").insert(row).execute()
            return True
        except Exception as exc:
            msg = str(exc)
            if "duplicate key" in msg.lower() or "23505" in msg:
                return False
            raise

    def unanalyzed_items(self, limit: int) -> list[dict]:
        analyzed_ids = {r["policy_item_id"]
                        for r in self._t("ea_analyses").select("policy_item_id").execute().data}
        items = self._t("ea_policy_items").select("*").execute().data
        pending = [r for r in items if r["id"] not in analyzed_ids]
        pending.sort(key=lambda r: (r.get("notice_end") is None, r.get("notice_end") or ""))
        return pending[:limit]

    def save_analysis(self, row: dict) -> None:
        self._t("ea_analyses").insert(row).execute()

    def analyses_today(self) -> int:
        midnight = iso(now_utc().replace(hour=0, minute=0, second=0, microsecond=0))
        return (self._t("ea_analyses").select("id", count="exact")
               .gte("created_at", midnight).limit(1).execute().count or 0)

    def stats(self) -> dict:
        today = datetime.now(KST).date().isoformat()
        total = self._t("ea_policy_items").select("id", count="exact").limit(1).execute().count or 0
        open_n = (self._t("ea_policy_items").select("id", count="exact")
                 .not_.is_("notice_end", "null").gte("notice_end", today)
                 .limit(1).execute().count or 0)
        analyzed = self._t("ea_analyses").select("id", count="exact").limit(1).execute().count or 0
        excluded = self._t("ea_url_ledger").select("url_source", count="exact").limit(1).execute().count or 0
        return {"total": total, "open": open_n, "analyzed": analyzed, "excluded": excluded}

    # ── 그룹사 소급 ──
    def backfill_group_targets(self) -> list[dict]:
        rows = self._t("ea_policy_items").select("id,title,law_name,category,group_companies").execute().data
        return [r for r in rows if not r.get("group_companies") or r.get("group_companies") in ("", "[]")]

    def update_item_groups(self, item_id: str, groups_json: str) -> None:
        self._t("ea_policy_items").update({"group_companies": groups_json}).eq("id", item_id).execute()

    def last_item_collected_at_raw(self) -> str | None:
        rows = (self._t("ea_policy_items").select("collected_at")
               .order("collected_at", desc=True).limit(1).execute().data)
        return rows[0]["collected_at"] if rows else None

    # ── 조회(뷰) — 3테이블 조인은 파이썬에서 ──
    def _joined_items(self) -> list[dict]:
        items = self._t("ea_policy_items").select("*").execute().data
        agency_by_id = {a["id"]: a["name"] for a in self._t("ea_agencies").select("id,name").execute().data}
        analyses = {a["policy_item_id"]: a for a in self._t("ea_analyses").select(
            "policy_item_id,impact_level,summary,impact_rationale,affected_areas,suggested_action"
        ).execute().data}
        out = []
        for r in items:
            row = dict(r)
            row["agency_name"] = agency_by_id.get(r.get("agency_id") or "")
            a = analyses.get(r["id"])
            if a:
                row.update({k: a.get(k) for k in
                           ("impact_level", "summary", "impact_rationale",
                            "affected_areas", "suggested_action")})
            out.append(row)
        return out

    def query_items(self, *, item_type: str = "", agency: str = "", impact: str = "",
                    status: str = "", due: str = "", q: str = "", group: str = "",
                    sort: str = "deadline") -> list[dict]:
        rows = self._joined_items()
        if group:
            parts = set(group.split(","))
            rows = [r for r in rows if parts & set(jload(r.get("group_companies"), []))]
        if item_type:
            types = set(item_type.split(","))
            rows = [r for r in rows if (r.get("item_type") or "") in types]
        if agency:
            names = set(agency.split(","))
            rows = [r for r in rows if (r.get("agency_name") or r.get("agency_raw") or "") in names]
        if impact:
            levels = set(impact.split(","))
            rows = [r for r in rows if (r.get("impact_level") or "") in levels]
        if status:
            statuses = set(status.split(","))
            rows = [r for r in rows if (r.get("status") or "") in statuses]
        if due in ("7", "14", "30"):
            today = datetime.now(KST).date().isoformat()
            end = (datetime.now(KST).date() + timedelta(days=int(due))).isoformat()
            rows = [r for r in rows if r.get("notice_end") and today <= r["notice_end"] <= end]
        if q:
            rows = [r for r in rows if q in (r.get("title") or "") or q in (r.get("law_name") or "")
                   or q in (r.get("summary") or "")]
        return _ea_sort_rows(rows, sort)

    def filters_agency_counts(self, types: list[str]) -> dict[str, int]:
        rows = self._joined_items()
        if types:
            rows = [r for r in rows if (r.get("item_type") or "") in types]
        counts: dict[str, int] = {}
        for r in rows:
            name = r.get("agency_name") or r.get("agency_raw")
            if name:
                counts[name] = counts.get(name, 0) + 1
        return counts

    def filters_group_counts(self, types: list[str]) -> dict[str, int]:
        rows = self._joined_items()
        if types:
            rows = [r for r in rows if (r.get("item_type") or "") in types]
        gcounts: dict[str, int] = {}
        for r in rows:
            for g in jload(r.get("group_companies"), []):
                gcounts[g] = gcounts.get(g, 0) + 1
        return gcounts

    def filters_statuses(self, types: list[str]) -> list[str]:
        rows = self._t("ea_policy_items").select("item_type,status").execute().data
        if types:
            rows = [r for r in rows if (r.get("item_type") or "") in types]
        return sorted({r["status"] for r in rows if r.get("status")})

    def item_detail(self, item_id: str) -> dict | None:
        rows = self._t("ea_policy_items").select("*").eq("id", item_id).limit(1).execute().data
        if not rows:
            return None
        row = dict(rows[0])
        row["agency_name"] = self.agency_name_by_id(row.get("agency_id") or "")
        arows = (self._t("ea_analyses")
                .select("impact_level,summary,impact_rationale,affected_areas,suggested_action")
                .eq("policy_item_id", item_id).limit(1).execute().data)
        if arows:
            row.update(arows[0])
        return row

    def recent_news_items_for_weekly(self, ea_types: Sequence[str], limit: int = 120) -> list[dict]:
        if not ea_types:
            return []
        types = set(ea_types)
        rows = [r for r in self._joined_items() if (r.get("item_type") or "") in types]
        rows.sort(key=lambda r: r.get("notice_start") or (r.get("collected_at") or "")[:10],
                  reverse=True)
        return rows[:limit]


def make_ea_db(ctx: Any):
    """DB_BACKEND 에 맞는 대외협력 저장소를 돌려준다.

    main.py 를 import 하지 않으므로 ctx.cfg 는 덕타이핑으로만 쓴다
    (db_backend, sqlite_path 속성만 있으면 됨). Supabase 접속 정보는 main.py 와
    같은 .env 변수(SUPABASE_URL 등)를 이 파일이 직접 읽는다.
    """
    backend = str(getattr(ctx.cfg, "db_backend", "sqlite") or "sqlite").lower()
    if backend == "supabase":
        return EaSupabaseDB()
    return EaDB(ctx.cfg.sqlite_path)


# ── 관심 부처 시드 ───────────────────────────────────────────────────
# 정부조직 개편으로 이름이 자주 바뀐다. 옛 이름도 함께 넣어 과거 공고를 놓치지 않는다.
SEED_AGENCIES: list[tuple[str, str, str]] = [
    ("산업통상자원부", "산업부", "ministry"),
    ("산업통상부", "산업부", "ministry"),            # 2025 개편 후 명칭
    ("기후에너지환경부", "기후부", "ministry"),
    ("환경부", "환경부", "ministry"),
    ("기획재정부", "기재부", "ministry"),
    ("재정경제부", "재경부", "ministry"),            # 2025 개편(기재부 분리) 후 명칭
    ("기획예산처", "기예처", "ministry"),
    ("행정안전부", "행안부", "ministry"),
    ("국토교통부", "국토부", "ministry"),
    ("고용노동부", "고용부", "ministry"),
    ("과학기술정보통신부", "과기정통부", "ministry"),
    ("중소벤처기업부", "중기부", "ministry"),
    ("공정거래위원회", "공정위", "ministry"),
    ("금융위원회", "금융위", "ministry"),
    ("원자력안전위원회", "원안위", "ministry"),
    ("관세청", "관세청", "ministry"),
    ("무역위원회", "무역위", "ministry"),
    ("소방청", "소방청", "ministry"),
    ("산림청", "산림청", "ministry"),
    ("농촌진흥청", "농진청", "ministry"),
    ("조달청", "조달청", "ministry"),
    ("특허청", "특허청", "ministry"),
    ("국회", "국회", "committee"),     # 소관 상임위를 못 읽은 의안의 폴백
    ("법제처", "법제처", "ministry"),
    ("국무조정실", "국조실", "ministry"),
    # 국회 상임위원회 (국회 입법예고 항목의 소관)
    ("산업통상자원중소벤처기업위원회", "산자위", "committee"),
    ("기후에너지환경노동위원회", "기환노위", "committee"),
    ("국토교통위원회", "국토위", "committee"),
    ("기획재정위원회", "기재위", "committee"),
    ("재정경제기획위원회", "재경위", "committee"),
    ("과학기술정보방송통신위원회", "과방위", "committee"),
    ("정무위원회", "정무위", "committee"),
    ("농림축산식품해양수산위원회", "농해수위", "committee"),
    ("보건복지위원회", "복지위", "committee"),
    ("행정안전위원회", "행안위", "committee"),
]

# ── G2.5 관련성 키워드 ───────────────────────────────────────────────
# 법제처 입법예고에는 전 부처의 모든 법령이 올라온다. 대부분 무관하다.
# 여기 걸리지 않으면 HTTP 를 쓰기 전에 ea_url_ledger 로 보낸다.
EA_RELEVANCE_KW: list[str] = [
    # 이차전지·소재
    "이차전지", "2차전지", "배터리", "양극재", "음극재", "전구체", "리튬", "니켈", "코발트",
    "흑연", "전해액", "분리막", "핵심광물", "희토류", "소재부품장비", "소부장",
    # 철강·산업
    "철강", "제철", "제련", "합금", "산업단지", "특화단지", "국가첨단전략산업",
    "탄소중립", "온실가스", "배출권", "수소", "재생에너지", "신재생", "전기요금",
    "전력수급", "전기사업", "전력시장", "발전사업", "송전", "배전", "계통",
    # 화학·환경·안전
    "화학물질", "화평법", "화관법", "유해화학", "폐기물", "자원순환", "대기환경",
    "물환경", "토양환경", "산업안전", "중대재해", "위험물",
    # 통상·무역
    "통상", "관세", "수출입", "무역구제", "반덤핑", "상계관세", "원산지", "FTA",
    "공급망", "수출통제", "전략물자",
    # 입지·건설 (포스코이앤씨)
    "건설산업", "주택법", "도시정비", "건축법", "국토계획",
]


def _keyword_sets_terms(db: Any, keyword_rows: list[dict]) -> list[str]:
    """keyword_rows(ctx.storage.enabled_keywords() 결과)의 산업·정책·통상 카테고리만 쓴다.

    main.py 의 저장소를 그대로 인자로 받는다 - DB_BACKEND 가 무엇이든 항상 '지금
    운영 중인' 키워드를 본다(예전엔 이 파일이 로컬 sqlite 의 keyword_sets 를 직접
    읽어, Supabase 로 전환한 뒤에도 마스터 패널에서 키워드를 바꾸면 반영되지 않았다).

    단, **부처·기관명은 뺀다.** keyword_sets 는 뉴스 검색어라 '국토교통부' 같은
    부처명이 들어 있는데, 그대로 쓰면 is_relevant 가 부처명만으로 통과시켜
    '관심 부처라는 이유만으로는 통과시키지 않는다'는 규칙이 뒷문으로 무너진다
    (실제로 '국토교통부와 그 소속기관 직제 일부개정령안'이 이 경로로 들어왔다).
    """
    terms = [r["keyword"] for r in keyword_rows if (r.get("category") or "") in ("산업", "정책", "통상")]
    try:
        agencies = db.agencies()
    except Exception:
        return terms
    names = {a["name"] for a in agencies} | {a["short_name"] for a in agencies if a.get("short_name")}
    return [t for t in terms if t not in names]


def _kw_in(text: str, terms: Sequence[str]) -> str | None:
    for kw in terms:
        if kw and kw in text:
            return kw
    return None


def is_relevant(title: str, law_name: str = "", agency: str = "",
                agency_names: set[str] | None = None, extra_terms: Sequence[str] = ()) -> bool:
    """G2.5 — 관련 산업 키워드가 제목·법령명·(있으면)본문에 있어야 통과한다.

    관심 부처라는 이유만으로는 통과시키지 않는다 — 공정위·기재부 등에서 나오는
    소비자·조세·행정 일반 개정안이 대거 섞여 들어오기 때문이다. 부처 목록은
    화면 필터 드롭다운에만 쓴다.
    """
    probe = f"{title or ''}\n{law_name or ''}"
    return bool(_kw_in(probe, EA_RELEVANCE_KW) or _kw_in(probe, extra_terms))


# ── 게이트 G0 ~ G2.5 ────────────────────────────────────────────────
class Gates:
    """기존 파이프라인과 같은 순서로 거른다. HTTP·LLM 은 G2.5 통과분에만 쓴다."""

    def __init__(self, db: Any, keyword_rows: list[dict] = ()) -> None:
        self.db = db
        self.seen: set[str] = db.recent_url_sources(EA_SEEN_CACHE_HOURS)
        self.agency_names = {a["name"] for a in db.agencies()} | {
            a["short_name"] for a in db.agencies() if a.get("short_name")}
        self.extra_terms = _keyword_sets_terms(db, list(keyword_rows))
        self.counts = {"fetched": 0, "g0": 0, "g1": 0, "g2": 0, "g2_5": 0, "off_topic": 0}

    def filter(self, items: list[dict]) -> list[dict]:
        """items = [{url_source, title, law_name, agency, ...}] — 비용 0 구간 전체."""
        self.counts["fetched"] += len(items)

        # G0 실행 내 중복 URL
        seen_run: set[str] = set()
        g0: list[dict] = []
        for it in items:
            u = it.get("url_source") or ""
            if not u or u in seen_run:
                continue
            seen_run.add(u)
            g0.append(it)
        self.counts["g0"] += len(g0)

        # G1 메모리 seen-cache
        g1 = [it for it in g0 if it["url_source"] not in self.seen]
        self.counts["g1"] += len(g1)

        # G2 DB 전체기간 대조 (ea_policy_items + ea_url_ledger)
        known = self.db.known_url_sources([it["url_source"] for it in g1])
        g2 = [it for it in g1 if it["url_source"] not in known]
        self.counts["g2"] += len(g2)

        # G2.5 관련성 — 걸리지 않으면 제외 원장에 기록해 다음 주기에 HTTP 를 안 쓴다.
        # 뉴스형 소스(부처 정책뉴스·KOTRA)는 RSS 가 본문까지 줬으므로 제목이 밋밋해도
        # 본문으로 한 번 더 본다(추가 HTTP 없음). 예고형은 종전처럼 제목·법령명만.
        kept: list[dict] = []
        for it in g2:
            body_probe = (it.get("_body") or "")[:2000]
            if is_relevant(it.get("title", ""), it.get("law_name", ""), it.get("agency", ""),
                           self.agency_names, self.extra_terms) \
                    or (body_probe and is_relevant(body_probe, extra_terms=self.extra_terms)):
                kept.append(it)
            else:
                self.db.upsert_ledger(it["url_source"], "off_topic")
                self.seen.add(it["url_source"])
                self.counts["off_topic"] += 1
        self.counts["g2_5"] += len(kept)
        return kept


# ── HTTP (기존 HttpClient·카운터 락에 개입하지 않는다) ────────────────
_http_lock = threading.Lock()   # 대외협력 전용. 기존 락 3종과 무관하다.


def _get_json(url: str, params: dict) -> Any:
    import requests
    resp = requests.get(url, params=params, timeout=EA_HTTP_TIMEOUT,
                        headers={"User-Agent": "P-FM-NEWS/EA (+internal)"})
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(f"JSON 아님 (HTTP {resp.status_code}): {resp.text[:200]}")


# ── S3 · 열린국회정보 — 국회의원 발의법률안 ──────────────────────────
ASSEMBLY_URL = "https://open.assembly.go.kr/portal/openapi/nzmimeepazxkubdpn"

# 실제 응답 필드명이 문서(로그인 필요)에만 있어, 흔히 쓰이는 이름을 후보로 두고
# 첫 성공 응답의 키를 로그로 남긴다. 확인되면 후보를 정리한다.
_BILL_FIELDS = {
    "bill_id":   ("BILL_ID", "billId"),
    "bill_no":   ("BILL_NO", "billNo"),
    "title":     ("BILL_NAME", "BILL_NM", "billName"),
    "proposer":  ("PROPOSER", "RST_PROPOSER", "proposer"),
    "propose_dt": ("PROPOSE_DT", "PROPOSE_DATE", "proposeDt"),
    "committee": ("COMMITTEE", "COMMITTEE_NM", "committee"),
    "result":    ("PROC_RESULT", "PROC_RESULT_CD", "procResult"),
    "link":      ("DETAIL_LINK", "LINK_URL", "detailLink"),
}
_logged_bill_keys = False


def _pick(row: dict, names: Sequence[str]) -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def fetch_assembly_bills(page_size: int = 100, max_pages: int = 3) -> list[dict]:
    """S3 수집. 키가 없으면 빈 목록(비활성). 크롤링 우회는 하지 않는다."""
    global _logged_bill_keys
    key = assembly_key()
    if not key:
        try:
            return _crawl().crawl_assembly_notices()   # 기한 보강은 G2.5 통과 후
        except Exception as exc:
            log.warning("S3 국회 입법예고 크롤링 실패: %s", exc)
            return []

    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"KEY": key, "Type": "json", "pIndex": page,
                  "pSize": page_size, "AGE": ASSEMBLY_AGE()}
        try:
            with _http_lock:
                data = _get_json(ASSEMBLY_URL, params)
        except Exception as exc:
            log.warning("S3 국회 의안 조회 실패 (page=%d): %s", page, exc)
            break

        rows, message = _unwrap_assembly(data)
        if message:
            log.warning("S3 응답 메시지: %s", message)
        if not rows:
            break
        if not _logged_bill_keys:
            log.info("S3 응답 필드명(첫 행): %s", sorted(rows[0].keys()))
            _logged_bill_keys = True

        for r in rows:
            link = _pick(r, _BILL_FIELDS["link"])
            bill_id = _pick(r, _BILL_FIELDS["bill_id"])
            if not link and bill_id:
                link = ("https://likms.assembly.go.kr/bill/billDetail.do?billId=" + bill_id)
            if not link:
                continue
            out.append({
                "url_source": link,
                "url_canonical": link,
                "item_type": "bill",
                "title": _pick(r, _BILL_FIELDS["title"]),
                "law_name": "",
                "agency": _pick(r, _BILL_FIELDS["committee"]) or "국회",
                "notice_start": parse_date(_pick(r, _BILL_FIELDS["propose_dt"])),
                "notice_end": None,           # 의안은 의견제출 마감일 개념이 없다
                "status": _pick(r, _BILL_FIELDS["result"]) or "국회심의",
                "opinion_url": "",
                "attachment_urls": [],
                "published_at": parse_date(_pick(r, _BILL_FIELDS["propose_dt"])),
                "_proposer": _pick(r, _BILL_FIELDS["proposer"]),
            })
        if len(rows) < page_size:
            break
    log.info("S3 국회 의안 수집 %d건", len(out))
    return out


def _unwrap_assembly(data: Any) -> tuple[list[dict], str]:
    """열린국회정보 공통 응답 껍질을 벗긴다. {서비스명: [{head:[...]}, {row:[...]}]}"""
    if isinstance(data, dict):
        if "RESULT" in data:      # 오류 응답
            res = data.get("RESULT") or {}
            return [], f'{res.get("CODE", "")} {res.get("MESSAGE", "")}'.strip()
        for value in data.values():
            if isinstance(value, list):
                rows: list[dict] = []
                message = ""
                for chunk in value:
                    if not isinstance(chunk, dict):
                        continue
                    if isinstance(chunk.get("row"), list):
                        rows.extend(x for x in chunk["row"] if isinstance(x, dict))
                    for hd in chunk.get("head", []) or []:
                        res = (hd or {}).get("RESULT") if isinstance(hd, dict) else None
                        if res and str(res.get("CODE", "")).startswith(("INFO-2", "ERROR")):
                            message = f'{res.get("CODE")} {res.get("MESSAGE", "")}'
                return rows, message
    return [], ""


# ── S1 · S2 · 국민참여입법센터 (opinion.lawmaking.go.kr/rest) ────────
# 스펙 출처: github.com/hollobit/assembly-api-mcp (src/api/lawmaking.ts)
#   입법예고 GET /rest/ogLmPp.xml   행정예고 GET /rest/ptcpAdmPp.xml
#   인증: OC (opinion.lawmaking.go.kr 정보공개 서비스 신청 ID)
#   www.lawmaking.go.kr 는 opinion. 으로 301 리다이렉트된다.
#   OC=test 로 실호출 시 <result><retMsg>401</retMsg></result> — 형식 확인됨.
LAWMAKING_BASE = "https://opinion.lawmaking.go.kr/rest"
# 국민참여입법센터 웹 상세/의견제출 화면
LAWMAKING_WEB = "https://opinion.lawmaking.go.kr/gcom"


def lawmaking_oc() -> str:
    return _env("EA_LAWMAKING_OC")


def _get_xml(url: str, params: dict) -> Any:
    """XML 응답을 ElementTree 로 파싱한다. 표준 라이브러리만 쓴다."""
    import requests
    import xml.etree.ElementTree as ET
    resp = requests.get(url, params=params, timeout=EA_HTTP_TIMEOUT,
                        headers={"User-Agent": "P-FM-NEWS/EA (+internal)"})
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    try:
        return ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise RuntimeError(f"XML 파싱 실패 (HTTP {resp.status_code}): {resp.text[:200]}") from exc


def _xml_rows(root: Any, marker_tag: str) -> tuple[list[dict], str]:
    """marker_tag(예: 'lsNm') 를 자식으로 가진 요소를 행으로 본다. 중첩 깊이 무관.

    <result><retMsg>401</retMsg></result> 같은 오류 응답은 (빈 목록, 메시지) 로 돌려준다.
    """
    ret = root.findtext(".//retMsg") or root.findtext(".//resultMsg") or ""
    if ret and ret not in ("00", "success", "정상", "OK"):
        return [], str(ret)
    rows: list[dict] = []
    for el in root.iter():
        if el.find(marker_tag) is not None:
            rows.append({c.tag: (c.text or "").strip() for c in el})
    return rows, ""


def _clean_law_name(name: str) -> str:
    return re.sub(r"\s*(일부개정|전부개정|제정)?(법률안|령안|규칙안|안)?\s*(입법예고|행정예고)?\s*$",
                  "", (name or "").strip()) or (name or "").strip()


def _crawl():
    import ea_crawl
    return ea_crawl


def fetch_legislation_notices(max_rows: int = 400) -> list[dict]:
    """S1 입법예고 (ogLmPp). OC 키가 있으면 REST, 없으면 크롤링(사용자 지정).

    두 사이트 모두 robots.txt = 'Allow: /' 이고 게시물은 공공누리(KOGL) 자료다.
    크롤링은 요청 간 1초 간격·페이지 상한을 지킨다(ea_crawl 참고).
    """
    oc = lawmaking_oc()
    if not oc:
        try:
            return _crawl().crawl_legislation_notices()
        except Exception as exc:
            log.warning("S1 입법예고 크롤링 실패: %s", exc)
            return []
    try:
        with _http_lock:
            root = _get_xml(f"{LAWMAKING_BASE}/ogLmPp.xml", {"OC": oc})
    except Exception as exc:
        log.warning("S1 입법예고 조회 실패: %s", exc)
        return []
    rows, msg = _xml_rows(root, "lsNm")
    if msg:
        log.warning("S1 입법예고 응답 코드: %s (OC 확인 필요)", msg)
        return []

    out: list[dict] = []
    for r in rows[:max_rows]:
        seq = r.get("ogLmPpSeq") or r.get("pntcNo") or ""
        if not seq:
            continue
        detail = f"{LAWMAKING_WEB}/ogLmPp/{seq}"
        out.append({
            "url_source": detail,
            "url_canonical": detail,
            "item_type": "legislation",
            "title": r.get("lsNm") or "",
            "law_name": _clean_law_name(r.get("lsNm") or ""),
            "agency": r.get("asndOfiNm") or "",
            "notice_start": parse_date(r.get("stYd") or r.get("pntcDt")),
            "notice_end": parse_date(r.get("edYd")),
            "status": "예고중" if not r.get("edYd") or (d_day(parse_date(r.get("edYd"))) or 0) >= 0
                      else "종료",
            "opinion_url": detail,
            "attachment_urls": [r["FileDownLink"]] if r.get("FileDownLink") else [],
            "published_at": parse_date(r.get("pntcDt")),
        })
    log.info("S1 입법예고 수집 %d건", len(out))
    return out


def fetch_admin_notices(max_rows: int = 400) -> list[dict]:
    """S2 행정예고. OC 키가 있으면 REST, 없으면 크롤링(사용자 지정)."""
    oc = lawmaking_oc()
    if not oc:
        try:
            return _crawl().crawl_admin_notices()
        except Exception as exc:
            log.warning("S2 행정예고 크롤링 실패: %s", exc)
            return []
    try:
        with _http_lock:
            root = _get_xml(f"{LAWMAKING_BASE}/ptcpAdmPp.xml", {"OC": oc})
    except Exception as exc:
        log.warning("S2 행정예고 조회 실패: %s", exc)
        return []
    rows, msg = _xml_rows(root, "admRulNm")
    if msg:
        log.warning("S2 행정예고 응답 코드: %s (OC 확인 필요)", msg)
        return []

    out: list[dict] = []
    for r in rows[:max_rows]:
        seq = r.get("ogAdmPpSeq") or r.get("pntcNo") or ""
        if not seq:
            continue
        detail = f"{LAWMAKING_WEB}/ptcpAdmPp/{seq}"
        out.append({
            "url_source": detail,
            "url_canonical": detail,
            "item_type": "admin_notice",
            "title": r.get("admRulNm") or "",
            "law_name": _clean_law_name(r.get("admRulNm") or ""),
            "agency": r.get("asndOfiNm") or "",
            "notice_start": parse_date(r.get("stYd") or r.get("pntcDt")),
            "notice_end": parse_date(r.get("edYd")),
            "status": "예고중" if not r.get("edYd") or (d_day(parse_date(r.get("edYd"))) or 0) >= 0
                      else "종료",
            "opinion_url": detail,
            "attachment_urls": [r["FileDownLink"]] if r.get("FileDownLink") else [],
            "published_at": parse_date(r.get("pntcDt")),
        })
    log.info("S2 행정예고 수집 %d건", len(out))
    return out


# 상세 본문은 ea_crawl.fetch_detail 이 담당한다 — 정부 사이트 상세 REST 는 필드명이
# 공개돼 있지 않아 본문 품질이 크롤보다 낮았다. (analyze_item 이 fetch_detail_text 로 폴백)

def fetch_ministry_news() -> list[dict]:
    """S4 부처 정책뉴스 — 표준 정부 홈페이지 CMS 의 POST 방식 RSS(본문 포함)."""
    try:
        return _crawl().crawl_ministry_news()
    except Exception as exc:
        log.warning("S4 부처 정책뉴스 크롤링 실패: %s", exc)
        return []


def fetch_kotra_news() -> list[dict]:
    """S5 KOTRA 해외시장뉴스 — data.go.kr 오픈API. 서비스키가 없으면 빈 목록."""
    key = kotra_service_key()
    if not key:
        return []
    try:
        return _crawl().crawl_kotra_news(key)
    except Exception as exc:
        log.warning("S5 KOTRA 해외시장뉴스 조회 실패: %s", exc)
        return []


SOURCES = [
    ("S1 입법예고", fetch_legislation_notices),
    ("S2 행정예고", fetch_admin_notices),
    ("S3 국회 의안", fetch_assembly_bills),
    ("S4 부처 정책뉴스", fetch_ministry_news),
    ("S5 KOTRA 해외시장뉴스", fetch_kotra_news),
]


# ── 분류 (category) ─────────────────────────────────────────────────
# 값만 채워 둔다. 선택 발송 기능은 이번 범위가 아니다 — 나중에 마스터 패널의
# '정책브리핑 알림'·'글로벌 통상환경 알림' 토글과 같은 방식으로 붙이면 된다.
EA_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("이차전지·소재", ["이차전지", "2차전지", "배터리", "양극재", "음극재", "전구체",
                        "리튬", "니켈", "코발트", "흑연", "전해액", "분리막",
                        "핵심광물", "희토류", "소부장", "소재부품장비"]),
    ("통상",          ["통상", "관세", "수출입", "무역구제", "반덤핑", "상계관세",
                        "원산지", "FTA", "공급망", "수출통제", "전략물자"]),
    ("환경·안전",     ["화학물질", "화평법", "화관법", "유해화학", "폐기물", "자원순환",
                        "대기환경", "물환경", "토양환경", "산업안전", "중대재해", "위험물",
                        "온실가스", "배출권", "탄소중립"]),
    ("에너지",        ["전기요금", "전력수급", "전기사업", "전력시장", "발전사업", "송전",
                        "배전", "전력계통", "재생에너지", "신재생", "수소", "원자력",
                        "원자력발전", "에너지", "집적화단지", "이격거리"]),
    ("철강·산업",     ["철강", "제철", "제련", "합금", "산업단지", "특화단지",
                        "국가첨단전략산업"]),
    ("건설·입지",     ["건설산업", "주택법", "도시정비", "건축법", "국토계획"]),
]


def detect_category(title: str, law_name: str = "") -> str:
    probe = f"{title or ''}\n{law_name or ''}"
    for name, words in EA_CATEGORY_RULES:
        if any(w in probe for w in words):
            return name
    return "기타"


# ── 포스코 그룹사 판정 ───────────────────────────────────────────────
# 규칙 기반이 1순위라는 기존 원칙(PRD F4.2)을 그대로 따른다. 입법·행정예고에는
# 회사명이 등장하지 않으므로 '규제 대상 사업'으로 판정한다. LLM 은 쓰지 않는다
# — 회사명이 본문에 없는데 LLM 에 맡기면 근거 없는 태깅이 대량으로 나온다.
EA_GROUP_RULES: list[tuple[str, list[str]]] = [
    ("포스코", ["철강", "제철", "제련", "합금", "고로", "전기로", "탄소중립", "온실가스",
                "배출권", "산업안전", "중대재해", "대기환경", "물환경", "화학물질",
                "유해화학", "화평법", "화관법", "폐기물", "자원순환", "위험물", "토양환경"]),
    ("포스코퓨처엠", ["이차전지", "2차전지", "배터리", "양극재", "음극재", "전구체", "리튬",
                      "니켈", "코발트", "흑연", "전해액", "분리막", "핵심광물", "희토류",
                      "소부장", "소재부품장비", "국가첨단전략산업"]),
    ("포스코이앤씨", ["건설산업", "주택법", "도시정비", "건축법", "국토계획", "산업단지",
                      "특화단지", "집적화단지", "이격거리", "시공", "건설"]),
    ("포스코인터내셔널", ["통상", "관세", "수출입", "무역구제", "반덤핑", "상계관세", "원산지",
                          "FTA", "공급망", "수출통제", "전략물자", "전기사업", "발전사업",
                          "재생에너지", "신재생", "수소", "전력계통", "전력시장", "전기요금",
                          "전력수급", "송전", "배전", "원자력"]),
    ("포스코DX", ["스마트팩토리", "산업용 로봇", "정보통신", "인공지능", "디지털전환",
                  "데이터산업"]),
]
# 화면 필터 칩 순서 — 기존 뉴스 탭의 GROUP_ORDER 와 같은 감각으로 맞춘다.
EA_GROUP_ORDER = [g for g, _ in EA_GROUP_RULES]


def detect_ea_groups(title: str, law_name: str = "", category: str = "") -> list[str]:
    """제목·법령명·분류에서 규제 대상 사업을 읽어 관련 그룹사를 고른다."""
    probe = f"{title or ''}\n{law_name or ''}\n{category or ''}"
    return [name for name, words in EA_GROUP_RULES if _kw_in(probe, words)]


def backfill_groups(db: Any) -> int:
    """group_companies 가 비어 있는 항목을 규칙으로 채운다(컬럼을 뒤늦게 추가했다).

    규칙만 쓰므로 LLM 비용이 없고, 규칙을 고치면 다시 돌려 소급 반영할 수 있다.
    """
    rows = db.backfill_group_targets()
    n = 0
    for r in rows:
        groups = detect_ea_groups(r.get("title") or "", r.get("law_name") or "",
                                  r.get("category") or "")
        if groups:
            db.update_item_groups(r["id"], jdump(groups))
            n += 1
    return n


# ── 수집 1회 ────────────────────────────────────────────────────────
def collect_once(ctx: Any, db: Any) -> dict:
    """스레드 C 가 하루 2회 부르는 진입점. 기존 수집 루프와 완전히 분리돼 있다."""
    started = time.monotonic()
    db.ensure_ready()
    db.seed_agencies(SEED_AGENCIES)
    relinked = db.relink_agencies()
    if relinked:
        log.info("대외협력: 부처 재연결 %d건", relinked)
    filled = backfill_groups(db)
    if filled:
        log.info("대외협력: 그룹사 소급 판정 %d건", filled)

    raw: list[dict] = []
    active_sources: list[str] = []
    for label, fn in SOURCES:
        try:
            got = fn()
        except Exception as exc:
            log.warning("%s 수집 실패: %s", label, exc)
            continue
        if got:
            active_sources.append(label)
            raw.extend(got)

    try:
        keyword_rows = ctx.storage.enabled_keywords()
    except Exception as exc:
        log.debug("키워드 조회 실패(무시, 대외협력 자체 규칙만 적용): %s", exc)
        keyword_rows = []
    gates = Gates(db, keyword_rows)
    kept = gates.filter(raw)

    # 처리 상한 — 실행 시간을 예측 가능하게 묶는다. 넘친 항목은 아직
    # ea_policy_items 에 없으므로 다음 주기에 다시 후보가 된다.
    # 유형(입법·행정·국회)을 번갈아 담아, 앞 소스가 상한을 독식하지 않게 한다.
    by_type: dict[str, list[dict]] = {}
    for it in kept:
        by_type.setdefault(it.get("item_type") or "legislation", []).append(it)
    interleaved: list[dict] = []
    queues = list(by_type.values())
    while queues and len(interleaved) < len(kept):
        for q in queues:
            if q:
                interleaved.append(q.pop(0))
        queues = [q for q in queues if q]
    overflow = max(0, len(interleaved) - EA_MAX_PROCESS_PER_RUN)
    kept = interleaved[:EA_MAX_PROCESS_PER_RUN]

    # G2.5 통과분 중 국회 입법예고는 예고기간이 목록에 없다 — 상세에서 보강(HTTP)
    for it in kept:
        if it.get("_need_detail_period"):
            try:
                _crawl().enrich_assembly_period(it)
            except Exception as exc:
                log.debug("국회 입법예고 기한 보강 실패: %s", exc)

    saved = 0
    fresh: list[tuple[dict, str]] = []   # (저장된 행, API 본문) — 본문은 메모리에만 둔다
    for it in kept:
        row = {
            "id": new_id(),
            "url_source": it["url_source"],
            "url_canonical": it.get("url_canonical") or it["url_source"],
            "item_type": it.get("item_type") or "legislation",
            "category": detect_category(it.get("title", ""), it.get("law_name", "")),
            "title": it.get("title") or "(제목 없음)",
            "agency_id": db.agency_id_by_name(it.get("agency", "")),
            "agency_raw": (it.get("agency") or "").strip() or None,
            "law_name": it.get("law_name") or None,
            "notice_start": parse_date(it.get("notice_start")),
            "notice_end": parse_date(it.get("notice_end")),
            "status": it.get("status") or None,
            "opinion_url": it.get("opinion_url") or None,
            "attachment_urls": jdump(it.get("attachment_urls") or []),
            "published_at": it.get("published_at") or None,
            "collected_at": iso(now_utc()),
        }
        # G3·G4 — 상세(개정이유·주요내용) 확보. G2.5 통과분에만 HTTP 가 발생한다.
        body = it.get("_body") or ""
        if not body:
            try:
                detail = _crawl().fetch_detail(it)
                body = detail.get("body") or ""
                real_title = detail.get("title")
                if real_title and len(real_title) > len(row["title"]) - 4:
                    row["title"] = real_title
                    row["law_name"] = _crawl()._law_name(real_title)
                    row["category"] = detect_category(real_title, row["law_name"] or "")
            except Exception as exc:
                log.debug("상세 확보 실패 %s: %s", it.get("url_source", ""), exc)
        # 본문(개정이유·주요내용)까지 확인해 여전히 산업 키워드가 없으면 저장하지 않는다
        if body and not is_relevant(row["title"], row.get("law_name") or "",
                                    extra_terms=gates.extra_terms) \
                and not is_relevant(body[:2000], extra_terms=gates.extra_terms):
            db.upsert_ledger(it["url_source"], "off_topic")
            gates.counts["off_topic"] += 1
            gates.seen.add(it["url_source"])
            continue
        # 관련 그룹사 — 제목·법령명·분류가 최종 확정된 뒤에 판정한다(규칙만, LLM 0)
        row["group_companies"] = jdump(
            detect_ea_groups(row["title"], row.get("law_name") or "", row.get("category") or ""))
        if db.insert_item(row):
            saved += 1
            fresh.append((row, body))
        gates.seen.add(it["url_source"])

    # G6 — 분석. KOTRA(trade_news)는 규칙 기반이라 LLM 상한과 무관하게 전부 처리하고,
    # 나머지(예고·부처뉴스)만 전용 일일 상한 안에서 LLM 분석한다.
    analyzed = 0
    try:
        kotra = [(r, b) for r, b in fresh if (r.get("item_type") or "") == "trade_news"]
        rest = [(r, b) for r, b in fresh if (r.get("item_type") or "") != "trade_news"]
        for row, _ in kotra:
            if analyze_item(ctx, db, row, ""):
                analyzed += 1
        budget = analysis_budget(ctx, db)
        for row, body in rest[:budget]:
            if analyze_item(ctx, db, row, body):
                analyzed += 1
        analyzed += analyze_backlog(ctx, db)   # 이전 회차에 밀린 건 (HTML 재확보)
    except Exception as exc:
        log.warning("대외협력 분석 단계 실패(수집분은 유지): %s", exc)

    # 저장 건수와 무관하게 '실행했다'를 남긴다 — 이게 없으면 새 항목이 0건일 때
    # 재시작마다 같은 슬롯을 다시 크롤한다. (_last_collect_at 주석 참고)
    db.set_run_state("last_collect_at", iso(now_utc()))

    took = time.monotonic() - started
    result = {**gates.counts, "saved": saved, "analyzed": analyzed, "overflow": overflow,
              "sources": active_sources, "duration_sec": round(took, 1)}
    log.info("대외협력 수집: 소스 %s · 수집 %d → G2.5 통과 %d(무관 %d) · 저장 %d · %.1f초",
             ", ".join(active_sources) or "없음", result["fetched"], result["g2_5"],
             result["off_topic"], saved, took)
    return result


# ── 스레드 C — 하루 2회 ─────────────────────────────────────────────
EA_MIN_GAP_HOURS = 1   # 재시작 폭주 방지 백스톱. 같은 시각 중복은 아래 slot_missed 가 막는다


def _last_collect_at(db: Any) -> datetime | None:
    """마지막으로 수집을 **실행한** 시각.

    저장된 항목의 collected_at 으로 판단하면 안 된다 — 크롤은 돌았는데 새 항목이
    0건이면 값이 안 움직여, 서버를 재시작할 때마다 같은 슬롯을 다시 크롤한다
    (회당 약 100 HTTP + LLM 예산 위험). 그래서 실행 자체를 ea_run_state 에 남긴다.
    저장된 마커가 없는 기존 설치는 예전 방식으로 폴백한다.
    """
    marked = parse_dt(db.get_run_state("last_collect_at"))
    if marked:
        return marked
    return parse_dt(db.last_item_collected_at_raw())


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def scheduler_loop(ctx: Any, stop: threading.Event) -> None:
    """기존 수집 루프(300초)에 얹지 않는다. 지정 시각이 지나면 그 시각당 1회만 돈다.

    마지막 수집 시각을 DB(ea_policy_items.collected_at)에서 읽어 판단하므로,
    서버를 자주 재시작해도 EA_MIN_GAP_HOURS 안에는 다시 크롤·분석하지 않는다.
    """
    db = make_ea_db(ctx)
    log.info("대외협력 수집 스레드 시작 (매일 %s시)",
             "·".join(str(h) for h in schedule_hours()))
    while not stop.is_set():
        try:
            if ea_enabled():
                now = now_local()   # 서버 TZ 가 아니라 운영 기준(KST) 시각으로 판단
                last = _last_collect_at(db)
                gap_ok = last is None or (now_utc() - last) >= timedelta(hours=EA_MIN_GAP_HOURS)
                due = any(now.hour >= h for h in schedule_hours())
                # 오늘 예정 시각 중 가장 최근 것을 지난 시점에, 아직 그 이후 수집이 없으면 실행
                today_slots = [now.replace(hour=h, minute=0, second=0, microsecond=0)
                               for h in schedule_hours() if now.hour >= h]
                slot_missed = today_slots and (last is None or
                    last.astimezone() < max(today_slots).astimezone())
                if due and gap_ok and slot_missed:
                    collect_once(ctx, db)
        except Exception as exc:
            log.exception("대외협력 수집 중 오류: %s", exc)
        stop.wait(300)


# ═════════════════════════════════════════════════════════════════════
# LLM 영향 분석 (G6 — 과금 지점)
#   · 뉴스 예산을 잠식하지 않도록 전용 일일 하위 상한을 둔다.
#   · SWOT 은 만들지 않는다. 기존 SWOT 파이프라인과 무관하다.
#   · 본문에서 읽어낼 근거가 없으면 impact_level='none' 이 정답이다.
# ═════════════════════════════════════════════════════════════════════

EA_MAX_BODY_CHARS = 6000
EA_IMPACT_LEVELS = ("high", "medium", "low", "none")

# medium 이상을 인정하는 근거 형태 — 조문 번호(제3조·안 제6조의2·제2항·제1호)
# 또는 원문을 10자 이상 그대로 따온 직접 인용. 둘 다 없으면 '느낌'이라 보고 low 로 내린다.
_EA_CITE = re.compile(
    r'(?:안\s*)?제\s?\d+\s?조|제\s?\d+\s?항|제\s?\d+\s?호|["“][^"”]{10,}["”]')

EA_SYSTEM = (
    "당신은 포스코 그룹 대외협력(대관) 담당자를 돕는 한국어 정책 분석 어시스턴트다. "
    "제공된 자료에 실제로 적힌 내용만 근거로 분석하고 JSON 으로만 답한다."
)

EA_PROMPT = """아래 입법·행정예고 또는 의안 자료를 분석해 JSON 하나로만 답하라.

[요약 — 이 개정안의 내용]
- summary: 원문 발췌의 **개정이유(제안이유)와 주요내용**을 3~5문장으로 정리한다
  · 첫 문장: 왜 개정하는가(개정이유)
  · 이후: 무엇을 어떻게 바꾸는가(주요내용), 시행일·적용대상이 원문에 있으면 포함
- 포스코와의 관련 여부와 무관하게, 개정 내용 자체를 항상 요약한다
- 원문에 없는 시행일·수치·대상을 만들어내지 않는다
- 원문 발췌가 비어 있거나 상용문구뿐이면 "원문을 확보하지 못했습니다" 한 줄만 쓴다

[영향도 — 포스코 그룹 관점]
- impact_level: "high" | "medium" | "low" | "none" 중 하나
- 포스코 그룹(철강·이차전지소재·건설/인프라·에너지) 사업에 미치는 영향 기준
- impact_rationale: 판단 근거가 된 **원문의 조문·항목을 그대로 인용**한다
- 포스코 사업과 연결되는 조문을 찾을 수 없으면 impact_level="none",
  impact_rationale="포스코 그룹 사업과 직접 연결되는 조항 없음" (summary 는 그대로 채운다)
- 불분명하면 추정하지 말고 "low" + "추가 검토 필요"

[관련 사업 영역]
- affected_areas: 해당하는 것만 배열로. 없으면 빈 배열
  ["철강", "이차전지소재", "건설·인프라", "에너지", "환경·안전", "통상", "노무"]

[대응 제안]
- suggested_action: 대관 담당자가 검토할 사항 1~2문장. 반드시 초안 성격으로 쓴다
- 법률 자문으로 읽힐 표현("~해야 한다", "위법이다")을 쓰지 않는다
- 당사에 유리한 방향으로 해석하지 않는다

[출력 형식 — 이 구조를 정확히 지킨다]
{{"summary":["문장1","문장2","문장3"],"impact_level":"none",
"impact_rationale":"...","affected_areas":[],"suggested_action":"..."}}

제목: {title}
대상 법령: {law_name}
소관: {agency}
예고기간: {period}
상태: {status}
원문 발췌:
{body}
"""

# 부처 정책뉴스·KOTRA 해외시장뉴스용 — '개정이유·조문'이 아니라 '동향·시사점' 관점.
EA_NEWS_PROMPT = """아래 정부부처(또는 KOTRA) 발표 자료를 분석해 JSON 하나로만 답하라.
법령 개정안이 아니라 정책·통상 동향 자료다. '개정이유'·'조문' 표현을 쓰지 마라.

[요약 — 이 발표의 내용]
- summary: 무엇을 발표·추진하는가를 3~4문장으로. 주체·시점·핵심 내용·후속 계획 순.
- 원문에 없는 수치·일정·대상을 지어내지 않는다
- 원문 발췌가 비어 있으면 "원문을 확보하지 못했습니다" 한 줄만 쓴다

[영향도 — 포스코 그룹 관점]
- impact_level: "high" | "medium" | "low" | "none"
- 포스코 그룹(철강·이차전지소재·건설/인프라·에너지·통상) 사업 관련성 기준
- impact_rationale: 판단 근거가 된 **원문의 문장을 그대로 인용**한다.
  포스코 사업과 연결되는 대목이 없으면 impact_level="none",
  impact_rationale="포스코 그룹 사업과 직접 연결되는 내용 없음"
- 불분명하면 "low" + "추가 검토 필요"

[관련 사업 영역]
- affected_areas: 해당하는 것만. ["철강","이차전지소재","건설·인프라","에너지","환경·안전","통상","노무"]

[대응 제안]
- suggested_action: 대관 담당자가 검토·모니터링할 사항 1~2문장(초안 성격). 단정적 표현 금지.

[출력 형식]
{{"summary":["문장1","문장2","문장3"],"impact_level":"none",
"impact_rationale":"...","affected_areas":[],"suggested_action":"..."}}

제목: {title}
발표 기관: {agency}
발표일: {period}
원문 발췌:
{body}
"""


def fetch_detail_text(url: str) -> str:
    """G3·G4 — 원문 페이지에서 텍스트만 뽑는다. 실패하면 빈 문자열(분석은 근거없음 처리)."""
    if not url:
        return ""
    try:
        import requests
        from bs4 import BeautifulSoup
        with _http_lock:
            resp = requests.get(url, timeout=EA_HTTP_TIMEOUT,
                                headers={"User-Agent": "P-FM-NEWS/EA (+internal)"})
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    except Exception as exc:
        log.debug("대외협력 원문 확보 실패 %s: %s", url, exc)
        return ""


def _ea_chat(ctx: Any, system: str, user: str) -> str:
    """LangSmith 에 external_affairs 태그를 붙여 호출한다. 비용을 따로 보기 위해서다.

    태그 전달이 안 되는 환경이면 태그 없이 그대로 호출한다(기능 우선).
    """
    client = ctx.llm.client
    kwargs: dict[str, Any] = {
        "model": ctx.cfg.llm_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    try:
        resp = client.chat.completions.create(
            **kwargs, langsmith_extra={"name": "ea_impact_analysis",
                                       "tags": ["external_affairs"]})
    except TypeError:
        resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def _parse_json_object(content: str) -> dict | None:
    text = (content or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


_TRADE_IMPACT_KW = {
    "high": ("한국산", "대(對)한국", "한국 기업", "반덤핑관세 부과", "상계관세 부과"),
    "medium": ("반덤핑", "상계관세", "세이프가드", "수입규제", "관세 인상", "수출통제",
               "탄소규제", "탄소국경", "CBAM", "원산지", "인증제도", "통관 규정"),
}


def _kotra_rule_analysis(db: Any, item: dict) -> bool:
    """KOTRA 해외시장뉴스는 본문이 없다(제목이 완결된 문장). LLM 없이 규칙으로
    요약·영향도를 채운다 — 전용 LLM 상한(EA_LLM_DAILY_LIMIT)을 예고 분석에 남긴다."""
    title = item.get("title") or ""
    probe = f"{title}"
    level = "none"
    for lv in ("high", "medium"):
        if any(k in probe for k in _TRADE_IMPACT_KW[lv]):
            level = lv
            break
    rationale = ("제목에 한국 대상 통상 조치가 명시됨 — 원문 확인 필요"
                 if level == "high" else
                 "해외 통상·규제 동향 — 포스코 수출입·조달 영향 여부는 원문 확인 필요"
                 if level == "medium" else
                 "포스코 그룹 사업과 직접 연결되는 내용 없음")
    db.save_analysis({
        "id": new_id(), "policy_item_id": item["id"],
        "summary": title,   # KOTRA 제목이 곧 요약이다
        "impact_level": level, "impact_rationale": rationale,
        "affected_areas": jdump([]),
        "suggested_action": ("해당 품목·지역이 포스코 수출입에 걸리는지 확인" if level != "none" else None),
        "model": "rule", "reviewed_by": None, "reviewed_at": None,
        "created_at": iso(now_utc()),
    })
    return True


def analyze_item(ctx: Any, db: Any, item: dict, source_text: str = "") -> bool:
    """항목 1건 분석 후 ea_analyses 에 저장. 성공하면 True.

    source_text: 수집 시 API 응답에 들어 있던 제안이유·주요내용. 이게 있으면
    HTML 스크래핑보다 정확하므로 우선 쓴다(정부 사이트는 JS 렌더링이 많다).
    본문은 저장하지 않는다 — 기존 §7-3 최소 보관 원칙을 그대로 따른다.
    """
    if (item.get("item_type") or "") == "trade_news":
        return _kotra_rule_analysis(db, item)   # 본문 없음 — LLM 안 씀
    body = source_text.strip()
    if not body:
        try:
            body = _crawl().fetch_detail(item).get("body") or ""
        except Exception:
            body = fetch_detail_text(item.get("opinion_url") or item.get("url_source") or "")
    period = " ~ ".join(x for x in (item.get("notice_start"), item.get("notice_end")) if x) or "미상"
    agency = ""
    if item.get("agency_id"):
        agency = db.agency_name_by_id(item["agency_id"])
    # 매핑된 부처명이 없으면 크롤 원문을 쓴다 — 화면(_item_view)과 같은 폴백이어야
    # 카드에는 부처가 보이는데 프롬프트에는 '(미상)' 이 들어가는 어긋남이 없다.
    agency = agency or (item.get("agency_raw") or "")

    is_news = (item.get("item_type") or "") in ("ministry_news", "trade_news")
    tmpl = EA_NEWS_PROMPT if is_news else EA_PROMPT
    news_period = item.get("published_at") or item.get("notice_start") or "(미상)"
    prompt = tmpl.format(
        title=item.get("title") or "", law_name=item.get("law_name") or "(없음)",
        agency=agency or "(미상)", period=(news_period if is_news else period),
        status=item.get("status") or "(미상)",
        body=(body[:EA_MAX_BODY_CHARS] or "(원문을 확보하지 못했습니다)"))

    try:
        parsed = _parse_json_object(_ea_chat(ctx, EA_SYSTEM, prompt))
    except Exception as exc:
        log.warning("대외협력 분석 실패 (%s): %s", (item.get("title") or "")[:30], exc)
        return False
    if not parsed:
        log.warning("대외협력 분석 JSON 파싱 실패: %s", (item.get("title") or "")[:30])
        return False

    summary = parsed.get("summary")
    if isinstance(summary, list):
        summary = " ".join(str(s).strip() for s in summary if str(s).strip())
    summary = str(summary or "").strip() or "원문을 확보하지 못했습니다 — 원문 링크로 확인하세요"

    level = str(parsed.get("impact_level") or "none").strip().lower()
    if level not in EA_IMPACT_LEVELS:
        level = "none"
    rationale = str(parsed.get("impact_rationale") or "").strip()
    # 조문 인용 없이 medium 이상을 주장하면 신뢰하지 않는다 — low 로 내린다.
    # 길이만 보면(예전 규칙) 조문도 인용도 없는 뭉뚱그린 문장이 medium 으로 통과한다.
    # (low/none 은 근거 문구가 없어도 그대로 둔다 — 요약은 이미 채워졌다)
    if level in ("high", "medium") and not _EA_CITE.search(rationale):
        level = "low"
        rationale = rationale or "포스코 그룹 사업과의 연결이 원문에서 확인되지 않음 — 추가 검토 필요"
    if not rationale:
        rationale = "포스코 그룹 사업과 직접 연결되는 조항 없음"

    areas = parsed.get("affected_areas")
    areas = [str(a).strip() for a in areas if str(a).strip()] if isinstance(areas, list) else []

    db.save_analysis({
        "id": new_id(),
        "policy_item_id": item["id"],
        "summary": summary,
        "impact_level": level,
        "impact_rationale": rationale,
        "affected_areas": jdump(areas),
        "suggested_action": str(parsed.get("suggested_action") or "").strip() or None,
        "model": ctx.cfg.llm_model,
        "reviewed_by": None,
        "reviewed_at": None,
        "created_at": iso(now_utc()),
    })
    return True


def analysis_budget(ctx: Any, db: Any) -> int:
    """이번에 분석할 수 있는 건수. 전용 상한과 전체 상한 중 작은 쪽."""
    ea_limit = EA_LLM_DAILY_LIMIT()
    if ea_limit <= 0:
        return 0
    budget = max(0, ea_limit - db.analyses_today())
    if budget == 0:
        log.info("대외협력 LLM 일일 상한(%d) 도달 — 분석 건너뜀 (뉴스 분석은 계속)", ea_limit)
        return 0
    # 전체 일일 상한은 뉴스와 공유한다. 초과 시 대외협력만 멈춘다.
    try:
        budget = max(0, min(budget, ctx.cfg.llm_daily_limit - ctx.storage.llm_calls_today()))
    except Exception as exc:
        log.debug("전체 LLM 카운터 조회 실패(대외협력 상한만 적용): %s", exc)
    if budget == 0:
        log.info("전체 LLM 일일 상한 도달 — 대외협력 분석 건너뜀")
    return budget


def _news_body_map(items: list[dict]) -> dict[str, str]:
    """뉴스형(부처 정책뉴스·KOTRA) 항목은 본문을 저장하지 않는다. 백로그 분석 때
    RSS/ API 를 한 번 더 받아 url→본문 을 맞춘다(피드에 남아 있는 최근분만 채워진다)."""
    types = {it.get("item_type") for it in items}
    out: dict[str, str] = {}
    try:
        if "ministry_news" in types:
            for r in _crawl().crawl_ministry_news():
                out[r["url_source"]] = r.get("_body") or ""
        if "trade_news" in types and kotra_service_key():
            for r in _crawl().crawl_kotra_news(kotra_service_key()):
                out[r["url_source"]] = r.get("_body") or ""
    except Exception as exc:   # pragma: no cover
        log.debug("뉴스 본문 재확보 실패(무시): %s", exc)
    return out


def analyze_backlog(ctx: Any, db: Any) -> int:
    """미분석 항목을 처리한다. KOTRA(trade_news)는 규칙 기반이라 상한 밖에서 전부,
    나머지는 전용 LLM 상한 안에서(마감 임박 우선)."""
    done = 0
    # 규칙 기반 KOTRA 는 상한과 무관하게 먼저 비운다
    for item in db.unanalyzed_items(200):
        if (item.get("item_type") or "") == "trade_news":
            if analyze_item(ctx, db, item, ""):
                done += 1
    budget = analysis_budget(ctx, db)
    if budget == 0:
        return done
    ea_used = db.analyses_today()
    ea_limit = EA_LLM_DAILY_LIMIT()
    items = [it for it in db.unanalyzed_items(budget)
             if (it.get("item_type") or "") != "trade_news"]
    news_bodies = _news_body_map(items)
    llm_done = 0
    for item in items:
        if analyze_item(ctx, db, item, news_bodies.get(item.get("url_source", ""), "")):
            llm_done += 1
    if llm_done:
        log.info("대외협력 분석 %d건 (전용 상한 %d 중 %d 사용)", llm_done, ea_limit, ea_used + llm_done)
    return done + llm_done


# ═════════════════════════════════════════════════════════════════════
# API — 전부 /api/ea/ 아래. 기존 라우트와 겹치지 않고,
#       /api/articles 쿼리에 ea_* 조인을 끼워 넣지 않는다.
# ═════════════════════════════════════════════════════════════════════

EA_PAGE_SIZE = 9          # 기존 포토카드와 같은 페이지당 건수
EA_DUE_SOON_DAYS = 7      # 마감 임박 배너 기준


def _item_view(row: dict) -> dict:
    return {
        "id": row["id"],
        "title": row.get("title") or "",
        "url": row.get("url_canonical") or row.get("url_source") or "",
        "item_type": row.get("item_type") or "",
        "category": row.get("category") or "",
        "law_name": row.get("law_name") or "",
        "agency": row.get("agency_name") or row.get("agency_raw") or "",
        "notice_start": row.get("notice_start"),
        "notice_end": row.get("notice_end"),
        "d_day": d_day(row.get("notice_end")),
        "status": row.get("status") or "",
        "opinion_url": row.get("opinion_url") or "",
        "attachment_urls": jload(row.get("attachment_urls"), []),
        "impact_level": row.get("impact_level") or "",
        "summary": row.get("summary") or "",
        "impact_rationale": row.get("impact_rationale") or "",
        "affected_areas": jload(row.get("affected_areas"), []),
        "suggested_action": row.get("suggested_action") or "",
        "group_companies": jload(row.get("group_companies"), []),
    }


_ITEM_SELECT = (
    "select p.*, g.name as agency_name,"
    " a.impact_level, a.summary, a.impact_rationale, a.affected_areas, a.suggested_action"
    " from ea_policy_items p"
    " left join ea_agencies g on g.id = p.agency_id"
    " left join ea_analyses a on a.policy_item_id = p.id"
)


_EA_IMPACT_LABEL = {"high": "높음", "medium": "보통", "low": "낮음", "none": "해당없음"}


def _ea_format_message(view: dict) -> str:
    """대외협력 항목 1건을 텔레그램 평문 메시지로 만든다. (수동 전송 버튼용)"""
    lines = [f"🏛 [{view.get('category') or '대외협력'}] {view.get('title') or ''}"]
    sub = [x for x in (view.get("agency"), view.get("law_name")) if x]
    if sub:
        lines.append(" · ".join(sub))
    if view.get("notice_start") or view.get("notice_end"):
        period = f"{view.get('notice_start') or '미정'} ~ {view.get('notice_end') or '미정'}"
        if view.get("d_day"):
            period += f" ({view['d_day']})"
        lines.append(f"공고기간: {period}")
    if view.get("status"):
        lines.append(f"상태: {view['status']}")
    groups = view.get("group_companies") or []
    if groups:
        lines.append("관련 계열사: " + ", ".join(groups))
    if view.get("summary"):
        lines.append("")
        lines.append(view["summary"])
    if view.get("impact_level"):
        lines.append("")
        lines.append(f"포스코 영향: {_EA_IMPACT_LABEL.get(view['impact_level'], view['impact_level'])}")
        if view.get("impact_rationale"):
            lines.append(f"근거: {view['impact_rationale']}")
    if view.get("suggested_action"):
        lines.append(f"대응(초안): {view['suggested_action']}")
    if view.get("url"):
        lines.append("")
        lines.append(f"원문: {view['url']}")
    return "\n".join(lines)


def _ea_telegram_send(ctx: Any, text: str) -> tuple[bool, str | None]:
    """대외협력 항목을 텔레그램으로 수동 전송한다 (뉴스 카드의 '↗ 직접 전송' 버튼과 같은
    direct-send 방식 - main.py 의 알림 큐 notifications 는 건드리지 않는다. 이 파일
    상단의 설계 원칙("텔레그램·notifications 는 읽지도 쓰지도 않는다")은 자동 알림
    파이프라인에 얹지 않는다는 뜻이라, 사람이 누르는 수동 전송 버튼과는 무관하다.

    main.py 를 import 하지 않는다는 원칙에 따라 발송 로직을 최소한으로 복제한다
    (main._telegram_send 의 flood-control·재시도까지는 필요 없다 - 수동 단발 전송이다).
    """
    token = getattr(ctx.cfg, "telegram_bot_token", "") or ""
    chat_id = getattr(ctx.cfg, "telegram_chat_id", "") or ""
    if not token or not chat_id:
        return False, "텔레그램이 설정되지 않았습니다."
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url, json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": False},
            timeout=10)
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            return True, None
        return False, str(data.get("description") or resp.status_code)
    except Exception as exc:
        return False, str(exc)


# 화면 상단 카테고리(=탭). key 는 프런트·필터 API 가 공유한다.
EA_CATEGORIES = [
    {"key": "notice", "label": "입법·행정예고"},
    {"key": "bill", "label": "국회 의안"},
    {"key": "policy", "label": "정책 동향"},
    {"key": "trade", "label": "통상 환경"},
    {"key": "ministry", "label": "부처별 동향"},
]
# 카테고리 → ea_policy_items.item_type 목록. policy 는 기사 재사용이라 비어 있다.
EA_CATEGORY_TYPES = {
    "notice": ["legislation", "admin_notice"],
    "bill": ["bill"],
    "ministry": ["ministry_news"],
    "trade": ["trade_news"],
    "policy": [],
}
_AGENCY_TAIL_RE = re.compile(r"(부|처|청|위원회|위|실|원|단|공사|진흥원|KOTRA)$")
# 제목 첫머리에 오는 부처 약칭 → 정식명. "산업부, …" / "국토부는 …"
_AGENCY_HEAD_RE = re.compile(r"^\s*([가-힣]{2,7}(?:부|처|청|위))\s*[,은는이가]?\s")
_AGENCY_ALIAS = {
    "산업부": "산업통상부", "산업통상자원부": "산업통상부", "기재부": "기획재정부",
    "국토부": "국토교통부", "환경부": "기후에너지환경부", "고용부": "고용노동부",
    "과기부": "과학기술정보통신부", "과기정통부": "과학기술정보통신부", "중기부": "중소벤처기업부",
    "복지부": "보건복지부", "행안부": "행정안전부", "농식품부": "농림축산식품부",
    "해수부": "해양수산부", "문체부": "문화체육관광부", "공정위": "공정거래위원회",
    "금융위": "금융위원회",
}


def _article_agency(row: dict) -> str:
    """기사에서 발표 기관을 뽑는다. 정책브리핑 기사는 파이프라인이 author 에 발표
    부처명을 넣어 둔다. 없으면 제목 첫머리의 부처 약칭을 정식명으로 바꿔 쓴다."""
    a = (row.get("author") or "").strip()
    if a and _AGENCY_TAIL_RE.search(a) and len(a) <= 12:
        return _AGENCY_ALIAS.get(a, a)
    m = _AGENCY_HEAD_RE.match(row.get("title") or "")
    if m:
        name = m.group(1)
        return _AGENCY_ALIAS.get(name, name)
    return ""


# 정렬 옵션 — 화면 드롭다운(§8.4). deadline 이 기본(마감일이 1순위 지표).
EA_SORTS = [
    {"key": "deadline", "label": "마감 임박순"},
    {"key": "recent", "label": "최신순"},
    {"key": "impact", "label": "영향도순"},
]
# 영향도 정렬 순위 — 큰 값이 위로. 미분석(빈 문자열)은 맨 아래.
_IMPACT_RANK = {"high": 4, "medium": 3, "low": 2, "none": 1, "": 0}

_EA_ORDER_SQL = {
    # 마감일 오름차순, 마감일 없는 항목은 뒤로 (기존 동작 그대로)
    "deadline": " order by (p.notice_end is null), p.notice_end asc, p.collected_at desc",
    # 공고 시작일이 최신인 순 → 없으면 수집 시각
    "recent": " order by coalesce(p.notice_start, substr(p.collected_at,1,10)) desc,"
              " p.collected_at desc",
    # 영향도는 파이썬에서 정렬한다(_IMPACT_RANK). SQL 은 2차 키만 준비.
    "impact": " order by (p.notice_end is null), p.notice_end asc, p.collected_at desc",
}


def register_api(app: Any, ctx: Any) -> None:
    """main.create_app 에서 한 줄로 호출된다. 기존 라우트는 건드리지 않는다."""
    from fastapi.responses import JSONResponse

    db = make_ea_db(ctx)
    # 뒤늦게 추가한 컬럼·시드를 서버 기동 시 1회 보강한다(부처 재연결 + 그룹사 소급).
    try:
        db.ensure_ready()
        db.seed_agencies(SEED_AGENCIES)
        db.relink_agencies()
        backfill_groups(db)
    except Exception as exc:   # pragma: no cover — 실패해도 API 는 떠야 한다
        log.warning("대외협력 부처·그룹사 보강 스킵: %s", exc)

    @app.get("/api/ea/items")
    def ea_items(item_type: str = "", agency: str = "", impact: str = "", status: str = "",
                 due: str = "", q: str = "", group: str = "", sort: str = "deadline",
                 page: int = 1, size: int = EA_PAGE_SIZE):
        page = max(1, page)
        size = max(1, min(size, 100))
        rows = db.query_items(item_type=item_type, agency=agency, impact=impact,
                             status=status, due=due, q=q, group=group, sort=sort)
        start = (page - 1) * size
        return JSONResponse({"total": len(rows), "page": page, "size": size,
                             "items": [_item_view(r) for r in rows[start:start + size]]})

    @app.get("/api/ea/stats")
    def ea_stats():
        urgent = [_item_view(r) for r in db.query_items(due=str(EA_DUE_SOON_DAYS))
                  if (r.get("status") or "") != "종료"]
        return JSONResponse({**db.stats(), "enabled": ea_enabled(),
                             # 크롤링이 있어 소스는 키 없이도 동작한다. rest 는 키가 있을 때만.
                             "sources_active": {"S1": True, "S2": True, "S3": True, "S4": True,
                                                "S5": bool(kotra_service_key())},
                             "sources_rest": {"S1": bool(lawmaking_oc()),
                                              "S2": bool(lawmaking_oc()),
                                              "S3": bool(assembly_key())},
                             "due_soon_days": EA_DUE_SOON_DAYS,
                             "urgent": urgent[:5], "urgent_total": len(urgent)})

    @app.get("/api/ea/filters")
    def ea_filters(category: str = "notice", item_type: str = ""):
        """카테고리별로 '기관' 드롭다운을 맞춘다.

        기관 목록은 '수집된 것만'이 아니라 **관심 기관 전체**를 건수와 함께 보여준다
        (비어 있으면 (0)). 카테고리마다 성격이 다르다 — 입법·행정예고=소관 부처,
        국회 의안=소관 상임위, 부처별 동향=부처, 통상 환경=KOTRA·부처, 정책 동향=발표 부처.
        """
        # item_type(구버전 파라미터)이 오면 카테고리로 역매핑
        cat = category or {"legislation,admin_notice": "notice", "bill": "bill",
                           "ministry_news": "ministry", "trade_news": "trade"}.get(item_type, "notice")
        types = EA_CATEGORY_TYPES.get(cat, [])
        want_kind = "committee" if cat == "bill" else "ministry"

        counts = db.filters_agency_counts(types)
        if cat in ("notice", "bill", "ministry"):
            seeded = [a["name"] for a in db.agencies(True) if (a.get("kind") or "") == want_kind]
            names = sorted(set(seeded) | set(counts), key=lambda n: (-counts.get(n, 0), n))
        elif cat == "trade":
            names = sorted(set(["KOTRA"]) | set(counts), key=lambda n: (-counts.get(n, 0), n))
        else:   # policy — 발표 부처는 기사에서 뽑는다(클라이언트가 목록으로 채움). 여기선 빈 목록.
            names = []
        agencies = [{"key": n, "label": f"{n} ({counts.get(n, 0)})" if n in counts else n,
                     "count": counts.get(n, 0)} for n in names]

        gcounts = db.filters_group_counts(types)
        groups = [{"key": g, "label": f"{g} ({gcounts.get(g, 0)})", "count": gcounts.get(g, 0)}
                  for g in EA_GROUP_ORDER]

        return JSONResponse({
            "agencies": agencies,
            "agency_kind": want_kind,
            "groups": groups,
            "categories": EA_CATEGORIES,
            "impacts": [{"key": "high", "label": "높음"}, {"key": "medium", "label": "보통"},
                        {"key": "low", "label": "낮음"}, {"key": "none", "label": "해당없음"}],
            "statuses": db.filters_statuses(types),
            "dues": [{"key": "7", "label": "D-7"}, {"key": "14", "label": "D-14"},
                     {"key": "30", "label": "D-30"}],
            "sorts": EA_SORTS,
        })

    @app.get("/api/ea/items/{item_id}")
    def ea_item(item_id: str):
        row = db.item_detail(item_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "항목을 찾을 수 없습니다."}, status_code=404)
        return JSONResponse({"ok": True, "item": _item_view(row)})

    @app.post("/api/ea/items/{item_id}/telegram")
    async def ea_item_telegram(item_id: str):
        """대외협력 카드의 '텔레그램 전송' 버튼 - 요약을 그대로 채널에 보낸다."""
        if not getattr(ctx.cfg, "telegram_enabled", False):
            return JSONResponse({"ok": False, "error": "텔레그램이 설정되지 않았습니다."},
                                status_code=400)
        row = db.item_detail(item_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "항목을 찾을 수 없습니다."}, status_code=404)
        import anyio
        text = _ea_format_message(_item_view(row))

        def _work():
            return _ea_telegram_send(ctx, text)

        ok, err = await anyio.to_thread.run_sync(_work)
        if ok:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": err or "발송에 실패했습니다."}, status_code=502)

    # ── 정책 동향·통상 환경 — 기존 기사 재사용(읽기 전용) + KOTRA 항목 병합 ──
    def _news(article_cat: str, limit: int, ea_types: Sequence[str] = (),
              agency: str = "") -> list[dict]:
        out: list[dict] = []
        for r in ctx.storage.list_articles(400, 0, now_utc() - timedelta(days=45), ""):
            if article_cat in jload(r.get("categories"), []):
                out.append({
                    "id": r.get("id"), "title": r.get("title") or "",
                    "url": r.get("url_canonical") or r.get("url_original") or "",
                    "press": r.get("press_name") or "",
                    "agency": _article_agency(r),
                    "published_at": (r.get("published_at") or "")[:10],
                    "score": int(r.get("importance_score") or 0),
                    "summary": r.get("summary_text") or "",
                    "source": "article",   # /api/articles/{id}/telegram 로 전송
                })
        if ea_types:
            for r in db.recent_news_items_for_weekly(ea_types, limit=120):
                out.append({
                    "id": r["id"], "title": r.get("title") or "",
                    "url": r.get("url_canonical") or r.get("url_source") or "",
                    "press": r.get("agency_name") or r.get("agency_raw") or "",
                    "agency": r.get("agency_name") or r.get("agency_raw") or "",
                    "published_at": (r.get("notice_start") or r.get("collected_at") or "")[:10],
                    "score": _IMPACT_RANK.get(r.get("impact_level") or "", 0) * 25,
                    "summary": r.get("summary") or "",
                    "source": "ea",   # /api/ea/items/{id}/telegram 로 전송
                })
        out.sort(key=lambda x: x["published_at"], reverse=True)
        if agency:
            picks = set(agency.split(","))
            out = [x for x in out if x["agency"] in picks]
        return out[:limit]

    @app.get("/api/ea/policy-news")
    def ea_policy_news(limit: int = 40, agency: str = ""):
        return JSONResponse({"items": _news("정부/정책", max(1, min(limit, 120)), (), agency)})

    @app.get("/api/ea/trade-news")
    def ea_trade_news(limit: int = 40, agency: str = ""):
        return JSONResponse({"items": _news("글로벌 통상환경", max(1, min(limit, 120)),
                                            ("trade_news",), agency)})

    log.info("대외협력 API 등록 (/api/ea/*)")
