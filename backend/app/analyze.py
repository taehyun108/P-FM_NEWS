"""② 필터링·중복판정·LLM 분석·저장 파이프라인"""
from __future__ import annotations

from typing import Any
from typing import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import html as html_mod
import json
import os
import re
import secrets
import socket
import time
from datetime import timedelta
from urllib.parse import urlsplit

from .collect import (
    ANALYSIS_PROMPT,
    ANALYSIS_SYSTEM,
    Analysis,
    GROUP_COMPANIES,
    KOREA_KR_NEWS_RE,
    MAX_BODY_CHARS,
    NEWS_AGGREGATORS,
    PEOPLE_NEWS_CATEGORY,
    PEOPLE_NOTICE_PROMPT,
    PEOPLE_NOTICE_SYSTEM,
    POLICY_RELEVANCE_KW,
    POSCO_MENTION_RE,
    SCORE_PEOPLE_NEWS,
    TRADE_MEASURE_KW,
    _kw_hit_any,
    _notice_text,
    _score_rules_cache,
    battery_company_hit,
    collect_google_rss,
    collect_naver,
    collect_rss_feeds,
    decode_html,
    detect_categories,
    detect_group_companies,
    extract_author,
    extract_body,
    extract_ministry,
    extract_published,
    extract_thumbnail,
    extract_title,
    format_people_llm,
    format_people_notice,
    get_score_rules,
    group_lead_text,
    is_battery_scope,
    is_policy_brief,
    is_trade_topic,
    normalize_group_list,
    people_news_kind,
    prefetch_articles,
    repair_truncated_title,
    resolve_canonical,
    score_article,
    select_naver_keywords,
)
from .core import (
    Config,
    Context,
    HttpClient,
    RawItem,
    _clean,
    _has_hangul,
    _hush_libraries,
    _import,
    _looks_like_domain,
    clamp,
    cosine,
    dedupe_chips,
    domain_of,
    get_env_int,
    iso,
    jload,
    log,
    new_id,
    normalize_url,
    now_utc,
    parse_dt,
    sha256,
    title_similarity,
)
from .notify import (
    effective_threshold,
    queue_manual_notify,
)
from .storage import (
    SEED_PRESS,
    Storage,
)
from .view import (
    build_card,
    refresh_scan_store,
)




def people_summary(ctx: Context, kind: str, title: str, press: str, body: str,
                   use_llm: bool, html: str = "") -> tuple[str, str, dict]:
    """인사·부고 요약 텍스트를 만든다. 반환: (요약, 사용 모델, 토큰 usage).

    use_llm 이면 구조화를 시도하고, 실패하거나 use_llm 이 아니면 규칙 기반으로 대체한다.
    공지가 짧아 본문 추출이 푸터를 잡은 경우 og:description·<article> 텍스트로 보강한다.
    """
    notice = _notice_text(html, body)
    if use_llm:
        parsed, usage = ctx.llm.people_notice(kind, title, press, notice)
        text = format_people_llm(parsed, kind) if parsed else ""
        if text:
            return text[:1600], ctx.llm.model, usage
    return format_people_notice(notice, kind), "", {}




def _make_openai_client(api_key: str):
    """OpenAI 클라이언트. LANGSMITH_TRACING 이 켜져 있으면 LangSmith 로 감싼다.

    langsmith 미설치·추적 off 면 순수 클라이언트를 그대로 쓴다(오버헤드 0).
    """
    openai = _import("openai", "openai")
    client = openai.OpenAI(api_key=api_key)
    if _clean(os.environ.get("LANGSMITH_TRACING")).lower() in ("1", "true", "yes"):
        try:
            from langsmith.wrappers import wrap_openai
            client = wrap_openai(client)
            log.info("LangSmith 트레이싱 활성화 (project=%s)",
                     os.environ.get("LANGSMITH_PROJECT") or "default")
        except ImportError:
            log.warning("LANGSMITH_TRACING 이 켜졌지만 langsmith 패키지가 없습니다. "
                        "`pip install langsmith` 후 다시 실행하세요.")
    return client




NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"


XAI_BASE_URL = "https://api.x.ai/v1"


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"




def _make_nvidia_client(api_key: str):
    """NVIDIA NIM 클라이언트. OpenAI 호환 엔드포인트라 openai 라이브러리를 그대로 쓴다."""
    openai = _import("openai", "openai")
    return openai.OpenAI(api_key=api_key, base_url=NVIDIA_NIM_BASE_URL)




def _make_xai_client(api_key: str):
    """xAI(Grok) 클라이언트. 마찬가지로 OpenAI 호환 엔드포인트다."""
    openai = _import("openai", "openai")
    return openai.OpenAI(api_key=api_key, base_url=XAI_BASE_URL)




def _make_gemini_client(api_key: str):
    """Gemini 클라이언트. 구글이 제공하는 OpenAI 호환 엔드포인트를 그대로 쓴다."""
    openai = _import("openai", "openai")
    return openai.OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL)




class LLMClient:
    def __init__(self, cfg: Config) -> None:
        _hush_libraries()  # openai/httpx 가 import 시 로깅을 다시 켜는 경우 대비
        self.client = _make_openai_client(cfg.openai_api_key)
        self.model = cfg.llm_model
        self.embedding_model = cfg.embedding_model
        # OpenAI 임베딩이 실패할 때만 쓰는 대체 경로. 키가 없으면 그대로 None — 기존처럼
        # 실패를 삼키고 넘어간다(4단계 임베딩 유사도 판정 생략, 3단계까지만 적용).
        # 서로 다른 임베딩 모델 벡터는 차원·벡터공간이 달라 직접 비교할 수 없지만,
        # cosine() 이 차원이 다르면 0.0 을 돌려주도록 이미 방어돼 있어 오작동 없이
        # '유사하지 않음'으로만 처리된다 — 대체가 걸린 동안 중복 탐지 정확도만 낮아진다.
        self.nvidia_embed_client = (
            _make_nvidia_client(cfg.nvidia_embed_api_key) if cfg.nvidia_embed_api_key else None)
        self.nvidia_embed_model = cfg.nvidia_embed_model
        # 채팅(분석·요약·주간레포트·챗봇 응답)이 실패할 때만 쓰는 대체 경로들.
        # 순서: OpenAI(기본) → Gemini → Grok(xAI) → NVIDIA — 키가 없는 단계는 건너뛴다.
        # 마찬가지로 전부 실패하면 OpenAI 실패가 곧 최종 실패가 되는 기존 동작 유지.
        self.gemini_llm_client = (
            _make_gemini_client(cfg.gemini_api_key) if cfg.gemini_api_key else None)
        self.gemini_llm_model = cfg.gemini_llm_model
        self.xai_llm_client = (
            _make_xai_client(cfg.xai_api_key) if cfg.xai_api_key else None)
        self.xai_llm_model = cfg.xai_llm_model
        self.nvidia_llm_client = (
            _make_nvidia_client(cfg.nvidia_llm_api_key) if cfg.nvidia_llm_api_key else None)
        self.nvidia_llm_model = cfg.nvidia_llm_model
        # 모델별로 지원하는 파라미터가 다르다. 첫 호출에서 학습해 이후 재시도를 줄인다.
        # 공급자마다 서로 다른 모델이라 지원 여부도 따로 학습해야 한다.
        self._supports_json_mode = True
        self._gemini_supports_json_mode = True
        self._xai_supports_json_mode = True
        self._nvidia_supports_json_mode = True
        # 중복 판정 4단계의 임베딩 호출을 1회 실행당 이 수로 제한한다.
        # 네이버 수집 시 유사 제목이 대량으로 들어와 임베딩 폭주가 발생할 수 있다.
        self.max_embed_per_run = MAX_EMBED_PER_RUN
        self._embed_calls = 0

    def reset_run(self) -> None:
        self._embed_calls = 0

    def _chat_once(self, client: Any, model: str, supports_json_attr: str,
                    system: str, user: str) -> tuple[str, dict]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if getattr(self, supports_json_attr):
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            # response_format 미지원 모델이면 한 번만 빼고 재시도한다.
            if getattr(self, supports_json_attr) and "response_format" in str(exc):
                log.info("모델이 JSON 모드를 지원하지 않아 일반 모드로 전환합니다.")
                setattr(self, supports_json_attr, False)
                kwargs.pop("response_format", None)
                resp = client.chat.completions.create(**kwargs)
            else:
                raise
        usage = {}
        if getattr(resp, "usage", None):
            usage = {"prompt": resp.usage.prompt_tokens, "completion": resp.usage.completion_tokens,
                     "total": resp.usage.total_tokens}
        return (resp.choices[0].message.content or ""), usage

    def _chat_chain(self) -> list[tuple[Any, str, str, str]]:
        """채팅 대체 순서: OpenAI(기본) → Gemini → Grok(xAI) → NVIDIA. 키 없는 단계는 뺀다."""
        chain = [(self.client, self.model, "_supports_json_mode", "OpenAI")]
        if self.gemini_llm_client is not None:
            chain.append((self.gemini_llm_client, self.gemini_llm_model,
                          "_gemini_supports_json_mode", f"Gemini({self.gemini_llm_model})"))
        if self.xai_llm_client is not None:
            chain.append((self.xai_llm_client, self.xai_llm_model,
                          "_xai_supports_json_mode", f"Grok({self.xai_llm_model})"))
        if self.nvidia_llm_client is not None:
            chain.append((self.nvidia_llm_client, self.nvidia_llm_model,
                          "_nvidia_supports_json_mode", f"NVIDIA({self.nvidia_llm_model})"))
        return chain

    def _chat(self, system: str, user: str) -> tuple[str, dict]:
        chain = self._chat_chain()
        last_exc: Exception | None = None
        for i, (client, model, flag, label) in enumerate(chain):
            try:
                return self._chat_once(client, model, flag, system, user)
            except Exception as exc:
                last_exc = exc
                if i + 1 < len(chain):
                    log.warning("%s 채팅 호출 실패, %s로 대체: %s", label, chain[i + 1][3], exc)
        assert last_exc is not None
        raise last_exc

    def analyze(self, title: str, press: str, body: str) -> Analysis:
        prompt = ANALYSIS_PROMPT.format(title=title, press=press or "미상", body=body[:MAX_BODY_CHARS])
        last_error: Exception | None = None
        for attempt in range(3):  # 지수 백오프 3회 (PRD F4.5)
            try:
                content, usage = self._chat(ANALYSIS_SYSTEM, prompt)
                parsed = _parse_json_object(content)
                if parsed is None:
                    raise ValueError("JSON 파싱 실패")
                return _build_analysis(parsed, usage)
            except Exception as exc:
                last_error = exc
                wait = 2 ** attempt
                log.warning("LLM 분석 실패 (%d/3): %s — %ds 후 재시도", attempt + 1, exc, wait)
                if attempt < 2:
                    time.sleep(wait)
        log.error("LLM 분석 최종 실패: %s", last_error)
        return Analysis(ok=False)

    def people_notice(self, kind: str, title: str, press: str, body: str) -> tuple[dict | None, dict]:
        """인사·부고 공지를 사람별 구조로 뽑는다. 실패하면 (None, {}).

        기사에 실제로 적힌 내용만 채운다 — 고시·이력·학력이 없으면 빈 값이다.
        """
        prompt = PEOPLE_NOTICE_PROMPT.format(
            kind=("부고" if kind == "obituary" else "인사"),
            title=title, press=press or "미상", body=(body or "")[:MAX_BODY_CHARS])
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                content, usage = self._chat(PEOPLE_NOTICE_SYSTEM, prompt)
                parsed = _parse_json_object(content)
                if parsed is None:
                    raise ValueError("JSON 파싱 실패")
                return parsed, usage
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        log.warning("인사·부고 구조화 실패(규칙 기반으로 대체): %s", last_error)
        return None, {}

    def chat_text(self, system: str, user: str) -> str:
        """일반 텍스트 응답(JSON 강제 없음). 텔레그램 챗봇 질의응답용."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        chain = self._chat_chain()
        last_exc: Exception | None = None
        for i, (client, model, _flag, label) in enumerate(chain):
            try:
                resp = client.chat.completions.create(model=model, messages=messages)
                return resp.choices[0].message.content or ""
            except Exception as exc:
                last_exc = exc
                if i + 1 < len(chain):
                    log.warning("%s 채팅 호출 실패, %s로 대체: %s", label, chain[i + 1][3], exc)
        assert last_exc is not None
        raise last_exc

    def weekly_brief(self, kind: str, name: str, articles: list[dict]) -> dict:
        """주간 레포트 섹션 1건을 합성한다.

        kind='swot'  → {"s","w","o","t"} 각 2~3문장 (그룹사 섹션)
        kind='impact'→ {"impact": 3~4문장}          (정부/정책·글로벌 통상 섹션)
        기사가 없으면 빈 dict. 실패해도 레포트 전체가 죽지 않도록 예외를 삼킨다.
        """
        if not articles:
            return {}
        digest = "\n".join(
            f"- ({a.get('published_at','')[:10]}) {a.get('title','')}\n  {a.get('summary_text') or ''}"
            for a in articles)
        if kind == "swot":
            system = ("당신은 포스코 그룹 전략 담당 애널리스트다. 아래 한 주간 기사만 근거로 "
                      "해당 계열사 관점의 주간 SWOT 를 한국어로 작성하고 JSON 으로만 답한다.")
            user = (f"[대상] {name}\n[이번 주 주요 기사]\n{digest}\n\n"
                    "각 항목 2~3문장, 기사에서 실제로 읽어낼 수 있는 내용만. 근거가 없으면 "
                    '"이번 주 해당 신호 없음". 형식: '
                    '{"s":"...","w":"...","o":"...","t":"..."}')
        else:
            system = ("당신은 포스코 그룹 대외전략 담당이다. 아래 한 주간 기사만 근거로 "
                      "이 이슈들이 포스코 그룹(철강·이차전지소재·인프라)에 미치는 영향을 "
                      "한국어로 정리하고 JSON 으로만 답한다.")
            user = (f"[주제] {name}\n[이번 주 주요 기사]\n{digest}\n\n"
                    "3~4문장으로 영향과 대응 관점을 정리한다. 단정하지 말고 검토 필요 톤. "
                    '형식: {"impact":"..."}')
        try:
            content, _ = self._chat(system, user)
            return _parse_json_object(content) or {}
        except Exception as exc:
            log.warning("주간 브리핑 생성 실패 (%s/%s): %s", kind, name, exc)
            return {}

    def embed(self, text: str) -> list[float] | None:
        if self._embed_calls >= self.max_embed_per_run:
            return None  # 이번 실행 임베딩 예산 소진 — 3단계 문자열 유사도까지만 적용된다
        self._embed_calls += 1
        try:
            resp = self.client.embeddings.create(model=self.embedding_model, input=text[:2000])
            return list(resp.data[0].embedding)
        except Exception as exc:
            log.warning("OpenAI 임베딩 생성 실패: %s", exc)
            if self.nvidia_embed_client is None:
                return None
            try:
                resp = self.nvidia_embed_client.embeddings.create(
                    model=self.nvidia_embed_model, input=text[:2000])
                log.info("NVIDIA 임베딩으로 대체했습니다 (model=%s)", self.nvidia_embed_model)
                return list(resp.data[0].embedding)
            except Exception as exc2:
                log.warning("NVIDIA 임베딩도 실패: %s", exc2)
                return None




def _parse_json_object(content: str) -> dict | None:
    """모델이 코드펜스나 설명을 덧붙여도 JSON 객체만 뽑아낸다."""
    text = (content or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None




def _build_analysis(data: dict, usage: dict) -> Analysis:
    sentences = [str(s).strip() for s in (data.get("summary") or []) if str(s).strip()]
    if not sentences or sentences == ["요약불가"]:
        return Analysis(ok=False, token_usage=usage)

    sentiment = str(data.get("sentiment") or "중립").strip()
    if sentiment not in ("긍정", "중립", "부정"):
        sentiment = "중립"

    # LLM 이 만든 그룹사명은 정규 목록에 없으면 버린다. (PRD F4.2)
    raw_groups = [str(g).strip() for g in (data.get("group_companies") or [])]
    groups = [g for g in raw_groups if g in GROUP_COMPANIES]

    swot: dict[str, dict[str, Any]] = {}
    raw_swot = data.get("swot") or {}
    for key in ("s", "w", "o", "t"):
        node = raw_swot.get(key) or {}
        try:
            score = int(clamp(float(node.get("score", 0) or 0), 0, 100))
        except (TypeError, ValueError):
            score = 0
        text = str(node.get("text") or "").strip() or "해당 없음"
        swot[key] = {"score": score, "text": text}

    return Analysis(
        summary_sentences=sentences[:5],
        perspective=str(data.get("perspective") or "").strip(),
        keywords=[str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()],
        group_companies=groups,
        sentiment=sentiment,
        swot=swot,
        token_usage=usage,
        ok=True,
    )




def swot_total(swot: dict[str, dict[str, Any]]) -> int:
    """(S+O)-(W+T) 를 0~100 으로 정규화한다. (PRD F4.4)

    원값 범위는 -200~+200 이므로 (raw + 200) / 4 로 옮긴다.
    """
    if not swot:
        return 0
    s = swot.get("s", {}).get("score", 0)
    w = swot.get("w", {}).get("score", 0)
    o = swot.get("o", {}).get("score", 0)
    t = swot.get("t", {}).get("score", 0)
    raw = (s + o) - (w + t)
    return int(round(clamp((raw + 200) / 4.0, 0, 100)))




# =====================================================================
# 11. 중복 판정 4단계 (PRD F2.2)
#     통신사 전재로 같은 기사가 10~20건 들어오는 것이 최대 노이즈 요인이다.
#     단계마다 비용이 오르므로 순서를 지킨다.
# =====================================================================

TITLE_SIM_THRESHOLD = 0.9      # 3단계


EMBED_SIM_THRESHOLD = 0.92     # 4단계


EMBED_PREFILTER_MIN = 0.62     # 이 아래는 4단계로 보내지 않는다(비용·지연 절감)


DEDUP_WINDOW_HOURS = 24


MAX_EMBED_PER_RUN = 30         # 1회 실행당 임베딩 호출 상한 (유사 제목 대량 유입 방어)




def find_duplicate(
    storage: Storage,
    llm: LLMClient | None,
    title: str,
    published_at: datetime,
    content_hash: str,
    url_canonical: str,
    candidates: Sequence[dict],
    exclude_id: str = "",
) -> dict | None:
    """중복이면 기존 대표 기사 행을, 아니면 None 을 돌려준다.

    exclude_id: 이미 DB에 존재하는 자기 자신의 id(예: _drain_deferred 가 재검증하는
    deferred 기사 — url_canonical 이 아직 자기 자신의 url_source 그대로다). 없으면
    무시하지만, 있으면 1·2단계에서 '자기 자신'과의 매칭은 건너뛰고 다음 단계로
    넘어간다 — 안 그러면 캐노니컬이 자기 자신으로 남아 있는 한 1단계가 항상
    자기 자신을 찾아 반환해버려 정작 다른 진짜 중복 기사(2단계 본문 해시 등)까지는
    확인이 안 된다(2026-09-14, 병렬화 검증 중 발견 — 순차 실행에서도 있던 버그).
    """
    # 1단계 — 정규화 URL 완전 일치
    if url_canonical:
        hit = storage.find_by_canonical(url_canonical)
        if hit and hit["id"] != exclude_id:
            return hit

    # 2단계 — 본문 해시 일치
    if content_hash:
        hit = storage.find_by_content_hash(content_hash)
        if hit and hit["id"] != exclude_id:
            return hit

    window = timedelta(hours=DEDUP_WINDOW_HOURS)
    leftovers: list[tuple[dict, float]] = []

    # 3단계 — 제목 유사도 AND 발행 시각 차이. 두 조건의 AND 다.
    # 유사도만 보면 연재·기획 기사가 잘못 묶인다.
    for cand in candidates:
        if cand.get("id") == exclude_id:
            continue
        cand_dt = parse_dt(cand.get("published_at"))
        if cand_dt is None or abs(cand_dt - published_at) > window:
            continue
        sim = title_similarity(title, cand.get("title", ""))
        if sim >= TITLE_SIM_THRESHOLD:
            return cand
        if sim >= EMBED_PREFILTER_MIN:
            leftovers.append((cand, sim))

    # 4단계 — 3단계에서 확정되지 않은 "잔여 후보"만 임베딩으로 본다.
    # 전건에 임베딩을 돌리면 비용이 요약 단계를 넘어선다. (PRD F2.2)
    if not leftovers or llm is None:
        return None
    new_vec = llm.embed(title)
    if not new_vec:
        return None
    finalists = [c for c, _ in sorted(leftovers, key=lambda x: -x[1])[:10]]
    # 후보 목록에는 임베딩이 실려 오지 않는다(행당 ~31KB라 사이클마다 수 MB가 된다).
    # 여기 도달한 소수 후보의 것만 지금 읽는다.
    cached = storage.embeddings_for([c["id"] for c in finalists])
    for cand in finalists:
        cand_vec = jload(cached.get(cand["id"]), None)
        if not cand_vec:
            cand_vec = llm.embed(cand.get("title", ""))
            if cand_vec:
                storage.update_article(cand["id"], {"title_embedding": cand_vec})
        if cosine(new_vec, cand_vec) >= EMBED_SIM_THRESHOLD:
            return cand
    return None




# =====================================================================
# 12. 파이프라인 (PRD F1.1 게이트)
# =====================================================================

# 1회 실행에서 본문 확보(G3/G4)·중복판정·저장까지 갈 최대 건수. 이 안에 든 것은
# 즉시 저장되고, 예산이 남으면 바로 분석된다. 나머지는 _defer_overflow 가 메타만
# 저장하므로(비용 0) 이 값을 올릴 이유가 없다 — 올리면 '본문 있는데 미분석' 큐만 커진다.
MAX_PROCESS_PER_RUN = 24


# 상한을 넘어 이번 회차에 못 다룬 신선 후보 — 버리지 않고 이만큼은 메타데이터만
# 저장해 둔다(본문·LLM 없이). 못 담은 나머지는 다음 회차에 다시 후보가 된다.
DEFER_PER_RUN = 40


# 메타만 저장된 기사(deferred)를 회차당 이만큼 본문 확보 + 분석한다.
# 이 드레인은 안전망이지 우선순위가 아니다 — 신규 분석이 예산을 먼저 쓴다.
# deferred 는 이제 화면에 안 뜨므로(분석 완료분만 노출) 조금 더 공격적으로 빼도 된다.
DEFER_DRAIN_PER_RUN = 16


# deferred 상태로 이 시간을 넘기면(본문을 계속 못 받음) 보관 처리한다.
DEFER_MAX_AGE_HOURS = 24


# 1회 실행에서 처리할 인사·부고 최대 건수 (점수 경쟁 없이 항상 처리)
PEOPLE_PER_RUN = 30


# 분석 백로그 드레인의 LLM 호출 동시 실행 수 (2026-09-14). 항목끼리 서로
# 참조하지 않는 독립 호출이라 prefetch_articles 와 같은 이유로 병렬화한다 —
# 순차 처리하면 20건에 100초 넘게 걸려 PRD §6(1회 실행 20초 목표)를 크게 넘긴다.
LLM_ANALYZE_WORKERS = 5


# 그 중 사람별 구조 요약(LLM)에 쓸 수 있는 최대 호출 수. 나머지는 규칙 기반으로 저장되고
# 나중에 `repeople` 로 채울 수 있다. 일일 상한(LLM_DAILY_LIMIT)도 함께 적용된다.
PEOPLE_LLM_PER_RUN = get_env_int("PEOPLE_LLM_PER_RUN", 12, 0)


# 인사·부고는 '기록·레퍼런스' 성격이라 일반 신선도 컷오프(72h)로 버리면 안 된다.
# 부처 인사는 발행 후 며칠 지나 인지되는 경우가 많다. 이 창 안이면 수집한다(알림은
# is_backfill=6h 규칙이 그대로 막으므로 오래된 인사가 텔레그램으로 가지는 않는다).
PEOPLE_BACKFILL_CUTOFF_HOURS = 24 * 30


# 메타만 저장분(deferred)이 이 수 미만이면 파이프라인을 '안정'으로 본다. 본문 대기 큐(30)와
# 달리 느슨하게 둔다 — deferred 는 알림 후보가 아니라서 억제를 유지할 이유가 약하다.
DEFER_BACKLOG_STABLE = 600



# ── 보존 정책 (Supabase 무료 500MB 한도 대비) ─────────────────────────
# 핵심 기사(알림 대상이었음/중요도 임계값 이상/그룹사 태그 O)는 cfg.article_retention_days
# (기본 550일 ≈ 18개월) 동안 보관하고, 그 외 잡음·주제이탈 기사는 아래 기간만 보관한다.
# 자식 테이블(요약·SWOT·본문·알림)은 on delete cascade 로 함께 삭제된다.
RETENTION_SOFT_DAYS   = 90    # 일반·archived 기사 보관일


RETENTION_LOG_DAYS    = 90    # collection_logs 보관일 (하루 약 288행 쌓임)


RETENTION_LEDGER_DAYS = 180   # url_ledger 보관일 (이후 재수집돼도 게이트가 다시 거른다)


RETENTION_TGLOG_DAYS  = 30    # telegram_log(봇 발신 로그) 보관일


RETENTION_INTERVAL_HOURS = 24 # 보존 정리 실행 간격 (매 수집 사이클마다 하면 과하다)


RETENTION_VACUUM_DAYS = 7     # SQLite VACUUM(파일 축소) 최소 간격


_RETENTION_LAST: "datetime | None" = None


_VACUUM_LAST: "datetime | None" = None




def matches_keywords(text: str, keywords: Sequence[str]) -> bool:
    """언론사 RSS 는 키워드로 질의할 수 없으므로 수집 후 로컬에서 거른다. (F1.3)"""
    lowered = (text or "").lower()
    return any(k.lower() in lowered for k in keywords if k)




def interleave_by_group(fresh: list[tuple[RawItem, bool]], cap: int) -> list[tuple[RawItem, bool]]:
    """그룹사별 큐를 라운드로빈으로 돌며 cap 건을 고른다.

    입력은 이미 중요도 내림차순. 그룹사가 감지 안 되는 항목은 마지막 순번으로 둔다.
    포스코퓨처엠 기사가 아무리 많아도 다른 그룹사 기사가 매 회차 최소 1건은 뽑힌다.
    """
    buckets: dict[str, list[tuple[RawItem, bool]]] = {}
    for pair in fresh:
        item = pair[0]
        groups = detect_group_companies(f"{item.title} {item.snippet}")
        key = groups[0] if groups else "_기타"
        buckets.setdefault(key, []).append(pair)

    # 그룹사 버킷을 먼저, '_기타'를 마지막에
    order = [k for k in buckets if k != "_기타"] + (["_기타"] if "_기타" in buckets else [])
    picked: list[tuple[RawItem, bool]] = []
    while len(picked) < cap and any(buckets[k] for k in order):
        for k in order:
            if buckets[k]:
                picked.append(buckets[k].pop(0))
                if len(picked) >= cap:
                    break
    return picked




def _title_snippet_relevant(title: str, snippet: str) -> tuple[bool, list[str]]:
    """본문 없이 관련성을 저비용 판정한다. (keep, groups) 반환.

    스니펫(특히 네이버 description)은 검색어를 되풀이하는 블러브라 신뢰도가 낮다.
    그래서 배터리·통상 **일반어**는 제목에서만 인정하고, 그룹사·포스코 언급과
    **회사명·명시적 조치명**은 스니펫도 함께 본다(고정밀 신호는 블러브에 우연히
    나오기 어렵다). 최종 관련성은 _drain_deferred 가 본문으로 다시 판정하므로
    여기서 조금 놓쳐도 복구된다."""
    probe = f"{title}\n{snippet or ''}"
    groups = normalize_group_list(detect_group_companies(probe))
    keep = bool(
        groups
        or POSCO_MENTION_RE.search(probe)
        or battery_company_hit(probe)                 # 회사명은 리드에 있어도 인정
        or _kw_hit_any(probe, TRADE_MEASURE_KW)       # 조치명도 마찬가지
        or is_battery_scope(title, "")
        or is_trade_topic(title, "")
        or people_news_kind("", title)
    )
    return keep, groups




def _defer_overflow(ctx: Context, overflow: list[tuple[RawItem, bool]],
                    dedup_candidates: list[dict]) -> int:
    """상한을 넘어 이번 회차에 못 다룬 신선 후보를 메타데이터만 저장한다.

    본문·HTTP·LLM 을 쓰지 않으므로 비용이 0이다. 관련성·카테고리·점수는 **제목** 기준으로만
    임시 판정하고(스니펫 블러브 오염 방지), 본문 재검증·최종 태깅은 _drain_deferred 가 한다.
    통과분은 analyzed_at=NULL 로 넣어 두되 화면에는 노출하지 않는다(분석 완료분만 노출).
    """
    storage = ctx.storage
    known = {c.get("url_canonical") or c.get("url_source") for c in dedup_candidates}
    score_overrides, score_customs = get_score_rules(storage)
    saved = 0
    for item, is_backfill in overflow[:DEFER_PER_RUN]:
        if item.url_source in known or item.url_original in known:
            continue
        keep, groups = _title_snippet_relevant(item.title, item.snippet or "")
        if not keep:
            continue   # 원장에 넣지 않는다 — 다음 회차 상한이 커지면 정식 처리될 수 있다
        pk = people_news_kind(item.url_source, item.title)
        cats = [PEOPLE_NEWS_CATEGORY] if pk else detect_categories(item.title, "")
        aid = new_id()
        row = {
            "id": aid, "url_source": item.url_source, "url_source_aliases": [],
            "url_canonical": item.url_source, "url_original": item.url_original,
            "title": item.title, "press_id": None,
            "press_name": item.press_hint or "", "author": "",
            "published_at": iso(item.published_at), "collected_at": iso(now_utc()),
            "source_type": item.source_type, "thumbnail_url": "",
            "content_hash": "", "dedup_group_id": aid, "is_representative": True,
            "is_backfill": is_backfill,
            "importance_score": SCORE_PEOPLE_NEWS if pk else score_article(
                item.title, "", groups, 3, score_overrides, score_customs),
            "sentiment": None, "keywords": [], "group_companies": groups,
            "categories": cats,
            "title_embedding": None, "analyzed_at": None, "status": "active",
        }
        if storage.insert_article(row):
            saved += 1
            known.add(item.url_source)
            ctx.seen_cache.add(item.url_source)
            dedup_candidates.append({
                "id": aid, "title": item.title, "published_at": iso(item.published_at),
                "dedup_group_id": aid, "is_representative": True, "title_embedding": None,
                "press_name": row["press_name"], "content_hash": "",
                "url_canonical": item.url_source, "url_source": item.url_source,
            })
    return saved




def _drain_deferred(ctx: Context, limit: int, dedup_candidates: list[dict],
                    people_llm: int = 0) -> int:
    """메타만 저장된(deferred) 기사를 본문 확보 → 관련성 재검 → 분석한다.

    본문을 계속 못 받으면 DEFER_MAX_AGE_HOURS 후 보관 처리한다.
    본문 기준 관련성에서 탈락하면(제목만 그럴듯했던 경우) 역시 보관한다.
    people_llm = 이 드레인에서 인사·부고 구조화에 쓸 수 있는 LLM 호출 수.
    """
    storage, http = ctx.storage, ctx.http
    score_overrides, score_customs = get_score_rules(storage)
    done = 0
    # 중복 판정을 통과한 항목만 모아 뒀다가 LLM 분석만 병렬로 보낸다(아래 참고).
    to_analyze: list[tuple[str, dict, str]] = []
    pending = storage.deferred_articles(limit)
    # 본문 확보는 신규 수집과 동일하게 병렬로 받는다. 순차로 받으면 느린 언론사 한 곳이
    # 회차 전체를 붙잡아 사이클 시간이 수십 초씩 늘어난다.
    targets = {a["id"]: (a.get("url_original") or a.get("url_canonical") or a.get("url_source"))
               for a in pending}
    fetched = prefetch_articles(http, [u for u in targets.values() if u])
    for art in pending:
        aid = art["id"]
        target = targets.get(aid)
        canonical, html = fetched.get(target, ("", "")) if target else ("", "")
        body = extract_body(html) if html else ""
        if len(body) < 200:
            collected = parse_dt(art.get("collected_at"))
            if collected and (now_utc() - collected) > timedelta(hours=DEFER_MAX_AGE_HOURS):
                storage.update_article(aid, {"status": "archived"})
            continue   # 다음 회차 재시도

        # 인사·부고면 사람별 구조로 정리한다 (예산 남으면 LLM, 아니면 규칙 기반)
        pk = people_news_kind(canonical or art["url_source"], art["title"])
        if pk:
            press_name, press_id, _ = resolve_press(storage, canonical or target,
                                                    art.get("press_name") or "", html, http)
            storage.update_article(aid, {
                "url_canonical": canonical or art["url_canonical"],
                "press_id": press_id, "press_name": press_name,
                "categories": [PEOPLE_NEWS_CATEGORY], "importance_score": SCORE_PEOPLE_NEWS,
                "analyzed_at": iso(now_utc()),
            })
            summary, model, usage = people_summary(ctx, pk, art["title"], press_name, body,
                                                   use_llm=people_llm > 0, html=html)
            if model:
                people_llm -= 1
            storage.save_summary({
                "id": new_id(), "article_id": aid,
                "summary_text": summary, "perspective_text": "",
                "summary_source": "notice", "model": model, "token_usage": usage or None,
                "created_at": iso(now_utc()),
            })
            done += 1
            continue

        rule_groups = detect_group_companies(f"{art['title']}\n{group_lead_text(body)}")
        probe = f"{art['title']}\n{body}"
        if not (rule_groups or POSCO_MENTION_RE.search(probe)
                or is_battery_scope(art["title"], body[:1500])
                or is_trade_topic(art["title"], body[:1500])
                or bool(KOREA_KR_NEWS_RE.search(canonical or ""))):
            storage.update_article(aid, {"status": "archived"})
            continue

        # 본문 도착 후 중복 재판정 — 그새 정식 수집된 기사와 겹치면 흡수한다
        content_hash = sha256(body)
        dup = find_duplicate(storage, ctx.llm, art["title"], parse_dt(art["published_at"]),
                             content_hash, canonical or art["url_canonical"], dedup_candidates,
                             exclude_id=aid)
        if dup and dup["id"] != aid:
            storage.append_alias(dup["id"], art["url_source"])
            storage.update_article(aid, {"status": "archived"})
            continue

        press_name, press_id, press_tier = resolve_press(storage, canonical or target,
                                                         art.get("press_name") or "", html, http)
        storage.update_article(aid, {
            "url_canonical": canonical or art["url_canonical"],
            "press_id": press_id, "press_name": press_name,
            "author": extract_author(html, body, press_name),
            "content_hash": content_hash, "thumbnail_url": extract_thumbnail(html),
            "importance_score": score_article(art["title"], body, rule_groups, press_tier,
                                              score_overrides, score_customs),
        })
        row = {"id": aid, "title": art["title"], "press_id": press_id, "press_name": press_name,
               "importance_score": score_article(art["title"], body, rule_groups, press_tier,
                                                 score_overrides, score_customs),
               "group_companies": rule_groups}
        # 중복 판정(find_duplicate)까지는 반드시 여기서 순차로 끝낸다 — 같은 배치 안의
        # 두 기사가 서로 중복이면, 먼저 처리된 쪽이 저장한 content_hash·canonical URL을
        # DB에서 바로 조회해 뒤 항목이 중복으로 잡아낸다. 이 시점부터는 서로 독립적인
        # LLM 호출만 남으므로(2026-09-14) 병렬로 보낸다.
        to_analyze.append((aid, row, body))

    if to_analyze:
        def _analyze_one(item: tuple[str, dict, str]) -> tuple[str, bool]:
            aid, row, body = item
            try:
                ok = analyze_and_save(ctx, aid, row, body, "fulltext") is not None
            except Exception as exc:
                log.warning("드레인 분석 실패(개별 항목, 나머지는 계속): %s — %s", aid, exc)
                ok = False
            return aid, ok

        with ThreadPoolExecutor(max_workers=min(LLM_ANALYZE_WORKERS, len(to_analyze))) as pool:
            for aid, ok in pool.map(_analyze_one, to_analyze):
                if ok:
                    done += 1
                else:
                    # 분석 실패(3회 재시도해도 동일) — 링크·제목 카드로 확정해 deferred 큐에서 뺀다.
                    storage.update_article(aid, {"analyzed_at": iso(now_utc())})
    return done




def _save_people_news(ctx: Context, item: RawItem, canonical: str, html: str, body: str,
                      kind: str, press_name: str, press_id: str | None,
                      dedup_candidates: list[dict], is_backfill: bool,
                      use_llm: bool = False) -> bool:
    """인사·부고 기사를 저장한다. 포스코 관점·SWOT 은 없다.

    use_llm 이면 사람별 구조 요약을 시도한다. 반환: LLM 을 실제로 썼으면 True.
    """
    storage = ctx.storage
    aid = new_id()
    row = {
        "id": aid, "url_source": item.url_source, "url_source_aliases": [],
        "url_canonical": canonical or item.url_source, "url_original": item.url_original,
        "title": item.title, "press_id": press_id, "press_name": press_name,
        "author": extract_author(html, body, press_name),
        "published_at": iso(item.published_at), "collected_at": iso(now_utc()),
        "source_type": item.source_type, "thumbnail_url": extract_thumbnail(html),
        "content_hash": sha256(body), "dedup_group_id": aid, "is_representative": True,
        "is_backfill": is_backfill, "importance_score": SCORE_PEOPLE_NEWS,
        "sentiment": "중립", "keywords": [], "group_companies": [],
        "categories": [PEOPLE_NEWS_CATEGORY], "title_embedding": None,
        "analyzed_at": iso(now_utc()), "status": "active",
    }
    if not storage.insert_article(row):
        return False
    ctx.seen_cache.add(item.url_source)
    dedup_candidates.append({
        "id": aid, "title": item.title, "published_at": iso(item.published_at),
        "dedup_group_id": aid, "is_representative": True, "title_embedding": None,
        "press_name": press_name, "content_hash": row["content_hash"],
        "url_canonical": row["url_canonical"], "url_source": item.url_source,
    })
    summary, model, usage = people_summary(ctx, kind, item.title, press_name, body, use_llm, html=html)
    storage.save_summary({
        "id": new_id(), "article_id": aid,
        "summary_text": summary, "perspective_text": "",
        "summary_source": "notice", "model": model, "token_usage": usage or None,
        "created_at": iso(now_utc()),
    })
    return bool(model)




def prettify_domain(domain: str) -> str:
    """SEED_PRESS 에도 없고 힌트도 없을 때 쓰는 최소 정리.

    예전에는 'bbsi.co.kr' → 'bbsi' 처럼 도막을 냈지만, 뜻 없는 영문 조각이
    화면에 그대로 노출된다. 매핑이 없으면 도메인 전체를 유지한다.
    """
    return (domain or "").strip()




def clean_site_name(name: str, domain: str = "") -> str:
    """매체명에서 부제·영문 병기·래퍼 접두를 떼어낸다.

    예) '경기신문 - 기본에 충실한 …'      → '경기신문'
        'AP신문 |  온라인뉴스미디어  …'    → 'AP신문'
        '더스탁(The Stock)'              → '더스탁'
        'Daum | 뉴스1'   (daum.net)     → '뉴스1'   (래퍼 도메인은 뒤쪽이 실제 출처)
    """
    name = re.sub(r"\s+", " ", html_mod.unescape(name or "")).strip()
    if not name:
        return ""
    # domain 은 domain_of 로 이미 접힌 값(v.daum.net → daum.net)이 들어온다.
    if domain in NEWS_AGGREGATORS:
        # '래퍼 | 실제출처' — 마지막 구분자 뒤(한글 조각)를 취한다.
        parts = re.split(r"\s*[|\-–·]\s*", name)
        name = next((p for p in reversed(parts) if _has_hangul(p)), parts[-1]).strip()
    else:
        # 첫 구분자 앞이 매체명. 뒤는 대개 슬로건·부제다.
        # 쉼표는 넣지 않는다 — '중국, 리튬 배터리…' 같은 기사 제목에서 첫 쉼표 앞
        # 조각이 매체명으로 오인된다(홈페이지 title 전용 처리는 _homepage_site_name 참고).
        name = re.split(r"\s*[|\-–]\s*", name, maxsplit=1)[0].strip()
        # 끝에 붙은 '(English…)' 영문 병기 제거 (한글 병기 '(주간)' 등은 남긴다).
        name = re.sub(r"\s*\([A-Za-z0-9 .,'&/\-]+\)\s*$", "", name).strip()
    return name




def site_name_from_html(html: str, domain: str = "") -> str:
    """HTML 의 og:site_name / <meta name=publisher> 에서 매체명을 뽑는다.

    SEED_PRESS 에 없는 매체도 대부분 이 태그에 한글 매체명을 넣는다.
    영문 사이트명·도메인 형태는 신뢰하지 않는다(한글이 있어야 채택).
    <title> 은 여기서 보지 않는다 — 개별 기사 페이지의 <title>은 '차세대 배터리
    기술 한눈에'처럼 기사 제목 그 자체인 경우가 흔해서, 짧다는 것만으로는 매체명과
    구분이 안 된다. <title> 기반 추정은 홈페이지에서만 신뢰할 수 있다
    (_homepage_site_name — 홈페이지 title은 '기사 제목'이 될 수 없다).
    """
    for pat in (r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\'](?:twitter:site|publisher|source)["\'][^>]+content=["\']([^"\']+)["\']'):
        m = re.search(pat, html or "", re.I)
        if m:
            name = clean_site_name(m.group(1), domain)
            if name and _has_hangul(name) and not _looks_like_domain(name):
                return name
    return ""




def _title_media_name(title_raw: str) -> str:
    """<title> 에서 매체명 후보를 뽑는다.

    '매체명 - 슬로건'(스카이데일리) · '매체명, 슬로건, …'(한국무역신문) 은 첫 조각이
    매체명이지만, '슬로건 - 매체명'(조선비즈가 만드는 프리미엄 경제 주간지 - 이코노미조선)
    처럼 매체명이 맨 뒤에 오는 사이트도 있다. 첫 조각이 실패하면 마지막 조각도 본다.
    """
    raw = html_mod.unescape(title_raw).strip()
    parts = [p.strip() for p in re.split(r"\s*[|\-–,]\s*", raw) if p.strip()]
    for cand in ((parts[0], parts[-1]) if parts else ()):
        if cand and _has_hangul(cand) and not _looks_like_domain(cand) and len(cand) <= 20:
            return cand
    return ""




def _homepage_site_name(http: "HttpClient", domain: str, source_host: str = "") -> str:
    """새 언론사를 처음 만났을 때 1회, 기사 페이지에서 매체명을 못 찾으면
    홈페이지에서 다시 시도한다 — 홈페이지 title 은 거의 항상 매체명을 담고,
    (기사 제목과 달리) 짧아도 매체명으로 신뢰할 수 있다.

    domain_of() 가 news.tvchosun.com 같은 서브도메인을 등록 도메인(tvchosun.com)
    으로 접는데, 그 등록 도메인 자체는 리다이렉트 스텁만 있는 경우가 있다(실사례:
    tvchosun.com·dizzo.com 은 각각 55·77바이트짜리 빈 페이지만 준다). 기사가 실제로
    있던 호스트를 먼저 시도하고, 안 되면 등록 도메인 · www. 붙인 도메인 순으로 넘어간다.
    호스트마다 https 를 먼저, 안 되면 http 로도 시도한다(실사례: areyou.co.kr 은
    인증서가 호스트명과 안 맞아 https 전체가 SSLError 로 막혀 있었다).
    """
    candidates = [source_host, domain]
    if not domain.startswith("www."):
        candidates.append(f"www.{domain}")
    hosts: list[str] = []
    for h in candidates:
        if h and h not in hosts:
            hosts.append(h)

    for host in hosts:
        html = ""
        for scheme in ("https", "http"):
            try:
                resp = http.get(f"{scheme}://{host}/", timeout=6)
                resp.raise_for_status()
                html = decode_html(resp)
                break
            except Exception as exc:
                log.debug("홈페이지 매체명 조회 실패 %s://%s: %s", scheme, host, exc)
        if not html:
            continue
        name = site_name_from_html(html, domain)
        if name:
            return name
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        if m:
            name = _title_media_name(m.group(1))
            if name:
                return name
    return ""




def resolve_press(storage: Storage, url: str, hint: str, html: str = "",
                  http: "HttpClient | None" = None) -> tuple[str, str | None, int]:
    """도메인으로 언론사를 식별한다. 미등록이면 pending 으로 적재하고 수집을 막지 않는다. (F2.3)

    우선순위: SEED_PRESS > 본문 og:site_name/<title> > 홈페이지 재조회 > 피드 힌트 > 도메인 표기.
    """
    domain = domain_of(url)
    if not domain:
        return (hint if hint and not _looks_like_domain(hint) else ""), None, 3

    row = storage.press_by_domain(domain)
    seed = SEED_PRESS.get(domain)
    og_name = site_name_from_html(html, domain)
    # 이 매체를 처음 보는데(row is None) 기사 페이지에서 이름을 못 찾았으면, 홈페이지를
    # 한 번 더 본다. 기사 페이지는 SEO 상 기사 제목만 <title>에 넣는 경우가 많아
    # 실패하기 쉬운데, 홈페이지는 거의 항상 매체명을 담는다.
    # 기존에 등록된 매체라도 이름이 아직 도메인 그대로('mdilbo.com')면 계속 재시도
    # 한다 — 예전엔 row is None 일 때 딱 1회만 시도해서, 그 1회가 실패하면 화면
    # 필터 칩에 도메인이 영구히 그대로 노출됐다(2026-09-16 지적: 실제 다수 발견).
    if not og_name and http is not None and not seed and (
            row is None or _looks_like_domain(row.get("name", ""))) and (
            not hint or _looks_like_domain(hint)):
        og_name = _homepage_site_name(http, domain, urlsplit(url).hostname or "")

    if row is None:
        if seed:
            name, tier, status = seed[0], seed[1], "approved"
        elif og_name:
            name, tier, status = og_name, 3, "approved"
        elif hint and not _looks_like_domain(hint):
            name, tier, status = hint, 3, "pending"
        else:
            name, tier, status = prettify_domain(domain), 3, "pending"
        row = storage.upsert_press(domain, name, tier, status)
    elif seed and row.get("name") != seed[0] and _looks_like_domain(row.get("name", "")):
        # 예전에 도메인 그대로 저장됐던 행을 SEED 정식 이름으로 교체한다.
        storage.update_press_name(domain, seed[0], seed[1])
        row = storage.press_by_domain(domain) or row
    elif not seed and og_name and _looks_like_domain(row.get("name", "")):
        # SEED 에 없고 도메인으로만 저장돼 있던 행을 og:site_name 으로 교체한다.
        storage.update_press_name(domain, og_name, int(row.get("tier") or 3))
        row = storage.press_by_domain(domain) or row

    name = row.get("name") or ""
    if _looks_like_domain(name):
        name = seed[0] if seed else (og_name or prettify_domain(domain))
    return name, row.get("id"), int(row.get("tier") or 3)




def run_once(ctx: Context, max_llm: int | None = None, force_naver: bool = False) -> dict:
    """수집 → 게이트 → 분석 → 저장 → 알림 큐 적재를 1회 수행한다.

    max_llm 을 주면 이번 실행의 LLM 호출을 그 수만큼으로 제한한다(검증용).
    force_naver 가 True면 네이버 호출 간격 제한을 무시한다(수동 1회 실행용).
    """
    started = time.monotonic()
    cfg, storage, http = ctx.cfg, ctx.storage, ctx.http
    http.reset()

    # 다중 인스턴스 오배포 안전장치 — 실행권을 못 얻으면 이번 회차는 건너뛴다.
    # 컬럼이 아직 없는 기존 DB(Supabase 수동 마이그레이션 전)에서도 파이프라인이
    # 멈추면 안 되므로, 락 자체가 실패하면(예외) '락 없이 진행'으로 폴백한다 —
    # 이건 필수 기능이 아니라 사고를 줄이는 안전장치일 뿐이다.
    try:
        stale_after = max(60, cfg.poll_interval_sec * PIPELINE_LOCK_STALE_MULT)
        if not storage.try_acquire_pipeline_lock(_INSTANCE_ID, stale_after):
            # 예전엔 여기서 '내' id 를 찍어 놓고 남의 것처럼 표시해 원인 추적이 어려웠다.
            # 실제 락 주인을 DB 에서 읽어 함께 남긴다(충돌 회차에만 1회 조회).
            owner = ""
            try:
                owner = str(storage.get_run_state().get("pipeline_lock_owner") or "")
            except Exception:
                pass
            log.warning(
                "다른 인스턴스(%s)가 파이프라인을 실행 중입니다 — 이번 회차는 건너뜁니다."
                " (내 id=%s) 기사 중복은 이 락이 막지만 네이버·LLM 같은 외부 API 의"
                " 일일 한도는 인스턴스끼리 공유되므로, 안 쓰는 쪽은 꼭 내려 주세요.",
                owner or "알 수 없음", _INSTANCE_ID)
            return {"fetched": 0, "new": 0, "skipped_locked": True}
    except Exception as exc:
        log.debug("파이프라인 락 확인 실패(무시하고 진행): %s", exc)

    state = storage.get_run_state()
    last_success = parse_dt(state.get("last_success_at"))
    bootstrap_at = parse_dt(state.get("bootstrap_at"))
    if bootstrap_at is None:
        # 최초 실행 — 이 시각 이전에 발행된 기사는 "역사"이므로 절대 알림하지 않는다.
        bootstrap_at = now_utc()
        storage.set_run_state({"bootstrap_at": iso(bootstrap_at)})

    # 최초 실행 또는 30분 이상 중단 후 재개는 억제 모드로 돈다. (PRD F1.1)
    suppressed = (
        state.get("notify_mode") != "active"
        or last_success is None
        or (now_utc() - last_success) > timedelta(minutes=30)
    )
    if suppressed:
        log.info("억제 모드로 실행합니다 (알림 발송 없음).")

    keyword_rows = storage.enabled_keywords()
    keywords = [k["keyword"] for k in keyword_rows]
    if not keywords:
        log.warning("활성 키워드가 없습니다. `python backend/main.py initdb` 를 먼저 실행하세요.")
        return {"fetched": 0, "new": 0}

    # 마스터 패널에서 고친 중요도 규칙 — state 를 이미 읽었으니 여기서 같이 반영한다
    # (get_score_rules 캐시도 이걸로 갱신돼 이후 _defer_overflow/_drain_deferred 호출에서
    # 같은 회차 안엔 재조회하지 않는다).
    score_overrides = jload(state.get("score_overrides"), {}) or {}
    score_customs = jload(state.get("score_custom_rules"), []) or []
    _score_rules_cache.update(overrides=score_overrides, customs=score_customs, at=time.monotonic())

    feeds = storage.enabled_feeds()
    feed_types = {f["source_type"] for f in feeds}

    # ── 수집 ─────────────────────────────────────────────────────────
    raw: list[RawItem] = []
    if "google_rss" in feed_types:
        raw += collect_google_rss(http, keyword_rows)
    if "naver_api" in feed_types and cfg.naver_enabled:
        # 하루 25,000회 한도 때문에 매 실행이 아니라 일정 간격으로만 호출한다.
        due = force_naver or (time.monotonic() - ctx.last_naver_fetch) >= cfg.naver_interval_sec
        if due:
            # 일일 무료 한도(25,000회)를 넘지 않도록 이번 회차 몫만 고른다.
            picked = select_naver_keywords(keyword_rows, ctx.naver_cycle)
            ctx.naver_cycle += 1
            if len(picked) < len(keyword_rows):
                log.info("네이버 키워드 교대 조회: %d/%d개 (회차 %d, 일일 한도 대응)",
                         len(picked), len(keyword_rows), ctx.naver_cycle)
            raw += collect_naver(http, cfg, picked)
            ctx.last_naver_fetch = time.monotonic()
    rss_feeds = [f for f in feeds if f["source_type"] == "rss"]
    if rss_feeds:
        # 언론사 RSS 는 전체 기사를 주므로 키워드로 먼저 거른다.
        # 이 필터가 없으면 무관한 기사까지 G3(HTTP)·G6(과금)까지 올라간다.
        # [부고]·[인사]·[승진] 말머리 기사는 키워드와 무관하게 통과시킨다. (사용자 지정)
        raw += [
            item for item in collect_rss_feeds(http, rss_feeds)
            if matches_keywords(f"{item.title} {item.snippet}", keywords)
            or people_news_kind(item.url_source, item.title)
        ]
    fetched_count = len(raw)

    # ── G0: 실행 내 중복 제거 ────────────────────────────────────────
    unique: dict[str, RawItem] = {}
    for item in raw:
        if item.url_source and item.url_source not in unique:
            unique[item.url_source] = item
    items = list(unique.values())

    # ── G1: seen-set 캐시 (메모리 조회) ──────────────────────────────
    if not ctx.seen_cache:
        ctx.seen_cache = storage.recent_url_sources(72)
    after_g1 = [i for i in items if i.url_source not in ctx.seen_cache]
    skipped_g1 = len(items) - len(after_g1)

    # ── G2: 전체 기간 DB 대조 (판정 권위) ────────────────────────────
    seen = storage.seen_url_sources([i.url_source for i in after_g1])
    after_g2 = [i for i in after_g1 if i.url_source not in seen]
    skipped_g2 = len(after_g1) - len(after_g2)
    ctx.seen_cache.update(seen)
    if seen:
        storage.bump_ledger(list(seen))  # 원장에 있는 항목만 카운트가 오른다

    # ── G2.5: 신선도 컷오프 (네트워크 불필요, 비용 0) ────────────────
    fresh: list[tuple[RawItem, bool]] = []   # (항목, is_backfill)
    now = now_utc()
    for item in after_g2:
        if item.published_at is None:
            storage.upsert_ledger(item.url_source, "no_pubdate")
            continue
        age = now - item.published_at
        # 인사·부고는 훨씬 긴 창을 쓴다 — 며칠 지나 올라온 부처 인사도 놓치지 않는다.
        cutoff = (PEOPLE_BACKFILL_CUTOFF_HOURS
                  if people_news_kind(item.url_source, item.title)
                  else cfg.backfill_cutoff_hours)
        if age > timedelta(hours=cutoff):
            storage.upsert_ledger(item.url_source, "stale")
            continue
        fresh.append((item, age > timedelta(hours=cfg.fresh_cutoff_hours)))

    log.info(
        "수집 %d건 → G0 %d → G1 통과 %d(-%d) → G2 통과 %d(-%d) → G2.5 통과 %d",
        fetched_count, len(items), len(after_g1), skipped_g1,
        len(after_g2), skipped_g2, len(fresh),
    )

    # 여기까지 외부 HTTP 요청은 소스 조회분뿐이어야 한다. (§6 검증 기준)
    source_requests = http.count

    dedup_candidates = storage.recent_articles_for_dedup(now - timedelta(hours=DEDUP_WINDOW_HOURS + 1))
    ctx.llm.reset_run()  # 이번 실행의 임베딩 호출 카운터 초기화

    # ── LLM 예산 배분 (일일·회차 상한, 이월분 예약) ────────────────────
    daily_left = max(0, cfg.llm_daily_limit - storage.llm_calls_today())
    # max_llm 이 주어지면(수동 실행) 그 값이 1회 상한을 대신한다. 아니면 설정값.
    per_run_cap = max_llm if max_llm is not None else cfg.llm_per_run
    llm_budget = min(daily_left, per_run_cap)
    if max_llm is not None:
        log.info("이번 실행의 LLM 분석을 최대 %d건으로 제한합니다.", llm_budget)
    if daily_left <= 0:
        log.warning("일일 LLM 호출 상한(%d)에 도달했습니다. 저장만 하고 분석은 다음 날 재개합니다.",
                    cfg.llm_daily_limit)

    # 메타만 저장된(deferred) 기사가 있으면 예산의 일부를 그 드레인에 예약한다(안 그러면
    # 신규 분석이 매 회차 예산을 다 써 deferred 가 안 빠진다). 다만 예약은 작게 —
    # 신규 기사 분석이 우선이고, 남는 예산은 아래에서 deferred 드레인이 더 쓴다.
    defer_pending = storage.deferred_count()
    defer_budget = min(6, defer_pending, max(1, llm_budget // 4)) if defer_pending else 0
    llm_budget = max(0, llm_budget - defer_budget)

    # ── 인사·부고 분리 (점수 경쟁에서 제외) ─────────────────────────────
    # 인사·부고는 점수 경쟁에서 빼고 항상 처리한다 — 제목 점수가 0이라 일반 큐에 두면
    # 영원히 상한에 밀린다. (사용자 지정) 사람별 구조 요약은 LLM 을 쓰되, 이번 회차·오늘
    # 남은 예산 안에서만 한다. 예산 밖은 규칙 기반으로 저장되고 `repeople` 로 채운다.
    people = [p for p in fresh if people_news_kind(p[0].url_source, p[0].title)][:PEOPLE_PER_RUN]
    people_urls = {p[0].url_source for p in people}
    fresh = [p for p in fresh if p[0].url_source not in people_urls]
    people_llm_left = min(PEOPLE_LLM_PER_RUN, max(0, daily_left - llm_budget - defer_budget))

    # ── 처리 상한 · 중요도 정렬 · 그룹사 균형 인터리브 ──────────────────
    # 저장 상한은 LLM 예산과 분리한다. 저장(본문 확보+중복판정)은 비용이 작고,
    # 여기서 조이면 관련 기사가 큐에서 굶어 72시간 뒤 stale 로 사라진다.
    # 넘친 후보는 _defer_overflow 가 메타만 저장해 두므로 '수집'은 무엇도 잃지 않는다.
    pending_now = storage.unanalyzed_count() + storage.deferred_count()
    fresh_available = len(fresh)   # 절단 전 신규 후보 수 — 안정화 판단에 쓴다
    process_cap = MAX_PROCESS_PER_RUN

    # 중요도 순 + 그룹사 균형. 중요도만 쓰면 포스코퓨처엠(제목 +50)이 큐를 독점해
    # 포스코DX·이앤씨 기사가 매 회차 뒤로 밀린다. 그룹사별로 번갈아 뽑는다. (PRD F4.6)
    fresh.sort(key=lambda pair: score_article(pair[0].title, pair[0].snippet, [], 3,
                                              score_overrides, score_customs), reverse=True)
    overflow: list[tuple[RawItem, bool]] = []
    if fresh_available > process_cap:
        picked = interleave_by_group(fresh, process_cap)
        picked_urls = {p[0].url_source for p in picked}
        overflow = [pair for pair in fresh if pair[0].url_source not in picked_urls]
        log.info("이번 실행 즉시 처리 %d건 · 메타 저장 대기 %d건 · 분석 대기 %d건",
                 len(picked), len(overflow), pending_now)
        fresh = picked

    # 인사·부고를 같은 처리 루프 앞에 붙인다 (본문만 받아 _save_people_news 로 확정)
    fresh = people + fresh
    if people:
        log.info("인사·부고 %d건 처리", len(people))

    # ── 처리 루프 준비: 카운터 · 알림 판정 키워드 ───────────────────────
    new_count = 0
    dup_count = 0
    # 알림 판정에 필요한 항목만 담는다. {id, score, is_backfill, published_at, priority,
    #  policy: (해당여부, 키워드통과), trade: (해당여부, 키워드통과)}
    saved_for_notify: list[dict] = []
    # '무조건 받을' 키워드 — 제목·계열사에 있으면 임계값 무관 무조건 알림(우선 기사)
    always_kws = [k for k in jload(state.get("always_notify_keywords"), []) if k]
    # '제외' 키워드 — 제목에 있으면 임계값·무조건 받을 키워드보다 우선해서 알림하지 않는다(최우선).
    exclude_kws = [k for k in jload(state.get("exclude_notify_keywords"), []) if k]
    # (하위호환) 무조건 발송 점수 — UI 제거됨, 컬럼·기본 0. 설정돼 있으면 우선 기사로 취급.
    try:
        hard_score = int(state.get("hard_notify_score") or 0)
    except (TypeError, ValueError):
        hard_score = 0
    # 특수 주제(정책·통상) — 관심 키워드 하나 AND 필수 공통 키워드 하나 (둘 다 필수).
    # 제목에 제외 키워드가 있으면 이 주제 알림에서 뺀다.
    policy_notify_kws = [k for k in jload(state.get("policy_notify_keywords"), []) if k]
    policy_required_kws = [k for k in jload(state.get("policy_required_keywords"), []) if k]
    policy_exclude_kws = [k for k in jload(state.get("policy_exclude_keywords"), []) if k]
    trade_notify_kws = [k for k in jload(state.get("trade_notify_keywords"), []) if k]
    trade_required_kws = [k for k in jload(state.get("trade_required_keywords"), []) if k]
    trade_exclude_kws = [k for k in jload(state.get("trade_exclude_keywords"), []) if k]

    # ── G3: 리다이렉트 해제 + HTML 확보 (병렬) ───────────────────────
    prefetched = prefetch_articles(http, [item.url_original for item, _ in fresh])

    for item, is_backfill in fresh:
        canonical, html = prefetched.get(item.url_original, ("", ""))
        if not canonical:
            storage.upsert_ledger(item.url_source, "extract_failed")
            continue

        # ── G4: 본문 추출 (G3 응답을 재사용하므로 추가 요청 없음) ────
        body = extract_body(html)
        # 네이버가 '...' 로 자른 제목을 원문 제목으로 되돌린다 (이후 모든 단계가 이 제목을 쓴다)
        item.title = repair_truncated_title(item.title, html)
        summary_source = "fulltext" if len(body) >= 300 else "snippet"
        if summary_source == "snippet":
            body = item.snippet or item.title

        press_name, press_id, press_tier = resolve_press(storage, canonical, item.press_hint, html, http)
        is_policy = bool(KOREA_KR_NEWS_RE.search(canonical or ""))
        # 정책브리핑 기사는 기자명 대신 발표 부처명을 넣는다. (사용자 지정)
        author = extract_ministry(html, body) if is_policy else extract_author(html, body, press_name)
        content_hash = sha256(body) if summary_source == "fulltext" else ""

        # ── G5: 중복 그룹 판정 ───────────────────────────────────────
        existing = find_duplicate(
            storage, ctx.llm, item.title, item.published_at, content_hash, canonical, dedup_candidates
        )
        if existing:
            # 같은 기사를 가리키는 다른 소스 URL 을 누적한다.
            # 다음 실행부터 G1 에서 탈락하므로 HTTP 요청이 발생하지 않는다.
            storage.append_alias(existing["id"], item.url_source)
            ctx.seen_cache.add(item.url_source)
            dup_count += 1
            # 더 상위 언론사에서 온 중복이면 카드의 표시 정보(제목·링크·언론사·
            # 기자·썸네일)를 그쪽으로 승격한다. 요약·SWOT·키워드 등 분석 결과는
            # 같은 사건이라 그대로 두고, 재정렬을 피하려 발행시각도 유지한다.
            if press_tier < storage.press_tier_by_id(existing.get("press_id")):
                promo = {
                    "title": item.title,
                    "url_canonical": canonical,
                    "url_original": item.url_original,
                    "press_id": press_id,
                    "press_name": press_name,
                    "author": author,
                }
                new_thumb = extract_thumbnail(html)
                if new_thumb:
                    promo["thumbnail_url"] = new_thumb
                storage.update_article(existing["id"], promo)
                log.info("대표 승격: %s → %s (%s)", existing.get("press_name") or "?",
                         press_name, item.title[:40])
            continue

        # ── 인사·부고 — 포스코 관점·SWOT 없이 사람별 구조 요약만 담는다 (사용자 지정) ──
        pk = people_news_kind(canonical or item.url_source, item.title)
        if pk:
            used = _save_people_news(ctx, item, canonical, html, body, pk, press_name,
                                     press_id, dedup_candidates, is_backfill,
                                     use_llm=people_llm_left > 0)
            if used:
                people_llm_left -= 1
            new_count += 1
            continue

        # 그룹사는 제목+리드까지만 본다 — 본문 말미의 스치는 계열사 언급이
        # 기사 주체를 가로채는 것을 막는다. (GROUP_LEAD_CHARS 주석 참고)
        rule_groups = detect_group_companies(f"{item.title}\n{group_lead_text(body)}")
        relevance_probe = f"{item.title}\n{item.snippet or ''}\n{body}"
        # 글로벌 통상환경 기사: 제목에 통상 조치명 + 포스코 관련 산업어가 함께 있으면
        # 포스코 미언급이어도 수집한다. (사용자 지정)
        # 조치명은 여전히 **제목**에서만 인정하되(is_trade_topic 내부), 산업어는 본문까지 본다 —
        # "산업장관, EU 산업가속화법 우려 전달" 처럼 조치명만 제목에 있고 철강·배터리는
        # 본문에서 설명되는 기사가 통째로 버려지던 문제를 막는다.
        is_trade = is_trade_topic(item.title, f"{item.snippet or ''}\n{body[:1500]}")
        if is_policy:
            # 정책브리핑 기사: 포스코 미언급이어도 포스코 산업에 닿는 주제면 수집.
            if not matches_keywords(relevance_probe, POLICY_RELEVANCE_KW):
                storage.upsert_ledger(item.url_source, "off_topic")
                ctx.seen_cache.add(item.url_source)
                continue
        elif is_trade:
            pass  # 통상 신호 + 산업 키워드가 확인됨 — 포스코 미언급 허용
        elif is_battery_scope(item.title, f"{item.snippet or ''}\n{body[:1500]}"):
            pass  # 배터리 생태계(소재·셀·전기차·ESS·원료) — 포스코 미언급 허용 (사용자 지정)
        elif not rule_groups and not POSCO_MENTION_RE.search(relevance_probe):
            # 일반 기사: 포스코·계열사가 어디에도 없으면 무관 기사 — 저장하지 않는다.
            storage.upsert_ledger(item.url_source, "off_topic")
            ctx.seen_cache.add(item.url_source)
            continue
        # 카테고리는 제목+스니펫으로만 잡고, 분석 후 요약으로 다시 계산한다.
        categories = detect_categories(item.title, item.snippet or "")
        if is_policy and "정부/정책" not in categories:
            categories = ["정부/정책"] + categories
        if is_trade and "글로벌 통상환경" not in categories:
            categories = ["글로벌 통상환경"] + categories
        score = score_article(item.title, body, rule_groups, press_tier, score_overrides, score_customs)

        article_id = new_id()
        row = {
            "id": article_id,
            "url_source": item.url_source,
            "url_source_aliases": [],
            "url_canonical": canonical,
            "url_original": item.url_original,
            "title": item.title,
            "press_id": press_id,
            "press_name": press_name,
            "author": author,
            "published_at": iso(item.published_at),
            "collected_at": iso(now_utc()),
            "source_type": item.source_type,
            "thumbnail_url": extract_thumbnail(html),
            "content_hash": content_hash,
            "dedup_group_id": article_id,
            "is_representative": True,
            "is_backfill": is_backfill,
            "importance_score": score,
            "sentiment": None,
            "keywords": [],
            "group_companies": rule_groups,
            "categories": categories,
            "title_embedding": None,
            "analyzed_at": None,
            "status": "active",
        }
        if not storage.insert_article(row):
            # UNIQUE 위반 = 경합 상황. 최종 방어선이 작동한 것이므로 조용히 넘어간다.
            ctx.seen_cache.add(item.url_source)
            continue

        new_count += 1
        ctx.seen_cache.add(item.url_source)
        dedup_candidates.append({
            "id": article_id, "title": item.title, "published_at": iso(item.published_at),
            "dedup_group_id": article_id, "is_representative": True,
            "title_embedding": None, "press_name": press_name, "content_hash": content_hash,
        })
        # 본문은 분석 전까지만 임시 보관한다 (§7-3). 분석 완료 시 삭제된다.
        storage.save_body(article_id, body, summary_source)

        # ── G6: LLM 분석 (과금 지점) ─────────────────────────────────
        # 이번 실행 예산 안에서만 즉시 분석하고, 나머지는 아래 드레인 단계나
        # 다음 실행에서 처리한다. 60초 주기를 지키기 위한 조치다.
        if llm_budget > 0:
            llm_budget -= 1
            analyzed = analyze_and_save(ctx, article_id, dict(row), body, summary_source)
            if analyzed is not None:
                score = analyzed
        probe = f"{item.title}\n{body}"
        # 우선 알림: '항상 발송 키워드' 매칭 또는 '무조건 발송 점수' 이상이면
        # 임계값·야간 게이트를 우회한다. (포스코퓨처엠 특례는 폐지 — 키워드로 추가)
        # '항상 발송 키워드' 는 **제목 + 판정된 주체 그룹사**로만 본다 — 본문 전체를 훑으면
        # 포스코 그룹 기사 대부분에 '포스코홀딩스'·'포스코퓨처엠'이 스치듯 등장해
        # 사실상 모든 그룹 기사가 야간에도 발송된다(파업 기사 오발송 사례, 2026-09-08).
        priority_probe = f"{item.title}\n{' '.join(rule_groups)}"
        excluded = _kw_hit_any(item.title, exclude_kws)   # 제목에 제외 키워드 → 무조건 알림 안 함
        is_priority = (not excluded) and (
            _kw_hit_any(priority_probe, always_kws)
            or (hard_score > 0 and score >= hard_score))
        # 특수 주제 발송 조건: 관심 키워드 하나 AND 필수 공통 키워드 하나 (둘 다 필수, 비면 발송 안 함).
        # 제목에 그 주제의 제외 키워드가 있으면 뺀다.
        policy_ok = (_kw_hit_any(probe, policy_notify_kws)
                     and _kw_hit_any(probe, policy_required_kws)
                     and not _kw_hit_any(item.title, policy_exclude_kws))
        trade_ok = (_kw_hit_any(probe, trade_notify_kws)
                    and _kw_hit_any(probe, trade_required_kws)
                    and not _kw_hit_any(item.title, trade_exclude_kws))
        saved_for_notify.append({
            "id": article_id, "score": score, "is_backfill": is_backfill,
            "published_at": item.published_at, "priority": is_priority, "excluded": excluded,
            "policy": (is_policy, policy_ok),
            "trade": (is_trade, trade_ok),
        })

    # ── 넘친 신선 후보를 메타데이터만 저장한다 (본문·LLM 없음, 비용 0) ──
    # '수집' 단계에서 관련 기사를 절대 잃지 않기 위한 장치. 아래 _drain_deferred 가
    # 다음 회차부터 본문을 받아 정식 분석한다.
    if overflow:
        deferred = _defer_overflow(ctx, overflow, dedup_candidates)
        if deferred:
            log.info("메타데이터만 저장 %d건 (다음 회차부터 본문 확보·분석)", deferred)

    # ── 분석 백로그 드레인 (병렬, 2026-09-14) ────────────────────────
    # 이전 실행에서 본문까지 저장되고 분석만 밀린 기사를 예산 안에서 처리한다.
    # 항목끼리 서로 참조하지 않는 독립적인 LLM 호출이라 병렬로 돌려도 안전하다
    # (dedup 판정이 껴 있는 _drain_deferred 와 달리 여긴 순서 의존이 없다).
    analyzed_backlog = 0
    if llm_budget > 0:
        pendings = storage.unanalyzed_with_body(llm_budget)

        def _analyze_pending(pending: dict) -> bool:
            row = {
                "id": pending["id"], "title": pending["title"],
                "press_id": pending.get("press_id"), "press_name": pending.get("press_name"),
                "importance_score": pending.get("importance_score") or 0,
                "group_companies": jload(pending.get("group_companies"), []),
            }
            try:
                return analyze_and_save(
                    ctx, pending["id"], row, pending["body"], pending["summary_source"]) is not None
            except Exception as exc:
                log.warning("백로그 분석 실패(개별 항목, 나머지는 계속): %s — %s",
                           pending.get("id"), exc)
                return False

        if pendings:
            with ThreadPoolExecutor(max_workers=min(LLM_ANALYZE_WORKERS, len(pendings))) as pool:
                analyzed_backlog = sum(1 for ok in pool.map(_analyze_pending, pendings) if ok)
            llm_budget -= len(pendings)
    if analyzed_backlog:
        log.info("분석 백로그 %d건 처리(병렬)", analyzed_backlog)

    # ── 메타만 저장된(deferred) 기사 드레인 — 본문 확보 → 관련성 재검 → 분석 ──
    # 예약해 둔 defer_budget + 앞 단계에서 남은 예산을 함께 쓴다.
    drain_budget = defer_budget + max(0, llm_budget)
    drained = _drain_deferred(ctx, min(drain_budget, DEFER_DRAIN_PER_RUN), dedup_candidates,
                              people_llm=people_llm_left) if drain_budget else 0
    if drained:
        log.info("메타 저장분 분석 %d건 (예약 예산 %d)", drained, defer_budget)

    # 30일 넘은 임시 본문 정리 (§7-3 보존 기간)
    purged = storage.cleanup_bodies(30)
    if purged:
        log.info("임시 본문 %d건 정리(30일 초과)", purged)
    # 24시간 넘게 등록·취소 안 한 URL 미리보기(draft) 정리
    dropped = storage.purge_stale_drafts(24)
    if dropped:
        log.info("미확정 미리보기 %d건 정리", dropped)
    # 48시간 지난 제목 임베딩 정리 — 중복 판정 창(25시간) 밖이면 다시 안 읽는다.
    # 행당 ~31KB 로 DB 용량의 대부분을 차지하므로 매일 되찾는다.
    cleared = storage.purge_stale_embeddings(48)
    if cleared:
        log.info("오래된 제목 임베딩 %d건 정리", cleared)

    # ── 보존 정책: 하루 1회만 (Supabase 무료 500MB 한도 대비) ─────────
    # 핵심 기사는 cfg.article_retention_days(≈18개월), 잡음은 RETENTION_SOFT_DAYS(90일).
    global _RETENTION_LAST, _VACUUM_LAST
    _n = now_utc()
    if _RETENTION_LAST is None or (_n - _RETENTION_LAST) >= timedelta(hours=RETENTION_INTERVAL_HOURS):
        _RETENTION_LAST = _n
        keep_score = effective_threshold(ctx)
        gone = storage.purge_old_articles(cfg.article_retention_days, RETENTION_SOFT_DAYS, keep_score)
        logs_gone = storage.prune_collection_logs(RETENTION_LOG_DAYS)
        ledger_gone = storage.prune_url_ledger(RETENTION_LEDGER_DAYS)
        tglog_gone = storage.prune_telegram_log(RETENTION_TGLOG_DAYS)
        if gone or logs_gone or ledger_gone or tglog_gone:
            log.info("보존 정리: 기사 %d · 수집로그 %d · URL원장 %d · 발송로그 %d 삭제 (핵심 %d일·잡음 %d일 보관)",
                     gone, logs_gone, ledger_gone, tglog_gone,
                     cfg.article_retention_days, RETENTION_SOFT_DAYS)
        if gone:
            # 대량 삭제분을 스캔 스토어(델타는 삭제를 못 봄)에 즉시 반영한다.
            try:
                refresh_scan_store(storage, full=True)
            except Exception as e:   # pragma: no cover
                log.debug("스캔 스토어 재적재 실패(무시): %s", e)
        # SQLite 는 삭제해도 파일이 안 줄어들어 가끔 VACUUM 으로 회수한다(락 위험 있어 드물게).
        if gone and cfg.db_backend == "sqlite" and (
                _VACUUM_LAST is None or (_n - _VACUUM_LAST) >= timedelta(days=RETENTION_VACUUM_DAYS)):
            _VACUUM_LAST = _n
            try:
                storage.vacuum()
                log.info("VACUUM 완료 — 삭제분 디스크 회수")
            except Exception as e:
                log.warning("VACUUM 실패(무시): %s", e)

    # ── 알림 큐 적재 (PRD F3.3 / F7.1) ───────────────────────────────
    queued = 0
    notify_threshold = effective_threshold(ctx)
    # 특수 주제 기사(정책브리핑·글로벌 통상환경)는 기본적으로 웹에만. 마스터가 켜야 알림. (사용자 지정)
    _on = lambda v: str(v or "0") not in ("0", "False", "false", "")
    notify_policy, notify_trade = _on(state.get("notify_policy")), _on(state.get("notify_trade"))

    def _topic_ok(pair: tuple[bool, bool], enabled: bool) -> bool:
        is_topic, kw_ok = pair
        return (not is_topic) or (enabled and kw_ok)

    for it in saved_for_notify:
        if not cfg.telegram_enabled:
            break
        # 정책·통상 주제 기사가 ①관심 + ②필수공통 키워드를 모두 통과하고 토글이 켜져 있으면
        # → 일반 중요도 임계값을 우회해 발송한다(정책·통상 기사는 포스코 미언급이라 점수가 낮다).
        #   ②(필수공통) 가 비어 있으면 kw_ok=False → 여전히 발송 안 됨.
        topic_hit = ((it["policy"][0] and it["policy"][1] and notify_policy)
                     or (it["trade"][0] and it["trade"][1] and notify_trade))
        should_send = (
            (not suppressed)                       # 부트스트랩·복구 억제
            and (not it.get("excluded"))           # 제목에 '제외' 키워드 → 무조건 웹에만
            and (not it["is_backfill"])            # 6시간 넘은 기사는 웹에만
            and _topic_ok(it["policy"], notify_policy)
            and _topic_ok(it["trade"], notify_trade)
            and (it["score"] >= notify_threshold or it["priority"] or topic_hit)  # 중요도 게이트(우선·주제매칭은 우회)
            and it["published_at"] is not None
            and it["published_at"] >= bootstrap_at  # 파이프라인 가동 이전 기사는 절대 알림 안 함
        )
        status = "queued" if should_send else "skipped"
        # priority: 1 = '무조건 받을 키워드'(야간도 우회 가능) · 2 = 정책·통상 주제 매칭(임계값만 우회)
        prio_val = 1 if it["priority"] else (2 if topic_hit else 0)
        if storage.queue_notification(it["id"], cfg.telegram_chat_id, status, prio_val):
            queued += 1 if should_send else 0

    duration_ms = int((time.monotonic() - started) * 1000)
    storage.log_collection({
        "run_at": iso(now_utc()),
        "source_type": "all",
        "fetched_count": fetched_count,
        "new_count": new_count,
        "dup_count": dup_count,
        "skipped_seen_count": skipped_g1 + skipped_g2,
        "http_request_count": http.count,
        "error": None,
        "duration_ms": duration_ms,
    })

    body_backlog = storage.unanalyzed_count()      # 본문까지 확보돼 곧 분석·알림될 큐 — 폭주 위험
    defer_backlog = storage.deferred_count()        # 메타만 저장분 — 본문·분석 없이 배경에서 천천히 드레인
    backlog = body_backlog + defer_backlog
    # 부트스트랩/복구 억제는 "한 회"가 아니라 파이프라인이 안정될 때까지 유지한다. (PRD F1.1)
    # 초기 수집 몇 시간 동안 밀린 기사가 한꺼번에 알림으로 쏟아지는 것을 막는 장치다.
    # 안정화 판정은 '알림 폭주 위험'(본문 대기 큐)만 본다. 메타만 저장분(deferred)은
    # 분석·본문이 없어 알림 후보가 아니고, 대부분 관련성 재검에서 보관 처리되며 느리게
    # 빠지므로, 여기에 세면 배경 큐가 조금만 쌓여도 알림이 영구히 억제된다. (조사 2026-09-08)
    stabilized = (fresh_available < 20 and new_count < 10
                  and body_backlog < 30 and defer_backlog < DEFER_BACKLOG_STABLE)
    next_mode = "active" if (not suppressed or stabilized) else "suppressed"
    if suppressed and next_mode == "active":
        log.info("파이프라인이 안정되어 알림을 활성화합니다.")
    storage.set_run_state({"last_success_at": iso(now_utc()), "notify_mode": next_mode})

    log.info(
        "완료: 신규 %d · 중복 %d · 분석대기 %d · 알림큐 %d · HTTP %d회(소스조회 %d) · %.1f초",
        new_count, dup_count, backlog, queued, http.count, source_requests, duration_ms / 1000,
    )
    return {
        "fetched": fetched_count, "new": new_count, "dup": dup_count, "queued": queued,
        "analysis_backlog": backlog,
        "http": http.count, "source_http": source_requests, "duration_ms": duration_ms,
        "suppressed": suppressed,
    }




def analyze_and_save(ctx: Context, article_id: str, row: dict, body: str, summary_source: str) -> int | None:
    """LLM 분석 결과를 저장하고, 갱신된 중요도 점수를 돌려준다.

    analyze() 가 내부적으로 3회 재시도하므로, 여기서 실패하면 재처리해도 같은 결과다.
    임시 본문은 성공·실패와 무관하게 삭제한다(§7-3 최소 보관). 실패 기사는
    본문이 없으므로 백로그 큐에서 빠지고, 링크·제목만 남는다. (PRD F4.5 취지)
    """
    analysis = ctx.llm.analyze(row["title"], row.get("press_name") or "", body)
    if not analysis.ok:
        log.warning("분석 실패 — 링크·제목만 저장합니다: %s", row["title"][:40])
        ctx.storage.delete_body(article_id)
        return None

    # 룰 기반 그룹사가 1순위. (PRD F4.2)
    # LLM 은 '이차전지·공급망 뉴스니까 포스코퓨처엠' 식으로 본문에 없는 계열사를
    # 태깅하는 경향이 강하다. LLM 이 보탠 그룹사는 제목·본문에 회사명(별칭)이
    # 실제로 등장하는 것만 채택한다.
    rule_groups = normalize_group_list(list(row.get("group_companies") or []))
    # LLM 이 보탠 계열사의 근거는 제목 + 리드 + 요약 + 키워드에서만 찾는다.
    # 본문 전체를 근거로 삼으면 말미의 스치는 언급까지 통과해 기사 주체가 뒤바뀐다.
    kw_text = " ".join(analysis.keywords)
    mentioned = set(detect_group_companies(
        f"{row['title']}\n{group_lead_text(body)}\n{analysis.summary_text}\n{kw_text}"))
    llm_verified = [g for g in analysis.group_companies if g in mentioned]
    # LLM 이 group_companies 에 안 넣었어도 키워드에 계열사명이 있으면 채택한다.
    kw_groups = [g for g in detect_group_companies(kw_text) if g != "포스코"]
    groups = normalize_group_list(rule_groups + llm_verified + kw_groups)
    # 그룹사로 표기된 값은 키워드에서 제외한다 — 칩 중복의 근본 원인이다.
    keywords = dedupe_chips(analysis.keywords, exclude=groups)[:6]
    score_overrides, score_customs = get_score_rules(ctx.storage)
    score = score_article(row["title"], body, groups, 3 if not row.get("press_id") else 1,
                          score_overrides, score_customs)
    score = max(int(row.get("importance_score") or 0), score)

    # 카테고리는 제목 + 요약 + LLM 키워드로 다시 계산한다(수집 때는 스니펫만 봤다).
    # 키워드는 LLM 이 뽑은 핵심어 6개뿐이라, 본문 전체를 스캔할 때 같은 과태깅 없이
    # 요약에서 빠진 주제('인허가', '이차전지 소재' 등)를 잡아 준다.
    categories = detect_categories(row["title"], f"{analysis.summary_text}\n{' '.join(keywords)}")
    if is_policy_brief(row) and "정부/정책" not in categories:
        categories = ["정부/정책"] + categories
    # 통상환경은 detect_categories 가 '제목에 조치명' 조건으로 이미 판정한다 — 강제 추가 안 함.

    ctx.storage.update_article(article_id, {
        "sentiment": analysis.sentiment,
        "keywords": keywords,
        "group_companies": groups,
        "categories": categories,
        "importance_score": score,
        "analyzed_at": iso(now_utc()),
    })
    ctx.storage.save_summary({
        "id": new_id(),
        "article_id": article_id,
        "summary_text": analysis.summary_text,
        "perspective_text": analysis.perspective,
        "summary_source": summary_source,
        "model": ctx.cfg.llm_model,
        "token_usage": analysis.token_usage,
        "created_at": iso(now_utc()),
    })
    # 근거가 부족한 snippet 기반 기사에는 SWOT 을 만들지 않는다. (PRD F4.5)
    if summary_source == "fulltext" and analysis.swot:
        ctx.storage.save_swot({
            "article_id": article_id,
            "s_score": analysis.swot["s"]["score"], "s_text": analysis.swot["s"]["text"],
            "w_score": analysis.swot["w"]["score"], "w_text": analysis.swot["w"]["text"],
            "o_score": analysis.swot["o"]["score"], "o_text": analysis.swot["o"]["text"],
            "t_score": analysis.swot["t"]["score"], "t_text": analysis.swot["t"]["text"],
            "total_score": swot_total(analysis.swot),
            "model": ctx.cfg.llm_model,
            "created_at": iso(now_utc()),
        })
    ctx.storage.delete_body(article_id)  # 분석 끝 — 임시 본문 삭제
    return score




def is_public_http_url(raw_url: str) -> bool:
    """공인 인터넷 주소인가 — 사설망·루프백·링크로컬이면 False.

    수동 URL 등록은 서버가 그 주소를 대신 가져온다. 막지 않으면 사내망 주소나
    클라우드 메타데이터(169.254.169.254)를 대신 긁게 만들 수 있다(SSRF).
    """
    import ipaddress
    import socket
    host = (urlsplit(raw_url).hostname or "").strip("[]")
    if not host:
        return False
    if host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal")):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False   # 이름을 못 풀면 가져올 수도 없다
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True




def analyze_url(ctx: Context, raw_url: str, activate: bool = True) -> dict:
    """사용자가 직접 붙여넣은 URL 하나를 포토카드로 만든다. (PRD F8 수동 등록)

    수집 게이트(G1/G2/G2.5 신선도)는 건너뛴다 — 오래된 기사여도 등록할 수 있어야 한다.
    분석 규격(요약·관점·키워드·SWOT)은 파이프라인과 동일하다.
    activate=False 면 status='draft' 로 저장한다 — 사용자가 '등록' 을 눌러야 목록에 뜬다.
    반환: {"ok": bool, "card": {...}, "draft_id": "..."} 또는 {"ok": False, "error": "..."}
    """
    storage, http = ctx.storage, ctx.http
    raw_url = (raw_url or "").strip()
    if not re.match(r"^https?://", raw_url, re.I):
        return {"ok": False, "error": "http/https 로 시작하는 URL 을 입력하세요."}
    if not is_public_http_url(raw_url):
        # 사설망·로컬 주소를 넣어 서버가 내부망을 대신 긁게 만드는 SSRF 를 막는다.
        return {"ok": False, "error": "외부에 공개된 뉴스 주소만 등록할 수 있습니다."}

    url_source = normalize_url(raw_url)

    def _existing_result(row_id: str) -> dict:
        """이미 있는 기사 — active 면 already, draft 면 다시 미리보기로 돌려준다."""
        detail = storage.article_detail(row_id)
        r = {"ok": True, "card": build_card(detail)}
        if (detail or {}).get("status") == "draft":
            r["draft_id"] = row_id
        else:
            r["already"] = True
        return r

    # 이미 등록·미리보기된 기사면 재분석·중복 저장 없이 그대로 돌려준다.
    existing = _find_article_by_any_url(storage, url_source, raw_url)
    if existing:
        return _existing_result(existing["id"])

    # ── G3: 리다이렉트 해제 + HTML ──────────────────────────────────
    canonical, html = resolve_canonical(http, raw_url)
    if not canonical or not html:
        return {"ok": False, "error": "페이지를 가져오지 못했습니다. 링크를 확인해 주세요."}

    existing = _find_article_by_any_url(storage, canonical, "")
    if existing:
        return _existing_result(existing["id"])

    # ── G4: 제목·본문·메타 ─────────────────────────────────────────
    title = extract_title(html)
    body = extract_body(html)
    summary_source = "fulltext" if len(body) >= 300 else "snippet"
    if not title and not body:
        return {"ok": False, "error": "기사 제목·본문을 찾지 못했습니다. 뉴스 기사 URL 이 맞는지 확인해 주세요."}
    if not title:
        title = body[:40].strip() or "제목 없음"

    press_name, press_id, press_tier = resolve_press(storage, canonical, "", html, http)
    author = extract_author(html, body, press_name)
    published = extract_published(html) or now_utc()
    content_hash = sha256(body) if summary_source == "fulltext" else ""

    # ── G5: 중복 판정 ──────────────────────────────────────────────
    candidates = storage.recent_articles_for_dedup(published - timedelta(hours=DEDUP_WINDOW_HOURS + 1))
    dup = find_duplicate(storage, ctx.llm, title, published, content_hash, canonical, candidates)
    if dup:
        storage.append_alias(dup["id"], url_source)
        return _existing_result(dup["id"])

    groups = detect_group_companies(f"{title}\n{group_lead_text(body)}")
    # 카테고리는 제목 기준 임시값. 아래 analyze_and_save 에서 요약으로 다시 계산된다.
    categories = detect_categories(title)
    score_overrides, score_customs = get_score_rules(storage)
    score = score_article(title, body, groups, press_tier, score_overrides, score_customs)

    article_id = new_id()
    row = {
        "id": article_id, "url_source": url_source, "url_source_aliases": [],
        "url_canonical": canonical, "url_original": raw_url, "title": title,
        "press_id": press_id, "press_name": press_name, "author": author,
        "published_at": iso(published), "collected_at": iso(now_utc()),
        "source_type": "manual", "thumbnail_url": extract_thumbnail(html),
        "content_hash": content_hash, "dedup_group_id": article_id,
        "is_representative": True,
        "is_backfill": (now_utc() - published) > timedelta(hours=ctx.cfg.fresh_cutoff_hours),
        "importance_score": score, "sentiment": None, "keywords": [],
        "group_companies": groups, "categories": categories,
        "title_embedding": None, "analyzed_at": None,
        "status": "active" if activate else "draft",
    }
    if not storage.insert_article(row):
        existing = _find_article_by_any_url(storage, url_source, raw_url)
        if existing:
            return {"ok": True, "card": build_card(storage.article_detail(existing["id"])), "already": True}
        return {"ok": False, "error": "저장에 실패했습니다. 잠시 후 다시 시도해 주세요."}

    storage.save_body(article_id, body if summary_source == "fulltext" else (body or title), summary_source)

    # ── G6: LLM 분석 (수동 등록은 일일 상한과 무관하게 바로 분석) ───
    try:
        analyze_and_save(ctx, article_id, dict(row), body or title, summary_source)
    except Exception as exc:
        log.warning("수동 등록 분석 실패: %s", exc)

    detail = storage.article_detail(article_id)
    out = {"ok": True, "card": build_card(detail), "already": False}
    if not activate:
        out["draft_id"] = article_id      # 프런트가 '등록'/'취소' 를 호출할 때 쓴다
    else:
        # 바로 목록에 뜬 수동 등록(봇 DM 등) — 임계값을 넘으면 채널에도 발송한다.
        try:
            if queue_manual_notify(ctx, article_id):
                out["notified"] = True
        except Exception as exc:   # pragma: no cover
            log.warning("수동 등록 알림 처리 실패: %s", exc)
    return out




def _find_article_by_any_url(storage: Storage, url_a: str, url_b: str) -> dict | None:
    """url_source / url_canonical / alias 어느 쪽으로든 이미 있는 기사를 찾는다."""
    for u in (url_a, url_b):
        if not u:
            continue
        hit = storage.find_by_canonical(u)
        if hit:
            return hit
        seen = storage.seen_url_sources([u])
        if u in seen:
            # url_source 로는 있는데 canonical 조회로 안 나온 경우 — 목록에서 다시 찾는다
            for row in storage.list_articles(200, 0, None, ""):
                if row.get("url_source") == u or u in jload(row.get("url_source_aliases"), []):
                    return row
    return None




# =====================================================================
# 13. 시세 티커 (PRD F9)
#     무료 공개 소스를 쓰기로 확정했으므로(PLAN 0-3) 실패는 상시 발생한다.
#     따라서 "마지막 성공 값 유지"가 선택이 아니라 필수 동작이다.
# =====================================================================

STOCK_SYMBOLS = [("005490", "포스코홀딩스"), ("003670", "포스코퓨처엠")]


FX_SYMBOLS = [
    ("FX_USDKRW", "USDKRW", "(미국) 원/$"),
    ("FX_CNYKRW", "CNYKRW", "(중국) 원/元"),
    ("FX_JPYKRW", "JPYKRW", "(일본) 원/100¥"),
    ("FX_EURKRW", "EURKRW", "(유럽) 원/€"),
]


QUOTE_STALE_MINUTES = 15   # 이보다 낡으면 '지연' 배지를 붙인다




def _deep_find(node: Any, key: str) -> Any:
    """중첩된 JSON 에서 키를 찾는다. 외부 API 응답 구조가 바뀌어도 잘 견디게 한다."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _deep_find(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _deep_find(value, key)
            if found is not None:
                return found
    return None




def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None




def fetch_quotes(http: HttpClient) -> list[dict]:
    """조회에 성공한 항목만 돌려준다. 실패 항목은 아예 넣지 않는다(마지막 값 유지)."""
    out: list[dict] = []
    headers = {"Referer": "https://m.stock.naver.com/", "Accept": "application/json"}

    for code, label in STOCK_SYMBOLS:
        try:
            resp = http.get(f"https://m.stock.naver.com/api/stock/{code}/basic", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            price = _to_number(_deep_find(data, "closePrice"))
            rate = _to_number(_deep_find(data, "fluctuationsRatio"))
            if price is None:
                raise ValueError("closePrice 없음")
            out.append({"symbol": code, "kind": "stock", "label": label,
                        "price": price, "change_rate": rate, "fetched_at": iso(now_utc())})
        except Exception as exc:
            log.warning("주가 조회 실패 (%s): %s — 마지막 값을 유지합니다", label, exc)

    for reuters_code, symbol, label in FX_SYMBOLS:
        quote = _fetch_fx(http, headers, reuters_code, symbol, label)
        if quote:
            out.append(quote)
        else:
            log.warning("환율 조회 실패 (%s) — 마지막 값을 유지합니다", label)

    return out




def _fetch_fx(http: HttpClient, headers: dict, reuters_code: str, symbol: str, label: str) -> dict | None:
    """환율 1건. 무료 소스는 언제든 형태가 바뀌므로 소스를 2개 두고 순서대로 시도한다."""
    endpoints = [
        ("https://m.stock.naver.com/front-api/marketIndex/prices",
         {"category": "exchange", "reutersCode": reuters_code, "page": 1}),
        (f"https://api.stock.naver.com/marketindex/exchange/{reuters_code}", None),
    ]
    for url, params in endpoints:
        try:
            resp = http.get(url, params=params, headers=headers) if params else http.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            price = _to_number(_deep_find(data, "closePrice"))
            rate = _to_number(_deep_find(data, "fluctuationsRatio"))
            if price is None:
                continue
            return {"symbol": symbol, "kind": "fx", "label": label,
                    "price": price, "change_rate": rate, "fetched_at": iso(now_utc())}
        except Exception as exc:
            log.debug("환율 소스 실패 (%s / %s): %s", label, url, exc)
    return None




def refresh_quotes(ctx: Context) -> int:
    rows = fetch_quotes(ctx.http)
    for row in rows:
        ctx.storage.upsert_quote(row)
    return len(rows)



# 이 프로세스를 run_state.pipeline_lock_owner 에 식별하는 값. 정상 운영은 항상
# 인스턴스 1개지만, AWS 등에서 desiredCount 를 잘못 설정하거나 배포 중 잠깐
# 신·구 인스턴스가 겹치면 수집·알림이 중복된다 — run_once() 가 매 회차 이
# 값으로 실행권(락)을 얻으려 시도해 그런 사고를 막는다.
_INSTANCE_ID = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"


PIPELINE_LOCK_STALE_MULT = 3   # 락 유효시간 = poll_interval_sec 의 이 배수 (죽은 소유자 회수용)
