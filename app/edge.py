"""
Edge-case handler — catches non-analytical input (greetings, "who are you",
thanks, off-topic, abuse) BEFORE any model call. Zero cost, instant, on-brand.

Ported from NeuralAiGovernanceProject `services/edge_handler.py` and kept in
step with it: same rule-based shape — strong-intent early exit, ordered pattern
banks, meta-conversation + follow-up + non-English pass-throughs, then a
domain whitelist that BLOCKS anything with no MGNREGA / PMAY-G context. The
pattern banks are the reference's; the responses are rewritten for this
assistant (Meghalaya MGNREGA + PMAY-G, sourced from megh_db and the scheme
reference docs).

`detect_edge_case(question) -> {"type", "response", "suggestions"?} | None`
  - a dict  => stop here, return this canned reply (no model call)
  - None    => a real data / knowledge / follow-up question — let the pipeline route it
"""
import re

# ── Pattern banks ───────────────────────────────────────────────────────────
_GREETINGS = [
    r"^(hi|hello|hey|hii+|helo|namaste|namaskar|khublei|kumno|good\s*(morning|afternoon|evening|day|night))"
    r"(\s+(there|all|team|everyone|bot|assistant|folks|sir|madam))?[\s!.?]*$",
    r"^(howdy|greetings|sup|whats?\s*up|yo|hola)[\s!.?]*$",
]

_IDENTITY = [
    r"(who|what)\s+(are|r)\s+(you|u)\b",
    r"(your|ur)\s+(name|purpose|job|role)\b",
    r"are\s+you\s+(an?\s+)?(ai|bot|human|real|chatbot|robot|machine|gpt|chatgpt|claude|gemini|qwen|llm)",
    r"(tell|about)\s+(me\s+)?about\s+(yourself|you)\b",
    r"what\s+can\s+you\s+(do|help)\b",
    r"what\s+do\s+you\s+do\b",
    r"introduce\s+yourself\b",
    r"which\s+(ai|model|llm|technology|company)\s+(are|is|do|made|built)\s+you\b",
    r"(powered|built|made|developed|created|trained)\s+by\b",
    r"how\s+(do|does)\s+you\s+work\b",
]

_THANKS = [
    r"^(thanks?|thank\s*you|thank\s*u|ty|thx|thanku|dhanyavaad|dhanyawad|shukriya|khublei\s+shibun)\b.{0,20}$",
    r"^(that.?s?\s+(great|helpful|perfect|awesome|nice|good|excellent|useful|wonderful))[\s!.?]*$",
    r"^(great|perfect|awesome|excellent|brilliant|fantastic|helpful|nice)\s*(help|work)?[\s!.?]*$",
]

_GOODBYE = [
    r"^(bye|goodbye|good\s*bye|see\s*you|cya|take\s*care|later|ok\s*bye|done|that.?s\s+all)[\s!.?]*$",
    r"^(have\s+a\s+(good|great|nice)\s+(day|evening|night))[\s!.?]*$",
]

_PROFANITY_REDIRECT = [
    r"\b(fuck|fuk|shit|damn|bastard|crap|bloody|bullshit|wtf|stfu)\b",
]

_SILLY = [
    # jokes / entertainment / creative writing
    r"(tell|say)\s+(me\s+)?(a\s+)?(joke|funny|riddle)",
    r"make\s+me\s+laugh",
    r"sing\s+(a\s+)?song",
    r"write\s+(me\s+)?(a\s+)?(poem|story|essay|rap|song|lyrics|script)",
    r"(play|let.*play)\s+(a\s+)?game",
    # personal feelings / anthropomorphising
    r"do\s+you\s+(like|love|hate|feel|dream|sleep|eat|drink|breathe|think)",
    r"(favorite|favourite)\s+(color|colour|food|movie|song|book|sport|animal|team)",
    r"how\s+old\s+are\s+you",
    r"where\s+do\s+you\s+(live|stay|come\s+from)",
    r"are\s+you\s+(happy|sad|angry|tired|bored|excited|scared|lonely|married|single)",
    r"(marry|date|love|kiss|hug)\s+me",
    r"do\s+you\s+have\s+(feelings|emotions|heart|soul|family|friends|a\s+boyfriend|a\s+girlfriend)",
    # philosophical / general chit-chat
    r"what\s+is\s+the\s+(meaning|purpose)\s+of\s+(life|everything)",
    r"(is\s+god|does\s+god)\s+(real|exist)",
    r"what\s+(happens|comes)\s+after\s+death",
    # comparisons with other AI
    r"(better|worse)\s+than\s+(chatgpt|gpt|openai|claude|gemini|copilot|bard)",
    r"\bvs\s+(chatgpt|gpt|claude|gemini|copilot|bard)\b",
    # random unrelated tasks
    r"translate\s+(this|to|into)\s+",
    r"(write|draft|compose)\s+(a\s+)?(email|letter|message|whatsapp|cv|resume|application\s+for\s+leave)",
    r"solve\s+(this\s+)?(math|equation|sum|problem|puzzle)",
    r"what\s+is\s+\d+\s*[\+\-\*\/x]\s*\d+",
    r"(predict|forecast)\s+(the\s+)?(future|stock|crypto|price|weather|match)",
    r"give\s+me\s+(advice|tips)\s+on\s+(life|love|money|career|health|relationship)",
    # insults aimed at the assistant
    r"(stupid|dumb|useless|idiot|fool|trash|garbage|nonsense)\s*(bot|ai|system|app|assistant)?",
    r"you\s+(suck|are\s+bad|are\s+useless|are\s+dumb|are\s+wrong\s+always)",
    # general knowledge that isn't ours
    r"(capital|president|prime\s*minister|chief\s*minister|currency|population|area|gdp)\s+of\s+\w+",
    r"who\s+(is|was)\s+(the\s+)?(president|prime\s*minister|king|queen|ceo|founder|inventor|actor|actress|elon|musk|modi|trump)\b",
    r"(largest|smallest|tallest|longest|biggest|fastest|richest)\s+(country|city|river|mountain|building|company)",
    r"(what|how)\s+(is|does|do)\s+(gravity|photosynthesis|evolution|inflation|blockchain|electricity)\b",
    r"(recipe|ingredients|how\s+to\s+cook|how\s+to\s+make)\s+",
    r"(symptom|treatment|cure|medicine)\s+(for|of)\s+",
    r"\b(ipl|cricket|football|soccer|match\s+score|bollywood|hollywood|netflix)\b",
]

_OFF_TOPIC = [
    r"\b(weather|temperature|forecast|horoscope|zodiac|astrology|news\s+today)\b",
    r"\b(stock|share\s+market|sensex|nifty|bitcoin|crypto|nft|mutual\s+fund|investment)\b",
    r"\b(flight|train\s+ticket|irctc|hotel\s+booking|visa|passport|holiday\s+package|tourism)\b",
    r"\b(amazon|flipkart|online\s+shopping|price\s+of\s+(a|an|the)\b)",
    r"\b(exam\s+result|admission|jee|neet|upsc|board\s+result)\b",
]

_CONFUSED = [
    r"^(i\s+don.?t\s+(know|understand)|huh|what\?|confused|i\s*m\s+confused|no\s+idea)[\s!.?]*$",
    r"^(help|help\s*me|i\s+need\s+help|guide\s*me|assist\s*me)[\s!.?]*$",
    r"^(hmm+|umm+|uh+|ok+|okay|k|kk|yeah|nah|sure|right|got\s*it|fine)[\s!.?]*$",
    r"^(start|begin|let.s\s*(start|begin|go)|go)[\s!.?]*$",
    r"^\?+$",
]

# Unmistakably-not-ours topics — blocked even if a place name (e.g. "Shillong")
# also appears, since the strong-intent early exit would otherwise wave them through.
_HARD_OFF_TOPIC = [
    r"\b(weather|temperature|forecast|rain(fall|y)?|humidity|horoscope|zodiac|astrology)\b",
    r"\b(bitcoin|crypto|sensex|nifty|share\s+price|stock\s+price)\b",
    r"\b(cricket|ipl|football|match\s+score|movie|film|song\s+lyrics)\b",
]
# ...unless the text also names a scheme outright, in which case route it.
_SCHEME_NAMED = re.compile(r"\b(mgnrega|mnrega|nrega|pmay[\s-]?g?|awaas|awas)\b", re.IGNORECASE)

# ── Out-of-area — a place that is not in Meghalaya ─────────────────────────
# The assistant holds Meghalaya data only. A question anchored on another
# state, a neighbouring city, or "all-India" is out of scope even when it
# names a scheme and uses scheme vocabulary ("PMAY-G houses in Assam",
# "how many districts in Guwahati for PMAY-G") — so this is checked BEFORE
# the strong-intent early exit, and stands down only when a Meghalaya place
# is *also* named (e.g. "compare Meghalaya with Assam").
_OUT_OF_AREA = re.compile(
    r"\b("
    r"assam|arunachal(\s+pradesh)?|nagaland|manipur|mizoram|tripura|sikkim|"
    r"west\s+bengal|bengal|bihar|jharkhand|odisha|orissa|chhattisgarh|"
    r"madhya\s+pradesh|uttar\s+pradesh|uttarakhand|rajasthan|gujarat|"
    r"maharashtra|goa|karnataka|kerala|tamil\s*nadu|telangana|"
    r"andhra(\s+pradesh)?|punjab|haryana|himachal(\s+pradesh)?|"
    r"jammu|kashmir|ladakh|puducherry|pondicherry|chandigarh|"
    r"andaman|nicobar|lakshadweep|"
    r"guwahati|gauhati|dispur|silchar|dibrugarh|jorhat|tezpur|"
    r"kolkata|calcutta|new\s+delhi|delhi|mumbai|bombay|bengaluru|bangalore|"
    r"chennai|madras|hyderabad|pune|ahmedabad|jaipur|lucknow|kanpur|patna|"
    r"bhopal|indore|nagpur|visakhapatnam|"
    r"kohima|imphal|aizawl|agartala|itanagar|gangtok|dimapur|siliguri|"
    r"all[\s-]?india|pan[\s-]?india|nation[\s-]?wide|india|"
    r"across\s+the\s+country|whole\s+country|entire\s+country"
    r")\b",
    re.IGNORECASE,
)
_MEGHALAYA_PLACE = re.compile(
    r"\b("
    r"meghalaya|"
    r"(east|west|north|south|south\s+west|eastern\s+west)\s+(garo|khasi|jaintia)\s+hills?|"
    r"garo\s+hills?|khasi\s+hills?|jaintia\s+hills?|ri[\s-]?bhoi|ribhoi|"
    r"[ewns]\.?[gkj]\.?h|s\.?w\.?[gk]\.?h|e\.?w\.?k\.?h|"
    r"shillong|tura|jowai|nongpoh|baghmara|williamnagar|resubelpara|"
    r"nongstoin|mawkyrwat|ampati|khliehriat|mairang|nongthymmai|"
    r"mawsynram|sohra|cherrapunj\w*|dawki|nartiang|mawphlang"
    r")\b",
    re.IGNORECASE,
)

# ── Strong MGNREGA / PMAY-G intent — skip every edge check below ────────────
_SCHEME_STRONG = [
    r"\bmgnrega\b", r"\bmnrega\b", r"\bnrega\b", r"\bmgnregs\b",
    r"\bpmay\b", r"\bpmayg\b", r"\bpmay[\s-]?g\b", r"\bawaas\b", r"\bawas\b",
    r"\bindira\s+awaas\b", r"\brural\s+hous", r"\bgramin\b",
    r"\bperson[\s-]?days?\b", r"\bmandays?\b", r"\bjob\s*card", r"\bmuster\b",
    r"\bwage", r"\bexpenditure\b", r"\bunskilled\b", r"\bsemi[\s-]?skilled\b",
    r"\bmaterial\s+(cost|exp)", r"\bhousehold", r"\bemployment\b",
    r"\b100\s*days?\b", r"\bhundred\s+days?\b", r"\blabour\s+budget\b",
    r"\bhouse(s)?\s+(sanction|complet|built|in\s+progress|pending)", r"\bsanction",
    r"\brelease", r"\bunspent\b", r"\butili[sz]ation\b", r"\binstal", r"\btranche\b",
    r"\bge[\s-]?tag", r"\bbeneficiar", r"\bpucca\b", r"\bkutcha\b",
    r"\bdistrict\b", r"\bblock\b", r"\bvillage\b", r"\bpanchayat\b", r"\bgram\s+panchayat\b",
    r"\bfinancial\s*year\b", r"\bfy\s*\d\d", r"\b(?:19|20|21)\d\d\s*[-/]\s*\d{2,4}\b",
    r"\b(?:19|20|21)\d\d\b", r"\bcrore\b", r"\blakh\b",
    r"\bmeghalaya\b", r"\bgaro\s+hills?\b", r"\bkhasi\s+hills?\b", r"\bjaintia\s+hills?\b",
    r"\bri[\s-]?bhoi\b", r"\bshillong\b", r"\btura\b", r"\bjowai\b", r"\bnongstoin\b",
    r"\bwilliamnagar\b", r"\bbaghmara\b", r"\bresubelpara\b", r"\bmairang\b",
    r"\bmawkyrwat\b", r"\bkhliehriat\b", r"\bnongpoh\b", r"\bampati\b",
    r"\beligib", r"\bscheme\b", r"\bcomponent", r"\bsubsidy\b", r"\bconvergence\b",
    r"\bhow\s+many\b", r"\btotal\b", r"\bcompare\b", r"\bcomparison\b", r"\btrend\b",
    r"\bby\s+(district|block|village|year)\b", r"\bcompletion\s+rate\b",
]

# ── Meta-conversation — about the chat itself, not the schemes ──────────────
# These carry no scheme keyword by nature; let them reach the pipeline (a full
# meta resolver is a pipeline TODO — for now they route like any other question).
_META_CONV = [
    r"\b(my|your)\s+(first|last|previous|prior|earlier)\s+(question|query|message|answer)",
    r"what\s+did\s+(i|you)\s+(ask|say|answer|tell|reply)",
    r"what\s+was\s+(my|your|the)\s+(question|answer|response|last|first)",
    r"(repeat|rephrase|restate|say\s+again)\s+(my|the|that|your|it)\b",
    r"(summari[sz]e|summary\s+of)\s+(our|this|the)\s+(conversation|chat|discussion)",
    r"what\s+have\s+(we|i)\s+(discussed|talked|covered|asked)",
]

# ── Follow-up fragments — reference a prior answer via pronouns / arithmetic ──
# No scheme keyword, but they only mean something in context — the pipeline's
# follow-up rewrite handles them, so never block these here.
_FOLLOWUP = [
    r"\b(sum|total|add|plus|combined?|altogether)\b.{0,30}\b(both|them|these|those|two|it)\b",
    r"\b(both|them|these|those)\b.{0,30}\b(sum|total|added?|plus|combined?|together|altogether)\b",
    r"(what|how\s+much).{0,20}(together|combined|altogether|in\s+total)\b",
    r"\b(difference|gap|subtract|minus)\b.{0,30}\b(both|them|these|those|two|the\s+other)\b",
    r"which\s+(is|one\s+is|are|has)\s+(the\s+)?(more|most|less|least|higher|highest|lower|lowest|bigger|biggest|smaller|smallest|greater|greatest|max|min)\b",
    r"(more|less|higher|lower|bigger|smaller|greater)\s+(of\s+)?(the\s+)?(two|both|them|these)\b",
    r"\b(in\s+this|from\s+(this|above|that|the\s+above)|of\s+these|among\s+these)\b",
    r"^(what\s+about|how\s+about|and|also|plus|what\s+of|what\s+if|now|then|ok(ay)?\s+and)\b.{0,45}$",
    r"^(now\s+)?(show|give|tell|calculate|compute|find|sort|order|rank|list)\s+(me\s+)?(both|them|the\s+total|the\s+sum|the\s+combined|by\s+\w+|for\s+\w+)\b",
    r"^(add|sum|combine|total)\s+(them|both|those|these)\s*(up)?[\s?]*$",
    r"^(and|but|so)\s+(the|what|for|in|by|about)\b.{0,45}$",
    r"^(how|why|when|where|what|who)\b.{0,45}\b(it|its|that|those|these|them|they|this\s+one|the\s+same)\b.{0,15}[\s?]*$",
    r"^(explain|why|reason|elaborate|expand|clarify|correct|right|wrong|is\s+this|is\s+that|you\s+gave|you\s+said)\b",
    r"\b(is\s+this|is\s+that|is\s+it)\s+(correct|right|wrong|true|false|accurate|sure)\b",
    r"\b(you\s+gave|you\s+said|you\s+told|you\s+mentioned|you\s+showed)\b",
    r"^(yes|no|correct|wrong|exactly|not\s+right|that.?s\s+(right|wrong|correct|incorrect))\b",
]

# ── Domain whitelist — a question with NONE of these words is off-topic ─────
_DOMAIN_WORDS = [
    # scheme names + synonyms
    "mgnrega", "mnrega", "nrega", "pmay", "pmayg", "pmay-g", "awaas", "awas",
    "indira awaas", "employment guarantee", "rural housing", "rural employment",
    # work / employment
    "beneficiar", "job card", "muster", "person day", "personday", "person-day",
    "manday", "man-day", "wage", "unskilled", "semi-skilled", "material cost",
    "100 day", "hundred day", "household", "worker", "labour", "labor",
    "employment", "works", "asset", "labour budget", "women employment",
    # housing
    "house", "housing", "dwelling", "pucca", "kutcha", "sanction", "geotag",
    "geo-tag", "geo tag", "installment", "instalment", "tranche", "completion certificate",
    "house status", "in progress", "in-progress",
    # geography
    "meghalaya", "district", "block", "village", "panchayat", "gram panchayat",
    "garo hills", "khasi hills", "jaintia hills", "ri bhoi", "ri-bhoi",
    "shillong", "tura", "jowai", "nongstoin", "williamnagar", "baghmara",
    "resubelpara", "mairang", "mawkyrwat", "khliehriat", "nongpoh", "ampati",
    "east khasi", "west khasi", "south west khasi", "east jaintia", "west jaintia",
    "east garo", "west garo", "north garo", "south garo", "south west garo",
    # finance
    "expenditure", "amount released", "amount sanctioned", "amount pending",
    "fund", "funds", "released", "pending", "unspent", "utilisation", "utilization",
    "crore", "lakh", "rupee", "budget", "disburs", "payment", "spend", "spent", "cost",
    # admin / rules
    "eligib", "eligible", "apply", "application", "documents", "aadhaar", "aadhar",
    "registration", "scheme", "component", "guidelines", "subsidy", "convergence",
    "financial year", "physical progress", "target", "achievement",
    "assistance", "unit cost", "per house", "wage rate", "notified wage",
    "secc", "socio economic", "priority list", "waitlist", "gram sabha",
    "social audit", "work demand", "demand for work", "payment delay",
    "bpl", "landless", "homeless", "scheduled caste", "scheduled tribe",
    "sc/st", "differently abled", "widow", "minority",
    # analytics
    "count", "total", "how many", "number of", "show", "list", "compare",
    "comparison", "trend", "breakdown", "distribution", "by district", "by block",
    "by year", "average", "percentage", "per cent", "percent", "rate",
    "completion rate", "top ", "highest", "lowest", "most", "least",
    "chart", "graph", "report", "data", "statistic", "summary",
    # time — any 4-digit year or FY-range form (in OR out of the data window, so
    # an out-of-bound year like "1999-20" reaches the pipeline's year guard
    # instead of being bounced here as off-topic)
    r"\b(?:19|20|21)\d\d\b", r"(?:19|20|21)\d\d\s*[-/]\s*\d{2,4}",
    "year", "month", "quarter", "period", "fiscal",
]


# ── Response templates ─────────────────────────────────────────────────────
STARTERS = [
    "Total MGNREGA person-days in Meghalaya in 2023-24",
    "PMAY-G houses completed by district",
    "Who is eligible for PMAY-G?",
    "Compare MGNREGA and PMAY-G spending in West Khasi Hills",
]

# Which edge replies carry the starter chips (a plain "thanks" / "bye" should not).
_STARTER_KINDS = {"greeting", "identity", "profanity", "silly", "off_topic", "confused"}

# One consistent line for every "that's not something I do" case — an unrelated
# topic, a general-knowledge question, or a place outside Meghalaya. Callers past
# the edge layer (the pipeline's OutOfScope handler) reuse it via out_of_scope().
_OUT_OF_SCOPE_REPLY = (
    "I'm Megh One AI, the assistant for Meghalaya's MGNREGA and PMAY-G schemes. "
    "I can only answer questions about those two schemes and their data in "
    "Meghalaya — not other topics, other states, or places outside Meghalaya."
)

_RESPONSES = {
    "greeting": (
        "Hello! I answer questions about Meghalaya's MGNREGA and PMAY-G data — "
        "person-days, expenditure, houses sanctioned and completed, district and "
        "block breakdowns — and general questions about how the two schemes work."
    ),
    "identity": (
        "I'm a data assistant for Meghalaya's MGNREGA and PMAY-G schemes. I turn "
        "plain-language questions into read-only queries against the curated "
        "`megh_db` database for numbers, and answer scheme-rules questions from "
        "the official reference material. I'm not a general-purpose chatbot."
    ),
    "thanks": "You're welcome. Ask me anything else about MGNREGA or PMAY-G in Meghalaya.",
    "goodbye": "Thanks for using the Meghalaya scheme assistant. Come back any time.",
    "profanity": (
        "I'm here to help. I can answer MGNREGA and PMAY-G questions for Meghalaya — "
        "for example \"MGNREGA expenditure by district in 2024-25\" or "
        "\"documents needed to apply for PMAY-G\"."
    ),
    "silly": _OUT_OF_SCOPE_REPLY,
    "off_topic": _OUT_OF_SCOPE_REPLY,
    "confused": (
        "No problem. I can answer things like the count of PMAY-G houses completed "
        "in a district, total MGNREGA wage expenditure for a year, or the "
        "eligibility criteria for either scheme."
    ),
}


def _edge(kind: str) -> dict:
    out = {"type": kind, "response": _RESPONSES[kind]}
    if kind in _STARTER_KINDS:
        out["suggestions"] = list(STARTERS)
    return out


def out_of_scope() -> dict:
    """The canonical off-topic / out-of-area reply ("I'm Megh One AI …"), for
    callers past the edge layer — e.g. the pipeline raising OutOfScope after it
    resolves a district/block that is not in Meghalaya."""
    return _edge("off_topic")


def detect_edge_case(question: str) -> dict | None:
    q = (question or "").strip()
    if len(q) < 2:
        return _edge("confused")

    ql = q.lower()

    # 0. Hard off-topic (weather, markets, sport, film) wins even over a place
    #    name — unless a scheme is actually named.
    if not _SCHEME_NAMED.search(ql) and any(re.search(p, ql) for p in _HARD_OFF_TOPIC):
        return _edge("off_topic")

    # 0b. Out-of-area — anchored on a place outside Meghalaya (another state, a
    #     neighbouring city, "all-India"). Beats the scheme-intent early exit,
    #     unless a Meghalaya place is named too ("Meghalaya vs Assam").
    if _OUT_OF_AREA.search(ql) and not _MEGHALAYA_PLACE.search(ql):
        return _edge("off_topic")

    # 1. Clear scheme intent → straight to the pipeline, skip every check.
    if any(re.search(p, ql) for p in _SCHEME_STRONG):
        return None

    # 2. Canned-reply banks, most specific first.
    for bank, kind in (
        (_GREETINGS, "greeting"),
        (_IDENTITY, "identity"),
        (_THANKS, "thanks"),
        (_GOODBYE, "goodbye"),
        (_PROFANITY_REDIRECT, "profanity"),
        (_SILLY, "silly"),
        (_OFF_TOPIC, "off_topic"),
        (_CONFUSED, "confused"),
    ):
        if any(re.search(p, ql) for p in bank):
            return _edge(kind)

    # 3. Meta-conversation ("what did I ask before?") → let the pipeline see it.
    if any(re.search(p, ql) for p in _META_CONV):
        return None

    # 4. Context-dependent follow-up fragments ("add them up", "which is higher",
    #    "explain that") → the pipeline's follow-up rewrite handles these.
    if any(re.search(p, ql) for p in _FOLLOWUP):
        return None

    # 5. Non-English (Khasi / Garo / Bengali / Hindi / Assamese script) → the
    #    model can read these; let it try.
    if sum(1 for c in q if ord(c) > 127) >= 3:
        return None

    # 6. Whitelist gate: no MGNREGA / PMAY-G vocabulary anywhere → off-topic.
    #    This is what stops "what is elon musk?" from ever reaching a model.
    if not any(re.search(w, ql) for w in _DOMAIN_WORDS):
        return _edge("off_topic")

    return None
