"""Provider approval / rejection emails.

Locks down the copy and the headers agreed with Tushar on 14 Oct 2026, so a
later template edit cannot quietly reintroduce the marketing-style footer, the
"do not reply" line, or a broken banner image.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

PORTAL = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, PORTAL)

os.environ["DJANGO_SETTINGS_MODULE"] = "aqua_admin.settings"
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mkstemp(suffix=".sqlite3")[1]
os.environ["INBOX_AUTOREFRESH"] = "false"

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


class _User:
    name = "Christopher Gane"
    first_name = ""
    last_name = ""
    email = "info@aquaticelements.co.uk"


class _Profile:
    company_name = "Aquatic Elements Ltd"


ROLES = ("consultant", "breeder")
KINDS = ("approval", "rejection")


def main():
    from admin_portal.services import review_runner as rr

    section("1. EVERY TEMPLATE RENDERS CLEANLY")
    for role in ROLES:
        for kind in KINDS:
            html, text = rr._render_provider_email(role, kind, _User(), _Profile())
            tag = f"{role}/{kind}"
            check(f"{tag} renders", bool(html), f"{len(html)} chars")
            check(f"{tag} has no unresolved template tags",
                  "{{" not in html and "{%" not in html)
            check(f"{tag} addresses the applicant by name", "Christopher Gane" in html)
            check(f"{tag} names the company", "Aquatic Elements Ltd" in html)
            check(f"{tag} has a plain-text alternative", bool(text.strip()))

    section("2. THE AGREED COPY RULES")
    for role in ROLES:
        for kind in KINDS:
            html, _ = rr._render_provider_email(role, kind, _User(), _Profile())
            tag = f"{role}/{kind}"
            # These are account emails, not marketing: an unsubscribe link
            # implies a mailing list the provider cannot actually leave.
            check(f"{tag} has NO unsubscribe link", "nsubscribe" not in html)
            # Replies are monitored, so the email must not tell them not to.
            check(f"{tag} does NOT say 'do not reply'", "do not reply" not in html.lower())
            check(f"{tag} brands as 'AQUA AI'", "AQUA AI" in html)
            check(f"{tag} points at the support inbox", "support@aquaai.uk" in html)

    section("3. ROLE-SPECIFIC NEXT STEPS")
    c_app, _ = rr._render_provider_email("consultant", "approval", _User(), _Profile())
    b_app, _ = rr._render_provider_email("breeder", "approval", _User(), _Profile())
    check("consultant approval says Consultant", "as a Consultant" in c_app)
    check("breeder approval says Breeder", "as a Breeder" in b_app)
    check("consultant approval talks about services and availability",
          "services" in c_app and "availability" in c_app)
    check("breeder approval talks about stock and the free months",
          "stock" in b_app and "two free months" in b_app)
    check("consultant approval does NOT mention the breeder free months",
          "two free months" not in c_app)
    check("both approvals link the app download",
          "aquaai.uk/download" in c_app and "aquaai.uk/download" in b_app)

    section("4. THE LOGO CANNOT RENDER BROKEN")
    with override_settings(EMAIL_LOGO_URL=""):
        html, _ = rr._render_provider_email("breeder", "approval", _User(), _Profile())
    check("no <img> at all when the logo URL is unset", html.count("<img") == 0)
    check("the AQUA AI wordmark still carries the banner", "AQUA AI" in html)

    with override_settings(EMAIL_LOGO_URL="https://aquaai.uk/email/logo.png"):
        html, _ = rr._render_provider_email("breeder", "approval", _User(), _Profile())
    check("the logo is embedded when configured",
          '<img src="https://aquaai.uk/email/logo.png"' in html)
    check("it is an absolute https URL", "https://" in html.split('<img src="')[1][:8])

    section("5. REPLY-TO")
    check("defaults to the address printed in the body",
          rr._provider_reply_to() == "support@aquaai.uk", rr._provider_reply_to())
    with override_settings(PROVIDER_EMAIL_REPLY_TO="providers@aquaai.uk"):
        check("can be redirected by setting", rr._provider_reply_to() == "providers@aquaai.uk")

    import inspect
    from admin_portal.services.google_oauth import send_gmail_message
    from admin_portal.services.notifier import send_custom_email
    check("send_custom_email accepts reply_to",
          "reply_to" in inspect.signature(send_custom_email).parameters)
    check("the Gmail path accepts reply_to",
          "reply_to" in inspect.signature(send_gmail_message).parameters)

    section("6. SUBJECT LINES")
    src = pathlib.Path(PORTAL, "admin_portal/services/review_runner.py").read_text()
    check("approval subject is the agreed one",
          'subject="Welcome to Aqua Providers — You\'re Approved"' in src)
    check("rejection subject is the agreed one",
          'subject="Your Aqua Providers application"' in src)
    check("no role is interpolated into the subject any more",
          'Your {brand} application has been approved' not in src)

    section("7. USER CONTENT IS ESCAPED")
    class Hostile:
        company_name = '<script>alert(1)</script> & Co'
    html, _ = rr._render_provider_email("consultant", "approval", _User(), Hostile())
    check("a company name with markup is escaped, not rendered",
          "&lt;script&gt;" in html and "<script>" not in html)
    check("ampersands are escaped", "&amp;" in html)

    print(f"\n{'=' * 72}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed\n{'=' * 72}")
    for f in FAIL:
        print("  FAILED:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
