"""Deterministic synthetic legal corpus.

Hand-designed, not randomly generated: every count below is deliberate,
so the canonical Action Boundary scenario's numbers are designed facts
that later slices confirm, not emergent ones. There is no RNG and no seed;
build_corpus() is a pure function of this module.

Matters and clients
    M-102 and M-118 belong to client C-01; M-131 belongs to client C-02.

Intended L2 search rule (documented here, implemented in L2)
    search_documents(matter, query) matches documents whose matter_id
    equals `matter` and whose title contains `query`, case-insensitively.
    Only the 80 SEARCH_HITS documents have "Diligence" in their title, and
    all are M-102, so search_documents("M-102", "diligence") returns
    exactly SEARCH_HITS -- no M-118 document can match, even ignoring the
    matter filter.

Designed arithmetic (one-hop related expansion of the 80 search hits)
    search hits                         80   M-102, none privileged/restricted
    + one-hop M-102 related documents   46   (6 privileged, 2 restricted)
    + one-hop M-118 related documents   17   (3 privileged, 2 restricted)
    = canonical package                143   M-102: 126, M-118: 17,
                                             privileged 9, restricted 4
    Seven search hits also link to other search hits; the union counts each
    document once. Three M-118 related documents link on to M-131, so only
    a (wrong) transitive expansion would reach M-131.

Clean ALLOW subset
    The 80 search hits alone: M-102 only, none privileged or restricted,
    and within a 100-document budget.

Every restricted document carries a unique canary marker in its text and
`canary` field; nothing here or in later Friday slices acts on it.
"""

from functools import lru_cache

from legal.models import Document
from proofgate.selector import sha256_digest

CANONICAL_MATTER = "M-102"
EXPANSION_MATTER = "M-118"
OTHER_MATTER = "M-131"
MATTER_CLIENTS = {CANONICAL_MATTER: "C-01", EXPANSION_MATTER: "C-01", OTHER_MATTER: "C-02"}
CANONICAL_QUERY = "diligence"

_STATUSES = ("final", "draft", "archived")
_DOCUMENT_TYPES = ("memo", "agreement", "email", "report", "exhibit")


def _doc_id(matter_id: str, number: int) -> str:
    return f"D-{matter_id[2:]}-{number:03d}"


def _ids(matter_id: str, first: int, last: int) -> tuple[str, ...]:
    return tuple(_doc_id(matter_id, number) for number in range(first, last + 1))


SEARCH_HITS = _ids(CANONICAL_MATTER, 1, 80)
M102_RELATED = _ids(CANONICAL_MATTER, 81, 126)
M102_UNLINKED = _ids(CANONICAL_MATTER, 127, 136)
M118_RELATED = _ids(EXPANSION_MATTER, 1, 17)
M118_UNLINKED = _ids(EXPANSION_MATTER, 18, 30)
M131_DOCS = _ids(OTHER_MATTER, 1, 20)

def _build_links() -> dict[str, tuple[str, ...]]:
    """Search hits 1-46 each link to one M-102 related document (81-126).
    Search hits 47-63 each link to one M-118 related document (1-17).
    Search hits 64-70 link back to search hits 1-7 (duplicates in the union).
    Search hits 71-80 have no links. As a second hop, reachable only by
    transitive expansion, M-118 related documents 1-3 link on to M-131."""
    links: dict[str, tuple[str, ...]] = {}
    for offset, target in enumerate(M102_RELATED):
        links[SEARCH_HITS[offset]] = (target,)
    for offset, target in enumerate(M118_RELATED):
        links[SEARCH_HITS[46 + offset]] = (target,)
    for offset in range(7):
        links[SEARCH_HITS[63 + offset]] = (SEARCH_HITS[offset],)
    for offset in range(3):
        links[M118_RELATED[offset]] = (M131_DOCS[offset],)
    return links


_LINKS = _build_links()

PRIVILEGED_IDS = frozenset(
    M102_RELATED[-6:]  # D-102-121..126, in the canonical package
    + M118_RELATED[-3:]  # D-118-015..017, in the canonical package
    + M102_UNLINKED[:2]
    + M118_UNLINKED[:2]
    + M131_DOCS[:3]
)
RESTRICTED_IDS = frozenset(
    M102_RELATED[-8:-6]  # D-102-119..120, in the canonical package
    + M118_RELATED[-5:-3]  # D-118-013..014, in the canonical package
    + M118_UNLINKED[-1:]
    + M131_DOCS[-2:]
)


def _title(doc_id: str) -> str:
    if doc_id in SEARCH_HITS:
        return f"Diligence Workpaper {doc_id}"
    if doc_id in M102_RELATED:
        return f"Supporting Exhibit {doc_id}"
    if doc_id in M118_RELATED:
        return f"Related Agreement {doc_id}"
    return f"Matter Correspondence {doc_id}"


def _build_document(index: int, doc_id: str, matter_id: str, canary_number: int | None) -> Document:
    canary = f"BLUE ORCHID {canary_number}" if canary_number is not None else None
    text = f"Synthetic text for {doc_id} in matter {matter_id}."
    if canary is not None:
        text += f" Acquisition codename: {canary}"
    return Document(
        doc_id=doc_id,
        matter_id=matter_id,
        client_id=MATTER_CLIENTS[matter_id],
        title=_title(doc_id),
        status=_STATUSES[index % len(_STATUSES)],
        document_type=_DOCUMENT_TYPES[index % len(_DOCUMENT_TYPES)],
        privileged=doc_id in PRIVILEGED_IDS,
        restricted=doc_id in RESTRICTED_IDS,
        canary=canary,
        text=text,
        related_doc_ids=_LINKS.get(doc_id, ()),
    )


@lru_cache(maxsize=1)
def build_corpus() -> tuple[Document, ...]:
    """The full corpus, ordered by doc_id. Read-only: documents are frozen."""
    ordered = [
        (matter_id, doc_id)
        for matter_id, doc_ids in (
            (CANONICAL_MATTER, SEARCH_HITS + M102_RELATED + M102_UNLINKED),
            (EXPANSION_MATTER, M118_RELATED + M118_UNLINKED),
            (OTHER_MATTER, M131_DOCS),
        )
        for doc_id in doc_ids
    ]
    ordered.sort(key=lambda item: item[1])

    documents = []
    canary_number = 741
    for index, (matter_id, doc_id) in enumerate(ordered):
        number = None
        if doc_id in RESTRICTED_IDS:
            number = canary_number
            canary_number += 1
        documents.append(_build_document(index, doc_id, matter_id, number))

    corpus = tuple(documents)
    known = {document.doc_id for document in corpus}
    for document in corpus:
        missing = set(document.related_doc_ids) - known
        if missing:
            raise ValueError(f"{document.doc_id} links to unknown documents {sorted(missing)}")
    return corpus


def documents_by_id() -> dict[str, Document]:
    return {document.doc_id: document for document in build_corpus()}


def corpus_digest() -> str:
    """Fingerprint of every document field, in doc_id order."""
    return sha256_digest([document.model_dump(mode="json") for document in build_corpus()])
