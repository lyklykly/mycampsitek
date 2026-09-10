# -*- coding: utf-8 -*-
"""
수원유스호스텔 캠핑장 '토요일 빈자리' 알리미

하는 일
1) 이번 달 + 다음 달 예약 달력을 브라우저처럼 열어본다.
2) 달력의 맨 오른쪽 칸(토요일)만 읽는다.
3) '예약완료'가 아니라 숫자가 보이면 = 예약 가능 → 텔레그램으로 알려준다.
4) 사이트가 접근을 막으면(403/429 등) 조회를 멈추고, 멈췄다고 알려준다.
"""
import calendar
import datetime as dt
import json
import os
import re
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ==================== 설정 (필요하면 여기만 고치세요) ====================
BASE_URL = ("https://yeyak.syf.or.kr/www/88?company_code=SYF09&part_code=02"
            "&place_code=2&days=1&date_yyyymm={yyyymm}")
MONTHS_TO_CHECK = 2        # 2 = 이번 달 + 다음 달
FULL_WORDS = ["예약완료", "마감", "불가", "만실", "종료", "휴관", "휴무", "대기"]
BLOCK_STATUS = {403, 429}  # 403 = 접근거부, 429 = 요청이 너무 많음
BLOCK_TEXTS = ["access denied", "too many requests", "접근이 거부", "접근 거부",
               "접근이 차단", "요청이 너무 많", "비정상적인 접근", "captcha"]
MAX_FAILS = 3              # 알 수 없는 오류가 연속 3번이면 멈춤
HEARTBEAT_DAYS = 30        # 30일마다 "아직 잘 돌고 있어요" 알림
STATE_FILE = "state.json"
DEBUG_DIR = "debug"
KST = ZoneInfo("Asia/Seoul")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
# ======================================================================

# 달력 표(일~토)를 찾아서, 각 줄의 '마지막 칸(토요일)' 글자를 가져오는 코드
JS_READY = """
(label) => {
  const body = document.body ? document.body.innerText : '';
  if (!body.includes(label)) return false;
  for (const t of document.querySelectorAll('table')) {
    const rows = Array.from(t.rows);
    if (rows.length < 2) continue;
    const head = Array.from(rows[0].cells).map(c => c.innerText.trim());
    if (head.length === 7 && head[0].startsWith('일') && head[6].startsWith('토'))
      return rows.slice(1).some(r => r.cells.length === 7);
  }
  return false;
}
"""

JS_SATURDAY_CELLS = """
() => {
  for (const t of document.querySelectorAll('table')) {
    const rows = Array.from(t.rows);
    if (!rows.length) continue;
    const head = Array.from(rows[0].cells).map(c => c.innerText.trim());
    if (head.length === 7 && head[0].startsWith('일') && head[6].startsWith('토')) {
      return rows.slice(1).map(r => {
        const cells = r.cells;
        const c = cells[cells.length - 1];
        return { n: cells.length, text: c ? c.innerText : '' };
      });
    }
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
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    print("----- 텔레그램 메시지 -----\n" + text + "\n--------------------------")
    if not token or not chat_id:
        print("[경고] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 가 없어서 보내지 못했어요.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        if not r.ok:
            print("텔레그램 전송 실패:", r.status_code, r.text[:300])
        return r.ok
    except requests.RequestException as e:
        print("텔레그램 전송 오류:", e)
        return False


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
    turned_off = disable_workflow()
    msg = (
        "⛔ 캠핑장 빈자리 조회를 멈췄어요\n\n"
        f"이유: {reason}\n\n"
        "사이트가 자동 조회를 막았거나, 사이트 모양이 바뀌었을 수 있어요.\n"
        "코드를 새로 짜야(수정해야) 할 수 있어요.\n\n"
        + ("GitHub 자동 실행도 꺼두었어요." if turned_off else
           "※ GitHub 자동 실행 끄기는 실패했지만, 다음 실행부터는 조회하지 않고 바로 끝나요.")
    )
    send_telegram(msg)


def months_to_check(today):
    y, m = today.year, today.month
    out = []
    for _ in range(MONTHS_TO_CHECK):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def month_url(y, m):
    return BASE_URL.format(yyyymm=f"{y}{m:02d}")


def interpret(rows, y, m, today):
    """
    달력 각 줄의 토요일 칸 글자 → [{day, status, text}] 로 바꾼다.
    - 줄 순서로 날짜를 계산한다(일요일 시작 달력 기준, 맨 끝 칸 = 토요일).
    - status: available(예약가능) / full(예약완료) / unknown(숫자도 완료도 아님)
    """
    weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(y, m)
    week_rows = [r for r in rows if r.get("n") == 7]
    result = []
    for i, r in enumerate(week_rows):
        day = weeks[i][6] if i < len(weeks) else 0
        if day == 0:
            continue                          # 다음 달 날짜 칸
        if dt.date(y, m, day) < today:
            continue                          # 이미 지난 토요일
        text = " ".join((r.get("text") or "").split())
        if any(w in text for w in FULL_WORDS):
            status = "full"
        elif re.search(r"\d", text):
            status = "available"
        else:
            status = "unknown"
        result.append({"day": day, "status": status, "text": text})
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
def check_month(page, blocked_hits, y, m, today, keep_screenshot=False):
    url = month_url(y, m)
    label = f"{y}.{m:02d}"
    print(f"[확인] {label} → {url}")

    resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if resp is not None and resp.status in BLOCK_STATUS:
        raise Blocked(f"HTTP {resp.status} 응답 ({label} 달력)")

    try:
        page.wait_for_function(JS_READY, arg=label, timeout=30000)
    except PWTimeout:
        print(f"[주의] {label} 달력이 30초 안에 완전히 뜨지 않았어요.")

    if blocked_hits:
        status, bad_url = blocked_hits[0]
        raise Blocked(f"HTTP {status} 응답 (달력 데이터 요청)")

    body = page.inner_text("body")
    if any(w in body.lower() for w in BLOCK_TEXTS):
        raise Blocked("페이지에 '접근 차단/요청 제한' 문구가 보여요")

    rows = page.evaluate(JS_SATURDAY_CELLS)
    if not rows or not any(r.get("n") == 7 for r in rows):
        raise RuntimeError(f"{label} 달력 표를 찾지 못했어요 (사이트 모양이 바뀌었을 수 있음)")
    if label not in body:
        raise RuntimeError(f"페이지에 '{label}' 표시가 없어요 (다른 달이 떴을 수 있음)")

    if keep_screenshot:
        save_debug(page, f"calendar_{y}{m:02d}")
    return interpret(rows, y, m, today)


# ---------------------------- 메인 ----------------------------
def main():
    test_mode = os.environ.get("TEST_MODE", "").lower() == "true"
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    now = dt.datetime.now(KST)
    today = now.date()

    state = load_state()
    first_run = not state
    state.setdefault("notified", [])
    state.setdefault("fail_count", 0)

    # 멈춤 상태면: 자동 실행은 그냥 끝, 수동 실행이면 다시 시작
    if state.get("stopped"):
        if not manual:
            print("멈춤 상태라서 조회하지 않고 끝냅니다.")
            return
        state["stopped"] = False
        state["fail_count"] = 0
        send_telegram("▶️ 수동 실행으로 캠핑장 빈자리 감시를 다시 시작해요.")

    results = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul",
                                          user_agent=USER_AGENT)
            page = context.new_page()
            blocked_hits = []
            page.on("response", lambda r: blocked_hits.append((r.status, r.url))
                    if "syf.or.kr" in r.url and r.status in BLOCK_STATUS else None)
            try:
                for y, m in months_to_check(today):
                    results[(y, m)] = check_month(page, blocked_hits, y, m, today,
                                                  keep_screenshot=test_mode)
                    page.wait_for_timeout(2000)   # 사이트에 부담 주지 않게 잠깐 쉬기
            except Exception:
                save_debug(page, "error")
                raise
            finally:
                browser.close()

    except Blocked as e:
        print("[차단]", e)
        stop_monitoring(state, str(e))
        save_state(state)
        return

    except Exception as e:
        state["fail_count"] += 1
        print(f"[오류] {e}  (연속 {state['fail_count']}회)")
        if state["fail_count"] >= MAX_FAILS:
            stop_monitoring(state, f"{MAX_FAILS}번 연속 조회 실패 — 마지막 오류: {e}")
        save_state(state)
        return

    # ---------- 여기까지 왔으면 조회 성공 ----------
    state["fail_count"] = 0

    if test_mode:
        lines = ["🧪 테스트 결과 (지금 보이는 토요일 칸)"]
        names = {"available": "✅ 예약가능", "full": "❌ 예약완료", "unknown": "❓ 판단불가"}
        for (y, m), cells in results.items():
            lines.append(f"\n[{y}년 {m}월] {month_url(y, m)}")
            if not cells:
                lines.append("  (남은 토요일 없음)")
            for c in cells:
                lines.append(f"  {c['day']}일(토): {names[c['status']]}  / 칸 글자: '{c['text']}'")
        send_telegram("\n".join(lines))
        save_state(state)
        return

    available_now = []
    details = {}
    for (y, m), cells in results.items():
        for c in cells:
            if c["status"] == "available":
                key = f"{y}-{m:02d}-{c['day']:02d}"
                available_now.append(key)
                details[key] = (y, m, c)

    new_keys = [k for k in available_now if k not in state["notified"]]
    if new_keys:
        lines = ["🏕️ 캠핑장 토요일 빈자리가 떴어요!\n"]
        for k in new_keys:
            y, m, c = details[k]
            lines.append(f"• {y}년 {m}월 {c['day']}일(토)  [표시: {c['text']}]")
            lines.append(f"  👉 {month_url(y, m)}")
        lines.append("\n얼른 들어가서 예약하세요!")
        send_telegram("\n".join(lines))
    else:
        print("새로 생긴 토요일 빈자리 없음. 지금 가능한 날:", available_now or "없음")

    # 지금 가능한 날만 기억 → 사라졌다가 다시 뜨면 또 알려줌
    state["notified"] = available_now

    # 시작 알림 / 한 달에 한 번 '살아있어요' 알림
    last = state.get("last_heartbeat")
    if first_run:
        send_telegram("✅ 캠핑장 토요일 빈자리 감시를 시작했어요! (약 5분마다 확인)")
        state["last_heartbeat"] = today.isoformat()
    elif not last or (today - dt.date.fromisoformat(last)).days >= HEARTBEAT_DAYS:
        send_telegram("🙂 캠핑장 빈자리 감시, 아직 잘 돌아가고 있어요.")
        state["last_heartbeat"] = today.isoformat()

    save_state(state)


if __name__ == "__main__":
    main()
