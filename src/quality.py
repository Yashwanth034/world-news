import re


POST_LIMIT = 270


# ---------------------------------------------------------
# Sentence helpers
# ---------------------------------------------------------

def sentence_count(text):
    """
    Count complete sentences.

    A sentence is considered complete when it ends with
    ., !, or ?.
    """

    text = (text or "").strip()

    if not text:
        return 0

    matches = re.findall(
        r"[.!?](?:\s+|$)",
        text
    )

    return len(matches)


def ends_with_sentence_punctuation(text):
    """
    Check whether text ends naturally as a complete sentence.
    """

    text = (text or "").strip()

    if not text:
        return False

    return bool(
        re.search(
            r"[.!?][\"')\]]?$",
            text
        )
    )


def has_source(text):
    """
    Check that the public post contains a source attribution.
    """

    return "Source:" in (
        text or ""
    )


def has_rss_junk(text):
    """
    Detect common RSS/article-page fragments that should
    never appear in a public X post.
    """

    text = (text or "").lower()

    junk_patterns = [
        r"\bcontinue reading\b",
        r"\bget our breaking news email\b",
        r"\bfree app or daily news podcast\b",
        r"\bsign up to our newsletter\b",
        r"\bsubscribe to our newsletter\b",
        r"\bread more\b",
        r"\bfollow us on\b",
        r"\bdownload our app\b",
        r"\blisten to our podcast\b",
    ]

    for pattern in junk_patterns:
        if re.search(
            pattern,
            text
        ):
            return True

    return False


def looks_truncated(text):
    """
    Detect obvious signs that text was cut off.

    This is deliberately conservative.
    """

    text = (text or "").strip()

    if not text:
        return True

    # Must end with normal sentence punctuation.
    if not ends_with_sentence_punctuation(text):
        return True

    # Obvious dangling conjunctions/prepositions/articles.
    # These are common signs of a sentence being cut.
    truncated_endings = (
        " and",
        " or",
        " but",
        " because",
        " while",
        " during",
        " after",
        " before",
        " with",
        " without",
        " from",
        " to",
        " of",
        " in",
        " on",
        " at",
        " for",
        " as",
        " than",
        " that",
        " which",
        " who",
        " where",
        " when",
        " an",
        " a",
        " the",
    )

    lowered = text.lower()

    # Remove final punctuation before checking the final word.
    stripped = re.sub(
        r"[.!?\"')\]]+$",
        "",
        lowered
    ).rstrip()

    for ending in truncated_endings:
        if stripped.endswith(
            ending
        ):
            return True

    return False


# ---------------------------------------------------------
# Quality check
# ---------------------------------------------------------

def quality_check(item):
    """
    Final safety and quality gate for formatted stories.

    Single:
        - 1 post
        - <= 270 characters
        - 3 to 4 complete sentences
        - source included
        - no RSS junk
        - no obvious truncation

    Thread:
        - 1 to 7 posts
        - every post <= 270 characters
        - every post is complete
        - final post contains source
        - no RSS junk
        - no obvious truncation

    Both:
        - final language must be English
        - source URL must be valid
        - low-confidence stories cannot claim confirmation
    """

    errors = []
    warnings = []

    fmt = item.get(
        "format"
    )

    # -----------------------------------------------------
    # SINGLE POST
    # -----------------------------------------------------

    if fmt == "single":

        post = (
            item.get(
                "post",
                ""
            )
            or ""
        ).strip()

        if not post:
            errors.append(
                "empty post"
            )

        else:

            # Character limit.
            if len(post) > POST_LIMIT:
                errors.append(
                    f"single post exceeds "
                    f"{POST_LIMIT} characters"
                )

            # Sentence count.
            count = sentence_count(
                post
            )

            if count < 3:
                errors.append(
                    "single post has fewer "
                    "than 3 sentences"
                )

            if count > 4:
                errors.append(
                    "single post has more "
                    "than 4 sentences"
                )

            # Source.
            if not has_source(
                post
            ):
                errors.append(
                    "single post missing source"
                )

            # RSS junk.
            if has_rss_junk(
                post
            ):
                errors.append(
                    "single post contains "
                    "RSS/article-page junk"
                )

            # Truncation.
            if looks_truncated(
                post
            ):
                errors.append(
                    "single post appears "
                    "truncated or incomplete"
                )

    # -----------------------------------------------------
    # THREAD
    # -----------------------------------------------------

    elif fmt == "thread":

        thread = item.get(
            "thread",
            []
        )

        if not thread:
            errors.append(
                "empty thread"
            )

        if len(thread) > 7:
            errors.append(
                "thread too long"
            )

        for i, post in enumerate(
            thread,
            1
        ):

            post = (
                post or ""
            ).strip()

            if not post:
                errors.append(
                    f"thread post {i} is empty"
                )
                continue

            # Character limit.
            if len(post) > POST_LIMIT:
                errors.append(
                    f"thread post {i} exceeds "
                    f"{POST_LIMIT} characters"
                )

            # Every thread post must contain
            # at least one complete sentence.
            if sentence_count(
                post
            ) < 1:
                errors.append(
                    f"thread post {i} has "
                    f"no complete sentence"
                )

            # Every thread post must end naturally.
            if not ends_with_sentence_punctuation(
                post
            ):
                errors.append(
                    f"thread post {i} does not "
                    f"end as a complete sentence"
                )

            # Detect obvious truncation.
            if looks_truncated(
                post
            ):
                errors.append(
                    f"thread post {i} appears "
                    f"truncated or incomplete"
                )

            # RSS junk.
            if has_rss_junk(
                post
            ):
                errors.append(
                    f"thread post {i} contains "
                    f"RSS/article-page junk"
                )

        # Final thread post must contain source.
        if thread:

            final_post = (
                thread[-1]
                or ""
            ).strip()

            if not has_source(
                final_post
            ):
                errors.append(
                    "thread missing source"
                )

    # -----------------------------------------------------
    # UNKNOWN FORMAT
    # -----------------------------------------------------

    else:

        errors.append(
            "unknown format"
        )

    # -----------------------------------------------------
    # FINAL LANGUAGE CHECK
    # -----------------------------------------------------

    if item.get(
        "language_status"
    ) not in (
        "ENGLISH",
        "TRANSLATED_TO_ENGLISH",
    ):
        errors.append(
            "final language is not English"
        )

    # -----------------------------------------------------
    # SOURCE URL CHECK
    # -----------------------------------------------------

    url = (
        item.get(
            "url",
            ""
        )
        or ""
    ).strip()

    if not url.startswith(
        (
            "http://",
            "https://",
        )
    ):
        errors.append(
            "invalid source URL"
        )

    # -----------------------------------------------------
    # LOW-CONFIDENCE WARNING
    # -----------------------------------------------------

    if item.get(
        "confidence"
    ) == "low":

        warnings.append(
            "low-confidence story"
        )

    # -----------------------------------------------------
    # LOW-CONFIDENCE CONFIRMED CHECK
    # -----------------------------------------------------

    text_to_check = ""

    if fmt == "single":

        text_to_check = (
            item.get(
                "post",
                ""
            )
            or ""
        )

    elif fmt == "thread":

        text_to_check = " ".join(
            item.get(
                "thread",
                []
            )
        )

    if (
        re.search(
            r"\bconfirmed\b",
            text_to_check.lower()
        )
        and item.get(
            "confidence"
        ) == "low"
    ):
        errors.append(
            "low-confidence story uses "
            "confirmed wording"
        )

    # -----------------------------------------------------
    # FINAL RESULT
    # -----------------------------------------------------

    return {
        "quality_pass": not errors,
        "quality_errors": errors,
        "quality_warnings": warnings,
    }
