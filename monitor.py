# -*- coding: utf-8 -*-
"""
수원유스호스텔 캠핑장 빈자리 알리미

하는 일
0) 한 번 켜지면 정해진 시간(기본 20분) 동안 일정 간격(기본 90초)으로 아래를 반복한다.
1) 달력 주소의 맨 끝 date_yyyymm 을 바꿔가며, 이번 달 + 다음 달 달력을 브라우저로 연다.
   (지정한 날짜가 다른 달에 있으면 그 달도 함께 본다)
2) 7번째 칸(토요일) + 내가 지정한 날짜(TARGET_DATES) 칸을 읽는다.
3) 날짜 숫자가 '누를 수 있는 상태'로 살아나면 = 예약 가능 → 텔레그램으로 알려준다.
4) 사이트가 접근을 막으면(403/429 등) 조회를 멈추고, 멈췄다고 알려준다.
"""
import calendar
import datetime as dt
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ==================== 설정 (필요하면 여기만 고치세요) ====================
# 달력 주소. 맨 끝 {yyyymm} 자리에 202610, 202611 … 이 들어간다.
BASE_URL = ("https://yeyak.syf.or.kr/www/88?company_code=SYF09&part_code=02"
            "&place_code=2&days=1&date_yyyymm={yyyymm}")
MONTHS_TO_CHECK = 2            # 2 = 이번 달 + 다음 달
MAX_NEXT_CLICKS = 3            # 달이 안 맞을 때 '다음월'을 최대 몇 번 눌러볼지
DEFAULT_INTERVAL = 90          # 몇 초마다 다시 볼지 (GitHub 변수 INTERVAL_SECONDS 로 바꿀 수 있음)
DEFAULT_LOOP_MINUTES = 20      # 한 번 켜진 실행이 몇 분 동안 반복할지
FULL_WORDS = ["예약완료", "마감", "불가", "만실", "종료", "휴관", "휴무", "대기", "신청불가"]
OPEN_WORDS = ["잔여", "가능", "신청", "예약하기", "여유"]
BLOCK_STATUS = {403, 429}      # 403 = 접근거부, 429 = 요청이 너무 많음
BLOCK_TEXTS = ["access denied", "too many requests", "접근이 거부", "접근 거부",
               "접근이 차단", "요청이 너무 많", "비정상적인 접근", "captcha"]
MAX_FAILS = 3                  # 알 수 없는 오류가 연속 3번이면 멈춤
HEARTBEAT_DAYS = 30            # 30일마다 "아직 잘 돌고 있어요" 알림
STATE_FILE = "state.json"
DEBUG_DIR = "debug"
KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = "월화수목금토일"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
# ======================================================================

# 달력(일~토 7칸) 표를 찾아서 모든 칸의 상태를 읽어오는 코드
JS_CALENDAR = """
() => {
  const bad = /disable|disabled|off|soldout|sold_out|end|close|impossible|not_?possible|unable/i;
  const findTable = () => {
    for (const t of document.querySelectorAll('table')) {
      const rows = Array.from(t.rows);
      if (rows.length < 2) continue;
      const head = Array.from(rows[0].cells).map(c => (c.innerText || '').trim());
      if (head.length === 7 && head[0].startsWith('일') && head[6].startsWith('토')) return t;
    }
    return null;
  };
  const t = findTable();
  if (!t) return { ok: false, reason: 'no_table' };
  const rows = Array.from(t.rows).slice(1).map(r => ({
    n: r.cells.length,
    cells: Array.from(r.cells).map(c => {
      const hit = c.querySelector('a,button,input[type=button],input[type=submit],[onclick]');
      const blocked = bad.test(c.className || '') ||
                      (hit && (bad.test(hit.className || '') || hit.hasAttribute('disabled')));
      return {
        text: c.innerText || '',
        clickable: !!hit && !blocked,
        cls: (c.className || '') + ' ' + ((hit && hit.className) || ''),
        html: (c.innerHTML || '').replace(/\\s+/g, ' ').trim().slice(0, 160)
      };
    })
  }));
  // 달력 표 가까운 곳부터, '2026.10' / '2026년 10월' 처럼 그 자체로 달을 뜻하는 글자만 모은다.
  // (오늘 날짜 '2026.10.21' 같은 건 날짜까지 있으니 거르고, '이전월/다음월' 꼬리표도 순서로 밀린다)
  const ONLY_MONTH = /^(20\\d\\d)\\s*[.\\-\\/년]\\s*(\\d{1,2})\\s*월?$/;
  const labels = [];
  const scan = root => {
    for (const el of root.querySelectorAll('*')) {
      if (el.children.length) continue;                 // 글자만 든 칸만 본다
      const t = (el.innerText || '').trim();
      const m2 = t.match(ONLY_MONTH);
      if (m2) labels.push([+m2[1], +m2[2]]);
    }
  };
  let scope = t.parentElement;                          // 달력 표 주변부터 넓혀가며 찾는다
  for (let i = 0; i < 4 && scope; i++, scope = scope.parentElement) {
    scan(scope);
    if (labels.length) break;
  }
  if (!labels.length) scan(document);
  return {
    ok: true,
    year: labels.length ? labels[0][0] : null,
    month: labels.length ? labels[0][1] : null,
    labels: labels.slice(0, 6),
    rows: rows
  };
}
"""


# 달력이 '해당 월'로 다 그려졌는지 확인하는 코드
JS_READY = """
(label) => {
  const body = document.body ? document.body.innerText : '';
  if (!body.includes(label)) return false;
  for (const t of document.querySelectorAll('table')) {
    const rows = Array.from(t.rows);
    if (rows.length < 2) continue;
    const head = Array.from(rows[0].cells).map(c => (c.innerText || '').trim());
    if (head.length === 7 && head[0].startsWith('일') && head[6].startsWith('토'))
      return rows.slice(1).some(r => r.cells.length === 7);
  }
  return false;
}
"""


# '다음월' 버튼을 찾아 누르는 코드
JS_NEXT_MONTH = """
() => {
  const norm = s => (s || '').replace(/\\s+/g, '');
  const byId = document.querySelector('#next_month, .next_month, [id*=next], [class*=next]');
  if (byId) { byId.click(); return 'id:' + (byId.id || byId.className); }
  for (const el of document.querySelectorAll('a,button,span,div,li')) {
    const t = norm(el.innerText || el.title || el.getAttribute('alt'));
    if (t === '다음월' || t === '다음달' || t === '다음') { el.click(); return 'text:' + t; }
  }
  return null;
}
"""


class Blocked(Exception):
    """사이트가 접근을 막았을 때"""



# ---------------------------- 작은 도우미들 ----------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    """보냈으면 (True, ''), 못 보냈으면 (False, 이유)"""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    print("----- 텔레그램 메시지 -----\n" + text + "\n--------------------------")
    if not token or not chat_id:
        missing = " / ".join(n for n, v in [("TELEGRAM_TOKEN", token),
                                            ("TELEGRAM_CHAT_ID", chat_id)] if not v)
        reason = f"{missing} 이(가) 비어 있어요 (Secrets 이름을 확인하세요)"
        print("[경고]", reason)
        return False, reason
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text[:3900],
                  "disable_web_page_preview": True},
            timeout=20,
        )
        if r.ok:
            return True, ""
        hint = {401: "토큰이 틀렸어요 (TELEGRAM_TOKEN 확인)",
                400: "chat_id 가 틀렸거나 봇에게 먼저 말을 걸지 않았어요",
                403: "봇이 차단되었어요 (텔레그램에서 봇 대화방을 열고 시작을 누르세요)"}
        reason = f"텔레그램 거절 {r.status_code} — {hint.get(r.status_code, '')} {r.text[:200]}"
        print("[경고]", reason)
        return False, reason
    except requests.RequestException as e:
        reason = f"텔레그램에 연결하지 못했어요: {e}"
        print("[경고]", reason)
        return False, reason


def telegram_ping():
    """메시지를 보내지 않고 토큰/대화방만 확인한다. (조용한 점검)"""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        missing = " / ".join(n for n, v in [("TELEGRAM_TOKEN", token),
                                            ("TELEGRAM_CHAT_ID", chat_id)] if not v)
        return False, f"{missing} 이(가) 비어 있어요 (Secrets 이름을 확인하세요)"
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20)
        if r.ok:
            return True, ""
        if r.status_code == 401:
            return False, "토큰이 틀렸어요 (TELEGRAM_TOKEN 확인)"
        return False, f"텔레그램 거절 {r.status_code} {r.text[:200]}"
    except requests.RequestException as e:
        return False, f"텔레그램에 연결하지 못했어요: {e}"


def disable_workflow():
    """GitHub 자동 실행(5분마다)을 스스로 끈다."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    wf = os.environ.get("WORKFLOW_FILE", "monitor.yml")
    if not token or not repo:
        return False
    try:
        r = requests.put(
            f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/disable",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=20,
        )
        print("워크플로 끄기 응답:", r.status_code)
        return r.status_code == 204
    except requests.RequestException as e:
        print("워크플로 끄기 오류:", e)
        return False


def stop_monitoring(state, reason):
    state["stopped"] = True
    state["stopped_reason"] = reason
    turned_off = disable_workflow()
    send_telegram(
        "⛔ 캠핑장 빈자리 조회를 멈췄어요\n\n"
        f"이유: {reason}\n\n"
        "사이트가 자동 조회를 막았거나, 사이트 모양이 또 바뀌었을 수 있어요.\n"
        "코드를 고쳐야 할 수 있어요.\n\n"
        + ("GitHub 자동 실행도 꺼두었어요.\n다시 켜려면 Actions에서 Enable workflow를 누르고 "
           "Run workflow를 한 번 눌러주세요."
           if turned_off else
           "※ GitHub 자동 실행 끄기는 실패했지만, 다음 실행부터는 조회하지 않고 바로 끝나요.")
    )


def read_target_dates(today):
    """GitHub 변수 TARGET_DATES (예: 2026-11-07,2026-12-05) → (앞으로 남은 날짜들, 잘못 적은 글자들)"""
    raw = os.environ.get("TARGET_DATES", "")
    good, bad = set(), []
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        try:
            d = dt.date.fromisoformat(part)
        except ValueError:
            bad.append(part)
            continue
        if d >= today:
            good.add(d)
    return good, bad


def months_to_check(today, targets):
    y, m = today.year, today.month
    out = set()
    for _ in range(MONTHS_TO_CHECK):
        out.add((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    for d in targets:
        out.add((d.year, d.month))
    return sorted(out)


def month_url(y, m):
    return BASE_URL.format(yyyymm=f"{y}{m:02d}")


def date_name(d):
    return f"{d.year}년 {d.month}월 {d.day}일({WEEKDAYS[d.weekday()]})"


def classify(cell):
    """한 칸의 상태: full(예약완료) / available(누를 수 있음) / closed(숫자만, 못 누름) / empty"""
    text = " ".join((cell.get("text") or "").split())
    if not text:
        return "empty", text
    if any(w in text for w in FULL_WORDS):
        return "full", text
    if not re.search(r"\d", text):
        return "empty", text
    if cell.get("clickable") or any(w in text for w in OPEN_WORDS):
        return "available", text
    return "closed", text


def layout_matches(rows, y, m):
    """달력에 찍힌 날짜 숫자 배치가 그 해 그 달과 맞는지 확인한다 (글자 표시보다 믿을 만함)."""
    weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(y, m)
    week_rows = [r for r in rows if r.get("n") == 7]
    if len(week_rows) < len(weeks):
        return False
    checked = 0
    for i, w in enumerate(weeks):
        for col, day in enumerate(w):
            if day == 0:
                continue
            got = re.search(r"\d+", week_rows[i]["cells"][col].get("text") or "")
            if not got or int(got.group()) != day:
                return False
            checked += 1
    return checked >= 20          # 한 달치 숫자가 충분히 맞아떨어져야 통과


def interpret(cal, y, m, today, targets):
    """달력 칸 → [{date, status, text, target, cls, html}] (토요일 전부 + 지정 날짜)"""
    weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(y, m)
    week_rows = [r for r in cal["rows"] if r.get("n") == 7]
    result = []
    for i, r in enumerate(week_rows):
        if i >= len(weeks):
            break
        for col in range(7):
            day = weeks[i][col]
            if day == 0:
                continue                       # 앞/뒤 달 날짜 칸
            date = dt.date(y, m, day)
            if date < today:
                continue                       # 이미 지난 날
            is_target = date in targets
            if col != 6 and not is_target:
                continue                       # 토요일도, 지정 날짜도 아님
            cell = r["cells"][col]
            status, text = classify(cell)
            result.append({"date": date, "status": status, "text": text,
                           "target": is_target, "clickable": cell.get("clickable"),
                           "cls": (cell.get("cls") or "").strip(),
                           "html": cell.get("html") or ""})
    return result


def save_debug(page, name):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        page.screenshot(path=f"{DEBUG_DIR}/{name}.png", full_page=True)
        with open(f"{DEBUG_DIR}/{name}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception as e:
        print("디버그 저장 실패:", e)


# ---------------------------- 달력 한 달 확인 ----------------------------
def check_month(page, blocked_hits, y, m, today, targets, keep_screenshot=False):
    """달력 주소를 열어 그 달의 토요일 + 지정 날짜 칸을 읽는다."""
    url = month_url(y, m)
    label = f"{y}.{m:02d}"
    print(f"[확인] {label} → {url}")

    resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if resp is not None and resp.status in BLOCK_STATUS:
        raise Blocked(f"HTTP {resp.status} 응답 ({label} 달력)")

    try:
        page.wait_for_function(JS_READY, arg=label, timeout=30000)
    except PWTimeout:
        print(f"[주의] {label} 달력이 30초 안에 다 뜨지 않았어요.")

    if blocked_hits:
        raise Blocked(f"HTTP {blocked_hits[0][0]} 응답 (달력 데이터 요청)")

    body = page.inner_text("body")
    if any(w in body.lower() for w in BLOCK_TEXTS):
        raise Blocked("페이지에 '접근 차단/요청 제한' 문구가 보여요")

    cal = page.evaluate(JS_CALENDAR)
    if not cal.get("ok") or not any(r.get("n") == 7 for r in cal.get("rows", [])):
        save_debug(page, f"no_calendar_{y}{m:02d}")
        raise RuntimeError(f"{label} 달력 표를 찾지 못했어요 (사이트 모양이 바뀌었을 수 있음)")
    # 화면에 적힌 달 표시는 틀릴 수 있어서(오늘 날짜 '2026.10.21', 이전월 꼬리표 등),
    # 달력에 찍힌 날짜 숫자 배치로 진짜 몇 월인지 확인한다.
    if not layout_matches(cal.get("rows", []), y, m):
        # 주소만으로 달이 안 바뀌는 사이트일 수 있으니 '다음월'을 눌러 맞춰본다
        for tries in range(1, MAX_NEXT_CLICKS + 1):
            moved = page.evaluate(JS_NEXT_MONTH)
            if not moved:
                break
            print(f"[이동] '다음월' 누름 ({tries}번째, {moved})")
            page.wait_for_timeout(2500)
            cal = page.evaluate(JS_CALENDAR)
            if cal.get("ok") and layout_matches(cal.get("rows", []), y, m):
                print(f"[이동] {label} 달력에 도착했어요.")
                break
        else:
            moved = None
        if not layout_matches(cal.get("rows", []), y, m):
            save_debug(page, f"wrong_month_{y}{m:02d}")
            seen = cal.get("labels") or []
            raise RuntimeError(
                f"{label} 달력을 열지 못했어요 "
                f"(화면에서 읽은 달 표시: {seen or '없음'}"
                f"{', 다음월 버튼도 못 찾음' if not moved else ''})")
    elif (cal.get("year"), cal.get("month")) != (y, m):
        print(f"[참고] 화면의 달 표시는 {cal.get('year')}.{cal.get('month')} 이지만, "
              f"날짜 배치는 {label} 이 맞아서 그대로 진행해요.")

    if keep_screenshot:
        save_debug(page, f"calendar_{y}{m:02d}")
    return interpret(cal, y, m, today, targets)


# ---------------------------- 한 바퀴 확인 ----------------------------
def one_cycle(page, state, today, targets, bad_dates, test_mode):
    """달력을 한 번 훑고 필요하면 알림을 보낸다. 사이트가 막으면 Blocked 를 던진다."""
    # 한 실행이 자정을 넘길 수 있으니, 매번 '오늘'을 기준으로 지난 지정 날짜를 걷어낸다
    targets = {d for d in targets if d >= today}

    blocked_hits = []
    handler = (lambda r: blocked_hits.append((r.status, r.url))
               if "syf.or.kr" in r.url and r.status in BLOCK_STATUS else None)
    page.on("response", handler)
    try:
        results = {}
        for y, m in months_to_check(today, targets):
            results[(y, m)] = check_month(page, blocked_hits, y, m, today,
                                          targets, keep_screenshot=test_mode)
            print(f"[읽음] {y}.{m:02d} — "
                  + ", ".join(f"{c['date'].day}:{c['status']}" for c in results[(y, m)]))
            page.wait_for_timeout(1500)   # 사이트에 부담 주지 않게 잠깐 쉬기
    finally:
        page.remove_listener("response", handler)

    state["fail_count"] = 0

    if test_mode:
        names = {"available": "✅ 예약가능(누를 수 있음)", "full": "❌ 예약완료",
                 "closed": "🔒 아직 안 열림(숫자만, 못 누름)", "empty": "⬜ 빈칸"}
        lines = ["🧪 테스트 결과 (토요일 + 📌지정 날짜)"]
        found = set()
        for (y, m), cells in results.items():
            lines.append(f"\n[{y}년 {m}월] {month_url(y, m)}")
            if not cells:
                lines.append("  (남은 날짜 없음)")
            for c in cells:
                found.add(c["date"])
                d = c["date"]
                lines.append(f"  {'📌' if c['target'] else ''}{d.day}일({WEEKDAYS[d.weekday()]}): "
                             f"{names[c['status']]}\n     글자='{c['text']}' / 누르기={c['clickable']}"
                             f" / class='{c['cls'][:40]}'")
        lines.append("\n📌 지정 날짜: " + (", ".join(date_name(d) for d in sorted(targets)) or "없음"))
        missing = sorted(targets - found)
        if missing:
            lines.append("⚠️ 달력에서 못 찾은 지정 날짜: " + ", ".join(date_name(d) for d in missing))
        if bad_dates:
            lines.append("⚠️ 날짜 모양이 잘못된 것(무시함): " + ", ".join(bad_dates)
                         + "  → 2026-11-07 처럼 적어주세요")
        send_telegram("\n".join(lines))
        return

    available_now, details, status_now = [], {}, {}
    for cells in results.values():
        for c in cells:
            key = c["date"].isoformat()
            status_now[key] = c["status"]
            details[key] = c
            if c["status"] == "available":
                available_now.append(key)

    before = state.get("last_status", {})

    # (가) 누를 수 있게 살아난 날
    new_keys = [k for k in available_now if k not in state["notified"]]

    # (나) 안전장치: '예약완료'였다가 그 글자가 사라진 날 = 취소가 난 것일 수 있다.
    #     사이트가 빈자리를 링크로 안 만들어 주는 경우에도 이걸로 잡아낸다.
    freed = [k for k, st in status_now.items()
             if before.get(k) == "full" and st != "full" and k not in new_keys]

    if new_keys or freed:
        lines = ["🏕️ 캠핑장 빈자리가 떴어요!\n"]
        for k in new_keys:
            c = details[k]
            lines.append(f"• {'📌 ' if c['target'] else ''}{date_name(c['date'])}  [표시: {c['text']}]")
            lines.append(f"  👉 {month_url(c['date'].year, c['date'].month)}")
        for k in freed:
            c = details[k]
            lines.append(f"• {'📌 ' if c['target'] else ''}{date_name(c['date'])} "
                         f"— '예약완료'가 풀렸어요  [표시: {c['text']}]")
            lines.append(f"  👉 {month_url(c['date'].year, c['date'].month)}")
        lines.append("\n얼른 들어가서 예약하세요!")
        send_telegram("\n".join(lines))
    else:
        print("새로 생긴 빈자리 없음. 지금 상태:",
              ", ".join(f"{k}:{v}" for k, v in sorted(status_now.items())) or "없음")

    state["notified"] = available_now
    state["last_status"] = status_now
    state["last_check"] = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------- 메인 ----------------------------
def main():
    test_mode = os.environ.get("TEST_MODE", "").lower() == "true"
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    interval = max(30, int(os.environ.get("INTERVAL_SECONDS", DEFAULT_INTERVAL)))
    loop_minutes = 0 if test_mode else max(0, int(os.environ.get("LOOP_MINUTES",
                                                                 DEFAULT_LOOP_MINUTES)))
    started = dt.datetime.now(KST)
    deadline = started + dt.timedelta(minutes=loop_minutes)
    today = started.date()
    targets, bad_dates = read_target_dates(today)
    print(f"오늘: {today} | 지정 날짜: {sorted(d.isoformat() for d in targets) or '없음'}")
    print(f"확인 간격: {interval}초 | 이번 실행이 도는 시간: {loop_minutes}분")

    # 0) 먼저 기억을 읽는다. 멈춤 상태면 텔레그램을 건드리기 전에 끝낸다.
    state = load_state()
    first_run = not state
    state.setdefault("notified", [])
    state.setdefault("fail_count", 0)
    state.setdefault("last_status", {})

    if state.get("stopped"):
        if not manual:
            print("멈춤 상태라서 조회하지 않고 끝냅니다.")
            print("다시 켜려면: Actions → Enable workflow → Run workflow")
            # 멈춰 있다는 사실은 하루에 한 번만 알린다 (매번 보내면 스팸이 된다)
            last_warn = state.get("stopped_warned")
            if last_warn != today.isoformat():
                send_telegram(
                    "⛔ 캠핑장 알리미가 멈춰 있어요 (하루 한 번 알림)\n\n"
                    f"멈춘 이유: {state.get('stopped_reason', '기록 없음')}\n\n"
                    "지금은 빈자리를 보고 있지 않아요.\n"
                    "다시 켜려면 GitHub → Actions → 알리미 선택 → "
                    "Enable workflow를 누른 뒤 Run workflow를 한 번 눌러주세요.")
                state["stopped_warned"] = today.isoformat()
                save_state(state)
            return
        state["stopped"] = False
        state["fail_count"] = 0
        state.pop("stopped_warned", None)
        state.pop("stopped_reason", None)
        send_telegram("▶️ 수동 실행으로 캠핑장 빈자리 감시를 다시 시작해요.")

    # 1) 메시지를 보내지 않고 텔레그램 연결만 조용히 확인한다
    ok, why = telegram_ping()
    if not ok:
        print("\n[치명적] 텔레그램으로 보낼 수 없어서 여기서 멈춥니다.")
        print("         이유:", why)
        sys.exit(1)
    print("[확인] 텔레그램 연결 정상")
    if test_mode:
        send_telegram("🧪 테스트를 시작했어요. 달력을 읽는 중이에요…")

    if first_run and not test_mode:
        send_telegram(f"✅ 캠핑장 빈자리 감시를 시작했어요! (약 {interval}초마다 확인)")
        state["last_heartbeat"] = today.isoformat()

    round_no = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul",
                                      user_agent=USER_AGENT)
        page = context.new_page()
        try:
            while True:
                round_no += 1
                now = dt.datetime.now(KST)
                print(f"\n===== {round_no}번째 확인 ({now:%H:%M:%S}) =====")
                try:
                    one_cycle(page, state, now.date(), targets, bad_dates, test_mode)
                except Blocked as e:
                    print("[차단]", e)
                    save_debug(page, "blocked")
                    stop_monitoring(state, str(e))
                    save_state(state)
                    if test_mode:
                        sys.exit(1)
                    return
                except Exception as e:
                    state["fail_count"] += 1
                    print(f"[오류] {e}  (연속 {state['fail_count']}회)")
                    save_debug(page, "error")
                    if test_mode:
                        send_telegram("❌ 테스트 실패 — 달력을 읽지 못했어요\n\n"
                                      f"이유: {e}\n\n"
                                      "Actions 화면의 debug-screenshots 를 받아서 "
                                      "로봇이 본 화면을 확인해 주세요.")
                        save_state(state)
                        sys.exit(1)
                    if state["fail_count"] >= MAX_FAILS:
                        stop_monitoring(state, f"{MAX_FAILS}번 연속 조회 실패 — 마지막 오류: {e}")
                        save_state(state)
                        return
                    # 화면이 꼬였을 수 있으니 새 창으로 바꿔서 다시 해본다
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = context.new_page()

                # 한 달에 한 번 '잘 돌고 있어요' 알림
                last = state.get("last_heartbeat")
                if not test_mode and last and \
                        (dt.date.today() - dt.date.fromisoformat(last)).days >= HEARTBEAT_DAYS:
                    cur = state.get("last_status", {})
                    marks = {"available": "✅", "full": "❌", "closed": "🔒", "empty": "⬜"}
                    detail = "\n".join(f"  {k} {marks.get(v, v)}"
                                        for k, v in sorted(cur.items())) or "  (없음)"
                    send_telegram("🙂 캠핑장 빈자리 감시, 아직 잘 돌아가고 있어요.\n\n"
                                  "지금 보고 있는 날짜 상태:\n" + detail)
                    state["last_heartbeat"] = dt.date.today().isoformat()

                save_state(state)

                if test_mode:
                    return
                if dt.datetime.now(KST) + dt.timedelta(seconds=interval) >= deadline:
                    print(f"\n이번 실행은 여기까지 ({round_no}번 확인했어요). "
                          f"곧 다음 실행이 이어받아요.")
                    return
                page.wait_for_timeout(interval * 1000)
        finally:
            browser.close()
            save_state(state)


if __name__ == "__main__":
    main()
