import hashlib
import json
import warnings
from datetime import datetime
from typing import Literal
from urllib.parse import urljoin
from xml.etree import ElementTree

import feedparser
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .normalization import normalize_url
from .outbound_policy import UnsafeOutboundURLError, validate_public_http_url
from .web_ingestion import discover_article_urls

PROBE_SCHEMA_VERSION = "source-probe-result.v1"
PIPELINE_SCHEMA_VERSION = "source-pipeline.v1"
PROBE_ENGINE_VERSION = "source-probe.rules.v1"
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ANALYSIS_BODY_LIMIT = 512 * 1024

DetectedFormat = Literal[
    "blocked",
    "rss",
    "atom",
    "sitemap_urlset",
    "sitemap_index",
    "json",
    "html",
    "empty",
    "unknown",
]
ProbeOutcome = Literal["success", "partial", "blocked", "unreachable", "invalid"]
AccessLevel = Literal[
    "public",
    "partial",
    "subscriber",
    "robots_blocked",
    "unknown",
]
RobotsStatus = Literal["allowed", "disallowed", "unavailable", "not_checked"]


class ProbeDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_url: str
    final_url: str
    observed_at: datetime
    status_code: int = Field(ge=0, le=599)
    content_type: str | None = None
    body: str = Field(default="", max_length=DEFAULT_MAX_RESPONSE_BYTES)
    robots_status: RobotsStatus = "not_checked"
    robots_url: str | None = None

    @model_validator(mode="after")
    def validate_document(self):
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must include timezone")
        validate_public_http_url(self.requested_url, resolve_dns=False)
        validate_public_http_url(self.final_url, resolve_dns=False)
        if self.robots_url:
            validate_public_http_url(self.robots_url, resolve_dns=False)
        return self


class ProbeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str
    code: str
    confidence: float = Field(ge=0, le=1)
    details: dict = Field(default_factory=dict)


class ProbeCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str
    resource_kind: Literal["html", "rss", "atom", "sitemap", "json_api"]
    url: str
    confidence: float = Field(ge=0, le=1)
    verified: bool
    evidence_refs: list[str] = Field(min_length=1)


class AccessAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: AccessLevel
    robots: RobotsStatus
    challenges: list[
        Literal["javascript", "captcha", "login", "paywall", "rate_limit", "maintenance"]
    ] = Field(default_factory=list)


class PipelineProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    probe_id: str
    candidate_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class SourcePipeline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["source-pipeline.v1"] = PIPELINE_SCHEMA_VERSION
    pipeline_id: str = Field(pattern=r"^pipeline-[0-9a-f]{16}$")
    state: Literal["draft", "verified", "blocked", "unsupported"]
    source_family: Literal["website", "data_provider"] = "website"
    channel_type: Literal["web", "rss", "api", "third_party_feed"] | None = None
    provider: str = "direct"
    engine: Literal[
        "static_http",
        "feed_direct",
        "sitemap_http",
        "json_api",
        "provider_api",
        "browser_rendered",
    ] | None = None
    start_url: str
    discovery_url: str | None = None
    discovery_chain: list[
        Literal["official_feed", "json_listing", "sitemap", "html_listing", "manual_seed"]
    ] = Field(default_factory=list)
    content_chain: list[
        Literal["feed_full_content", "feed_summary", "html_detail", "json_detail", "metadata_only"]
    ] = Field(default_factory=list)
    parser_chain: list[
        Literal[
            "feed_parser",
            "structured_data",
            "configured_selector",
            "generic_article_parser",
            "json_mapping",
        ]
    ] = Field(default_factory=list)
    incremental_strategy: list[
        Literal[
            "etag",
            "last_modified",
            "external_id",
            "cursor",
            "updated_watermark",
            "published_watermark",
            "content_hash_overlap",
        ]
    ] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    reason_code: str | None = None
    requires_verification: list[str] = Field(default_factory=list)
    provenance: PipelineProvenance

    @model_validator(mode="after")
    def validate_pipeline(self):
        if self.state in {"draft", "verified"} and (not self.channel_type or not self.engine):
            raise ValueError("draft/verified pipeline requires channel_type and engine")
        if self.state in {"blocked", "unsupported"} and not self.reason_code:
            raise ValueError("blocked/unsupported pipeline requires reason_code")
        if self.channel_type == "third_party_feed" and self.provider == "direct":
            raise ValueError("third_party_feed requires a provider")
        if self.engine == "browser_rendered" and self.state != "unsupported":
            raise ValueError("browser_rendered is not an enabled engine")
        return self


class SourceProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["source-probe-result.v1"] = PROBE_SCHEMA_VERSION
    probe_engine_version: Literal["source-probe.rules.v1"] = PROBE_ENGINE_VERSION
    probe_id: str
    requested_url: str
    final_url: str
    probed_at: datetime
    outcome: ProbeOutcome
    detected_format: DetectedFormat
    subtype: str | None = None
    confidence: float = Field(ge=0, le=1)
    http_status: int
    content_type: str | None
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access: AccessAssessment
    evidence: list[ProbeEvidence]
    candidates: list[ProbeCandidate]
    article_samples: list[str] = Field(default_factory=list)
    json_item_paths: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    recommended_pipeline: SourcePipeline

    @model_validator(mode="after")
    def validate_references(self):
        evidence_refs = {item.ref for item in self.evidence}
        if len(evidence_refs) != len(self.evidence):
            raise ValueError("evidence refs must be unique")
        candidate_ids = {item.candidate_id for item in self.candidates}
        if len(candidate_ids) != len(self.candidates):
            raise ValueError("candidate ids must be unique")
        for candidate in self.candidates:
            if not set(candidate.evidence_refs) <= evidence_refs:
                raise ValueError("candidate references missing evidence")
        if self.recommended_pipeline.provenance.probe_id != self.probe_id:
            raise ValueError("pipeline provenance must reference this probe")
        pipeline_candidate = self.recommended_pipeline.provenance.candidate_id
        if pipeline_candidate and pipeline_candidate not in candidate_ids:
            raise ValueError("pipeline candidate must reference this probe")
        if self.outcome == "success" and not self.candidates:
            raise ValueError("successful probe requires candidates")
        if self.outcome in {"blocked", "unreachable", "invalid"} and any(
            item.verified for item in self.candidates
        ):
            raise ValueError("blocked or invalid probe cannot contain verified candidates")
        return self


def _content_type(value: str | None) -> str | None:
    return value.split(";", 1)[0].strip().lower() if value else None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _probe_id(document: ProbeDocument, body_sha256: str) -> str:
    basis = "|".join(
        [
            PROBE_ENGINE_VERSION,
            normalize_url(document.requested_url),
            normalize_url(document.final_url),
            str(document.status_code),
            body_sha256,
        ]
    )
    return f"sha256:{hashlib.sha256(basis.encode()).hexdigest()}"


def _pipeline_id(probe_id: str) -> str:
    return f"pipeline-{probe_id.removeprefix('sha256:')[:16]}"


def _find_json_list_paths(value: object, prefix: str = "$", depth: int = 0) -> list[str]:
    if depth > 3:
        return []
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value[:10]):
            return [prefix]
        return []
    if not isinstance(value, dict):
        return []
    paths: list[str] = []
    for key, item in value.items():
        path = str(key) if prefix == "$" else f"{prefix}.{key}"
        paths.extend(_find_json_list_paths(item, path, depth + 1))
    return paths[:10]


def _html_block(document: ProbeDocument, soup: BeautifulSoup) -> tuple[str | None, list[str]]:
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    text = soup.get_text(" ", strip=True)[:2000].lower()
    challenges: list[str] = []
    if document.status_code == 429:
        return "rate_limited", ["rate_limit"]
    if document.status_code in {401, 407}:
        return "auth_gate", ["login"]
    if document.status_code == 451:
        return "policy_blocked", []
    if document.status_code == 410:
        return "source_gone", []
    if document.status_code == 403:
        return "access_denied", []

    challenge_selector = (
        "#challenge-form, [id*='cf-chl'], [class*='cf-chl'], "
        "#wappoc_appmsgcaptcha, iframe[src*='captcha'], form[action*='captcha']"
    )
    challenge_title = any(
        marker in title
        for marker in ("just a moment", "security check", "access denied", "安全验证")
    )
    if soup.select_one(challenge_selector) or (
        challenge_title and len(text) < 1200 and not soup.select_one("article")
    ):
        return "challenge", ["captcha"]
    if soup.select_one("input[type='password']") and len(text) < 2000:
        return "auth_gate", ["login"]
    if any(marker in title for marker in ("maintenance", "service unavailable", "系统维护")):
        return "maintenance", ["maintenance"]
    if document.status_code == 503 and len(text) < 2000:
        return "maintenance", ["maintenance"]
    if any(marker in title for marker in ("404", "page not found", "页面不存在", "内容已删除")):
        return "soft_not_found", challenges
    return None, challenges


def _feed_subtype(parsed) -> str:
    entries = list(parsed.entries)
    if not entries:
        return "link_only_feed"
    content_lengths = [
        max((len(str(part.get("value") or "")) for part in entry.get("content", [])), default=0)
        for entry in entries[:10]
    ]
    if content_lengths and max(content_lengths) >= 500:
        return "full_feed"
    if any(str(entry.get("summary") or "").strip() for entry in entries[:10]):
        return "summary_feed"
    return "link_only_feed"


def _candidate(
    candidates: list[ProbeCandidate],
    *,
    kind: Literal["html", "rss", "atom", "sitemap", "json_api"],
    url: str,
    confidence: float,
    verified: bool,
    evidence_refs: list[str],
) -> ProbeCandidate:
    item = ProbeCandidate(
        candidate_id=f"candidate_{len(candidates) + 1:02d}",
        resource_kind=kind,
        url=normalize_url(url),
        confidence=confidence,
        verified=verified,
        evidence_refs=evidence_refs,
    )
    candidates.append(item)
    return item


def _blocked_pipeline(
    probe_id: str,
    document: ProbeDocument,
    evidence: list[ProbeEvidence],
    reason: str,
    confidence: float,
) -> SourcePipeline:
    return SourcePipeline(
        pipeline_id=_pipeline_id(probe_id),
        state="blocked",
        start_url=normalize_url(document.requested_url),
        confidence=confidence,
        reason_code=reason,
        provenance=PipelineProvenance(
            probe_id=probe_id,
            evidence_refs=[item.ref for item in evidence],
        ),
    )


def analyze_probe_document(document: ProbeDocument) -> SourceProbeResult:
    body = document.body[:ANALYSIS_BODY_LIMIT]
    body_sha256 = hashlib.sha256(document.body.encode()).hexdigest()
    probe_id = _probe_id(document, body_sha256)
    content_type = _content_type(document.content_type)
    evidence: list[ProbeEvidence] = []
    candidates: list[ProbeCandidate] = []
    diagnostics: list[str] = []
    article_samples: list[str] = []
    json_item_paths: list[str] = []

    def add(code: str, confidence: float, **details) -> str:
        ref = f"ev_{len(evidence) + 1:02d}"
        evidence.append(
            ProbeEvidence(ref=ref, code=code, confidence=confidence, details=details)
        )
        return ref

    status_ref = add("http_status", 1.0, status=document.status_code)
    if content_type:
        add("content_type", 0.8, value=content_type)
    if len(document.body) > ANALYSIS_BODY_LIMIT:
        diagnostics.append("analysis_body_truncated")

    if document.robots_status == "disallowed":
        ref = add("robots_disallowed", 1.0, url=document.robots_url)
        pipeline = _blocked_pipeline(probe_id, document, evidence, "robots_disallowed", 1.0)
        return SourceProbeResult(
            probe_id=probe_id,
            requested_url=normalize_url(document.requested_url),
            final_url=normalize_url(document.final_url),
            probed_at=document.observed_at,
            outcome="blocked",
            detected_format="blocked",
            subtype="policy_blocked",
            confidence=1.0,
            http_status=document.status_code,
            content_type=content_type,
            body_sha256=body_sha256,
            access=AccessAssessment(level="robots_blocked", robots="disallowed"),
            evidence=evidence,
            candidates=[],
            diagnostics=["robots_disallowed"],
            recommended_pipeline=pipeline,
        )

    stripped = body.lstrip("\ufeff\r\n\t ")
    if not stripped:
        ref = add("empty_body", 0.98)
        pipeline = SourcePipeline(
            pipeline_id=_pipeline_id(probe_id),
            state="unsupported",
            start_url=normalize_url(document.requested_url),
            confidence=0.98,
            reason_code="empty_response",
            provenance=PipelineProvenance(probe_id=probe_id, evidence_refs=[status_ref, ref]),
        )
        return SourceProbeResult(
            probe_id=probe_id,
            requested_url=normalize_url(document.requested_url),
            final_url=normalize_url(document.final_url),
            probed_at=document.observed_at,
            outcome="invalid",
            detected_format="empty",
            subtype="empty_response",
            confidence=0.98,
            http_status=document.status_code,
            content_type=content_type,
            body_sha256=body_sha256,
            access=AccessAssessment(level="unknown", robots=document.robots_status),
            evidence=evidence,
            candidates=[],
            diagnostics=["empty_response"],
            recommended_pipeline=pipeline,
        )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(body, "lxml")
    block_reason, challenges = _html_block(document, soup)
    if block_reason:
        confidence = 1.0 if block_reason == "policy_blocked" else 0.97
        ref = add(block_reason, confidence)
        pipeline = _blocked_pipeline(probe_id, document, evidence, block_reason, confidence)
        level: AccessLevel = "subscriber" if block_reason == "auth_gate" else "unknown"
        return SourceProbeResult(
            probe_id=probe_id,
            requested_url=normalize_url(document.requested_url),
            final_url=normalize_url(document.final_url),
            probed_at=document.observed_at,
            outcome="blocked",
            detected_format="blocked",
            subtype=block_reason,
            confidence=confidence,
            http_status=document.status_code,
            content_type=content_type,
            body_sha256=body_sha256,
            access=AccessAssessment(
                level=level,
                robots=document.robots_status,
                challenges=challenges,
            ),
            evidence=evidence,
            candidates=[],
            diagnostics=[block_reason],
            recommended_pipeline=pipeline,
        )

    root = None
    try:
        root = ElementTree.fromstring(stripped)
    except ElementTree.ParseError:
        pass
    root_name = _local_name(root.tag) if root is not None else ""
    detected_format: DetectedFormat
    subtype: str | None
    confidence: float
    primary: ProbeCandidate | None = None

    if root_name == "rss":
        root_ref = add("rss_root", 0.99)
        parsed = feedparser.parse(body)
        entries = list(parsed.entries)
        if entries:
            add("feed_entries", 0.95, count=len(entries))
        subtype = _feed_subtype(parsed)
        confidence = 0.98 if entries else 0.84
        detected_format = "rss"
        primary = _candidate(
            candidates,
            kind="rss",
            url=document.final_url,
            confidence=confidence,
            verified=True,
            evidence_refs=[root_ref],
        )
    elif root_name == "feed":
        namespace = root.tag.partition("}")[0].lstrip("{")
        entries = [item for item in root.iter() if _local_name(item.tag) == "entry"]
        root_ref = add("atom_root", 0.99, namespace=namespace)
        if entries:
            add("feed_entries", 0.95, count=len(entries))
        subtype = _feed_subtype(feedparser.parse(body))
        confidence = 0.98 if entries and "Atom" in namespace else 0.86
        detected_format = "atom"
        primary = _candidate(
            candidates,
            kind="atom",
            url=document.final_url,
            confidence=confidence,
            verified=True,
            evidence_refs=[root_ref],
        )
    elif root_name in {"urlset", "sitemapindex"}:
        locs = [
            (item.text or "").strip()
            for item in root.iter()
            if _local_name(item.tag) == "loc" and (item.text or "").strip()
        ]
        valid_locs = []
        for item in locs:
            try:
                validate_public_http_url(item, resolve_dns=False)
            except UnsafeOutboundURLError:
                continue
            valid_locs.append(normalize_url(item))
        root_ref = add(f"sitemap_{root_name}", 0.99, locations=len(valid_locs))
        detected_format = "sitemap_index" if root_name == "sitemapindex" else "sitemap_urlset"
        subtype = detected_format
        confidence = 0.98 if valid_locs else 0.82
        article_samples = valid_locs[:10] if root_name == "urlset" else []
        primary = _candidate(
            candidates,
            kind="sitemap",
            url=document.final_url,
            confidence=confidence,
            verified=True,
            evidence_refs=[root_ref],
        )
        if root_name == "sitemapindex":
            diagnostics.append("child_sitemaps_require_probe")
    else:
        json_value = None
        try:
            json_value = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        if json_value is not None:
            ref = add("json_parsed", 0.95, root_type=type(json_value).__name__)
            json_item_paths = _find_json_list_paths(json_value)
            error_keys = (
                set(json_value) & {"error", "errors", "code", "message"}
                if isinstance(json_value, dict)
                else set()
            )
            if error_keys and not json_item_paths:
                detected_format = "json"
                subtype = "json_error"
                confidence = 0.9
                diagnostics.append("json_error_object")
                pipeline = SourcePipeline(
                    pipeline_id=_pipeline_id(probe_id),
                    state="unsupported",
                    start_url=normalize_url(document.requested_url),
                    confidence=confidence,
                    reason_code="json_error_object",
                    provenance=PipelineProvenance(probe_id=probe_id, evidence_refs=[ref]),
                )
            else:
                detected_format = "json"
                subtype = "json_listing" if json_item_paths else "json_detail"
                confidence = 0.9 if json_item_paths else 0.72
                primary = _candidate(
                    candidates,
                    kind="json_api",
                    url=document.final_url,
                    confidence=confidence,
                    verified=True,
                    evidence_refs=[ref],
                )
                if content_type not in {"application/json"} and not (
                    content_type and content_type.endswith("+json")
                ):
                    diagnostics.append("content_type_mismatch")
        elif soup.find("html") or content_type == "text/html":
            html_ref = add("html_document", 0.9)
            alternate_refs: list[str] = []
            for link in soup.select("link[rel~='alternate'][href]"):
                mime = str(link.get("type") or "").lower()
                if mime not in {"application/rss+xml", "application/atom+xml"}:
                    continue
                kind: Literal["rss", "atom"] = (
                    "atom" if mime == "application/atom+xml" else "rss"
                )
                href = normalize_url(urljoin(document.final_url, str(link["href"])))
                ref = add("alternate_feed", 0.92, url=href, type=mime)
                alternate_refs.append(ref)
                _candidate(
                    candidates,
                    kind=kind,
                    url=href,
                    confidence=0.92,
                    verified=False,
                    evidence_refs=[ref],
                )
            config = {"max_articles": 10}
            article_samples = discover_article_urls(body, document.final_url, config)
            has_article_schema = any(
                token in body for token in ('"NewsArticle"', '"Article"', '"BlogPosting"')
            )
            is_article = bool(
                has_article_schema
                or soup.select_one("meta[property='og:type'][content='article']")
                or (soup.select_one("article") and len(soup.get_text(" ", strip=True)) >= 500)
            )
            is_listing = len(article_samples) >= 2
            detected_format = "html"
            subtype = (
                "html_listing"
                if is_listing
                else "html_article"
                if is_article
                else "html_unknown"
            )
            confidence = 0.9 if is_listing or is_article else 0.7
            if is_listing:
                listing_ref = add("article_link_candidates", 0.88, count=len(article_samples))
                primary = _candidate(
                    candidates,
                    kind="html",
                    url=document.final_url,
                    confidence=confidence,
                    verified=True,
                    evidence_refs=[html_ref, listing_ref],
                )
            if len(soup.get_text(" ", strip=True)) < 200 and len(soup.select("script")) >= 3:
                diagnostics.append("possible_javascript_shell")
            if alternate_refs:
                diagnostics.append("alternate_feed_requires_probe")
        else:
            ref = add("unsupported_document", 0.7)
            detected_format = "unknown"
            subtype = "unknown"
            confidence = 0.7
            pipeline = SourcePipeline(
                pipeline_id=_pipeline_id(probe_id),
                state="unsupported",
                start_url=normalize_url(document.requested_url),
                confidence=confidence,
                reason_code="unsupported_document",
                provenance=PipelineProvenance(probe_id=probe_id, evidence_refs=[ref]),
            )

    if "pipeline" not in locals():
        verified_feed = next(
            (
                item
                for item in candidates
                if item.verified and item.resource_kind in {"rss", "atom"}
            ),
            None,
        )
        unverified_feed = next(
            (
                item
                for item in candidates
                if not item.verified and item.resource_kind in {"rss", "atom"}
            ),
            None,
        )
        chosen = verified_feed or unverified_feed or primary
        if detected_format in {"rss", "atom"} or unverified_feed:
            if subtype == "full_feed" and verified_feed is not None:
                content_chain = ["feed_full_content"]
            elif subtype == "summary_feed" and verified_feed is not None:
                content_chain = ["feed_summary", "html_detail"]
            else:
                content_chain = ["html_detail"]
            pipeline = SourcePipeline(
                pipeline_id=_pipeline_id(probe_id),
                state="draft",
                channel_type="rss",
                engine="feed_direct",
                start_url=normalize_url(document.requested_url),
                discovery_url=(chosen.url if chosen else normalize_url(document.final_url)),
                discovery_chain=["official_feed"],
                content_chain=content_chain,
                parser_chain=["feed_parser", "structured_data", "generic_article_parser"],
                incremental_strategy=[
                    "etag",
                    "last_modified",
                    "external_id",
                    "content_hash_overlap",
                ],
                confidence=chosen.confidence if chosen else confidence,
                requires_verification=(
                    [] if verified_feed else ["verify_feed_endpoint", "verify_feed_completeness"]
                ),
                provenance=PipelineProvenance(
                    probe_id=probe_id,
                    candidate_id=chosen.candidate_id if chosen else None,
                    evidence_refs=chosen.evidence_refs if chosen else [],
                ),
            )
        elif detected_format in {"sitemap_urlset", "sitemap_index"}:
            pipeline = SourcePipeline(
                pipeline_id=_pipeline_id(probe_id),
                state="draft",
                channel_type="web",
                engine="sitemap_http",
                start_url=normalize_url(document.requested_url),
                discovery_url=normalize_url(document.final_url),
                discovery_chain=["sitemap"],
                content_chain=["html_detail"],
                parser_chain=[
                    "structured_data",
                    "configured_selector",
                    "generic_article_parser",
                ],
                incremental_strategy=[
                    "last_modified",
                    "published_watermark",
                    "content_hash_overlap",
                ],
                confidence=confidence,
                requires_verification=(
                    ["probe_child_sitemaps"]
                    if detected_format == "sitemap_index"
                    else ["verify_article_detail_samples"]
                ),
                provenance=PipelineProvenance(
                    probe_id=probe_id,
                    candidate_id=primary.candidate_id if primary else None,
                    evidence_refs=primary.evidence_refs if primary else [],
                ),
            )
        elif detected_format == "json" and subtype != "json_error":
            pipeline = SourcePipeline(
                pipeline_id=_pipeline_id(probe_id),
                state="draft",
                channel_type="api",
                engine="json_api",
                start_url=normalize_url(document.requested_url),
                discovery_url=normalize_url(document.final_url),
                discovery_chain=["json_listing"],
                content_chain=["json_detail"],
                parser_chain=["json_mapping"],
                incremental_strategy=[
                    "external_id",
                    "cursor",
                    "updated_watermark",
                    "content_hash_overlap",
                ],
                confidence=confidence,
                requires_verification=["verify_json_mapping", "verify_pagination"],
                provenance=PipelineProvenance(
                    probe_id=probe_id,
                    candidate_id=primary.candidate_id if primary else None,
                    evidence_refs=primary.evidence_refs if primary else [],
                ),
            )
        elif detected_format == "html" and subtype == "html_listing":
            pipeline = SourcePipeline(
                pipeline_id=_pipeline_id(probe_id),
                state="draft",
                channel_type="web",
                engine="static_http",
                start_url=normalize_url(document.requested_url),
                discovery_url=normalize_url(document.final_url),
                discovery_chain=["html_listing"],
                content_chain=["html_detail"],
                parser_chain=[
                    "structured_data",
                    "configured_selector",
                    "generic_article_parser",
                ],
                incremental_strategy=[
                    "last_modified",
                    "published_watermark",
                    "content_hash_overlap",
                ],
                confidence=confidence,
                requires_verification=["verify_listing_and_article_selectors"],
                provenance=PipelineProvenance(
                    probe_id=probe_id,
                    candidate_id=primary.candidate_id if primary else None,
                    evidence_refs=primary.evidence_refs if primary else [],
                ),
            )
        else:
            reason = "article_entry_not_listing" if subtype == "html_article" else "manual_review"
            pipeline = SourcePipeline(
                pipeline_id=_pipeline_id(probe_id),
                state="unsupported",
                start_url=normalize_url(document.requested_url),
                confidence=confidence,
                reason_code=reason,
                requires_verification=["provide_listing_or_feed_entry"],
                provenance=PipelineProvenance(
                    probe_id=probe_id,
                    evidence_refs=[item.ref for item in evidence],
                ),
            )

    recommended_candidate = next(
        (
            item
            for item in candidates
            if item.candidate_id == pipeline.provenance.candidate_id
        ),
        None,
    )
    outcome: ProbeOutcome = (
        "success"
        if pipeline.state == "draft"
        and recommended_candidate is not None
        and recommended_candidate.verified
        else "partial"
        if candidates or detected_format in {"html", "json"}
        else "invalid"
    )
    return SourceProbeResult(
        probe_id=probe_id,
        requested_url=normalize_url(document.requested_url),
        final_url=normalize_url(document.final_url),
        probed_at=document.observed_at,
        outcome=outcome,
        detected_format=detected_format,
        subtype=subtype,
        confidence=confidence,
        http_status=document.status_code,
        content_type=content_type,
        body_sha256=body_sha256,
        access=AccessAssessment(level="public", robots=document.robots_status),
        evidence=evidence,
        candidates=candidates,
        article_samples=article_samples,
        json_item_paths=json_item_paths,
        diagnostics=diagnostics,
        recommended_pipeline=pipeline,
    )


def pipeline_to_legacy_parser_config(pipeline: SourcePipeline) -> dict:
    if pipeline.state != "verified":
        raise ValueError("only a verified pipeline can be compiled")
    method_by_engine = {
        "static_http": "html",
        "feed_direct": "feed",
        "sitemap_http": "sitemap",
        "json_api": "json",
        "provider_api": "json",
    }
    discovery_method = method_by_engine.get(str(pipeline.engine))
    if not discovery_method:
        raise ValueError("pipeline engine is not executable")
    config: dict = {
        "pipeline_schema_version": pipeline.schema_version,
        "pipeline_id": pipeline.pipeline_id,
        "probe_id": pipeline.provenance.probe_id,
        "execution_engine": str(pipeline.engine),
        "discovery_method": discovery_method,
        "access_level": "public",
    }
    if pipeline.discovery_url:
        config["discovery_url"] = pipeline.discovery_url
    if pipeline.provider != "direct":
        config["provider"] = pipeline.provider
    if "feed_full_content" in pipeline.content_chain:
        config["ingest_feed_content"] = True
        config["content_completeness"] = "full"
    elif "feed_summary" in pipeline.content_chain:
        config["content_completeness"] = "partial"
    if "json_detail" in pipeline.content_chain:
        config["article_response_format"] = "json"
    return config
