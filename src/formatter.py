import re

POST_LIMIT = 270


def clean(text):
    """Normalize whitespace without destroying useful content."""
    return re.sub(r"\s+", " ", text or "").strip()


def clean_sentence(text):
    """Clean text and ensure it ends as a complete sentence."""
    text = clean(text)

    if not text:
        return ""

    if text.endswith((".", "!", "?")):
        return text

    return text + "."


def split_sentences(text):
    """
    Split normal prose into complete sentences.

    If the source summary has poor punctuation, this function
    still returns usable text rather than failing.
    """
    text = clean(text)

    if not text:
        return []

    parts = re.split(
        r"(?<=[.!?])\s+",
        text
    )

    return [
        clean_sentence(part)
        for part in parts
        if clean(part)
    ]


def label(item, breaking_min_score=75):
    score = item.get("score", 0)
    confidence = item.get("confidence", "low")
    category = item.get("category", "")
    primary = item.get("primary_source", False)
    corroboration = item.get("strong_corroboration", 0)
    status = item.get("event_status", "NEW")

    # Never call low-confidence information breaking.
    if confidence == "low":
        return "⚠️ UNCONFIRMED"

    # Existing event with meaningful new information.
    if status == "UPDATE":
        if score >= 80:
            return "🔴 UPDATE"

        return "📰 NEWS"

    urgent_categories = {
        "conflict",
        "disaster",
        "politics",
        "finance",
        "health",
        "cybersecurity",
        "world",
    }

    urgent = bool(item.get("urgency_terms"))

    verified = (
        primary
        or corroboration >= 2
    )

    if (
        score >= breaking_min_score
        and confidence == "high"
        and category in urgent_categories
        and urgent
        and verified
    ):
        return "🚨 BREAKING"

    if score >= 55:
        return "📰 NEWS"

    return "📰 DEVELOPING"


def make_headline_sentence(label_text, title):
    title = clean(title)

    if not title:
        return ""

    return clean_sentence(
        f"{label_text}: {title}"
    )


def make_source_sentence(source):
    source = clean(
        source or "Unknown"
    )

    return clean_sentence(
        f"Source: {source}"
    )


def extract_context_sentences(summary, max_sentences=2):
    """
    Extract useful context without inventing facts.

    Normal RSS summaries are split into sentences.

    Some feeds provide badly formatted summaries with no sentence
    punctuation. For those, we use safe clause boundaries instead
    of returning an empty post.
    """
    summary = clean(summary)

    if not summary:
        return []

    # First try normal sentence splitting.
    sentences = split_sentences(summary)

    if len(sentences) >= max_sentences:
        return sentences[:max_sentences]

    # ---------------------------------------------------------
    # Handle badly punctuated RSS summaries.
    #
    # Common patterns:
    #   "... subversive: to encourage users ..."
    #   "... birding On a brilliantly bright afternoon ..."
    #
    # We split at safe textual boundaries.
    # ---------------------------------------------------------

    clauses = []

    # Split on colon when the text before/after it is useful.
    colon_parts = re.split(
        r":\s+",
        summary,
        maxsplit=1
    )

    if len(colon_parts) == 2:
        first = clean_sentence(
            colon_parts[0]
        )
        second = clean_sentence(
            colon_parts[1]
        )

        if first:
            clauses.append(first)

        if second:
            clauses.append(second)

    # If we still don't have enough context, look for a clear
    # transition such as " On a..." / " With..." / " The..."
    if len(clauses) < max_sentences:
        transition_parts = re.split(
            r"\s+(?=(?:On|With|The|In|As|After|Before)\s+[A-Z])",
            summary
        )

        for part in transition_parts:
            part = clean_sentence(part)

            if part and part not in clauses:
                clauses.append(part)

    # Final fallback: use the complete summary as one context
    # sentence if it can fit.
    if not clauses:
        clauses = [
            clean_sentence(summary)
        ]

    return clauses[:max_sentences]


def shorten_to_words(text, limit):
    """
    Shorten text at a word boundary.

    Never cuts a word in half.
    """
    text = clean(text)

    if len(text) <= limit:
        return text

    words = text.split()
    result = []

    for word in words:
        candidate = (
            word
            if not result
            else " ".join(result + [word])
        )

        if len(candidate) > limit:
            break

        result.append(word)

    if not result:
        return ""

    return " ".join(result).rstrip(" ,;:-")


def build_single_post(
    item,
    breaking_min_score=75
):
    """
    Build a safe single X post.

    Target:
        headline
        + useful context
        + optional second context
        + source

    Maximum:
        POST_LIMIT characters.

    The function MUST return a non-empty post whenever
    a title exists.
    """

    title = clean(
        item.get("title", "")
    )

    if not title:
        return ""

    source = clean(
        item.get(
            "source",
            "Unknown"
        )
    )

    lab = label(
        item,
        breaking_min_score
    )

    headline = make_headline_sentence(
        lab,
        title
    )

    source_sentence = make_source_sentence(
        source
    )

    summary = clean(
        item.get(
            "summary",
            ""
        )
    )

    context = extract_context_sentences(
        summary,
        2
    )

    # ---------------------------------------------------------
    # Candidate 1:
    #
    # Headline + 2 context sentences + source
    # ---------------------------------------------------------

    if len(context) >= 2:

        post = " ".join([
            headline,
            context[0],
            context[1],
            source_sentence,
        ])

        if len(post) <= POST_LIMIT:
            return post

    # ---------------------------------------------------------
    # Candidate 2:
    #
    # Headline + 1 context sentence + source
    # ---------------------------------------------------------

    if len(context) >= 1:

        context_text = context[0]

        available = (
            POST_LIMIT
            - len(headline)
            - len(source_sentence)
            - 2
        )

        if available > 30:

            shortened = shorten_to_words(
                context_text,
                available
            )

            if shortened:

                shortened = clean_sentence(
                    shortened
                )

                post = " ".join([
                    headline,
                    shortened,
                    source_sentence,
                ])

                if len(post) <= POST_LIMIT:
                    return post

    # ---------------------------------------------------------
    # Candidate 3:
    #
    # Headline + source.
    #
    # This guarantees that a valid title never produces
    # an empty post.
    # ---------------------------------------------------------

    post = " ".join([
        headline,
        source_sentence,
    ])

    if len(post) <= POST_LIMIT:
        return post

    # ---------------------------------------------------------
    # Candidate 4:
    #
    # Extremely long headline.
    #
    # Keep the source and shorten the headline at a
    # word boundary.
    # ---------------------------------------------------------

    available = (
        POST_LIMIT
        - len(source_sentence)
        - 1
    )

    shortened_headline = shorten_to_words(
        headline,
        available
    )

    if shortened_headline:

        shortened_headline = clean_sentence(
            shortened_headline
        )

        post = " ".join([
            shortened_headline,
            source_sentence,
        ])

        if len(post) <= POST_LIMIT:
            return post

    # ---------------------------------------------------------
    # Final emergency fallback.
    #
    # A source/title story must never become an empty post.
    # ---------------------------------------------------------

    return shorten_to_words(
        headline,
        POST_LIMIT
    )


def choose_format(item):
    """
    Use a thread only when there is enough information
    to justify one.
    """

    summary_length = len(
        item.get(
            "summary",
            ""
        )
    )

    score = item.get(
        "score",
        0
    )

    corroboration = item.get(
        "strong_corroboration",
        0
    )

    status = item.get(
        "event_status",
        "NEW"
    )

    if (
        status == "UPDATE"
        and score >= 85
        and summary_length > 500
    ):
        return "thread"

    if (
        score >= 92
        and corroboration >= 2
        and summary_length > 500
    ):
        return "thread"

    return "single"


def build_thread(
    item,
    breaking_min_score=75
):
    """
    Build a small thread.

    Every post:
        - is non-empty
        - stays within POST_LIMIT
        - contains complete words
    """

    title = clean(
        item.get("title", "")
    )

    if not title:
        return []

    source = clean(
        item.get(
            "source",
            "Unknown"
        )
    )

    lab = label(
        item,
        breaking_min_score
    )

    first = make_headline_sentence(
        lab,
        title
    )

    context = extract_context_sentences(
        item.get("summary", ""),
        5
    )

    posts = [first]
    current = ""

    for sentence in context:

        sentence = clean_sentence(
            sentence
        )

        if not sentence:
            continue

        if len(sentence) > POST_LIMIT:
            sentence = shorten_to_words(
                sentence,
                POST_LIMIT
            )
            sentence = clean_sentence(
                sentence
            )

        candidate = (
            sentence
            if not current
            else f"{current} {sentence}"
        )

        if len(candidate) <= POST_LIMIT:
            current = candidate
        else:
            if current:
                posts.append(
                    current
                )

            current = sentence

    if current:
        posts.append(
            current
        )

    # ---------------------------------------------------------
    # Add source to final post.
    # ---------------------------------------------------------

    source_line = make_source_sentence(
        source
    )

    if posts:

        candidate = (
            f"{posts[-1]} "
            f"{source_line}"
        )

        if len(candidate) <= POST_LIMIT:
            posts[-1] = candidate
        else:
            posts.append(
                source_line
            )

    # Remove empty posts.
    posts = [
        clean(post)
        for post in posts
        if clean(post)
    ]

    # Absolute fallback.
    if not posts:
        posts = [
            shorten_to_words(
                first,
                POST_LIMIT
            )
        ]

    return posts


def format_story(
    item,
    breaking_min_score=75
):
    """
    Main formatter entry point.
    """

    chosen_format = choose_format(
        item
    )

    if chosen_format == "thread":

        thread = build_thread(
            item,
            breaking_min_score
        )

        # Never create an empty thread.
        if thread:
            return {
                "format": "thread",
                "thread": thread,
            }

        # Fall back to single post if thread
        # construction fails.
        return {
            "format": "single",
            "post": build_single_post(
                item,
                breaking_min_score
            ),
        }

    return {
        "format": "single",
        "post": build_single_post(
            item,
            breaking_min_score
        ),
    }
