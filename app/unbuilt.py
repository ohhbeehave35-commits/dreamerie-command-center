"""
The unbuilt tools -- one honest refusal for each, instead of "Unknown tool".

WHY THIS EXISTS
59 tools were advertised to the model with no handler. Left alone they hit
`answer = f"Unknown tool: {block.name}"`, which tells a client nothing and
tells Annabelle even less -- she'd try the tool, get a shrug, and often
improvise something that sounded like it worked.

Each entry here answers two questions plainly:
  needs   -- exactly what account, key, or integration it would take to build.
             Not "coming soon"; the real requirement, so a build decision is
             an informed one.
  instead -- a tool that ALREADY WORKS and does most of the job, when one
             exists. This is the difference between "I can't" and "I can't,
             but here's the thing that does."

The rule is honesty in both directions: never claim it works, and never say
"impossible" when a real path exists. A tool with `instead` is a redirect, not
a dead end.

Kept as data, dispatched through one branch, so adding the 60th unbuilt tool
is one line -- and the coverage guard's KNOWN_UNBUILT list is what keeps this
table honest: a tool that gets truly built must leave both.
"""

# needs: the real prerequisite. instead: a working tool that covers most of it.
REGISTRY = {
    # ---- Ad platforms: every one needs a live, funded ad account + API access.
    "adspirer_ad_performance": ("a connected Google/Meta/Amazon Ads account (Adspirer or the platform APIs)", ""),
    "adspirer_audience_analysis": ("a connected ad account with audience data", ""),
    "adspirer_budget_allocation": ("connected ad accounts to move budget between", ""),
    "adspirer_campaign_optimization": ("a live ad account with running campaigns", ""),
    "adspirer_search_terms_analysis": ("a connected Google Ads account", ""),
    "adspirer_wasted_spend_google": ("a connected Google Ads account", ""),
    "adspirer_wasted_spend_linkedin": ("a connected LinkedIn Ads account", ""),
    "adspirer_wasted_spend_meta": ("a connected Meta (Facebook/Instagram) Ads account", ""),

    # ---- PDF: reading and creating already ship under different names.
    "pdf_extract_text": ("nothing -- this is already possible", "read_link (paste the PDF's URL) or just attach the PDF in chat; I read it natively"),
    "pdf_search": ("nothing -- reading already covers this", "read_link or attach the PDF and ask what you're looking for"),
    "pdf_create_new": ("nothing -- document creation already works", "write_proposal, write_long_form_content, or legal_terms_privacy -- they create documents you can open and save as PDF"),
    "pdf_modify_existing": ("a PDF-editing library not installed here (fpdf/pypdf); editing an existing binary PDF is real work", "recreate the document with write_long_form_content and edit that"),
    "pdf_merge": ("a PDF library (pypdf) that isn't installed", ""),
    "pdf_annotate": ("a PDF-rendering library that isn't installed", ""),
    "pdf_fill_form": ("a PDF form library (pypdf) plus the form's field names", ""),
    "pdf_display_viewer": ("an interactive PDF viewer UI that doesn't exist in this app", "read_link or attach it and I'll tell you what it says"),

    # ---- Twilio: account exists, but these are beyond SMS.
    "twilio_make_call": ("Twilio Voice set up with TwiML/a call flow -- the account has SMS only right now", ""),
    "twilio_send_whatsapp": ("a Twilio WhatsApp sender, which needs Meta business verification", "send_sms for a text (once the A2P campaign is approved)"),
    "twilio_create_ivr": ("Twilio Voice + a Studio/TwiML phone-tree flow, none of which is set up", ""),
    "twilio_get_call_recording": ("Twilio Voice recording, which isn't enabled (no voice on the account)", ""),
    "twilio_sms_shortcode": ("a leased short code -- a months-long, four-figure carrier process", "send_sms uses the existing 10-digit number"),
    "twilio_verify_otp": ("the Twilio Verify service enabled on the account", ""),

    # ---- Whole vendors with no account here.
    "nimble_analytics": ("a Nimble CRM account", "the built-in CRM -- find_leads, list_prospects"),
    "nimble_contact_management": ("a Nimble CRM account", "log_lead and find_leads store and look up contacts here already"),
    "nimble_interaction_history": ("a Nimble CRM account", "twilio_get_sms_history for texts; the CRM notes for the rest"),
    "nimble_sales_pipeline": ("a Nimble CRM account", "list_prospects -- the prospects pipeline is built in"),
    "nimble_team_collaboration": ("a Nimble CRM account with a team", ""),

    "sanity_publish_content": ("a Sanity CMS project + token", "publish_social_post (via Zapier) for social, or create_landing_page for a public page"),
    "sanity_query_content": ("a Sanity CMS project", ""),
    "sanity_manage_assets": ("a Sanity CMS project", "the asset library -- save_asset, find_assets"),
    "sanity_schedule_publish": ("a Sanity CMS project", ""),
    "sanity_content_analytics": ("a Sanity CMS project with analytics", ""),

    "zapier_automation": ("a Zapier account connected with an API key", "publish_social_post already routes through a Zapier webhook"),
    "zapier_create_workflow": ("Zapier's API, which doesn't let you build Zaps programmatically -- they're made in Zapier's own UI", ""),
    "zapier_list_workflows": ("Zapier API access", ""),
    "zapier_test_workflow": ("Zapier API access", ""),
    "zapier_pause_resume": ("Zapier API access", ""),

    "design_collaboration": ("a design tool account (Figma/Canva)", ""),
    "design_feedback_management": ("a connected design tool", ""),
    "design_prototyping": ("a design/prototyping tool account", ""),
    "design_system_management": ("a design-system tool", ""),
    "figma_access": ("a Figma account + access token", ""),

    "legal_contract_management": ("a contract store; the DocuSign signing path exists but full lifecycle tracking isn't built", "legal_terms_privacy drafts documents; send_proposal_docusign sends for signature"),
    "legal_compliance_tracking": ("a compliance-deadline store and a scheduler to fire reminders", "log a task/build request and the daily digest will surface it"),
    "legal_entity_management": ("an entity/filings register that isn't built", ""),

    "linear_create_issue": ("a Linear account + API key", "log_build_request queues work for the dev team here"),
    "linear_list_issues": ("a Linear account + API key", "the Pending panel shows queued build requests"),
    "asana_task_management": ("an Asana account + token", "log a task; the daily digest surfaces open ones"),
    "notion_workspace_access": ("a Notion integration token", "the knowledge base and artifacts store documents here"),

    "slack_notify": ("a Slack workspace webhook or bot token", "phone push notifications are wired here (push.py) for owner alerts"),
    "engineering_debug": ("a connected error/monitoring source", "run_diagnostic checks this app's own health; check_app_health_log reads recent faults"),
    "honeycomb_investigate": ("a Honeycomb account + API key", "check_app_health_log for this app's own faults"),

    "linkedin_research": ("LinkedIn's API, which is heavily gated and effectively closed for this", "research_prospect and scout_prospects do web-based prospect research"),
    "lusha_prospect_research": ("a Lusha account + API key (paid contact-data provider)", "research_prospect via web search -- no private contact data, but real company research"),
    "sales_intelligence": ("a firmographics provider (ZoomInfo/Apollo/Clearbit), all paid", "research_prospect for web-sourced company research"),
    "brand_voice_discover": ("connected platforms (Drive/Dropbox/Slack) to search -- none are wired for content search", "brand_voice_enforce applies the brand voice you've already defined"),

    "quickbooks_accounting": ("a QuickBooks Online account + OAuth", "stripe_dashboard for payments; expense_tracking for a simple ledger"),
    "freshbooks_invoicing": ("a FreshBooks account + token", "create_stripe_invoice and list_stripe_invoices already do invoicing"),
    "tax_compliance": ("a tax data source and, honestly, an accountant -- not something to automate blind", "expense_tracking keeps the records an accountant would need"),
}


def refuse(tool_name: str) -> str:
    """The honest answer for an unbuilt tool. Falls back gracefully if a tool
    somehow reaches here without a registry entry -- better a plain 'not built'
    than a crash."""
    entry = REGISTRY.get(tool_name)
    if not entry:
        return (f"'{tool_name}' isn't built yet -- I was offered it but there's nothing "
                f"behind it. I'm telling you rather than pretending I did it.")
    needs, instead = entry
    if needs.startswith("nothing"):
        # It IS doable, just under another name -- lead with the redirect.
        return (f"You don't need '{tool_name}' -- {instead}." if instead
                else f"That's already possible another way; ask me to do it directly.")
    msg = f"I can't do '{tool_name}' yet: it needs {needs}."
    if instead:
        msg += f"\n\nWhat I CAN do right now: {instead}."
    else:
        msg += ("\n\nNothing here covers it without that, so I'm not going to fake it. "
                "It's a real build decision -- say the word and it gets queued.")
    return msg
