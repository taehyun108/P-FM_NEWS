#!/usr/bin/env node
// ─────────────────────────────────────────────────────────────────────────
// demo-video 스킬의 범용 러너.
// YAML 시나리오 한 장을 읽어서 Playwright 를 조작해 화면 시연 영상을 만든다.
// 새 데모를 찍을 때마다 이 파일을 고칠 필요는 없다 — scripts/*.yaml 만 새로 쓰면 된다.
//
// 사용법:
//   node scripts/run_scenario.js --scenario scripts/fixture/example-scenario.yaml
//   node scripts/run_scenario.js --scenario my-scenario.yaml --headed --out ./out
// ─────────────────────────────────────────────────────────────────────────
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const { chromium } = require('playwright');
const YAML = require('yaml');
const ffmpegPath = require('ffmpeg-static');

const SKILL_ROOT = path.resolve(__dirname, '..');

function parseArgs(argv) {
  const opts = { headed: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--scenario') opts.scenario = argv[++i];
    else if (a === '--out') opts.out = argv[++i];
    else if (a === '--headed') opts.headed = true;
    else if (a === '--help' || a === '-h') opts.help = true;
    else throw new Error(`알 수 없는 옵션: ${a}`);
  }
  return opts;
}

function printHelp() {
  console.log(`사용법: node scripts/run_scenario.js --scenario <시나리오.yaml> [--out <디렉터리>] [--headed]

  --scenario  실행할 YAML 시나리오 파일 경로 (필수)
  --out       결과 mp4·실패 스크린샷을 저장할 디렉터리 (기본: <스킬 폴더>/out)
  --headed    브라우저 창을 띄운 채로 실행 (기본은 headless)
`);
}

function describeStep(step) {
  const key = Object.keys(step).find((k) => k !== 'caption' && k !== 'duration' && k !== 'hold') || Object.keys(step)[0];
  const val = step[key];
  return `${key}: ${typeof val === 'object' ? JSON.stringify(val) : val}`;
}

function resolveUrl(target, baseUrl, scenarioDir) {
  // http(s):// 등 이미 완전한 URL 이면 그대로 쓴다.
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(target)) return target;
  // baseUrl 이 있으면 그 기준 상대 경로로 해석한다 (실제 서버 대상 시연용).
  if (baseUrl) return new URL(target, baseUrl).toString();
  // baseUrl 이 없으면 시나리오 파일 기준 로컬 파일 경로로 본다
  // (별도 서버 없이 정적 HTML 픽스처만으로 시나리오를 테스트할 때 쓴다).
  if (scenarioDir) return 'file://' + path.resolve(scenarioDir, target);
  throw new Error(`상대 경로 "${target}" 를 해석할 baseUrl 이 시나리오에 없습니다.`);
}

async function waitDemoReady(page) {
  await page.waitForFunction(() => window.__demo && window.__demo.ready === true, null, { timeout: 10000 });
}

async function boundingBoxOf(page, selector) {
  const locator = page.locator(selector).first();
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error(`요소를 찾을 수 없습니다: ${selector}`);
  return box;
}

// 커서를 요소 중심으로 곡선 이동시킨다. page.evaluate 는 안의 함수가 반환하는
// Promise 가 끝날 때까지 기다려 주므로, Node 쪽에서 별도로 sleep 할 필요가 없다.
async function moveCursorToSelector(page, selector) {
  const box = await boundingBoxOf(page, selector);
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  const duration = 450 + Math.random() * 350; // 450~800ms — 사람 손 느낌 나는 이동 속도
  await page.evaluate(({ x, y, duration }) => window.__demo.moveCursorTo(x, y, duration), { x, y, duration });
  return { x, y };
}

async function clickOn(page, selector) {
  const { x, y } = await moveCursorToSelector(page, selector);
  await page.evaluate(({ x, y }) => window.__demo.clickRipple(x, y), { x, y });
  await page.waitForTimeout(140); // 리플이 눈에 보일 시간을 준다
  await page.locator(selector).first().click();
}

// 타이핑 사이사이에 무작위 지연을 줘서 사람이 치는 것처럼 보이게 한다.
async function typeHuman(page, selector, text) {
  const locator = page.locator(selector).first();
  await locator.click();
  const chars = String(text ?? '').split('');
  for (const ch of chars) {
    if (typeof locator.pressSequentially === 'function') {
      await locator.pressSequentially(ch, { delay: 0 });
    } else {
      // 구버전 Playwright 호환: pressSequentially 가 없으면 type() 으로 대체
      await locator.type(ch, { delay: 0 });
    }
    await page.waitForTimeout(40 + Math.random() * 120); // 40~160ms
  }
}

async function runStep(pageCtx, step, index) {
  const { page, baseUrl, scenarioDir } = pageCtx;

  // caption 은 다른 동작과 함께 붙여 쓸 수 있는 보조 필드다.
  // (예: click 스텝에 caption 을 같이 써서 "이 버튼을 클릭합니다" 처럼 설명을 붙인다)
  if (typeof step.caption === 'string') {
    await page.evaluate((text) => window.__demo.setCaption(text), step.caption);
  }

  if (step.chapter !== undefined) {
    await page.evaluate(
      ({ title, sub }) => window.__demo.showChapter(title, sub),
      { title: step.chapter, sub: step.subtitle || '' }
    );
    await page.waitForTimeout(step.duration ?? 2200);
    await page.evaluate(() => window.__demo.hideChapter());
    await page.waitForTimeout(450); // 페이드아웃 트랜지션 대기
    return;
  }

  if (step.goto !== undefined) {
    const url = resolveUrl(step.goto, baseUrl, scenarioDir);
    await page.goto(url, { waitUntil: 'load' });
    await waitDemoReady(page);
    return;
  }

  if (step.wait !== undefined) {
    await page.waitForTimeout(step.wait);
    return;
  }

  if (step.highlight !== undefined) {
    const box = await boundingBoxOf(page, step.highlight);
    await page.evaluate((r) => window.__demo.showSpotlight(r), box);
    await page.waitForTimeout(step.duration ?? 1800);
    if (!step.hold) {
      await page.evaluate(() => window.__demo.hideSpotlight());
      await page.waitForTimeout(300);
    }
    return;
  }

  if (step.hideHighlight) {
    await page.evaluate(() => window.__demo.hideSpotlight());
    return;
  }

  if (step.hover !== undefined) {
    await moveCursorToSelector(page, step.hover);
    await page.locator(step.hover).first().hover();
    return;
  }

  if (step.scroll !== undefined) {
    await page.locator(step.scroll).first().scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    return;
  }

  if (step.click !== undefined) {
    await clickOn(page, step.click);
    return;
  }

  if (step.type !== undefined) {
    const { selector, text } = step.type;
    if (!selector) throw new Error('type 스텝에는 selector 가 필요합니다.');
    await moveCursorToSelector(page, selector);
    await typeHuman(page, selector, text);
    return;
  }

  if (step.clearCaption) {
    await page.evaluate(() => window.__demo.clearCaption());
    return;
  }

  console.warn(`  [${index}] 알 수 없는 스텝, 건너뜁니다: ${JSON.stringify(step)}`);
}

function convertToMp4(inputPath, outputPath) {
  return new Promise((resolve, reject) => {
    // Playwright 번들 ffmpeg 는 VP8/webm 전용이라 mp4(H.264)를 못 만든다.
    // 그래서 ffmpeg-static 이 받아온 정적 ffmpeg 바이너리를 직접 호출한다.
    const args = [
      '-y',
      '-i', inputPath,
      '-c:v', 'libx264',
      '-pix_fmt', 'yuv420p',   // 구형 플레이어·SNS 업로드 호환용 픽셀 포맷
      '-movflags', '+faststart', // moov atom 을 앞으로 옮겨 스트리밍 재생 가능하게
      outputPath,
    ];
    const proc = spawn(ffmpegPath, args, { stdio: ['ignore', 'ignore', 'pipe'] });
    let stderr = '';
    proc.stderr.on('data', (d) => { stderr += d.toString(); });
    proc.on('error', reject);
    proc.on('close', (code) => {
      if (code === 0) resolve();
      else reject(new Error(`ffmpeg 변환 실패 (code ${code})\n${stderr.slice(-2000)}`));
    });
  });
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help || !opts.scenario) {
    printHelp();
    process.exit(opts.help ? 0 : 1);
  }

  const scenarioPath = path.resolve(opts.scenario);
  const scenarioDir = path.dirname(scenarioPath);
  const scenario = YAML.parse(fs.readFileSync(scenarioPath, 'utf8'));
  const outDir = path.resolve(opts.out || path.join(SKILL_ROOT, 'out'));
  fs.mkdirSync(outDir, { recursive: true });

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'demo-video-'));
  const injectSrc = fs.readFileSync(path.join(__dirname, 'inject.js'), 'utf8');

  const width = (scenario.viewport && scenario.viewport.width) || 1920;
  const height = (scenario.viewport && scenario.viewport.height) || 1080;
  const baseUrl = scenario.baseUrl || '';

  console.log(`▶ 시나리오: ${scenario.title || path.basename(scenarioPath)}`);
  console.log(`▶ 해상도: ${width}x${height}  |  headless: ${!opts.headed}`);

  // DEMO_VIDEO_CHROMIUM_PATH 를 지정하면 Playwright 가 자체 다운로드한 브라우저 대신
  // 그 경로의 크로미움을 쓴다. 사내망처럼 playwright install 이 막힌 환경에서
  // 이미 설치돼 있는 크로미움(예: 시스템 패키지, Docker 베이스 이미지)을 재사용할 때 쓴다.
  const launchOpts = { headless: !opts.headed };
  if (process.env.DEMO_VIDEO_CHROMIUM_PATH) {
    launchOpts.executablePath = process.env.DEMO_VIDEO_CHROMIUM_PATH;
  }
  const browser = await chromium.launch(launchOpts);
  const context = await browser.newContext({
    viewport: { width, height },
    recordVideo: { dir: tmpDir, size: { width, height } },
  });
  // addInitScript 는 컨텍스트의 모든 페이지·모든 새 문서(내비게이션 포함)마다
  // 페이지 자신의 스크립트보다 먼저 실행된다 — 그래서 goto 로 페이지를 옮겨도
  // 커서·자막 오버레이가 계속 다시 만들어진다.
  await context.addInitScript({ content: injectSrc });
  const page = await context.newPage();
  page.setDefaultTimeout(scenario.timeout ?? 15000);

  const pageCtx = { page, baseUrl, scenarioDir };
  let failed = false;

  try {
    const startUrl = scenario.startUrl || baseUrl;
    if (startUrl) {
      await page.goto(resolveUrl(startUrl, baseUrl, scenarioDir), { waitUntil: 'load' });
      await waitDemoReady(page);
    }
    const steps = scenario.steps || [];
    for (let i = 0; i < steps.length; i++) {
      console.log(`  [${i + 1}/${steps.length}] ${describeStep(steps[i])}`);
      await runStep(pageCtx, steps[i], i + 1);
    }
  } catch (err) {
    failed = true;
    console.error(`✖ 시나리오 실행 실패: ${err.message}`);
    try {
      const shotPath = path.join(outDir, `failure-${Date.now()}.png`);
      await page.screenshot({ path: shotPath });
      console.error(`  실패 시점 스크린샷 저장: ${shotPath}`);
    } catch (shotErr) {
      console.error(`  스크린샷 저장도 실패했습니다: ${shotErr.message}`);
    }
  }

  // 영상 파일은 context.close() 로 컨텍스트를 닫아야 완전히 마무리(flush)된다.
  const video = page.video();
  await context.close();
  await browser.close();

  if (!video) {
    console.error('✖ 녹화된 영상을 찾을 수 없습니다 (recordVideo 설정을 확인하세요).');
    process.exit(1);
  }

  const webmPath = await video.path();
  const outputName = String(scenario.output || 'demo').replace(/\.(mp4|webm)$/i, '');
  const mp4Path = path.join(outDir, `${outputName}.mp4`);

  console.log(`▶ mp4 로 변환 중 (H.264 / yuv420p / +faststart): ${mp4Path}`);
  await convertToMp4(webmPath, mp4Path);
  fs.rmSync(tmpDir, { recursive: true, force: true });

  if (failed) {
    console.error(`⚠ 실패했지만 그 시점까지의 영상은 저장했습니다: ${mp4Path}`);
    process.exitCode = 1;
    return;
  }
  console.log(`✅ 완료: ${mp4Path}`);
}

main().catch((err) => {
  console.error('예상치 못한 오류:', err);
  process.exit(1);
});
