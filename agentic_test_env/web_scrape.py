"""The same numbered-control idea, against a real web page through Selenium.

The point of this file is how little of it there is. `agent.decide()` is reused
unchanged: it takes anything with a `.number` and a `.describe()`, so swapping a
Windows accessibility tree for a browser DOM touches only the scraper and the
executor. If the numbering approach were secretly relying on something UIA-shaped,
this is where it would fall over.

Two differences from the desktop version, both of which matter:

  * A real page has far more interactive nodes than a mock app, and many are
    invisible, off-viewport, or duplicated by nested wrappers. Pruning is not a
    nicety here, it is what keeps the list inside the 255-option Choice cap that
    experiment 04 measured.
  * The safety boundary is a domain allowlist rather than a window allowlist, and
    it is checked against `driver.current_url` immediately before every action -
    because a click can navigate, and the page you are about to act on may not be
    the page you scraped.

Elements are tagged in the DOM with `data-jev-id` as they are collected, so acting
on one is a fresh lookup by that attribute rather than a stale Selenium handle
that goes invalid the moment the page re-renders. On a site like YouTube, which
re-renders constantly, stale handles are the default failure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# Kept below the 255-option Choice cap, leaving room for the "none" option.
MAX_OPTIONS = 240

COLLECT_JS = r"""
const SEL = [
  'a[href]', 'button', 'input', 'textarea', 'select', 'summary',
  '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]',
  '[role=checkbox]', '[role=radio]', '[role=option]', '[role=switch]',
  '[role=searchbox]', '[role=combobox]', '[role=textbox]',
  '[contenteditable=true]', '[onclick]'
].join(',');

document.querySelectorAll('[data-jev-id]').forEach(e => e.removeAttribute('data-jev-id'));

const NOISY = /^(input|textarea|select)$/;
const seen = new Map();
const out = [];
let n = 0;

for (const el of document.querySelectorAll(SEL)) {
  const cs = getComputedStyle(el);
  if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) continue;
  if (el.getAttribute('aria-hidden') === 'true') continue;
  if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;

  const r = el.getBoundingClientRect();
  if (r.width < 6 || r.height < 6) continue;
  // Must be at least partly on screen. Off-viewport nodes are real controls but
  // a person could not click them without scrolling, so offering them invites
  // an action that silently does nothing.
  if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;

  let name = (el.getAttribute('aria-label') || '').trim();
  if (!name) name = (el.getAttribute('title') || '').trim();
  if (!name) name = (el.innerText || '').trim().replace(/\s+/g, ' ');
  if (!name) name = (el.getAttribute('placeholder') || '').trim();
  if (!name) name = (el.getAttribute('alt') || '').trim();
  if (!name && NOISY.test(el.tagName.toLowerCase())) name = (el.value || '').trim();
  if (!name) continue;
  if (name.length > 140) name = name.slice(0, 140) + '...';

  const tag = el.tagName.toLowerCase();
  const role = (el.getAttribute('role') || '').toLowerCase();
  const itype = (el.getAttribute('type') || '').toLowerCase();
  const typeable =
      (tag === 'input' && !['button','submit','checkbox','radio','hidden','file','image','reset'].includes(itype))
      || tag === 'textarea' || el.isContentEditable
      || ['searchbox','combobox','textbox'].includes(role);

  // Nested wrappers on real sites produce the same control several times over.
  // Keep the innermost, smallest box per (name, kind).
  const key = (typeable ? 'T:' : 'C:') + name.toLowerCase();
  const area = r.width * r.height;
  if (seen.has(key)) {
    const prev = seen.get(key);
    if (area >= prev.area) continue;
    prev.dropped = true;
  }

  n += 1;
  el.setAttribute('data-jev-id', String(n));
  const rec = {id: n, name: name, tag: tag, role: role,
               kind: typeable ? 'type' : 'click', area: area, dropped: false,
               x: Math.round(r.left), y: Math.round(r.top),
               w: Math.round(r.width), h: Math.round(r.height)};
  seen.set(key, rec);
  out.push(rec);
}
return {total: out.length, items: out.filter(r => !r.dropped)};
"""

PLAYBACK_JS = r"""
const vids = Array.from(document.querySelectorAll('video'))
    .filter(v => v.readyState > 0 || v.currentTime > 0 || !v.paused);
const v = vids[0];
if (!v) return {found: false, url: location.href, title: document.title};
return {found: true, paused: v.paused, muted: v.muted, volume: v.volume,
        currentTime: Math.round(v.currentTime * 100) / 100,
        duration: isFinite(v.duration) ? Math.round(v.duration) : null,
        url: location.href, title: document.title};
"""


@dataclass
class WebControl:
    """Duck-typed to match uia_scrape.Element, so agent.decide() is unchanged."""
    number: int
    label: str
    kind: str
    tag: str
    role: str
    rect: tuple[int, int, int, int]
    jev_id: int = field(default=0)

    def describe(self) -> str:
        noun = {"type": "text field"}.get(self.kind)
        if noun is None:
            noun = {"a": "link", "button": "button", "select": "dropdown"}.get(
                self.tag, self.role or "control")
        return f"{self.label} ({noun})"


class DomainNotAllowed(RuntimeError):
    pass


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def check_domain(driver, allowed: list[str]) -> str:
    host = host_of(driver.current_url)
    if not any(host == a or host.endswith("." + a) for a in allowed):
        raise DomainNotAllowed(
            f"refusing to act: {host!r} is not on the domain allowlist {allowed}")
    return host


def scrape(driver, allowed: list[str]) -> tuple[list[WebControl], dict]:
    check_domain(driver, allowed)
    t0 = time.perf_counter()
    raw = driver.execute_script(COLLECT_JS)
    items = raw["items"]

    controls: list[WebControl] = []
    for rec in items:
        controls.append(WebControl(
            number=0, label=rec["name"], kind=rec["kind"], tag=rec["tag"],
            role=rec["role"],
            rect=(rec["x"], rec["y"], rec["w"], rec["h"]),
            jev_id=rec["id"]))

    controls.sort(key=lambda c: (c.rect[1] // 40, c.rect[0]))
    dropped_for_cap = max(0, len(controls) - MAX_OPTIONS)
    controls = controls[:MAX_OPTIONS]
    for i, c in enumerate(controls, start=1):
        c.number = i

    stats = {
        "found": raw["total"],
        "after_dedupe": len(items),
        "kept": len(controls),
        "dropped_for_cap": dropped_for_cap,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    return controls, stats


def act(driver, control: WebControl, allowed: list[str], value: str | None = None,
        submit: bool = False, dry_run: bool = False) -> dict:
    """Click or type, re-checking the domain first and re-finding the element.

    The re-find by data-jev-id is not defensive padding: on a page that
    re-renders between the scrape and the action, a held Selenium reference is
    already stale and would raise instead of acting.
    """
    host = check_domain(driver, allowed)
    plan = {"action": control.kind, "label": control.label, "host": host,
            "value": value, "performed": False}
    if dry_run:
        return plan

    el = driver.find_element(By.CSS_SELECTOR, f'[data-jev-id="{control.jev_id}"]')
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', behavior:'instant'});", el)
    time.sleep(0.08)

    if control.kind == "type":
        el.clear()
        el.send_keys(value or "")
        if submit:
            el.send_keys(Keys.RETURN)
            plan["submitted"] = True
    else:
        try:
            el.click()
        except Exception:
            # Overlays and sticky headers intercept ordinary clicks constantly on
            # real sites; a scripted click still reaches the element and still
            # counts as a user gesture for autoplay purposes.
            driver.execute_script("arguments[0].click();", el)
            plan["via"] = "script_click"

    plan["performed"] = True
    time.sleep(0.35)
    return plan


def playback_state(driver) -> dict:
    return driver.execute_script(PLAYBACK_JS)
