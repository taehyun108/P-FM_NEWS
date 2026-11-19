// P-FM NEWS 앞문(Cloudflare Worker)
//
// 회사 PC → 이 Worker(xxx.workers.dev) → 실제 서버(AWS) 로 요청을 그대로 전달만 한다.
// 회사에서는 duckdns.org 주소가 막혀 있어서, 열리는 workers.dev 주소를 앞문으로 쓴다.
//
// 이 파일은 Cloudflare 화면에 "복사·붙여넣기"로 올리는 부품이다. 수정이 필요하면
// 아래 ORIGIN 한 줄만 바꾸면 된다(서버를 옮기는 날에도 이 줄만 바꾼다).

// ★ 전달할 실제 서버 주소(맨 끝에 / 를 붙이지 말 것)
const ORIGIN = "https://pfm-news.duckdns.org";

export default {
  async fetch(request, env) {
    // env.ORIGIN 이 있으면 우선(점검용). 없으면 위의 ORIGIN 을 쓴다.
    const origin = ((env && env.ORIGIN) || ORIGIN).replace(/\/+$/, "");
    const url = new URL(request.url);
    const target = origin + url.pathname + url.search;

    // 1) 요청 헤더 정리 — 서버가 "https 로 들어온 실제 접속자"로 인식하게 한다.
    const headers = new Headers(request.headers);
    // 로그인 시도 제한이 접속자별로 걸리도록 실제 접속자 IP 를 맨 앞에 둔다.
    const clientIp = request.headers.get("cf-connecting-ip")
      || request.headers.get("x-real-ip")
      || "";
    if (clientIp) headers.set("x-forwarded-for", clientIp);
    headers.set("x-forwarded-proto", "https");
    headers.set("x-forwarded-host", url.host);

    // 2) 서버로 전달 — 캐시 금지(새 기사가 늦게 보이는 일을 막는다), 리다이렉트는 그대로 돌려줌
    const method = request.method.toUpperCase();
    const init = {
      method,
      headers,
      redirect: "manual",
      cf: { cacheTtl: 0, cacheEverything: false },
    };
    // 본문(로그인·URL 분석 등 작은 JSON)은 통째로 읽어서 보낸다 — 실행 환경과 무관하게 안전.
    if (method !== "GET" && method !== "HEAD") init.body = await request.arrayBuffer();

    let upstream;
    try {
      upstream = await fetch(target, init);
    } catch (e) {
      // 서버가 꺼져 있거나 연결이 안 될 때 사용자에게 보여줄 안내
      return new Response("서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.", {
        status: 502,
        headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
      });
    }

    // 3) 응답 정리
    const out = new Headers(upstream.headers);
    // 서버가 돌려준 주소(Location)가 원래 서버 주소면 이 앞문 주소로 바꿔 준다.
    const loc = out.get("location");
    if (loc && loc.startsWith(origin)) {
      out.set("location", url.origin + loc.slice(origin.length));
    }
    // API 응답은 중간에 저장되지 않게 한다(실시간 갱신 보장).
    if (url.pathname.startsWith("/api")) {
      out.set("cache-control", "no-store");
    }
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: out,
    });
  },
};
