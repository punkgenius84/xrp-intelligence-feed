    duplicate_sources: list[str] = field(default_factory=list)\n    duplicate_urls: list[str] = field(default_factory=list)\n    # Conservative cross-source correlation metadata. Correlation never implies truth or relevance.
    correlated_source_ids: list[str] = field(default_factory=list)
    correlated_candidate_ids: list[str] = field(default_factory=list)
    correlation_reasons: list[str] = field(default_factory=list)
    correlation_score: int = 0
    # Optional discovery metadata. Existing RSS callers and positional fields remain compatible.
    candidate_id: str = ""
    discovery_method: str = ""
    source_url: str = ""
    source_native_id: str = ""
    document_type: str = ""
    content_hash: str = ""
    etag: str = ""
    last_modified: str = ""
    change_kind: str = "new"
    primary_url: str = ""
    discovery_lead_url: str = ""
    provenance: list[str] = field(default_factory=list)
    duplicate_of: str = ""
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    source_status: str = ""
    source_error: str = ""
    source_native_metadata: dict[str, str] = field(default_factory=dict)

    @property
    def canonical_url(self) -> str:
        return self.url

    @property
    def publisher(self) -> str:
        return self.source

    @property
    def publication_time(self) -> datetime | None:
        return self.published_at

    @property
    def discovered_time(self) -> datetime:
        return self.collected_at

    @property
    def fingerprint(self) -> str:
        # Preserve the v0.1.1 key so existing state entries remain valid.
        raw = f"{self.url.strip().lower()}|{self.title.strip().lower()}"
        return sha256(raw.encode("utf-8")).hexdigest()

    @property
    def event_fingerprint(self) -> str:
        """Fingerprint a sufficiently distinctive headline within its event day."""
        normalized = unicodedata.normalize("NFKC", self.title).casefold()
        tokens = re.findall(r"[a-z0-9]+", normalized)
        # Short/generic headlines are too collision-prone; fall back to URL+title.
        if len(tokens) < 4 or len("".join(tokens)) < 20:
            return self.fingerprint
        event_time = self.published_at or self.collected_at
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        event_day = event_time.astimezone(timezone.utc).date().isoformat()
        return sha256(("headline|" + event_day + "|" + " ".join(tokens)).encode("utf-8")).hexdigest()
