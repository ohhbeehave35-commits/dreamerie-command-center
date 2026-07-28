"""
Skill Toolbox -- turns Annabelle's tool roster into human-readable capability
cards so users can DISCOVER what to say instead of having to guess trigger
phrases.

Cards are generated from agents.DELEGATION_TOOLS and enriched with a curated
overlay of friendly titles, groups, and example phrases. A tool with no
overlay entry still gets an auto-generated card, so new skills self-register
in the Toolbox the moment they're added to DELEGATION_TOOLS.

Consumed two ways:
- GET /api/toolbox           -> card JSON for the dashboard Toolbox panel
- list_capabilities tool     -> plain-text cards Annabelle reads to the user
"""

from . import agents

# tool name -> (group, friendly title, [example phrases users can say])
_OVERLAY = {
    # -- Sales ---------------------------------------------------------------
    "scout_prospects": ("Sales", "Lead Scouting", [
        "Draft tonight's TikTok caption for the candle drop",
        "Who should I target in Stuart for AI services?"]),
    "research_prospect": ("Sales", "Prospect Research", [
        "What events do we have coming up this month?, they're a pool builder",
        "Prep me for my call with ABC Plumbing tomorrow"]),
    "list_prospects": ("Sales", "Prospect Pipeline", [
        "Who are my prospects?", "Where are we with Louden?"]),
    "write_proposal": ("Sales", "Proposal Writer", [
        "Write a proposal for Dreamerie's Command Center"]),
    "write_opportunity_audit": ("Sales", "AI Opportunity Audit", [
        "Write an opportunity audit for Joe's HVAC"]),
    "ask_pricing_advisor": ("Sales", "Pricing Advisor", [
        "What does a Command Center build cost?"]),
    "send_proposal_docusign": ("Sales", "DocuSign Signature", [
        "Send the Louden proposal for signature"]),
    "push_lead_to_hubspot": ("Sales", "HubSpot Sync", [
        "Push this lead to HubSpot"]),
    "log_lead": ("Sales", "Log a Lead", [
        "Log a lead: Jane Smith, 772-555-0199, wants a quote"]),
    "find_leads": ("Sales", "Find Leads", [
        "Show me this week's leads"]),
    # -- Research ------------------------------------------------------------
    "scrape_page": ("Research", "Read Any Web Page", [
        "Read stluciepoolandspa.com and tell me what they offer",
        "Does this prospect's site have online booking?"]),
    "run_seo_audit": ("Research", "SEO Audit", [
        "Run an SEO audit on stluciepoolandspa.com",
        "Give me an SEO report for this prospect's website"]),
    # -- Money ---------------------------------------------------------------
    "create_stripe_payment_link": ("Money", "Payment Link", [
        "Make a Stripe payment link for $500"]),
    "create_stripe_invoice": ("Money", "Send an Invoice", [
        "Invoice Daryl $2,997 for the build"]),
    "list_stripe_invoices": ("Money", "Invoice Status", [
        "Which invoices are still unpaid?"]),
    "predict_video_cost": ("Money", "Video Cost Forecast", [
        "What would a 30-second promo video cost to generate?"]),
    # -- Content & Media -----------------------------------------------------
    "draft_email": ("Content & Media", "Email Drafts", [
        "Draft a follow-up email to Daryl about the proposal"]),
    "send_email": ("Content & Media", "Send Email", [
        "Send that email"]),
    "draft_social_post": ("Content & Media", "Social Posts", [
        "Draft an Instagram post about swarm season"]),
    "publish_social_post": ("Content & Media", "Publish Social", [
        "Publish that post to Facebook"]),
    "write_long_form_content": ("Content & Media", "Long-Form Content", [
        "Write a blog post about bee removal myths"]),
    "generate_image": ("Content & Media", "AI Images", [
        "Generate an image of our bee mascot in a hard hat"]),
    "generate_video": ("Content & Media", "AI Video", [
        "Make a short video of honey dripping over the logo"]),
    # -- Scheduling ----------------------------------------------------------
    "check_availability": ("Scheduling", "Calendar Check", [
        "Am I free Thursday afternoon?"]),
    "create_removal_event": ("Scheduling", "Book a Removal", [
        "Book a removal Friday at 9am at 123 Oak St"]),
    "create_inspection_event": ("Scheduling", "Book an Inspection", [
        "Schedule an inspection Tuesday morning"]),
    # -- Files ---------------------------------------------------------------
    "search_dropbox": ("Files", "Dropbox Search", [
        "Find the Louden proposal in Dropbox"]),
    "list_dropbox_folder": ("Files", "Dropbox Browse", [
        "What's in my Business Records folder?"]),
    "search_drive": ("Files", "Google Drive Search", [
        "Search Drive for the pricing sheet"]),
    "save_asset": ("Files", "Asset Library", [
        "Save this image to the asset library"]),
    "find_assets": ("Files", "Find Assets", [
        "Find the logo files in the asset library"]),
    # -- Team & Ops ----------------------------------------------------------
    "run_diagnostic": ("Team & Ops", "System Diagnostic", [
        "Run a diagnostic", "Is everything green?"]),
    "log_build_request": ("Team & Ops", "Build Request", [
        "Log a build request: I want voice notes on mobile"]),
    "log_skill_note": ("Team & Ops", "Skill Note", [
        "Remember this for later: Instagram posts always need a photo"]),
    "get_buildertrend_jobs": ("Team & Ops", "Buildertrend Jobs", [
        "What's on the Buildertrend board?"]),
    "flag_for_review": ("Team & Ops", "Flag for Review", [
        "Flag this conversation for Vinny to look at"]),
    "set_speaker": ("Team & Ops", "Speaker Tagging", [
        "This is Jane speaking now"]),
    # -- Bee Services --------------------------------------------------------
    "ask_ohh_beehave_agent": ("Bee Services", "Ohh Beehave Specialist", [
        "How much is a swarm removal?", "Do you remove wasps?"]),
    "ask_deep_removal_specialist": ("Bee Services", "Deep Removal Expert", [
        "Bees are inside my wall -- what happens next?"]),
}

_GROUP_ORDER = ["Sales", "Research", "Money", "Content & Media", "Scheduling",
                "Files", "Team & Ops", "Bee Services", "More"]


def _auto_title(name: str) -> str:
    return name.replace("_", " ").title()


def _short_desc(tool: dict) -> str:
    desc = (tool.get("description") or "").replace("OWNER-ONLY. ", "")
    if len(desc) <= 220:
        return desc
    return desc[:217].rsplit(" ", 1)[0] + "..."


def get_cards(tool_names=None, topic: str = "") -> list:
    """Capability cards, optionally restricted to a set of tool names (the
    current mode's allowlist) and/or filtered by a topic keyword."""
    topic = (topic or "").strip().lower()
    cards = []
    for tool in agents.DELEGATION_TOOLS:
        name = tool["name"]
        if tool_names is not None and name not in tool_names:
            continue
        group, title, examples = _OVERLAY.get(name, ("More", _auto_title(name), []))
        desc = _short_desc(tool)
        if topic and topic not in f"{name} {title} {group} {desc}".lower():
            continue
        cards.append({"tool": name, "title": title, "group": group,
                      "description": desc, "examples": examples})
    cards.sort(key=lambda c: (
        _GROUP_ORDER.index(c["group"]) if c["group"] in _GROUP_ORDER else 99,
        c["title"],
    ))
    return cards


def render_text(tool_names=None, topic: str = "") -> str:
    """Plain-text Toolbox for the list_capabilities tool -- grouped cards with
    an example phrase per card, ready for Annabelle to relay conversationally."""
    cards = get_cards(tool_names, topic)
    if not cards:
        return ("No capabilities matched that topic. Try without a filter -- "
                "or this might be a build request worth logging.")
    out, last_group = [], None
    for c in cards:
        if c["group"] != last_group:
            out.append(f"\n== {c['group']} ==")
            last_group = c["group"]
        line = f"- {c['title']}: {c['description']}"
        if c["examples"]:
            line += f'  Say: "{c["examples"][0]}"'
        out.append(line)
    return "\n".join(out).strip()
