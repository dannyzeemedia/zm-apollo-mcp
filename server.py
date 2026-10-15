"""Zee.Media Apollo research MCP server.

Lets a named teammate (e.g. Claire) do Apollo people-research from claude.ai
using Danny's Apollo subscription — no extra Apollo seat. Every call runs on the
one server-side Apollo key, so it spends Danny's API calls / credits.

Two-step research model, mirroring the lead engine:
  1) find_people(...)      -> POST /mixed_people/api_search  (FREE, no credits)
                              returns candidates (name, title, company, LinkedIn,
                              person_id) with emails still masked
  2) reveal_contacts(ids)  -> POST /people/bulk_match        (~1 credit each)
                              reveals verified email + LinkedIn for chosen ids

Guardrails:
  - Auth: WorkOS AuthKit (OAuth + Dynamic Client Registration, which claude.ai
    needs). On top of that, an in-code ALLOWLIST (ALLOWED_EMAILS) — only those
    callers may use the tools. Fail-closed: unknown caller => refused.
  - Daily reveal cap (DAILY_REVEAL_CAP) so research can't drain the credit
    balance the lead engines already spend each morning.

The Apollo key stays server-side only and is never exposed to the client.

Env vars (set in Render):
  APOLLO_API_KEY     Apollo master key (server-side only)
  AUTHKIT_DOMAIN     https://<app>.authkit.app
  PUBLIC_URL         https://<service>.onrender.com   (this server's public URL)
  ALLOWED_EMAILS     comma list of emails allowed to call the tools
  DAILY_REVEAL_CAP   max email reveals per UTC day (default 50)
  USERINFO_URL       optional override for the OIDC userinfo endpoint
  PORT               provided by Render
"""
import os
import sys
import time
import json
import base64
import datetime
import threading

import requests
from fastmcp import FastMCP
from fastmcp.server.auth.providers.workos import AuthKitProvider
from fastmcp.server.dependencies import get_access_token

APOLLO = "https://api.apollo.io/api/v1"
APOLLO_KEY = os.environ["APOLLO_API_KEY"]
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}
DAILY_REVEAL_CAP = int(os.environ.get("DAILY_REVEAL_CAP", "50"))
AUTHKIT_DOMAIN = os.environ["AUTHKIT_DOMAIN"].rstrip("/")
USERINFO_URL = os.environ.get("USERINFO_URL", f"{AUTHKIT_DOMAIN}/oauth2/userinfo")

auth = AuthKitProvider(
    authkit_domain=AUTHKIT_DOMAIN,
    base_url=os.environ["PUBLIC_URL"],
    # Advertise the identity scopes so the MCP client (claude.ai) requests them.
    # Without this the issued token carries no email, so the allowlist can't tell
    # who is calling and refuses everyone. Not enforced on the token — just
    # advertised — so a client that omits one still authenticates.
    scopes_supported=["openid", "profile", "email"],
)
mcp = FastMCP("Zee.Media Apollo Research", auth=auth)

_sess = requests.Session()
_sess.headers.update({"Content-Type": "application/json", "Cache-Control": "no-cache",
                      "X-Api-Key": APOLLO_KEY})

# ---- daily reveal cap (in-memory; resets at UTC midnight or on redeploy) ------
_lock = threading.Lock()
_usage = {"day": None, "reveals": 0}


def _today():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _usage_today():
    with _lock:
        if _usage["day"] != _today():
            _usage["day"] = _today()
            _usage["reveals"] = 0
        return _usage["reveals"]


def _add_reveals(n):
    with _lock:
        if _usage["day"] != _today():
            _usage["day"] = _today()
            _usage["reveals"] = 0
        _usage["reveals"] += n
        return _usage["reveals"]


# ---- Claire-only gate ---------------------------------------------------------
_email_cache = {}


def _b64url_json(seg):
    """Decode one base64url JWT segment to a dict; {} on any problem."""
    try:
        pad = "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg + pad))
    except Exception:  # noqa: BLE001
        return {}


def _caller_email():
    """Resolve the authenticated caller's email. Tries, in order: the verified
    token claims, the raw JWT payload, then the OIDC userinfo endpoint. Cached
    per access token. Logs (flushed) wherever a step comes up empty so the
    reason is visible in the server logs."""
    tok = get_access_token()
    raw = getattr(tok, "token", None)
    claims = getattr(tok, "claims", None) or {}
    # 1) straight from the verified claims
    for k in ("email", "user_email", "https://workos.com/email"):
        v = claims.get(k)
        if v:
            return str(v).lower()
    # 2) decode the raw JWT payload directly (WorkOS may carry email there)
    if raw and raw.count(".") == 2:
        payload = _b64url_json(raw.split(".")[1])
        for k in ("email", "user_email"):
            v = payload.get(k)
            if v:
                return str(v).lower()
        print(f"[auth] jwt payload keys={sorted(payload)}", file=sys.stderr, flush=True)
    # 3) OIDC userinfo (needs the email scope on the token)
    if raw and raw in _email_cache:
        return _email_cache[raw]
    if raw:
        try:
            r = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {raw}"}, timeout=15)
            if r.ok:
                body = r.json()
                email = (body.get("email") or "").lower()
                _email_cache[raw] = email
                if not email:
                    print(f"[auth] userinfo ok, no email; keys={sorted(body)}", file=sys.stderr, flush=True)
                return email
            print(f"[auth] userinfo {r.status_code}: {r.text[:120]}", file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[auth] userinfo error {str(e)[:100]}", file=sys.stderr, flush=True)
    else:
        print(f"[auth] no raw token; claim keys={sorted(claims)}", file=sys.stderr, flush=True)
    return ""


def _require_allowed():
    """Raise if the caller isn't on the allowlist. Fail-closed."""
    if not ALLOWED_EMAILS:
        return  # no allowlist configured => open to any authenticated user
    email = _caller_email()
    if email not in ALLOWED_EMAILS:
        raise PermissionError(
            "This Apollo connector is restricted. Your sign-in isn't on its "
            "allowlist — ask Danny to add you." + (f" (signed in as {email})" if email else "")
        )


def _post(path, body, tries=5):
    for i in range(tries):
        r = _sess.post(f"{APOLLO}/{path}", json=body, timeout=90)
        if r.status_code == 429:
            time.sleep(min(2 ** i, 30) + 1)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Apollo {path}: rate-limited, retries exhausted")


def _person_card(p):
    org = p.get("organization") or {}
    return {
        "person_id": p.get("id"),
        "name": p.get("name") or " ".join(filter(None, [p.get("first_name"), p.get("last_name")])),
        "title": p.get("title"),
        "seniority": p.get("seniority"),
        "departments": p.get("departments") or [],
        "company": org.get("name") or p.get("organization_name"),
        "company_domain": org.get("primary_domain") or org.get("website_url"),
        "linkedin_url": p.get("linkedin_url"),
        "location": ", ".join(filter(None, [p.get("city"), p.get("state"), p.get("country")])),
        "email_status": p.get("email_status"),  # 'verified' candidates are revealable
    }


@mcp.tool
def find_people(company_domain: str, titles: list[str] | None = None,
                seniorities: list[str] | None = None, limit: int = 10) -> dict:
    """Find people at a company by its website domain (e.g. "amazon.com"). FREE —
    spends no Apollo credits; emails come back masked. Optionally narrow by
    job titles (e.g. ["head of partnerships","vp marketing"]) and/or seniorities
    (one or more of: owner, founder, c_suite, partner, vp, head, director,
    manager, senior, entry, intern). Returns up to `limit` candidates with their
    person_id — pass those ids to reveal_contacts to get verified emails.

    Tip: big companies return thousands, so always pass titles or seniorities to
    narrow to the person you actually want."""
    _require_allowed()
    dom = (company_domain or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    if dom.startswith("www."):
        dom = dom[4:]
    if not dom or "." not in dom:
        return {"error": "Give a company website domain, e.g. 'amazon.com'."}
    limit = max(1, min(int(limit or 10), 25))
    body = {"q_organization_domains_list": [dom], "per_page": limit, "page": 1}
    if titles:
        body["person_titles"] = [str(t) for t in titles]
    if seniorities:
        body["person_seniorities"] = [str(s) for s in seniorities]
    data = _post("mixed_people/api_search", body)
    people = data.get("people", []) or []
    pg = data.get("pagination", {}) or {}
    return {
        "company_domain": dom,
        "total_matches": pg.get("total_entries"),
        "showing": len(people),
        "note": "Free step: you get title, company and person_id, but names, "
                "LinkedIn and emails stay hidden until you reveal. Pick the "
                "person_ids you want, then call reveal_contacts.",
        "people": [_person_card(p) for p in people],
    }


@mcp.tool
def reveal_contacts(person_ids: list[str]) -> dict:
    """Reveal verified work emails + LinkedIn for specific people, by the
    person_id values returned from find_people. COSTS ~1 Apollo credit per id.
    Subject to a daily cap; reveals beyond the cap are refused. Pass only the
    ids you actually need."""
    _require_allowed()
    ids = [str(x) for x in (person_ids or []) if str(x).strip()]
    if not ids:
        return {"error": "Pass one or more person_id values from find_people."}
    used = _usage_today()
    if used + len(ids) > DAILY_REVEAL_CAP:
        return {"error": f"Daily reveal cap reached ({used}/{DAILY_REVEAL_CAP} used today). "
                         f"Try fewer, or ask Danny to raise the cap.",
                "reveals_used_today": used, "daily_cap": DAILY_REVEAL_CAP}
    out, credits = [], 0
    for i in range(0, len(ids), 10):
        d = _post("people/bulk_match",
                  {"details": [{"id": x} for x in ids[i:i + 10]], "reveal_personal_emails": False})
        credits += d.get("credits_consumed", 0) or 0
        for m in d.get("matches", []):
            if not m:
                continue
            org = m.get("organization") or {}
            out.append({
                "person_id": m.get("id"),
                "name": m.get("name"),
                "title": m.get("title"),
                "email": m.get("email"),
                "email_status": m.get("email_status"),  # trust 'verified'
                "linkedin_url": m.get("linkedin_url"),
                "company": org.get("name"),
            })
        time.sleep(0.3)
    total = _add_reveals(len(ids))
    return {"revealed": out, "apollo_credits_consumed": credits,
            "reveals_used_today": total, "daily_cap": DAILY_REVEAL_CAP}


@mcp.tool
def check_usage() -> dict:
    """How many Apollo email reveals have been used today against the daily cap."""
    _require_allowed()
    used = _usage_today()
    return {"reveals_used_today": used, "daily_cap": DAILY_REVEAL_CAP,
            "remaining_today": max(0, DAILY_REVEAL_CAP - used),
            "note": "Finding people is free and uncapped; only email reveals count."}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
