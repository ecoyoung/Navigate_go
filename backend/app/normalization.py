import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src"}


def normalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    path = parts.path or "/"
    query = urlencode(
        sorted(
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
        ),
        doseq=True,
    )
    return urlunsplit((scheme, hostname, path, query, ""))


def identity_key(
    external_id: str | None, canonical_url: str | None, title: str, published: str
) -> str:
    if external_id:
        basis = f"guid:{external_id.strip()}"
    elif canonical_url:
        basis = f"url:{canonical_url}"
    else:
        basis = f"fallback:{title.strip()}|{published.strip()}"
    return hashlib.sha256(basis.encode()).hexdigest()
