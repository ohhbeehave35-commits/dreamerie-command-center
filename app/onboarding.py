"""
Client onboarding interview -- how a new client's assistant learns who they are.

WHY THIS EXISTS
Phase 1 of standing up a client Command Center is "REWRITE agents.py for the
client's own business hats and public persona" -- by hand, per client, from
whatever Vinny remembers about them. That is the slowest step in every build
and it fails silently: nothing tells you a fact was forgotten.

The load-bearing case is compliance language. NS Peptides may never say
"discreet shipping". Bear Arms is an LLC with no FFL and may never imply it
transfers firearms. Those boundaries exist today ONLY because Vinny said them
out loud once and someone typed them into a prompt. A client onboarded without
that conversation gets an assistant that will confidently say the illegal
thing. Capturing them as stored facts is the difference between a rule and a
memory.

DESIGN RULES (each one is a bug we already paid for)
1. WRITE AFTER EVERY ANSWER. Not at the end. A 20-minute interview that saves
   once loses everything to one failure.
2. STORE THE ANSWER VERBATIM. A summary becomes "ground truth" and then drifts.
   Summaries are generated separately and labelled as summaries.
3. TRACK WHAT WAS NEVER ASKED. An unanswered question is a KNOWN gap, so the
   assistant can say "nobody asked about pricing" instead of guessing -- the
   same absent-is-not-zero rule the rest of this app now follows.
4. ONE QUESTION AT A TIME. A wall of twenty gets abandoned halfway.
5. SOME ANSWERS ARE REQUIRED. A profile missing its compliance boundaries is
   not "incomplete", it is unsafe to build an assistant from, and readiness
   reporting says so in those words.
"""

import logging

import httpx

from . import crm

log = logging.getLogger(__name__)

PROFILE_TABLE = "Client Profiles"
_profile_table_id_cache = None

# The interview mirrors the ESTABLISHED / STARTUP build questionnaires in
# Client Deliverables/02-Master-Templates -- same sections, same order, same
# reasons. Those forms were written for humans to fill in; this is the same
# packet conducted as a conversation, so the answers land as stored facts
# instead of a PDF someone has to re-key.
#
# HOW THE QUESTIONS ARE BUILT (this is the craft, not decoration):
#  * Every question carries a WHY naming a concrete consequence. "Exactly as
#    filed -- carrier registration is rejected character for character, and a
#    rejection restarts the clock" gets a careful answer; "what's your business
#    name" gets a careless one.
#  * ANCHOR where a blank page is hard. People correct a number far more
#    readily than they invent one, so offer a range and invite them to write
#    over it.
#  * MAKE NOT KNOWING SAFE. "Honestly no idea" is a real answer and saying so
#    out loud is what stops someone guessing to look competent.
#  * NAME THE STAKES. "Whatever you say here is exactly what the assistant
#    quotes your customers, word for word" changes how hard someone thinks.
#  * ASK FOR THE LAST REAL INSTANCE, not the general case. Specific memory
#    beats abstraction every time.
#
# `edition`: "both" | "established" | "startup". A startup has no customers
# yet, so asking what people always ask them produces invention; the startup
# wording asks from trade experience instead.
#
# `required=True` is the packet's red star: it genuinely holds the build up.
EDITIONS = ("established", "startup")

QUESTIONS = [
    # ---- Section 1: The Business -------------------------------------------
    {"id": "legal_name", "section": "The Business", "required": True, "edition": "both",
     "q": "Full legal business name, exactly as it appears on your state filing -- not the "
          "trading name. Character for character, including any LLC or Inc.",
     "why": "Carrier registration for business texting is rejected if this does not match the "
            "filing exactly, and a rejection restarts a multi-week clock. This company has "
            "already had an A2P campaign rejected once."},
    {"id": "dba", "section": "The Business", "required": False, "edition": "both",
     "q": "And what do customers actually call you? The name on the truck or the sign.",
     "why": "The assistant speaks the customer-facing name and files under the legal one."},
    {"id": "owners", "section": "The Business", "required": True, "edition": "both",
     "q": "Who owns and runs this? Full legal names as they would appear on a contract, with "
          "the best number and email for each.",
     "why": "Contracts and carrier registration both need the real names."},
    {"id": "crew_size", "section": "The Business", "required": True, "edition": "both",
     "q": "How many crews and how many people, today?",
     "why": "Sets the monthly price tier AND decides how many jobs a day the schedule offers "
            "-- get it wrong and the assistant overbooks them."},
    {"id": "service_area", "section": "The Business", "required": True, "edition": "both",
     "q": "Where do you work -- cities and ZIP codes? And anywhere you DO NOT serve, or "
          "charge extra to reach?",
     "why": "ZIPs are what actually gets used: the assistant checks a caller address against "
            "them before it books anything."},
    {"id": "hours_afterhours", "section": "The Business", "required": True, "edition": "both",
     "q": "What are your hours -- and at 9pm on a Saturday, should it book a Monday slot, or "
          "take details and say you will call in the morning?",
     "why": "The assistant answers 24/7 regardless. This is about what it DOES after hours, "
            "and it stops it promising a callback nobody can keep."},
    {"id": "license_insurance", "section": "The Business", "required": True, "edition": "both",
     "q": "Licence and insurance -- the actual numbers. If any of it is still in progress, "
          "just say so.",
     "why": "The site will carry a Licensed and Insured line and we only publish claims we "
            "can back up. Still-in-progress is a real answer -- we leave the claim off "
            "rather than post something unbackable."},

    # ---- Section 2: What You Already Have -----------------------------------
    {"id": "existing_website", "section": "What You Already Have", "required": True,
     "edition": "both",
     "q": "Do you have a website today -- any address at all, even a one-pager or a Facebook "
          "page you treat as the website? And who built or maintains it now?",
     "why": "It is something we either connect to or replace, and we need to know which."},
    {"id": "domain_ownership", "section": "What You Already Have", "required": True,
     "edition": "both",
     "q": "Who owns the domain name, and where is it registered -- GoDaddy, Wix, Squarespace? "
          "Can you log in right now, would you have to ask someone, or honestly no idea? Any "
          "of those is a fine answer.",
     "why": "Whoever's name is on the domain CONTROLS it. People find out years later that a "
            "marketing company owns their web address and they cannot leave without losing "
            "it. No idea is worth finding out, not worth guessing."},
    {"id": "phone_ownership", "section": "What You Already Have", "required": True,
     "edition": "both",
     "q": "Your business phone number -- keep it and port it over, get a new one and forward "
          "the old, or start fresh?",
     "why": "Porting takes a couple of weeks and has to start early. Same ownership trap as "
            "the domain."},

    # ---- Section 3: Services and Pricing ------------------------------------
    {"id": "services_offered", "section": "Services and Pricing", "required": True,
     "edition": "both",
     "q": "What do you actually sell? List the services, and your real prices -- a range is "
          "fine, and so is it depends. If it depends, tell me what on.",
     "why": "The assistant quotes from a fixed list and NEVER makes a number up. Whatever you "
            "say here is exactly what it quotes your customers, word for word."},
    {"id": "price_drivers", "section": "Services and Pricing", "required": True,
     "edition": "both",
     "q": "What makes a job cost more?",
     "why": "Lets it give a range honestly instead of a single wrong number."},
    {"id": "explicitly_not_offered", "section": "Services and Pricing", "required": True,
     "edition": "both",
     "q": "What do people ask you for constantly that you DO NOT do? What was the last job "
          "you turned down?",
     "why": "The highest-value question on the form. An assistant that does not know what you "
            "refuse will cheerfully book it. Asking for the LAST one you turned down gets a "
            "real answer; asking in general gets a shrug."},
    {"id": "needs_eyes_on", "section": "Services and Pricing", "required": True,
     "edition": "both",
     "q": "Does someone need to look at the job before you can price it, or can most be "
          "quoted over the phone?",
     "why": "Decides whether it books outright or books an estimate visit. This is the line "
            "between a booked job and a wasted truck roll."},
    {"id": "job_duration", "section": "Services and Pricing", "required": False,
     "edition": "both",
     "q": "How long does a job take, and how many can you do in a day? Exact times or arrival "
          "windows?",
     "why": "Stops it booking four jobs on one afternoon across three towns."},
    {"id": "repeat_plan", "section": "Services and Pricing", "required": False,
     "edition": "both",
     "q": "Do you want a repeat or maintenance plan? And how often should a normal customer "
          "actually have this done?",
     "why": "Whatever you say becomes the automatic reminder schedule for past customers."},

    # ---- Section 4: How It Should Talk --------------------------------------
    {"id": "tone", "section": "How It Should Talk", "required": False, "edition": "both",
     "q": "How should it come across -- warm and neighbourly, straight to the point, calm and "
          "reassuring, patient because a lot of your customers are older? Specific examples "
          "beat adjectives.",
     "why": "This decides whether customers can tell."},
    {"id": "disclose_ai", "section": "How It Should Talk", "required": True, "edition": "both",
     "q": "Should it say it is an assistant -- if asked, up front every time, or would you "
          "rather it did not bring it up?",
     "why": "Strong recommendation: let it say so if asked. In a trade with a trust problem, "
            "being CAUGHT pretending to be a person is far more damaging than admitting it."},
    {"id": "forbidden_language", "section": "How It Should Talk", "required": True,
     "edition": "both",
     "q": "What should it NEVER say? Claims you cannot make, licensing limits, words that get "
          "you in trouble.",
     "why": "COMPLIANCE, and non-negotiable. Trades have been pursued by the FTC for health "
            "claims their industry cannot back. This is the answer that stops an assistant "
            "confidently saying the illegal thing."},
    {"id": "common_questions", "section": "How It Should Talk", "required": True,
     "edition": "established",
     "q": "The three questions you get on nearly every call -- and how you answer them, in "
          "your words.",
     "why": "The highest-value thing on the whole form. It is what the assistant will spend "
            "most of its time doing."},
    {"id": "common_questions", "section": "How It Should Talk", "required": True,
     "edition": "startup",
     "q": "From your years in the trade -- what do customers always ask, and how do you want "
          "those answered?",
     "why": "Same as the established version, asked from experience rather than their own call "
            "log, because they do not have one yet. Asking a startup what they get asked "
            "invites them to invent."},
    {"id": "objection_price", "section": "How It Should Talk", "required": True,
     "edition": "both",
     "q": "When a caller says the ad down the road is cheaper -- what should it say? In your "
          "words.",
     "why": "Customers WILL bring up the cheap competitor. Your answer to that, verbatim, is "
            "what the assistant uses."},
    {"id": "differentiator", "section": "How It Should Talk", "required": False,
     "edition": "both",
     "q": "Straight up -- why should someone pick you over the other guy?",
     "why": "The one line worth repeating in every conversation."},

    # ---- Section 5: Leads, Booking and Escalation ---------------------------
    {"id": "must_capture", "section": "Leads and Escalation", "required": True,
     "edition": "both",
     "q": "What must it get from every caller before it is a usable lead?",
     "why": "Anything not on this list it will not ask for, and you get leads you cannot act on."},
    {"id": "turn_away", "section": "Leads and Escalation", "required": True, "edition": "both",
     "q": "Who should it turn away, politely? Outside the area, work you do not do, jobs too "
          "small to be worth the drive?",
     "why": "Better it says that is not something we handle than books you into a losing job."},
    {"id": "autonomy", "section": "Leads and Escalation", "required": True, "edition": "both",
     "q": "How much rope does it get? (a) Nothing goes out without you seeing it -- it drafts, "
          "you approve. (b) It answers and books on its own, but money or complaints come to "
          "you first. (c) Flag you only when something is actually wrong.",
     "why": "Start at (a) and loosen after watching it a week or two. Nearly everyone moves "
            "down within a month, but starting loose is how trust gets broken early."},
    {"id": "escalation_contact", "section": "Leads and Escalation", "required": True,
     "edition": "both",
     "q": "When something needs a human, whose phone rings? Name and mobile -- and is that "
          "different at 9pm or on a Sunday?",
     "why": "Without this the assistant decides for itself what is serious, and who to bother."},
    {"id": "what_is_urgent", "section": "Leads and Escalation", "required": True,
     "edition": "both",
     "q": "What counts as urgent in your trade -- the thing that should not wait until Monday?",
     "why": "Safety cases must skip the questionnaire entirely. If nobody defines urgent, the "
            "assistant runs a four-question intake at someone with a real emergency."},

    # ---- Section 7: Launch (startup only) -----------------------------------
    {"id": "launch_plan", "section": "Launch", "required": True, "edition": "startup",
     "q": "When do you want to be taking your first call, and what is not in place yet -- "
          "licence, insurance, vehicle, equipment, bank account?",
     "why": "A startup assistant must not take bookings for work that cannot be delivered yet. "
            "Knowing what is outstanding is what keeps it honest at launch."},
    {"id": "first_customers", "section": "Launch", "required": False, "edition": "startup",
     "q": "Where are your first customers coming from -- people you already know, a territory, "
          "an existing employer's overflow?",
     "why": "Shapes the opening flow and what the assistant should lead with."},

    # ---- Anything we've missed ----------------------------------------------
    {"id": "anything_missed", "section": "Anything We've Missed", "required": False,
     "edition": "both",
     "q": "Anything I should have asked and did not? Anything about how you run this that "
          "would change how it talks to your customers?",
     "why": "The catch-all that surfaces the thing no template anticipated."},
]

def questions_for(edition: str = "established") -> list:
    """The questions that apply to one edition, in order.

    Some ids exist twice with different wording -- common_questions asks an
    established business what it GETS asked, and a startup what it EXPECTS from
    trade experience, because asking a startup about its own call log invites
    invention. Same id because it is the same fact; different question because
    they are different people.
    """
    edition = edition if edition in EDITIONS else "established"
    return [q for q in QUESTIONS if q["edition"] in ("both", edition)]


def ids_for(edition: str = "established") -> list:
    return [q["id"] for q in questions_for(edition)]


def required_for(edition: str = "established") -> list:
    return [q["id"] for q in questions_for(edition) if q["required"]]


def question_text(question_id: str, edition: str = "established") -> dict:
    for q in questions_for(edition):
        if q["id"] == question_id:
            return q
    for q in QUESTIONS:  # validation only -- any edition's variant proves the id is real
        if q["id"] == question_id:
            return q
    return {}


# Deduped: an id appearing in both editions must be counted once.
QUESTION_IDS = list(dict.fromkeys(q["id"] for q in QUESTIONS))
REQUIRED_IDS = list(dict.fromkeys(q["id"] for q in QUESTIONS if q["required"]))
_BY_ID = {q["id"]: q for q in QUESTIONS}

_FIELDS = [("Client", "singleLineText"), ("Question ID", "singleLineText"),
           ("Question", "multilineText"), ("Answer", "multilineText")]


def _ensure_table() -> str:
    global _profile_table_id_cache
    if _profile_table_id_cache:
        return _profile_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                  headers=crm._headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == PROFILE_TABLE.lower():
                _profile_table_id_cache = t["id"]
                for name, ftype in _FIELDS:
                    try:
                        crm._ensure_field(c, t["id"], t, name, ftype)
                    except Exception as e:
                        log.warning("PROFILE_MIGRATE_SKIP field=%s %s", name, type(e).__name__)
                return _profile_table_id_cache
        fields = [{"name": n, "type": ft} for n, ft in _FIELDS]
        r = c.post(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                   headers=crm._headers(), json={"name": PROFILE_TABLE, "fields": fields})
        r.raise_for_status()
        _profile_table_id_cache = r.json()["id"]
        return _profile_table_id_cache


def record_answer(client: str, question_id: str, answer: str) -> tuple:
    """Save ONE answer, verbatim, immediately. Returns (ok, message).

    Called after every single response rather than batching at the end -- the
    whole point is that an interrupted interview keeps everything said so far.
    """
    client = (client or "").strip()
    question_id = (question_id or "").strip()
    answer = (answer or "").strip()
    if not client:
        return False, "I need to know which client this interview is for."
    if question_id not in _BY_ID:
        return False, (f"'{question_id}' isn't one of the interview questions. "
                       f"Valid ids: {', '.join(QUESTION_IDS)}")
    if not answer:
        return False, "Nothing to record -- the answer was empty."
    if not crm.is_configured():
        return False, (f"NOT SAVED -- the profile store isn't connected. Don't continue the "
                       f"interview until it is, or everything said will be lost.")
    try:
        tid = _ensure_table()
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(),
                       json={"fields": {"Client": client[:200],
                                        "Question ID": question_id,
                                        "Question": _BY_ID[question_id]["q"],
                                        # verbatim, never a summary
                                        "Answer": answer[:20000]}, "typecast": True})
        if r.status_code >= 400:
            body = (r.text or "")[:300]
            log.error("PROFILE_SAVE_FAIL client=%s q=%s http=%s body=%s",
                      client, question_id, r.status_code, body)
            return False, (f"NOT SAVED (HTTP {r.status_code}): {body}. Stop and fix this "
                           f"before asking anything else -- answers are being lost.")
        return True, f"Recorded: {question_id}"
    except Exception as e:
        log.exception("PROFILE_SAVE_FAIL client=%s q=%s", client, question_id)
        return False, (f"NOT SAVED ({type(e).__name__}). Stop the interview -- answers are "
                       f"being lost.")


def get_profile(client: str, edition: str = "established") -> dict:
    """Everything captured for one client, plus what is still missing.

    'unanswered' is as important as 'answered': it is what lets the assistant
    say "nobody ever asked about pricing" instead of inventing an answer.
    """
    client = (client or "").strip()
    if not client:
        return {"client": "", "answered": {}, "unanswered": ids_for(edition),
                "missing_required": required_for(edition), "reachable": False}
    if not crm.is_configured():
        return {"client": client, "answered": {}, "unanswered": ids_for(edition),
                "missing_required": required_for(edition), "reachable": False}
    try:
        tid = _ensure_table()
        safe = client.replace("'", "")
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(),
                      params={"filterByFormula": f"{{Client}}='{safe}'", "pageSize": "100"})
            r.raise_for_status()
        answered = {}
        for rec in r.json().get("records", []):
            f = rec.get("fields", {})
            qid, ans = f.get("Question ID", ""), f.get("Answer", "")
            if qid and ans:
                answered[qid] = ans  # later answers overwrite earlier: re-asking updates
        unanswered = [q for q in ids_for(edition) if q not in answered]
        return {"client": client, "answered": answered, "unanswered": unanswered,
                "missing_required": [q for q in required_for(edition) if q not in answered],
                "reachable": True}
    except Exception:
        log.exception("PROFILE_READ_FAIL client=%s", client)
        return {"client": client, "answered": {}, "unanswered": ids_for(edition),
                "missing_required": required_for(edition), "reachable": False}


def next_question(client: str, edition: str = "established") -> dict:
    """The next thing to ask. Required questions first, then the rest."""
    prof = get_profile(client, edition)
    if not prof["reachable"]:
        return {"done": False, "error": (
            "I can't reach the profile store, so I won't start asking -- answers would be "
            "lost as fast as you gave them.")}
    total = len(ids_for(edition))
    pending = ([q for q in required_for(edition) if q in prof["unanswered"]]
               or prof["unanswered"])
    if not pending:
        return {"done": True, "answered": len(prof["answered"]), "total": total}
    q = question_text(pending[0], edition)
    return {"done": False, "id": q["id"], "question": q["q"], "why": q.get("why", ""),
            "required": q["required"], "section": q.get("section", ""),
            "answered": len(prof["answered"]), "total": total,
            "remaining_required": len(prof["missing_required"])}


def recall(client: str, question: str = "", limit: int = 4) -> dict:
    """What the client actually SAID, for quoting back.

    V2 of the interview: capturing answers is worthless if nothing reads them.
    This is the retrieval half -- the same move that makes the knowledge base
    worth having. An answer that came out of this function is QUOTABLE; one
    generated around it is not.

    Returns {"found": [...], "never_asked": [...], "reachable": bool}. The
    never_asked list is as important as the hits: it is what lets the
    assistant say "nobody asked them about pricing" instead of inventing a
    plausible answer, which is the whole absent-is-not-zero rule.
    """
    prof = get_profile(client)
    if not prof["reachable"]:
        return {"found": [], "never_asked": [], "reachable": False}

    answered = prof["answered"]
    q = (question or "").strip().lower()
    terms = [t for t in q.split() if len(t) > 2]

    scored = []
    for qid, ans in answered.items():
        meta = question_text(qid) or {}
        hay = f"{qid} {meta.get('q','')} {meta.get('section','')} {ans}".lower()
        score = sum(1 for t in terms if t in hay) if terms else 1
        if q and score == 0:
            continue
        scored.append((score, {
            "question_id": qid,
            "question": meta.get("q", qid),
            "answer": ans,            # VERBATIM -- never summarised here
            "section": meta.get("section", ""),
        }))
    scored.sort(key=lambda x: -x[0])

    # Unanswered questions relevant to what was asked, so a gap is nameable.
    never = []
    for qid in prof["unanswered"]:
        meta = question_text(qid) or {}
        hay = f"{qid} {meta.get('q','')} {meta.get('section','')}".lower()
        if not terms or any(t in hay for t in terms):
            never.append({"question_id": qid, "question": meta.get("q", qid)})

    return {"found": [s[1] for s in scored[:max(1, limit)]],
            "never_asked": never[:limit], "reachable": True,
            "answered_count": len(answered)}


def build_persona(client: str, edition: str = "established") -> tuple:
    """Generate the client's assistant persona FROM THEIR OWN ANSWERS.

    V3, and the reason the interview is worth building at all. Phase 1 of every
    client build is "REWRITE agents.py for the client's business hats and
    public persona" -- by hand, from whatever was remembered. This composes it
    from captured facts instead.

    Returns (ok, text). REFUSES while any required answer is missing, because
    the required ones are the compliance boundaries: a persona generated
    without "what should it never say" is an assistant that will confidently
    say the illegal thing. That refusal is the whole safety property.

    Everything it emits is either a quoted answer or a [BRACKET] marking a gap.
    It never invents a fact to fill a hole.
    """
    prof = get_profile(client, edition)
    if not prof["reachable"]:
        return False, ("I couldn't reach the profile store, so I can't build a persona. "
                       "That's different from the interview being empty.")
    if not prof["answered"]:
        return False, (f"Nothing is captured for {client}. Run the interview first -- a "
                       f"persona built from no answers is a persona I invented.")
    missing = prof["missing_required"]
    if missing:
        names = ", ".join(missing)
        return False, (
            f"NOT SAFE to build {client}'s assistant yet. Missing required answers: "
            f"{names}.\n\nThe compliance ones matter most -- an assistant built without "
            f"'what should it never say' will confidently say it. Finish the interview "
            f"(client_interview action=next) and I'll build this properly."
        )

    a = prof["answered"]

    def q(qid, fallback=""):
        v = (a.get(qid) or "").strip()
        return v if v else (fallback or f"[NOT CAPTURED: {qid}]")

    lines = [
        f"# {q('legal_name')} — assistant persona",
        f"# Generated from the onboarding interview on file. Every line below is either "
        f"the client's own words or a [BRACKET] marking something nobody asked.",
        "",
        f'You are the AI assistant for {q("dba", q("legal_name"))}.',
        f'Legal entity: {q("legal_name")}. Use this on anything contractual or filed.',
        "",
        "WHAT THIS BUSINESS DOES (their words):",
        f'  {q("services_offered")}',
        "",
        "WHAT IT DOES NOT DO — turn these away politely, never book them:",
        f'  {q("explicitly_not_offered")}',
        f'  Also turn away: {q("turn_away")}',
        "",
        "⛔ NEVER SAY (non-negotiable, from the owner):",
        f'  {q("forbidden_language")}',
        "",
        "PRICING — quote ONLY what is written here, never a number of your own:",
        f'  {q("services_offered")}',
        f'  What changes the price: {q("price_drivers")}',
        f'  Needs eyes on it before quoting: {q("needs_eyes_on")}',
        "",
        "SERVICE AREA:",
        f'  {q("service_area")}',
        "",
        "HOURS AND AFTER-HOURS BEHAVIOUR:",
        f'  {q("hours_afterhours")}',
        "",
        "WHAT TO CAPTURE FROM EVERY CALLER:",
        f'  {q("must_capture")}',
        "",
        "ESCALATE IMMEDIATELY — do not run the intake questions first:",
        f'  What counts as urgent: {q("what_is_urgent")}',
        f'  Who to reach: {q("escalation_contact")}',
        "",
        "AUTONOMY:",
        f'  {q("autonomy")}',
        "",
        "HOW TO SOUND:",
        f'  {q("tone")}',
        f'  Disclosure: {q("disclose_ai")}',
        "",
        "THE QUESTIONS YOU WILL GET MOST, AND THE OWNER'S OWN ANSWERS:",
        f'  {q("common_questions")}',
        "",
        "WHEN SOMEONE SAYS A COMPETITOR IS CHEAPER:",
        f'  {q("objection_price")}',
        "",
        "WHY CUSTOMERS PICK THEM:",
        f'  {q("differentiator")}',
    ]
    gaps = [qid for qid in prof["unanswered"]]
    if gaps:
        lines += ["", "# NOT CAPTURED (say 'nobody asked them about that' rather than "
                      "guessing): " + ", ".join(gaps)]
    return True, "\n".join(lines)


def readiness(client: str, edition: str = "established") -> str:
    """Plain-language: is this profile safe to build an assistant from?"""
    prof = get_profile(client, edition)
    if not prof["reachable"]:
        return (f"I couldn't reach the profile store, so I can't tell you what's captured "
                f"for {client or 'this client'} -- that's different from it being empty.")
    if not prof["answered"]:
        return f"Nothing captured for {client} yet. The interview hasn't started."
    missing = prof["missing_required"]
    head = (f"{client} ({edition}): {len(prof['answered'])} of {len(ids_for(edition))} answered.")
    if missing:
        return (head + f"\n\nNOT READY to build an assistant from. Missing required: "
                + ", ".join(missing)
                + ".\nThe compliance ones matter most -- an assistant built without "
                  "'what you're not allowed to say' will confidently say it.")
    return head + "\n\nAll required questions answered. Safe to build from."
