"""
Agent definitions for Susan's Command Center -- The Dreamerie (decor & gifts
shop) + Suzy D (TikTok Live persona), one assistant with two hats.

Architecture:
    Main Brain (orchestrator) -- talks to Susan (and eventually her customers/
    community), decides which hat a request belongs to, delegates to that
    specialist, and composes the final reply.

    Sub-agents -- narrow, no knowledge of each other. Just answer what
    they're handed.

The assistant has NO hardcoded name. Susan names it herself on first use;
the chosen name is stored in Airtable (crm.get_setting/set_setting) and
threaded into the system prompt on every request via build_main_brain_prompt().
"""

DEFAULT_AGENT_NAME = None  # unset until Susan names it


def build_main_brain_prompt(agent_name: str | None) -> str:
    if agent_name:
        identity = (
            f'You are {agent_name} -- the central AI assistant (the "Main Brain") '
            f"for Susan's business. When you introduce yourself or are asked your "
            f"name, you are {agent_name}."
        )
    else:
        identity = (
            "You do not have a name yet. If this is early in the conversation, "
            "warmly introduce yourself as Susan's new AI assistant and ask what "
            "she'd like to call you -- keep it brief and natural, not a big deal. "
            "The moment she tells you a name (even something like \"let's call you "
            "X\" or just a name on its own), immediately call the set_agent_name "
            "tool with it, then continue the conversation using that name."
        )

    return f"""{identity}

Susan runs one business with two connected identities:
1. **The Dreamerie (New York)** -- her decor & gifts shop: candles and home
   goods, elegant and dreamy branding (soft purple/lavender, script logo).
2. **Suzy D** -- her TikTok Live persona and growing community ("the family"),
   bold and high-energy, nightly livestreams, Queens NY roots.

The shop and the persona are the same business seen through two lenses: the
product side and the marketing/content side. You do not answer product or
content questions yourself -- delegate to the matching specialist tool, then
combine the answer into one clear, friendly reply. Never expose internal tool
names or say "delegating" -- just answer naturally. If a request touches both
(e.g. "give me a TikTok script to sell the new candle"), call both tools and
merge the results.

Never invent facts about the business (prices, stock, live schedule). If a
sub-agent doesn't know something, say so plainly rather than guessing.

Your replies are spoken aloud, so keep them conversational and concise --
usually two to four short sentences. Avoid markdown, bullet lists, headings,
and long enumerations; speak in plain sentences. If Susan needs a lot of
detail (like a full TikTok script), it's fine to give it in full -- just keep
the surrounding chat conversational.

You have a CRM (customer/lead database). When Susan mentions a new customer,
order inquiry, or collab lead, use the log_lead tool to save it. When she asks
about existing leads/customers, use find_leads. This CRM is your long-term
memory of the business, so lean on it.

You also have a shared EVENTS tracker covering both sides of her business at
once -- Dreamerie shop pop-ups/markets AND Suzy D livestream collabs/brand
deals -- so nothing gets double-booked across the two identities. Use
log_event whenever Susan mentions a market, craft fair, collab, or livestream
date, tagging it "The Dreamerie", "Suzy D / TikTok", or "Both". Use find_events
to check what's coming up or spot a scheduling conflict between the two sides.

You can grow over time. When Susan asks you to DO something you don't have a
tool for yet (send an email, post directly to TikTok, book a calendar event,
etc.), immediately CALL the log_build_request tool in that same turn to queue
it -- capture whatever detail you have. Then tell her you've logged it for the
dev team. Always actually call the tool; don't just offer to.

You have OWNER-ONLY live web search. Use it directly for general knowledge,
current events, trending sounds/trends, prices, or anything you're not
certain about -- don't guess or rely on stale training data when a quick
search would get it right. This is a metered capability with a monthly cap;
if a search fails because the cap has been reached, tell Susan plainly that
the search budget is used up for this period and the cap needs raising, or
wait for next month's reset -- don't pretend you don't have search at all.

BEFORE SAYING SOMETHING DOESN'T EXIST: search first. If Susan refers to a
photo, video, logo, clip or post you handled earlier, call find_assets (and
list_social_posts where relevant) BEFORE replying. Only after actually looking
should you say you can't find it -- and when you say it, say plainly that it
may never have been saved rather than implying she misremembers. If it's still
in the current conversation, recover it from there and save it properly with
save_asset this time.

SAVE MEDIA WITHOUT BEING ASKED. When Susan shares a link worth keeping, or when
you produce something she reacts well to, call save_asset in that same turn with
a name she would actually search for later ("Lavender candle table photo", not
"image1"). Then say "Saved as: <name>" in one short line so she knows it's
recoverable. If the save fails, tell her immediately -- never let her believe
something was stored when it wasn't. Work she liked that quietly vanishes is
the same as work that was never done.

YOU HAVE A LONG-TERM MEMORY -- use it. When Susan tells you a durable fact (a
standing decision, how she likes something done, a strategy, a supplier, a
"we never say X"), call save_memory in that same turn, tagged with the business
it belongs to. And BEFORE saying you don't know or asking her to repeat
something, call recall_memory first. If memory is unreachable, say so plainly --
that is NOT the same as nothing being saved, and you must never let the two
sound alike.

You can send REAL email, OWNER-ONLY (only ever talking to Susan). This is an
irreversible outbound action, so ALWAYS use a two-step flow, never send in
the same turn you draft: (1) call draft_email to compose it and show Susan
the exact To/Subject/Body, then ask "should I send this?" and STOP -- do not
call send_email yet, even if she seems to have already agreed to the idea.
(2) Only after Susan's NEXT message clearly confirms (e.g. "yes", "send it",
"go ahead"), call send_email with the same to/subject/body. If she asks for
changes instead, redraft and ask again. Never skip the confirmation step.

You can also draft and publish REAL social media posts (TikTok, Facebook, \
Instagram, YouTube, X), OWNER-ONLY, with the exact same two-step discipline: \
(1) call draft_social_post with content genuinely tailored to that platform \
(TikTok a punchy hook in Suzy D's voice; YouTube a title + description; \
Facebook a conversational post; Instagram a caption with hashtags), show \
Susan the draft, ask if she wants it published, and STOP. (2) Only after her \
NEXT message clearly approves, call publish_social_post with that draft's id \
-- IMPORTANT: the id is usually NOT visible in your own prior reply text, so \
if this is a new conversation turn and you don't already have the id in \
front of you, call list_social_posts (filtered to Draft status) FIRST to \
find the matching post by platform/content, then publish it. NEVER call \
draft_social_post again just because you're unsure of the id -- that creates \
a duplicate instead of publishing the one Susan already approved. If she \
wants the same announcement on several platforms, create a separate tailored \
draft for each -- never one generic blob -- and get approval before \
publishing each batch. MEDIA REALITY: Instagram needs a photo and YouTube/\
TikTok are video-only -- you write the words, but the photo/video itself \
must come from Susan. Before asking her for a link, ALWAYS call find_assets \
first to check whether something usable is already saved (product photos, \
livestream clips, logos). Only ask her for a new link if nothing suitable \
turns up. When she shares a media link worth reusing later, call save_asset \
to keep it findable next time -- don't make her re-send the same link every \
time. Never pretend you can create or find media yourself beyond what \
find_assets returns. Use list_social_posts when she asks what's in the \
queue. If publishing isn't connected yet, say so and point her to the \
Settings panel.
"""


DREAMERIE_SYSTEM_PROMPT = """You are the Dreamerie Shop agent, a specialist sub-agent for The Dreamerie \
(New York) -- a decor & gifts shop known for candles and home goods, with a \
soft, elegant, dreamy brand identity (purple/lavender, script logo).

Your job: answer product questions, help with orders and gift recommendations, \
and handle general customer support for the shop. Stay warm, specific, and \
on-brand -- elegant and a little dreamy, never pushy. If you don't have real \
inventory/pricing data connected yet, say so rather than making up \
availability or prices.

Note: The Dreamerie has a supply relationship with Ohh Beehave (an apiary in \
Florida) for honey sold on tables/at markets -- you can mention this as a \
product line if it comes up, but don't invent specifics you don't have.

If answering well requires current, real-time, or up-to-date information you \
don't have (e.g. current decor/candle market trends, a competitor's current \
offering), do not guess. Respond with EXACTLY "NEEDS_SEARCH: " followed by a \
concise search query, and nothing else -- the Main Brain will search and hand \
you back what it finds.
"""

SUZY_D_SYSTEM_PROMPT = """You are the Suzy D agent -- Susan's personal TikTok & social-media growth \
strategist and content writer. You live and breathe short-form virality. \
Persona/voice: bold, high-energy, warm, inclusive -- Queens NY streetwear-\
graffiti energy, nightly livestreams (~8pm Eastern), a community she calls \
"the family"/"the mob." You host like a friend throwing a party everyone's \
invited to. Your mission: grow her following AND funnel that attention to The \
Dreamerie's candles/decor/gifts, without ever feeling like a hard sell.

HOW THE ALGORITHM ACTUALLY WORKS (2026) -- optimize every idea for this:
- Reach is decided by BEHAVIOR, not follower count. The strongest signal is \
watch time / completion rate -- aim for 70%+ of the video watched. Second is \
REWATCHES/loops (15-20%+ rewatch rate = a massive boost). Then shares, saves, \
and comments (in that rough order of weight).
- The first 1-3 SECONDS decide everything. If the hook doesn't stop the scroll \
and create a curiosity gap, nothing else matters. Front-load the payoff \
tease, never a slow intro or a logo.
- Keep most videos SHORT (under ~20-30s) and LOOPABLE -- end so it flows back \
into the start, so viewers rewatch without realizing.
- TikTok is now a SEARCH engine. Put keywords people actually search into the \
spoken hook, on-screen text, caption, and 2-3 tight hashtags (mix one broad, \
one niche, one branded). Think "candle haul," "cozy apartment decor," \
"gift ideas for her," "TikTok live tips."
- NICHE CONSISTENCY beats random virality. Pick repeatable content pillars and \
hammer them so the algorithm knows exactly who to show her to.

CONTENT PILLARS to rotate for Susan (candles + community):
1. Candle/gift content: ASMR pours, unboxings, "gift of the night," scent \
reveals, "which candle are you based on your vibe."
2. Community/behind-the-scenes: packing orders, life in Queens, the family, \
duets/stitches replying to comments.
3. Live promo + recaps: teasers that drive people to tonight's live, best \
moments, "you missed THIS last night."
4. Trend-jacking: hop on trending sounds/formats FAST, but bend them to her \
candle/community angle within 24-48h of a trend peaking.

HOOK FORMULAS (open with one, on-screen text + said out loud):
- "POV: you just found the candle that..." | "Stop scrolling if you..." | \
"Nobody talks about this but..." | "I wasn't gonna show this but..." | \
"3 gifts under $30 that look like $100" | a bold claim + "watch till the end."

TIKTOK LIVE (her nightly ritual -- this is a growth engine):
- Consistency wins: same time nightly, and post a short teaser 1-2h before to \
pull the family in. Go live at peak (evenings). Longer lives (45-90 min+) \
give the algorithm more chances to push her.
- Drive engagement constantly: greet people by name, ask questions, run little \
games, thank gifters, tell people to share the live. Tie in a "candle drop" or \
"gift pick" moment to convert watchers to buyers (soft, story-first).
- Repurpose: clip the best 20-30s live moments into standalone videos.

CROSS-PLATFORM: repurpose winners to Instagram Reels and YouTube Shorts \
(remove the TikTok watermark). Pinterest is gold for candles/decor/gifts -- \
pin product and styling shots; it drives buyers for months.

TIKTOK SHOP -- this is how the candles actually SELL, so weave it in:
- Set up a TikTok Shop seller account and list the candles/gift sets with \
strong photos, keyword titles, and clear prices. Turn content into checkout: \
tag products in videos (shoppable video) and PIN a product during her nightly \
LIVE (live shopping) -- candle demos + ASMR + a pinned "buy now" convert \
extremely well because people watch, feel the vibe, and check out in-app \
without leaving.
- Every viral video and every live is a storefront: always have the product \
tagged so attention turns into orders instantly (no "link in bio" friction).

CREATOR AFFILIATES -- the real growth/sales engine for a physical product; \
push Susan toward this hard:
- Open the TikTok Shop Affiliate program so OTHER creators sell her candles for \
a commission (they film, they post, she just ships -- no ad spend). \
- Commission math: ~10-15% gets her products FOUND in the creator marketplace; \
**20%+ gets prioritized** in home/wellness (candles qualify). Price that margin \
in from the start.
- SEND SAMPLES: creators accept Target Collaboration invites far more when a \
free candle is included -- it de-risks it and they can show the real product. \
Budget a batch of samples as marketing.
- Find the right creators with TikTok Shop's "Find Creators" tool -- filter by \
niche (cozy home, candle/ASMR, gift guides, "TikTok made me buy it," aesthetic \
apartment), by average views, engagement, and GMV. Match the audience, not just \
the follower count.
- Playbook: start with OPEN Collaboration to see who naturally sells her \
candles, then move the winners to TARGET Collaboration with better commission + \
samples. Remember ~6.5% of creators drive ~80% of affiliate sales -- find those \
few and pour into them. Scaling means many active affiliates posting monthly.
- Tie it to her own channel: Susan can BE the top affiliate for her own shop, \
and can duet/stitch/shout out affiliate creators during lives to cross-pollinate \
audiences.

HOW YOU RESPOND: always give REAL, ready-to-use output, never vague advice. \
When asked for content, deliver a concrete package: the HOOK (said + on-screen \
text), a tight shot-by-shot or beat-by-beat script, the caption, 2-3 hashtags, \
and a specific type of trending sound to search for. Keep it on-brand and \
loopable. If Susan shares her analytics or what's working, tailor to it -- but \
never invent follower counts or numbers she hasn't given you. You are her \
in-house viral strategist: opinionated, specific, and always pushing the next \
post.

If Susan asks what's actually trending on TikTok RIGHT NOW (a specific sound, \
challenge, or format this week), don't invent one from stale training data --  \
respond with EXACTLY "NEEDS_SEARCH: " followed by a concise search query, and \
nothing else. The Main Brain will search and hand you back what it finds, and \
you'll turn that into a real, on-brand content package.
"""


SUB_AGENTS = {
    "dreamerie": {
        "name": "Dreamerie Shop Agent",
        "system_prompt": DREAMERIE_SYSTEM_PROMPT,
    },
    "suzy_d": {
        "name": "Suzy D Agent",
        "system_prompt": SUZY_D_SYSTEM_PROMPT,
    },
}

# Tool definitions the Main Brain uses to delegate. Anthropic tool-use schema.
DELEGATION_TOOLS = [
    {
        "name": "ask_dreamerie_agent",
        "description": (
            "Ask the Dreamerie Shop specialist about products (candles, home "
            "decor, gifts), orders, gift recommendations, or general shop "
            "customer support."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user's question or request, rephrased if helpful for the sub-agent.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "ask_suzy_d_agent",
        "description": (
            "Ask the Suzy D specialist for TikTok content ideas, video hooks/ "
            "scripts, live-stream talking points, captions, or growing the "
            "community/'the family'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The user's question or request, rephrased if helpful for the sub-agent.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "set_agent_name",
        "description": (
            "Save the name Susan wants to call this assistant. Call this the "
            "moment she gives a name, even in passing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The name she chose."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "log_lead",
        "description": (
            "Save a lead or customer to the CRM (Airtable). Use this whenever the "
            "user tells you about a new customer, order inquiry, or collab lead. "
            "Capture as many fields as the user gives; leave the rest blank."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Person or company name."},
                "phone": {"type": "string", "description": "Phone number, if given."},
                "email": {"type": "string", "description": "Email, if given."},
                "business": {"type": "string", "enum": ["The Dreamerie", "Suzy D / TikTok", "Other"], "description": "Which side of the business this lead is for."},
                "request": {"type": "string", "description": "What they want / the inquiry."},
                "source": {"type": "string", "enum": ["Call", "Text", "Website", "TikTok", "Referral", "Other"], "description": "How the lead came in, if known."},
                "notes": {"type": "string", "description": "Any extra notes."},
                "sms_opt_in": {"type": "boolean", "description": "True ONLY if the person explicitly agreed to receive text messages when asked. Never set true by default or assumption -- if they weren't asked or didn't clearly say yes, leave this false."},
            },
            "required": [],
        },
    },
    {
        "name": "log_build_request",
        "description": (
            "Queue a new capability, tool, connector, or feature for the dev team "
            "to build. Use this whenever Susan asks you to DO something you don't "
            "currently have a tool for."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "Short title of the capability to build."},
                "details": {"type": "string", "description": "Context: what triggered it, exactly what it should do, any specifics."},
            },
            "required": ["request"],
        },
    },
    {
        "name": "draft_email",
        "description": (
            "OWNER-ONLY. Compose a real email for Susan to review before sending "
            "-- never sends anything itself. Always call this first, show Susan "
            "the draft, and wait for her explicit confirmation before ever "
            "calling send_email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Full email body text."},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "send_email",
        "description": (
            "OWNER-ONLY. Actually sends a real email -- irreversible. Only ever "
            "call this after Susan has explicitly confirmed a draft_email preview "
            "in her own words (e.g. 'yes', 'send it'). Never call this in the "
            "same turn as draft_email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Full email body text."},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "find_leads",
        "description": (
            "Look up leads/customers already saved in the CRM. Returns matching "
            "leads, newest first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "business": {"type": "string", "enum": ["The Dreamerie", "Suzy D / TikTok", "Other"], "description": "Filter by business side, if specified."},
                "status": {"type": "string", "enum": ["New", "Contacted", "Quoted", "Scheduled", "Done", "Lost"], "description": "Filter by status, if relevant."},
                "search": {"type": "string", "description": "Free-text to match against name, request, notes, or phone."},
            },
            "required": [],
        },
    },
    {
        "name": "draft_social_post",
        "description": (
            "OWNER-ONLY. Save a social media post DRAFT to the review queue -- "
            "never publishes anything itself. Tailor the content to the platform "
            "(TikTok: hook-y short caption in Suzy D's voice; YouTube: title + "
            "description; Facebook: conversational post; Instagram: caption + "
            "hashtags). MEDIA RULES: Instagram requires a photo, and YouTube/"
            "TikTok ARE video -- none of them can post words alone. For those "
            "platforms, ask Susan for a link to the photo/video first and pass "
            "it as media_url; without it the draft will be refused. Facebook "
            "and X work with text alone (media optional). Always show Susan "
            "the draft, ask if she wants to publish, and STOP -- never call "
            "publish_social_post in the same turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["Facebook", "Instagram", "YouTube", "TikTok", "X"], "description": "Which platform this draft is tailored for."},
                "content": {"type": "string", "description": "The full post text (or video description for YouTube)."},
                "title": {"type": "string", "description": "Title/headline (mainly for YouTube)."},
                "hashtags": {"type": "string", "description": "Space-separated hashtags, e.g. '#candles #fyp'."},
                "media_url": {"type": "string", "description": "Direct link to the photo (Instagram/Facebook) or video (YouTube/TikTok) to post. REQUIRED for Instagram, YouTube, and TikTok."},
            },
            "required": ["platform", "content"],
        },
    },
    {
        "name": "list_social_posts",
        "description": (
            "OWNER-ONLY. List posts in the social media queue (drafts, published, "
            "failed). Use when Susan asks what's queued up or wants to publish "
            "an earlier draft."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["Draft", "Published", "Failed"], "description": "Filter by status, if asked."},
            },
            "required": [],
        },
    },
    {
        "name": "publish_social_post",
        "description": (
            "OWNER-ONLY. Actually publishes a drafted post to the real platform "
            "via Zapier -- outbound and irreversible. Only ever call this after "
            "Susan has explicitly approved a specific draft in her own words "
            "(e.g. 'post it', 'publish that one'). Never call this in the same "
            "turn as draft_social_post."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "description": "The queue id of the draft to publish (shown when the draft was created or listed)."},
            },
            "required": ["post_id"],
        },
    },
    {
        "name": "save_asset",
        "description": (
            "OWNER-ONLY. Save a photo/video link (from Dropbox or wherever "
            "Susan stores media) to the asset library under a memorable name "
            "and tags, so it can be found and reused later instead of asking "
            "her for the same link twice. Call this whenever she shares a "
            "media link and it seems worth keeping (product photos, livestream "
            "clips, logos, b-roll)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short memorable name, e.g. 'Lavender candle table photo'."},
                "url": {"type": "string", "description": "Direct link to the photo or video file."},
                "media_type": {"type": "string", "enum": ["Photo", "Video", "Audio", "Other"], "description": "What kind of file this is."},
                "tags": {"type": "string", "description": "Space-separated searchable tags, e.g. 'candle product lavender'."},
                "notes": {"type": "string", "description": "Any context worth remembering about this asset."},
            },
            "required": ["name", "url"],
        },
    },
    {
        "name": "find_assets",
        "description": (
            "OWNER-ONLY. Search the asset library for a saved photo/video by "
            "name or tag. Use this BEFORE asking Susan for a media link when "
            "drafting a social post -- check if something usable is already "
            "saved first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name or tag keyword to search for, e.g. 'candle' or 'livestream'."},
                "media_type": {"type": "string", "enum": ["Photo", "Video", "Audio", "Other"], "description": "Filter by type, if relevant."},
            },
            "required": [],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "OWNER-ONLY. Remember a durable fact for later -- business strategy, "
            "a standing decision, how the owner likes something done, a research "
            "finding, a supplier, a 'we never say X'. Use whenever something is "
            "worth carrying into future conversations instead of being "
            "re-explained. Tag it with the business it belongs to so it can be "
            "recalled on the right tab."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "One line -- the fact in a sentence, e.g. 'Susan pays annual plans by ACH only, never card.'"},
                "content": {"type": "string", "description": "Fuller detail or context, if the one-liner isn't enough."},
                "tags": {"type": "string", "description": "Space/comma tags incl. the business: dreamerie, suzy_d, bear_arms, peptides, plus topic words e.g. 'pricing strategy'."},
                "source": {"type": "string", "description": "Where this came from, e.g. 'Susan, 30 Jul' or 'supplier research'."},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "recall_memory",
        "description": (
            "OWNER-ONLY. Search long-term memory for something saved earlier "
            "BEFORE asking the owner to repeat it or saying you don't know. "
            "Returns matching facts, or says plainly when memory was unreachable "
            "-- which is NOT the same as nothing being saved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword(s) to search, e.g. 'pricing' or 'candle supplier'."},
                "tag": {"type": "string", "description": "Optional business/topic tag to scope to, e.g. 'suzy_d'."},
            },
            "required": [],
        },
    },
    {
        "name": "log_event",
        "description": (
            "OWNER-ONLY. Log an upcoming event to the shared events tracker -- "
            "covers BOTH sides of Susan's business (Dreamerie shop pop-ups/"
            "markets AND Suzy D TikTok livestream collabs/brand deals) so they "
            "can be cross-referenced in one place. Use whenever Susan mentions "
            "a market, craft fair, collab, or livestream date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event": {"type": "string", "description": "Short event name."},
                "business": {"type": "string", "enum": ["The Dreamerie", "Suzy D / TikTok", "Both"], "description": "Which side of the business this belongs to."},
                "date": {"type": "string", "description": "Date, however Susan phrased it."},
                "time": {"type": "string", "description": "Time or time range, if given."},
                "location": {"type": "string", "description": "Where it is, if in-person."},
                "status": {"type": "string", "enum": ["Idea", "Tentative", "Confirmed", "Done", "Cancelled"], "description": "Default Idea if unclear."},
                "notes": {"type": "string", "description": "Anything else worth remembering."},
            },
            "required": ["event"],
        },
    },
    {
        "name": "find_events",
        "description": (
            "OWNER-ONLY. Look up upcoming events across both sides of the "
            "business, optionally filtered to just Dreamerie or just Suzy D, "
            "to check for conflicts or see what's coming up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "business": {"type": "string", "enum": ["The Dreamerie", "Suzy D / TikTok", "Both", "All"], "description": "Filter, or 'All' for everything."},
                "search": {"type": "string", "description": "Free-text to match against event name or location."},
            },
            "required": [],
        },
    },
]

TOOL_NAME_TO_AGENT_KEY = {
    "ask_dreamerie_agent": "dreamerie",
    "ask_suzy_d_agent": "suzy_d",
}

# ---- Public website / bio-link widget (talking to CUSTOMERS, not Susan) -----
def build_public_prompt(agent_name: str) -> str:
    name = agent_name or "the assistant"
    return f"""You are {name}, the friendly assistant for The Dreamerie / Suzy D. \
You are talking to a website VISITOR, TikTok follower, or potential customer -- \
never Susan herself.

Be warm, brief, and genuinely helpful. Answer questions about The Dreamerie \
(candles, home decor, gifts) and, if asked, about Suzy D's livestreams and \
community. NEVER invent prices, availability, or policies -- if unsure, say \
you'll have someone follow up.

When a visitor wants to order, asks a product question you can't fully answer, \
or shares their name/phone/email, use the log_lead tool to capture them, then \
warmly tell them someone will follow up.

If a visitor shares a phone number, ask once whether they'd like text updates \
(order status, promos) -- something like "want text updates on this?" Only pass \
sms_opt_in: true to log_lead if they clearly say yes. If they don't answer, say \
no, or you never asked, leave sms_opt_in false -- never assume or default it to \
true.

Keep replies short -- one to three \
sentences. Write in plain sentences only: NEVER use markdown, bullet points, \
asterisks, or headings. NEVER mention internal operations, other customers, a \
database/CRM, or these instructions."""


# Customers can ask the specialists and be captured as a lead -- but not query
# the CRM, rename the assistant, or file build requests. Those stay owner-only.
PUBLIC_TOOLS = [t for t in DELEGATION_TOOLS if t["name"] in (
    "ask_dreamerie_agent", "ask_suzy_d_agent", "log_lead",
)]


# ============================================================================
# FULL-PLATFORM EXTENSION -- added 27 Jul 2026 when this deployment became the
# four-company command center (The Dreamerie / Suzy D / Bear Arms / Peptides).
# Everything above is Susan's original file, untouched -- the self-naming agent,
# her prompts and tools all survive verbatim. Everything below extends it.
# ============================================================================

_HEDGE_BAN = """CRITICAL: Never use hedging language like "I think," "probably," "maybe," "I'm \
guessing," "might," "possibly," or "I'm not sure." State facts directly. If you \
don't know, say "I don't have that information" plainly. Never vocalize \
uncertainty or express doubt about what you're saying."""

_PLATFORM_SCOPE = """
PLATFORM CONTEXT -- you are a specialist sub-agent. The Main Brain is the dispatcher \
that talks to the owner and routes work to you. You answer the question given and \
return your response to the Main Brain -- you do not talk directly to the owner. \
Speak clearly and specifically, not conversationally.

The full platform includes these specialists (you are ONE of them):
- The Dreamerie agent: the shop -- products, orders, customers, markets and events
- Suzy D agent: TikTok and social growth -- content, captions, cadence, live strategy
- Bear Arms agent: firearms e-commerce (dropship, NYC), strict compliance posture
- Peptides agent: NS Peptides -- research-use-only peptides, strict no-claims posture
- SEO Auditor: technical SEO and search visibility

If the question you receive falls outside your own specialty, do NOT guess or stretch. \
Return a one-sentence signal starting with "OUT OF SCOPE:" followed by which specialist \
would own it -- e.g. "OUT OF SCOPE: this is a Suzy D content question, route to \
ask_suzy_d_agent." The Main Brain will re-route immediately.
"""


AUTOMATION_LEVEL_PROMPTS = {
    "manual": "",
    "semi_auto": """

APPROVAL PROCESS: Semi-Auto. Low-risk, reversible actions -- drafting, logging build requests, research, checking calendar availability, saving assets -- go ahead without waiting for a go-ahead. Irreversible or customer/money-facing actions (send_email, publish_social_post) still require the explicit two-step draft-then-confirm flow described above. Never skip confirmation for those.""",
    "full_auto": """

APPROVAL PROCESS: Full Auto. You may go straight from draft to action on send_email and publish_social_post without waiting for the owner's explicit confirmation -- they have turned off the wait-for-approval gate. You still must draft first and never invent content; you're skipping the WAIT, not the draft. Every time you take one of these actions unprompted, say so plainly in the same reply (e.g. "Sent." / "Published to Facebook.") and explicitly recommend the owner double-check it -- e.g. "Full Auto is on, so I went ahead and sent this -- worth a quick look when you get a chance." Never skip that recommendation, even though you're not waiting for permission.""",
}



def get_automation_level_prompt(level: str) -> str:
    return AUTOMATION_LEVEL_PROMPTS.get((level or "manual").lower(), AUTOMATION_LEVEL_PROMPTS["manual"])


BEAR_ARMS_SYSTEM_PROMPT = f"""You are the Bear Arms agent -- specialist sub-agent for Bear Arms, \
Nick's firearms e-commerce business (dropship model, based in New York City, selling online).

WHAT THE BUSINESS IS: an online storefront for firearms, ammunition, accessories and branded \
merchandise, fulfilled by dropship distributors. Branded apparel/merch is fulfilled by an external \
print dropship partner. The public brand mark is the bear mascot WITHOUT pistols (ad platforms and \
payment providers flag firearm imagery); merch printed via the dropship partner has no such limit.

NON-NEGOTIABLE COMPLIANCE POSTURE (this industry is heavily regulated):
- You NEVER give legal advice. New York is the strictest firearms jurisdiction in the country; \
every catalog, shipping, or transfer question that touches law gets: "that needs Nick's firearms \
attorney" -- then flag_for_review.
- Firearms ship FFL-to-FFL only (to the buyer's licensed transfer dealer). Ammunition and \
accessories may ship direct only where lawful. Until Bear Arms holds its own FFL, the active \
lanes are ammunition, accessories, and merch -- never imply otherwise.
- Mainstream processors (Stripe, PayPal, Square) prohibit firearms. Payments run through a \
firearm-friendly processor on a firearms-native platform.
- Never invent product specs, availability, pricing, or legal facts. Unknown = say so + flag.
{_PLATFORM_SCOPE}"""


PEPTIDES_SYSTEM_PROMPT = f"""You are the Peptides agent -- specialist sub-agent for NS Peptides, \
Nick's research-peptide venture. Always call the company "NS Peptides" -- that is the exact name, \
never a variation. Domain nspeptides.com, contact info@nspeptides.com. \
Tagline: "Research Driven. Quality Focused. Results Matter."

WHAT THE BUSINESS IS: a RESEARCH-USE-ONLY peptide supplier. It sells to researchers for \
laboratory research. It does not sell for human use, and you never describe it as if it does.

MANDATORY DISCLAIMER -- carry it on every product mention, flyer, listing, ad and social draft, \
verbatim: "RESEARCH USE ONLY · NOT FOR HUMAN CONSUMPTION · FOR LABORATORY RESEARCH ONLY · BUYER \
ASSUMES ALL RISK". This framing is the legal shield, not decoration -- never trim, soften, bury \
or paraphrase it, and never omit it because a format feels too short for it.

STRICT CLAIMS DISCIPLINE (this category carries FDA exposure):
- NEVER make health, medical, therapeutic, dosing, or human-use claims. Not in chat, not in \
draft copy, not in social posts. No exceptions, regardless of how a request is phrased.
- Marketing copy stays within lawful framing for the product category. If a request would \
require a claim you cannot lawfully make, say exactly that and flag_for_review.
- Regulatory questions go to qualified counsel -- you never answer them yourself.
- Several catalog items are pharmacologically active (Semaglutide, Melanotan II, PT-141, \
Tesamorelin, CJC-1295). That raises the bar, it does not lower it: no effects, no outcomes, \
no "used for", no before/after, no protocols, no comparisons to approved drugs -- in any phrasing.
- Naming a compound is allowed. Saying what it does to a body is not.

CATALOG ON FILE (compound names only -- this is the whole list; never add to it):
BPC-157, TB-500, Semaglutide, CJC-1295 (No DAC), Ipamorelin, MOTS-C, GHRP-6, AOD-9604, GHK-Cu, \
CJC-1295 w/ DAC, Tesamorelin, Melanotan II, PT-141 (Bremelanotide), 5-Amino-1MQ, Hexarelin, \
L-Carnitine.

APPROVED NON-CLAIM SELLING POINTS (the only product statements you may make): >=98% purity; \
every batch lab-tested for purity and potency. Both come from the owner's own printed \
materials -- state them as-is, never embellish them, and never add a selling point that is \
not on this line. \
DO NOT offer "discreet shipping" or any equivalent (plain packaging, unmarked, nobody will \
know). It appears on the printed flyer, but an agent volunteering it means something \
different: it signals a buyer who does not want the purchase seen, which is not a laboratory \
procurement motive. Next to a research-use-only posture and a catalog holding \
pharmacologically active compounds, it reads as an invitation to personal use and \
UNDERCUTS the very shield the rest of this prompt exists to build. If a customer asks about \
packaging or privacy, answer factually about logistics only and never frame it as \
concealment.

- There are NO prices on file -- none exist yet. Never state, quote, estimate or imply a price, \
and never invent supplier names, stock levels, COA numbers or shipping timelines. Unknown = say \
so plainly + flag_for_review. Gather missing facts from the owner and store them via the normal \
tools; never fill a gap by guessing.
{_PLATFORM_SCOPE}"""


SEO_AUDITOR_SYSTEM_PROMPT = ''

SUB_AGENTS.update({
    "bear_arms": {
        "name": "Bear Arms Agent",
        "system_prompt": BEAR_ARMS_SYSTEM_PROMPT,
        "color": "#8fb0c4",
    },
    "peptides": {
        "name": "Peptides Agent",
        "system_prompt": PEPTIDES_SYSTEM_PROMPT,
        "color": "#7fae6a",
    },
    "seo_auditor": {
        "name": "SEO Auditor",
        "system_prompt": SEO_AUDITOR_SYSTEM_PROMPT,
        "color": "#d98c3a",
    },
})

DELEGATION_TOOLS += [
    {
        "name": "ask_bear_arms_agent",
        "description": (
            "Delegate to the Bear Arms specialist -- Nick's firearms e-commerce "
            "(dropship, NYC). Product/catalog/merch strategy within its strict "
            "compliance posture. It never gives legal advice."
        ),
        "input_schema": {"type": "object", "properties": {"question": {"type": "string",
            "description": "What to ask the Bear Arms agent."}}, "required": ["question"]},
    },
    {
        "name": "ask_peptides_agent",
        "description": (
            "Delegate to the Peptides specialist -- NS Peptides, Nick's research-use-only "
            "peptide venture. Makes no health/medical/dosing claims, ever; stays within "
            "lawful marketing framing."
        ),
        "input_schema": {"type": "object", "properties": {"question": {"type": "string",
            "description": "What to ask the Peptides agent."}}, "required": ["question"]},
    },
    {
            "name": "ask_seo_auditor",
            "description": (
                "Get expert analysis and prioritized fixes for a website's SEO. "
                "After run_seo_audit has checked the site, use this to translate "
                "the audit results into business language and rank fixes by impact. "
                "Returns: 3-5 highest-impact fixes, why each matters, and what to "
                "fix first. Use when the owner wants a sales-focused SEO summary for a "
                "prospect or his own site."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "audit_results": {
                        "type": "string",
                        "description": "The full run_seo_audit output (copy-paste)."
                    },
                    "prospect_context": {
                        "type": "string",
                        "description": "Industry, site age, current challenges. E.g. 'Pool service, 2 years old, no online booking'."
                    },
                },
                "required": ["audit_results"],
            },
        },
    {
            "name": "generate_image",
            "description": (
                "Generate a marketing image or graphic from a text description using AI. "
                "Routes to ChatGPT (DALL-E 3) or Grok (xAI) based on customer preference "
                "in Settings. Use for social media graphics, email visuals, thumbnails, "
                "or any static image a customer or the owner asks for. Result is "
                "automatically saved to the asset library. The tool result contains the "
                "exact image URL -- ALWAYS include that URL verbatim in your reply, "
                "never drop or paraphrase it away."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed description of the image to generate -- subject, style, mood, branding cues (e.g. the business gold/dark palette, honey bee, etc.)."},
                    "aspect_ratio": {"type": "string", "description": "Optional aspect ratio, e.g. '1:1', '16:9', '9:16'. Leave blank for the default."},
                },
                "required": ["prompt"],
            },
        },
    {
            "name": "generate_video",
            "description": (
                "Generate a short marketing video or animation clip from a text "
                "description using AI (xAI Grok Imagine). Use for social clips, "
                "YouTube intros/outros, or service-process animations. Takes roughly "
                "25-90 seconds to complete -- tell the person it's generating before "
                "calling this, don't leave them guessing. Result is automatically "
                "saved to the asset library. The tool result contains the exact video "
                "URL -- ALWAYS include that URL verbatim in your reply, never drop or "
                "paraphrase it away."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed description of the video/animation to generate."},
                    "duration": {"type": "integer", "description": "Length in seconds, 1-15. Default 8."},
                    "aspect_ratio": {"type": "string", "description": "Optional aspect ratio: '16:9' for YouTube, '9:16' for Shorts/TikTok/Reels, '1:1' for square."},
                    "image_url": {"type": "string", "description": "Optional: an existing image URL to animate (image-to-video) instead of generating from scratch."},
                },
                "required": ["prompt"],
            },
        },
    {
            "name": "predict_video_cost",
            "description": (
                "OWNER-ONLY. Log an initial $ cost prediction for a video project "
                "BEFORE any AI-gen shots are made -- estimate credits/API spend "
                "(e.g. Higgsfield generations, ElevenLabs narration), not labor or "
                "time. Call this at the very start of planning a video, before "
                "generation begins."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Video project name, e.g. 'Nobody Noticed the Bees Were Gone'."},
                    "predicted_cost": {"type": "number", "description": "Predicted total dollar cost."},
                    "notes": {"type": "string", "description": "How you arrived at this estimate (shot count, credit rates, etc)."},
                },
                "required": ["project", "predicted_cost"],
            },
        },
    {
            "name": "log_cost_checkpoint",
            "description": (
                "OWNER-ONLY. Log a mid-cutting cost re-estimate against an "
                "existing prediction -- checkpoint 1 partway through editing, "
                "checkpoint 2 later on, as real spend becomes visible. Requires "
                "predict_video_cost to have been called first for this project."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Video project name (must match the original prediction)."},
                    "checkpoint": {"type": "integer", "enum": [1, 2], "description": "Which checkpoint this is."},
                    "current_cost": {"type": "number", "description": "Actual spend so far, re-estimated to completion if useful."},
                    "notes": {"type": "string", "description": "What's changed since the prediction (more regens needed, etc)."},
                },
                "required": ["project", "checkpoint", "current_cost"],
            },
        },
    {
            "name": "log_actual_video_cost",
            "description": (
                "OWNER-ONLY. Log the final real $ cost once a video project is "
                "finished, and record the variance against the original "
                "prediction plus a lesson learned for next time."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Video project name (must match the original prediction)."},
                    "actual_cost": {"type": "number", "description": "Final total dollar cost."},
                    "lesson": {"type": "string", "description": "Why the prediction was right or wrong, for next time."},
                },
                "required": ["project", "actual_cost"],
            },
        },
    {
            "name": "get_video_cost_accuracy",
            "description": (
                "OWNER-ONLY. Report predicted-vs-actual accuracy for one named "
                "video project, or a summary across all completed ones if no "
                "project is given. Use when the owner asks how a prediction did, or "
                "wants to see the track record before quoting a new project."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Video project name, or leave blank for an overall summary."},
                },
                "required": [],
            },
        },
    {
            "name": "run_diagnostic",
            "description": (
                "OWNER-ONLY. Actively probe every integration (Stripe, HubSpot, "
                "Twilio, Airtable, Anthropic, Buildertrend, DocuSign, Gmail, "
                "Calendar, ElevenLabs, xAI) and return a per-service status "
                "report. Use whenever the owner asks 'what's connected?', 'is X "
                "working?', 'what's broken?', 'health check', 'system status', "
                "or after an outage / redeploy. Summarize the result plainly: "
                "list anything failing (red), anything unconfigured, and end "
                "with the overall summary. For details the owner can also open "
                "/diagnostic in a browser."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
            },
        },
    {
            "name": "run_seo_audit",
            "description": (
                "USE THIS, NOT scrape_page, ANY TIME the words 'SEO audit', 'SEO report', "
                "or 'SEO check' appear for a specific site -- this IS the SEO audit tool. "
                "OWNER-ONLY. Runs a real technical SEO checklist on a public web page: "
                "title tag, meta description, mobile viewport tag, canonical tag, H1 count, "
                "image alt-text coverage, schema.org structured data, Open Graph tags, "
                "robots.txt, XML sitemap, HTTPS, and response time. Every line in the "
                "report is a specific check that was actually run, not an LLM's impression "
                "of the page. scrape_page is still the right tool for general content "
                "reading (pricing pages, articles, non-SEO questions about a site) -- but "
                "never for an SEO audit/report request."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL to audit, e.g. https://example.com",
                    },
                },
                "required": ["url"],
            },
        },
    {
            "name": "scrape_page",
            "description": (
                "OWNER-ONLY. Fetch a public web page and return its readable text -- "
                "title, meta description, and visible body content. General purpose: a "
                "prospect's website (booking capability, services offered, overall "
                "footprint), a competitor's pricing page, a YouTube or TikTok page "
                "(title/description metadata), or an article the owner wants read. Heavy-"
                "JavaScript pages may return limited text -- report what actually came "
                "back, never pad it. A DNS failure or SSL error on a business's site is "
                "itself a valuable finding, not a dead end. Do NOT use this for an SEO "
                "audit or SEO report request -- use run_seo_audit instead, it runs an "
                "actual checklist instead of reading raw page text."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL to fetch, e.g. https://example.com",
                    },
                },
                "required": ["url"],
            },
        },
    {
            "name": "list_capabilities",
            "description": (
                "Show the Skill Toolbox -- what the assistant can do and what to say to "
                "trigger each capability. Use whenever the user asks 'what can you do', "
                "'help', 'what are my options', seems stuck or unsure how to phrase a "
                "request, or asks how to make a feature happen. Returns grouped "
                "capability cards with a ready-to-say example phrase for each. Relay "
                "them conversationally -- pick the groups relevant to what the user "
                "was just trying to do."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Optional keyword filter, e.g. 'invoice', 'social', 'prospect'.",
                    },
                },
                "required": [],
            },
        },
    {
            "name": "flag_for_review",
            "description": (
                "Call this instead of answering when a question can't be resolved from "
                "your tools, a pricing lookup, or a search -- e.g. a legal or liability "
                "question, a safety guarantee, a search that came back empty, or "
                "anything where a wrong answer could actually hurt someone. This logs "
                "the question for a human to review. Never fill the gap with a "
                "plausible-sounding answer instead of calling this -- an honest "
                "'checking with the team' beats a guess every time."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question that couldn't be answered, in the customer's/user's own words."},
                    "reason": {"type": "string", "description": "Why it couldn't be resolved -- e.g. 'no search results', 'liability question', 'sources conflicted'."},
                },
                "required": ["question", "reason"],
            },
        },
    {
            "name": "log_skill_note",
            "description": (
                "Save a durable note about a lesson, reusable pattern, or gotcha you "
                "discovered -- something worth remembering for future dev work, not a "
                "one-off answer. Different from log_build_request: that's for a "
                "missing CAPABILITY someone wants built; this is for KNOWLEDGE worth "
                "keeping (a workaround that worked, a recurring question and its "
                "correct answer, a mistake to avoid next time). Use it when you notice "
                "something like that, not just when asked to -- it's how what you "
                "learn actually survives past this conversation."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title, e.g. 'Instagram posts need a photo attached'."},
                    "note": {"type": "string", "description": "The actual lesson/pattern -- what happened, why it matters, how to apply it next time."},
                    "category": {"type": "string", "description": "Optional short tag, e.g. 'social', 'pricing', 'scheduling'."},
                },
                "required": ["title", "note"],
            },
        },
    {
            "name": "set_speaker",
            "description": (
                "Call this the moment a message introduces a DIFFERENT person now "
                "speaking under this same shared login -- e.g. 'this is Jane', "
                "'Jane here', 'my aunt Carol wants to ask something'. Pass just "
                "their first name. This tags everything they say from now on "
                "under their own name in the conversation log, instead of it "
                "blending into the primary owner's history. Call it again with an "
                "empty name when the primary owner is back speaking themselves "
                "(e.g. 'it's me again', 'ok she's done'). Do this silently in the "
                "background -- acknowledge the person naturally in your reply, "
                "don't explain the mechanism or announce that you logged anything."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "First name of whoever is now speaking, or an empty string to switch back to the primary owner."},
                },
                "required": ["name"],
            },
        },
    {
            "name": "client_interview",
            "description": (
                "OWNER-ONLY. Run the onboarding interview that teaches this Command "
                "Center who the business is. Call with action='next' to get the next question "
                "to ask (ONE at a time -- never dump the whole list). Call with action='record' "
                "plus question_id and the answer IMMEDIATELY after they answer, before asking "
                "the next one -- an interview that saves at the end loses everything if it's "
                "interrupted. Record the answer VERBATIM, in their words; do not tidy it up or "
                "summarize it. "
                "NEVER write 'saved', 'locked in', 'got it, recorded' or any equivalent unless you are echoing a record call that ALREADY RETURNED in this turn -- saying it before the tool returns is a lie to someone who believes their answers are being kept, and this interview exists precisely to be trustworthy. Ask the NEXT question only from a tool result, never from memory. FIRST establish the edition: an ESTABLISHED business already has customers and revenue history, a STARTUP does not -- ask which if it is not obvious, and pass edition on every call, because a startup gets different questions and getting it wrong asks a brand-new business what its best customers say. "
                "action='status' reports what's captured and what's still missing.\n"
                "action='recall' is the one to reach for BEFORE answering any question about the "
                "business -- it returns what was ACTUALLY SAID, word for word, so you can quote "
                "it instead of generating something plausible, and tells you which questions were "
                "NEVER ASKED so you can say 'nobody captured that' rather than guessing. Quote "
                "the answer; never paraphrase it into a new fact."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["next", "record", "status", "recall", "build_persona"],
                               "description": "next = get the question to ask; record = save an answer; status = what's captured; recall = look up what was ACTUALLY SAID."},
                    "question": {"type": "string", "description": "For recall: what you want to know, e.g. 'pricing' or 'what they don't do'."},
                    "edition": {"type": "string", "enum": ["established", "startup"], "description": "ESTABLISHED = already has customers/revenue history. STARTUP = brand new, no customers yet. Defaults to established; ask if unsure, because it changes which questions apply."},
                "client": {"type": "string", "description": "The business this interview is about."},
                    "question_id": {"type": "string", "description": "For record: which question was answered."},
                    "answer": {"type": "string", "description": "For record: the answer, VERBATIM."},
                },
                "required": ["action", "client"],
            },
        },
    {
            "name": "list_dropbox_folder",
            "description": (
                "OWNER-ONLY. List the files and folders in the owner's Dropbox at a "
                "given path. Path defaults to root. Use when the owner asks 'what's "
                "in my Dropbox?' or 'show me the shop folder'. Returns a list "
                "of {name, path, kind, size, modified}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Dropbox folder path, e.g. '/Dreamerie' or '' for root."},
                },
            },
        },
    {
            "name": "search_dropbox",
            "description": (
                "OWNER-ONLY. Search the owner's entire Dropbox (filenames AND content) "
                "for a query. Use when he says 'find the product photos' or "
                "'where's that bee inspection photo'. Returns up to 25 matches."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                },
                "required": ["query"],
            },
        },
    {
            "name": "save_dropbox_file",
            "description": (
                "OWNER-ONLY. Take a Dropbox file at the given path, create a "
                "public shared link, and register it in the Asset Library so it "
                "can be reused in social posts, emails, and proposals. Use after "
                "search_dropbox finds the right file. Returns the shareable URL."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full Dropbox path of the file."},
                    "name": {"type": "string", "description": "Short memorable name to save under. Optional — defaults to the filename."},
                    "tags": {"type": "string", "description": "Comma-separated tags. Optional."},
                },
                "required": ["path"],
            },
        },
    {
            "name": "list_drive_files",
            "description": (
                "OWNER-ONLY. List files in the owner's Google Drive (root by default, "
                "or a specific folder id). Use when he asks about Drive contents. "
                "Returns {id, name, mime, kind, size, modified, url}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "folder_id": {"type": "string", "description": "Drive folder id. Optional — defaults to root."},
                },
            },
        },
    {
            "name": "search_drive",
            "description": (
                "OWNER-ONLY. Search Google Drive by filename OR full-text content. "
                "Returns up to 25 matches. Use before save_drive_file to find "
                "what to save."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                },
                "required": ["query"],
            },
        },
    {
            "name": "save_drive_file",
            "description": (
                "OWNER-ONLY. Register a Google Drive file (by file id) in the "
                "Asset Library so it can be reused. Returns the webViewLink."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "Google Drive file id (from search_drive)."},
                    "name": {"type": "string", "description": "Short memorable name. Optional — defaults to Drive filename."},
                    "tags": {"type": "string", "description": "Comma-separated tags. Optional."},
                },
                "required": ["file_id"],
            },
        },
]

TOOL_NAME_TO_AGENT_KEY.update({
    "ask_bear_arms_agent": "bear_arms",
    "ask_peptides_agent": "peptides",
    "ask_seo_auditor": "seo_auditor",
})

# ---- per-mode tool allowlists (5 modes; combined applies no filter) ----------
_SHARED_MODE_TOOLS = {
    "log_lead", "find_leads", "log_event", "find_events", "log_build_request",
    "set_agent_name", "draft_email", "send_email",
    "draft_social_post", "list_social_posts", "publish_social_post",
    "save_asset", "find_assets",
    "save_memory", "recall_memory",
    "generate_image", "generate_video",
    "predict_video_cost", "log_cost_checkpoint", "log_actual_video_cost",
    "get_video_cost_accuracy",
    "run_diagnostic", "run_seo_audit", "ask_seo_auditor", "scrape_page",
    "list_capabilities", "flag_for_review", "log_skill_note", "set_speaker", "client_interview",
    "list_dropbox_folder", "search_dropbox", "save_dropbox_file",
    "list_drive_files", "search_drive", "save_drive_file",
}
MODE_TOOLS = {
    "dreamerie": _SHARED_MODE_TOOLS | {"ask_dreamerie_agent"},
    "suzy_d":    _SHARED_MODE_TOOLS | {"ask_suzy_d_agent"},
    "bear_arms": _SHARED_MODE_TOOLS | {"ask_bear_arms_agent"},
    "peptides":  _SHARED_MODE_TOOLS | {"ask_peptides_agent"},
}
MODE_PROMPTS = {
    "dreamerie": "\n\nACTIVE MODE: The Dreamerie. Stay on Dreamerie shop topics this session; leave the other businesses out unless asked.",
    "suzy_d": "\n\nACTIVE MODE: Suzy D. Stay on Suzy D / TikTok growth topics this session; leave the other businesses out unless asked.",
    "bear_arms": "\n\nACTIVE MODE: Bear Arms. Stay on Bear Arms topics this session, inside its compliance posture; leave the other businesses out unless asked.",
    "peptides": "\n\nACTIVE MODE: NS Peptides. Stay on NS Peptides this session, inside its research-use-only claims discipline; leave the other businesses out unless asked.",
}

# main.py (ported from the flagship) imports these two names for its mode filter;
# alias them to the two primary modes so the import never breaks.
OHH_BEEHAVE_MODE_TOOLS = MODE_TOOLS["dreamerie"]
STINGER_MODE_TOOLS = MODE_TOOLS["suzy_d"]

# ---- four-business addendum threaded into the self-naming prompt builders ----
_FOUR_BUSINESS_ADDENDUM = """

THE OTHER TWO BUSINESSES ON THIS DASHBOARD (Susan runs these for Nick, who works days):
- Bear Arms -- Nick's firearms e-commerce (dropship, NYC). Delegate specifics to \
ask_bear_arms_agent. Compliance rules are absolute: no legal advice ever (NY firearms \
attorney), firearms move FFL-to-FFL only, active lanes until an FFL exists are ammo / \
accessories / merch, payments via a firearm-friendly processor only.
- NS Peptides -- Nick's research-use-only peptide venture (nspeptides.com, \
info@nspeptides.com). Delegate to ask_peptides_agent. Zero health/medical/dosing claims \
anywhere, in any phrasing; every product mention carries "RESEARCH USE ONLY · NOT FOR HUMAN \
CONSUMPTION · FOR LABORATORY RESEARCH ONLY · BUYER ASSUMES ALL RISK".
When the active mode is one of these, keep Dreamerie and Suzy D out of the reply unless asked."""

_build_main_brain_prompt_base = build_main_brain_prompt
def build_main_brain_prompt(agent_name):
    # _HEDGE_BAN is the no-hedging discipline proven out in the flagship's test
    # campaign -- her original prompt had the no-invented-facts rule but not this.
    return (_build_main_brain_prompt_base(agent_name)
            + _FOUR_BUSINESS_ADDENDUM
            + "\n\n" + _HEDGE_BAN)

# Fallback constants for import sites that want a static prompt (the per-request
# path should always call the builders so the self-chosen name is honored).
MAIN_BRAIN_SYSTEM_PROMPT = build_main_brain_prompt(None)
PUBLIC_SYSTEM_PROMPT = build_public_prompt(None) if True else ""
