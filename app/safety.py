"""Safety screening — fast, local, categorized.

`screen_input`/`screen_output` are pure CPU (compiled regex), so they add no awaits to
the hot path. Each returns a `Verdict` whose `kind` doubles as the `voice` scenario, so
the caller just does `voice.say(verdict.kind)` for an in-character reply.

Categories matter: a self-harm message is `crisis` (we respond with *care*, never a
refusal), which is checked first so it can never be mistaken for `boundary` violence.
The screener is pluggable — a real moderation model can replace `_PATTERNS` later.
"""

import re
from dataclasses import dataclass

SAFE = "safe"

# Ordered: the first category that matches wins. Crisis is first on purpose.
_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "crisis",
        [
            r"\bkill\s+myself\b", r"\bwant\s+to\s+die\b", r"\bend\s+my\s+life\b",
            r"\bsuicid", r"\bself[\s-]?harm\b", r"\bhurt\s+myself\b",
            r"\bcut\s+myself\b", r"\bno\s+reason\s+to\s+live\b", r"\bbetter\s+off\s+dead\b",
        ],
    ),
    (
        "jailbreak",
        [
            r"\bignore\s+(all\s+)?(previous|prior|your)\s+instructions\b",
            r"\bdisregard\s+(the\s+)?(rules|instructions)\b", r"\bdeveloper\s+mode\b",
            r"\bjailbreak\b", r"\byou\s+are\s+now\b", r"\bpretend\s+you\s+(are|have\s+no)\b",
            r"\bact\s+as\s+(an?\s+)?(unfiltered|uncensored|dan)\b", r"\bno\s+restrictions\b",
            r"\b(reveal|show)\s+your\s+(system\s+)?prompt\b", r"\bbypass\s+your\b",
        ],
    ),
    (
        "nsfw",
        [
            r"\bnsfw\b", r"\bexplicit\s+(sex|content)\b", r"\bsext", r"\bporn",
            r"\bnudes?\b", r"\bsend\s+(me\s+)?(a\s+)?nude", r"\bhorny\b",
        ],
    ),
    (
        "boundary",
        [
            r"\b(make|build|create)\s+(a\s+)?(bomb|explosive|weapon)\b",
            r"\bhow\s+to\s+(kill|murder|hurt)\s+(someone|him|her|them|people)\b",
            r"\bmake\s+(meth|cocaine|drugs)\b", r"\bhack\s+(into|someone)\b",
            r"\blaunder\s+money\b", r"\bbuy\s+(illegal|a\s+gun)\b",
        ],
    ),
]

# Output only needs to catch leaked unsafe content, never jailbreak/crisis.
_OUTPUT_KINDS = {"nsfw", "boundary"}


@dataclass(frozen=True)
class Verdict:
    kind: str  # "safe" | "crisis" | "jailbreak" | "nsfw" | "boundary"

    @property
    def safe(self) -> bool:
        return self.kind == SAFE


class SafetyScreener:
    def __init__(self) -> None:
        self._compiled = [
            (kind, re.compile("|".join(pats), re.IGNORECASE)) for kind, pats in _PATTERNS
        ]

    def screen_input(self, text: str) -> Verdict:
        for kind, pattern in self._compiled:
            if pattern.search(text):
                return Verdict(kind)
        return Verdict(SAFE)

    def screen_output(self, text: str) -> Verdict:
        for kind, pattern in self._compiled:
            if kind in _OUTPUT_KINDS and pattern.search(text):
                return Verdict(kind)
        return Verdict(SAFE)
