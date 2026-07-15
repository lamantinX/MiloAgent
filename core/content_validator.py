"""Content validation against business profile before posting."""

import re
import logging
from typing import Dict, List, Optional, Tuple
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


class ContentValidator:
    """Validates LLM-generated content against business profile facts.

    Checks:
    1. Product name accuracy (exact spelling)
    2. URL accuracy (correct domain)
    3. Forbidden phrases (never_say rules)
    4. Bot-like patterns
    5. Length appropriateness
    6. Pricing claim verification
    7. Organic mode enforcement (no product/URL leakage)
    """

    # Minimum clamped score required to accept content. This is the single
    # source of truth for is_valid; platform adapters must not reinterpret
    # scores with their own threshold.
    ACCEPTANCE_THRESHOLD = 0.6

    # Pre-compiled regex patterns for bot detection (compiled once at class load)
    BOT_PATTERNS = [
        # ── Generic openers (instant bot tell) ──
        (re.compile(r"^(Great question|I totally agree|This is amazing|This!)"), "generic opener"),
        (re.compile(r"^(Hey there|Hi there|Hello there|Hey!)"), "bot-like greeting"),
        (re.compile(r"^(Absolutely|Definitely|Totally|Certainly|Exactly)[,!]"), "LLM cliche opener"),
        (re.compile(r"^(This resonates|This hits|This is so relatable|Love this)"), "sycophantic opener"),
        (re.compile(r"^(Well,|So,|Honestly,|Actually,|In my experience,)\s"), "formulaic opener"),
        (re.compile(r"^(As someone who|Speaking as a|Coming from a)"), "role-declaration opener"),
        # ── AI self-reference ──
        (re.compile(r"(As an AI|I'm an AI|As a language model)"), "AI self-reference"),
        # ── Formatting tells ──
        (re.compile(r"(!!+)"), "excessive exclamation"),
        (re.compile(r"(#\w+\s*){2,}"), "hashtags"),
        (re.compile(r"\*\*.+?\*\*.*\*\*.+?\*\*"), "too much bold formatting"),
        (re.compile(r"(?:^[-•] .+\n?){3,}", re.MULTILINE), "excessive bullet list"),
        (re.compile(r"(?:^\d+[.)]\s.+\n?){3,}", re.MULTILINE), "numbered list"),
        # ── LLM structural tells ──
        (re.compile(r"(?i)in\s+(?:conclusion|summary|short)[,:]"), "essay conclusion"),
        (re.compile(r"(?i)let me\s+(?:break this down|explain|elaborate|walk you)"), "LLM explanation"),
        (re.compile(r"(?i)here'?s\s+(?:the thing|what I think|my take|the deal)[,:]"), "formulaic transition"),
        (re.compile(r"(?i)(?:that being said|with that said|having said that|not to mention)"), "transition cliche"),
        (re.compile(r"(?i)(?:on top of that|to add to this|building on this|to piggyback)"), "stacking transition"),
        # ── Corporate / marketing language ──
        (re.compile(r"(?i)\b(?:leverage|utilize|streamline|optimize|maximize)\s+(?:your|the|this)\b"), "corporate language"),
        (re.compile(r"(?i)\bgame[- ]?changer\b"), "marketing cliche"),
        (re.compile(r"(?i)\b(?:next level|level up|up your game|take it to)\b"), "hype phrase"),
        (re.compile(r"(?i)\b(?:don'?t sleep on|hidden gem|you won'?t regret|must[- ]have)\b"), "promotional cliche"),
        (re.compile(r"(?i)\b(?:robust|seamless|comprehensive|cutting[- ]edge|innovative)\b"), "corporate adjective"),
        (re.compile(r"(?i)\b(?:landscape|paradigm|synergy|ecosystem|holistic)\b"), "corporate noun"),
        # ── AI hedging / service phrases ──
        (re.compile(r"(?i)\bit(?:'?s| is) worth (?:noting|mentioning|pointing out)\b"), "AI hedging phrase"),
        (re.compile(r"(?i)\b(?:I'?d be happy to|feel free to|don't hesitate to)\b"), "AI service phrase"),
        (re.compile(r"(?i)\b(?:it'?s important to (?:note|remember|understand))\b"), "AI didactic phrase"),
        (re.compile(r"(?i)\b(?:I would (?:recommend|suggest|argue|say) that)\b"), "AI hedging recommendation"),
        # ── Bot-like closers ──
        (re.compile(r"(?i)(?:Hope this helps|Happy to help|Good luck|You'?ve got this)[!.]?\s*$"), "bot-like closer"),
        (re.compile(r"(?i)(?:Let me know if|Feel free to ask|Happy coding|Cheers!)\s*$"), "bot-like closer"),
        (re.compile(r"(?i)(?:Best of luck|Wishing you|All the best)[!.]?\s*$"), "bot-like closer"),
        # ── Unnatural superlatives ──
        (re.compile(r"(?i)\b(?:incredibly|remarkably|phenomenally|extraordinarily|insanely)\s+(?:useful|helpful|powerful|important|good)\b"), "unnatural superlative"),
        # ── AI empathy / validation ──
        (re.compile(r"(?i)^(?:I completely understand|I hear you|That's a great point|I can relate)"), "AI empathy opener"),
        (re.compile(r"(?i)^(?:What a great|Such a great|Really great)\s+(?:question|post|point|topic)"), "AI validation opener"),
        # ── AI favorite words ──
        (re.compile(r"(?i)\bdelve\s+(?:into|deeper)\b"), "LLM favorite word 'delve'"),
        (re.compile(r"(?i)\b(?:straightforward|arguably|nuanced|multifaceted)\b"), "LLM favorite word"),
        (re.compile(r"(?i)\b(?:a plethora of|a myriad of|a wealth of)\b"), "LLM quantity phrase"),
        # ── Repetitive sentence starts ──
        (re.compile(r"(?:^|\n)(I [a-z]+[^.!?]*[.!?]\s*I [a-z]+[^.!?]*[.!?]\s*I [a-z]+)"), "3+ sentences starting with I"),
        # ── LLM structural fingerprints ──
        (re.compile(r"(?i)the part about\b"), "LLM template phrase 'the part about'"),
        (re.compile(r"(?i)\bpersonally,?\s+I\b"), "LLM 'personally I' pattern"),
        (re.compile(r"(?i)\b(?:it'?s|this is) (?:kinda|pretty) (?:intriguing|interesting|fascinating)\b"), "forced casual pattern"),
        (re.compile(r"(?i)\bagentic breakthroughs?\b"), "leaked research context"),
        (re.compile(r"(?i)\bOpenClaw\b"), "leaked research context"),
        (re.compile(r"(?i)(?:^|\. )(?:nah|tbh|kinda|imo|fwiw)[,.]?\s+(?:nah|tbh|kinda|imo|fwiw)\b"), "forced slang stacking"),
    ]

    def validate(
        self,
        content: str,
        project: Dict,
        platform: str = "reddit",
        is_promotional: Optional[bool] = None,
    ) -> Tuple[bool, float, List[str]]:
        """Validate content against business profile.

        Returns:
            (is_valid, score, issues)
            - is_valid: True if content passes all critical checks
            - score: 0.0-1.0 quality score (clamped)
            - issues: list of issue descriptions
        """
        # Reject empty or trivially short comments. Return the same 3-tuple
        # shape as the rest of this method so adapters can always unpack it.
        if not content or len(content.strip()) < 15:
            return False, 0.0, ["empty_or_trivial: comment too short (<15 chars)"]

        issues = []

        # Initialize score before ANY check so the order of deductions can
        # never change the result. Every penalty below starts from 1.0.
        score = 1.0
        proj = project.get("project", project)
        profile = proj.get("business_profile", {})

        # Block RSS link spam
        rss_spam_patterns = [
            "found this earlier", "came across this", "saw this and thought",
            "stumbled upon this", "check this out:", "interesting read:",
            "news.google.com", "rss/articles/",
        ]
        lower_content = content.lower() if content else ""
        for rp in rss_spam_patterns:
            if rp in lower_content:
                issues.append(f"RSS spam pattern: '{rp}'")
                score -= 0.5
                break

        # Check 1: Product name accuracy
        name_issues = self._check_product_name(content, proj)
        issues.extend(name_issues)
        score -= len(name_issues) * 0.15

        # Check 2: URL accuracy
        url_issues = self._check_url(content, proj)
        issues.extend(url_issues)
        score -= len(url_issues) * 0.2

        # Check 3: Forbidden phrases
        forbidden_issues = self._check_forbidden(content, profile)
        issues.extend(forbidden_issues)
        score -= len(forbidden_issues) * 0.25

        # Check 4: Bot-like patterns
        bot_issues = self._check_bot_patterns(content)
        issues.extend(bot_issues)
        score -= len(bot_issues) * 0.15

        # Check 5: Length appropriateness
        length_issues = self._check_length(content, platform)
        issues.extend(length_issues)
        score -= len(length_issues) * 0.1

        # Check 6: Pricing claims
        if profile:
            claim_issues = self._check_pricing_claims(content, proj, profile)
            issues.extend(claim_issues)
            score -= len(claim_issues) * 0.15

        # Check 7: Organic mode enforcement — product/URL must NOT appear
        if is_promotional is False:
            organic_issues = self._check_organic_leakage(content, proj)
            issues.extend(organic_issues)
            score -= len(organic_issues) * 0.3

        # Clamp once, immediately before computing is_valid, so deductions
        # can never push the score below 0.0 or above 1.0.
        score = max(0.0, min(1.0, score))
        is_valid = score >= self.ACCEPTANCE_THRESHOLD and not any(
            "CRITICAL" in i for i in issues
        )

        if issues:
            logger.info(
                f"Content validation: score={score:.2f}, issues={issues}"
            )
        else:
            logger.debug(f"Content validation: score={score:.2f}, clean")

        return is_valid, score, issues

    def _check_product_name(self, content: str, proj: Dict) -> List[str]:
        """Check if product name appears with correct spelling."""
        issues = []
        name = proj.get("name", "")
        if not name or len(name) < 2:
            return issues

        name_lower = name.lower()
        content_lower = content.lower()

        if name_lower not in content_lower:
            return issues  # Product not mentioned, that's fine

        # Find all occurrences and check spelling
        for match in re.finditer(re.escape(name_lower), content_lower):
            start, end = match.start(), match.end()
            actual = content[start:end]
            if actual != name:
                # Case difference only → soft warning (casual Reddit style is OK)
                issues.append(
                    f"Product name case: '{actual}' vs '{name}'"
                )

        return issues

    def _check_url(self, content: str, proj: Dict) -> List[str]:
        """Check if URLs in content match the project URL."""
        issues = []
        correct_url = proj.get("url", "")
        if not correct_url:
            return issues

        correct_domain = (
            correct_url
            .replace("https://", "")
            .replace("http://", "")
            .rstrip("/")
        )

        # Find all URLs in content
        url_pattern = r"https?://[^\s\)\]>\"']+"
        found_urls = re.findall(url_pattern, content)

        for url in found_urls:
            domain = (
                url
                .replace("https://", "")
                .replace("http://", "")
                .split("/")[0]
            )
            # Check similarity — catch hallucinated close-but-wrong URLs
            similarity = SequenceMatcher(
                None, domain.lower(), correct_domain.lower()
            ).ratio()
            if similarity > 0.5 and domain.lower() != correct_domain.lower():
                issues.append(
                    f"CRITICAL: Wrong URL '{url}' "
                    f"(should be {correct_url})"
                )

        return issues

    def _check_forbidden(self, content: str, profile: Dict) -> List[str]:
        """Check for forbidden phrases from business profile rules."""
        issues = []
        rules = profile.get("rules", {})

        for phrase in rules.get("never_say", []):
            if phrase.lower() in content.lower():
                issues.append(
                    f"CRITICAL: Contains forbidden phrase: '{phrase}'"
                )

        return issues

    def _check_bot_patterns(self, content: str) -> List[str]:
        """Check for bot-like writing patterns (pre-compiled regex)."""
        issues = []
        for compiled_re, desc in self.BOT_PATTERNS:
            if compiled_re.search(content):
                issues.append(f"Bot-like pattern: {desc}")
        return issues

    def _check_length(self, content: str, platform: str) -> List[str]:
        """Check content length is appropriate."""
        issues = []
        word_count = len(content.split())

        if platform == "twitter" and len(content) > 280:
            issues.append(f"Tweet exceeds 280 chars ({len(content)})")
        elif platform == "reddit":
            if word_count < 15:
                issues.append(f"CRITICAL: Comment too short ({word_count} words, min 15)")
            elif word_count > 120:
                # Real Reddit comments that perform well are SHORT
                issues.append(f"CRITICAL: Comment too long ({word_count} words, max 120)")

        return issues

    def _check_organic_leakage(self, content: str, proj: Dict) -> List[str]:
        """Check that organic (non-promotional) content has NO product mentions.

        This is critical — organic comments that 'accidentally' mention the
        product are the #1 reason accounts get flagged as spam bots.
        """
        issues = []
        content_lower = content.lower()

        # Check product name
        name = proj.get("name", "")
        if name and len(name) >= 2 and name.lower() in content_lower:
            issues.append(
                f"CRITICAL: Product '{name}' mentioned in organic comment"
            )

        # Check alt names
        for alt in proj.get("alt_names", []):
            if alt and len(alt) >= 2 and alt.lower() in content_lower:
                issues.append(
                    f"CRITICAL: Product alt name '{alt}' in organic comment"
                )

        # Check project URL
        url = proj.get("url", "")
        if url:
            domain = (
                url.replace("https://", "").replace("http://", "").rstrip("/")
            )
            if domain.lower() in content_lower:
                issues.append(
                    f"CRITICAL: Product URL '{domain}' in organic comment"
                )

        # Check for URLs in organic comment (suspicious, but allow common reference sites)
        urls = re.findall(r"https?://([^\s/]+)", content)
        safe_domains = {
            "reddit.com", "wikipedia.org", "en.wikipedia.org",
            "stackoverflow.com", "github.com", "youtube.com",
            "docs.google.com", "imgur.com", "i.imgur.com",
        }
        for domain in urls:
            if not any(domain.endswith(safe) for safe in safe_domains):
                issues.append(f"URL in organic comment: {domain}")

        return issues

    def _check_pricing_claims(
        self, content: str, proj: Dict, profile: Dict
    ) -> List[str]:
        """Check that pricing claims match the business profile."""
        issues = []
        content_lower = content.lower()
        name_lower = proj.get("name", "").lower()

        # Only check if the product is actually mentioned
        if name_lower not in content_lower:
            return issues

        pricing = profile.get("pricing", {})
        if not pricing:
            return issues

        # If content says "free" but pricing model is not free/freemium
        if " free" in content_lower and pricing.get("model") not in (
            "free", "freemium"
        ):
            issues.append(
                "Claims product is free but pricing model is "
                f"'{pricing.get('model', 'unknown')}'"
            )

        # Check for price amounts that don't match known plans
        price_pattern = r"\$\d+(?:\.\d{2})?"
        mentioned_prices = re.findall(price_pattern, content)
        if mentioned_prices and pricing.get("paid_plans"):
            valid_prices = [
                p.get("price", "") for p in pricing["paid_plans"]
            ]
            for mp in mentioned_prices:
                if not any(mp in vp for vp in valid_prices):
                    issues.append(
                        f"Price {mp} not found in business profile"
                    )

        return issues
