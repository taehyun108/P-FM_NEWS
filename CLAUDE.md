[나와 일하는 방식]
- 항상 한국어로 답할 것
- 확실하지 않은 것은 추측하지 말고 나에게 질문할 것
- 여러 파일을 건드리거나 되돌리기 어려운 작업은 코드를 만들기 전에 먼저 계획을 보여줄 것

[개발 규칙]
- 백엔드는 backend/app/ 패키지로 나눠져 있다. 새 코드는 성격이 맞는 모듈에 넣을 것
  (core 설정·유틸 / storage DB / collect 수집 / analyze 분석 / notify 알림 /
   view 표시가공 / auth 로그인 / web 라우트 / cli 운영커맨드)
- 주석은 한글로 알아보기 쉽게 쓸 것
- 새 라이브러리는 쓰기 전에 먼저 물어볼 것
- Supabase 테이블에 새 컬럼(alter table)이 필요한 변경을 할 때는, 실행할
  SQL과 함께 Supabase SQL Editor 주소(https://supabase.com/dashboard/project/
  <프로젝트 ref>/sql/new, .env 의 SUPABASE_URL에서 프로젝트 ref 추출)를
  반드시 같이 알려줄 것 — REST API로는 DDL을 실행할 수 없어 사용자가 직접
  SQL Editor에서 1회 실행해야 하기 때문.

[보안]
- API 키는 반드시 .env 에서 읽어올 것
- 코드 안에 키를 직접 쓰지 말 것
- .env 의 키 값은 내가 직접 채울 것이므로 비워둘 것

[참고]
- 자세한 기획 내용은 PRD.md 를 참고할 것
- 백엔드 검증은 `python backend/main.py selftest` (DB·API 키 불필요)
