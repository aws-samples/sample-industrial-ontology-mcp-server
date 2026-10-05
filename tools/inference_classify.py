"""Classify inference triples as meaningful / trivial / suspicious (task code I3; see docs/reference/task-glossary.md).

Categories:
- **trivial**: schema-level bloat with no domain semantics
    * (s, rdf:type, owl:NamedIndividual / owl:Thing / rdfs:Resource / owl:Class)
    * (s, rdf:type, rdfs:Class / rdf:Property)
- **suspicious**: cross-namespace cross-contamination
    * subject starts with domain prefix but predicate OR object is in a
      foreign namespace that is NOT a standard reasoning vocab
      (rdf/rdfs/owl/xsd).
- **meaningful**: everything else — domain class rdf:type, domain OP links,
  domain DP values.
"""
from __future__ import annotations

import logging
import os
from collections import Counter

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from config import INFERRED_PATH
from domain.namespaces import DOMAIN_NS
from tools.common import error_response, success_response

logger = logging.getLogger(__name__)

_TRIVIAL_TYPE_OBJECTS = {
    str(OWL.NamedIndividual),
    str(OWL.Thing),
    str(OWL.Class),
    str(RDFS.Resource),
    str(RDFS.Class),
    str(RDF.Property),
}
# Objects that carry no domain semantics when appearing on the right of
# rdfs:subClassOf — the reasoner routinely asserts "X subClassOf owl:Thing"
# which parallels "X rdf:type owl:Thing" and is equally trivial.
_TRIVIAL_SUBCLASSOF_OBJECTS = {
    str(OWL.Thing),
    str(RDFS.Resource),
}
_STANDARD_VOCAB_PREFIXES = (
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/2002/07/owl#",
    "http://www.w3.org/2001/XMLSchema#",
)

# 도메인 확장 어휘 — 프로젝트가 **의도적으로 쓰는** 보조 네임스페이스.
# 여기에 등재된 prefix 를 쓴 triple 은 suspicious 로 오분류하지 않는다.
# FOAF 는 의도적 도메인 확장이 아니라 "LLM 이 잘못 매핑한 예시" 로
# 기존 테스트가 쓰고 있으므로 **제외** — 실제 파이프라인이 FOAF 를 주입
# 하는 경로는 없다.
_EXTENSION_VOCAB_PREFIXES = (
    "http://www.w3.org/2004/02/skos/core#",          # SKOS — abstract category (Step 24)
    "http://purl.org/dc/terms/",                      # DCTERMS — module membership (T4)
    "http://purl.org/dc/elements/1.1/",               # DC (legacy)
    "http://www.w3.org/ns/prov#",                     # PROV-O — provenance (A4, I2)
    "http://purl.obolibrary.org/obo/",                # OBO — upper ontology (BFO 등)
    "https://spec.industrialontologies.org/",         # IOF — industrial upper ontology
    "http://www.w3.org/2003/11/swrl#",                # SWRL — rule language (I4)
    "http://www.w3.org/2003/11/swrlb#",               # SWRL builtins
    "http://www.w3.org/ns/shacl#",                    # SHACL
)


def _in_domain(node) -> bool:
    """True if node is a URIRef inside the configured domain namespace."""
    if not isinstance(node, URIRef):
        return False
    return str(node).startswith(DOMAIN_NS)


def _in_standard_vocab(node) -> bool:
    """True if node is a URIRef in rdf/rdfs/owl/xsd (reasoning vocab)."""
    if not isinstance(node, URIRef):
        return False
    return str(node).startswith(_STANDARD_VOCAB_PREFIXES)


def _in_extension_vocab(node) -> bool:
    """True if node is a URIRef in an intentional extension vocabulary
    (SKOS/DCTERMS/PROV/FOAF/IOF/OBO/SWRL/SHACL) that the pipeline uses on purpose.

    Also accepts the project's own ``{DOMAIN_NS}-ontoclean#`` prefix — T2 / T4
    annotations live there and must not be flagged as cross-namespace leakage.
    """
    if not isinstance(node, URIRef):
        return False
    s = str(node)
    if s.startswith(_EXTENSION_VOCAB_PREFIXES):
        return True
    # Domain's own ontoclean extension ({DOMAIN_NS stripped '#'} + "-ontoclean#")
    oc_prefix = DOMAIN_NS.rstrip("#") + "-ontoclean#"
    return bool(s.startswith(oc_prefix))


def _classify_triple(s, p, o) -> str:
    """Return 'trivial' | 'suspicious' | 'meaningful' for a single triple."""
    # trivial: blank-node subject — OWL reasoner writes restriction bodies and
    # other internal structures with BNode subjects. They carry no domain
    # semantics so aggregating them with meaningful triples skews the ratio.
    if isinstance(s, BNode):
        return "trivial"
    # trivial: rdf:type pointing at bloat vocabulary
    if p == RDF.type and isinstance(o, URIRef) and str(o) in _TRIVIAL_TYPE_OBJECTS:
        return "trivial"
    # trivial: rdfs:subClassOf owl:Thing / rdfs:Resource — reasoner-implicit
    # superclass chains with no domain meaning.
    if (
        p == RDFS.subClassOf
        and isinstance(o, URIRef)
        and str(o) in _TRIVIAL_SUBCLASSOF_OBJECTS
    ):
        return "trivial"
    # suspicious: domain subject, foreign predicate/object that is NOT in
    # either (a) reasoning standard vocab (rdf/rdfs/owl/xsd) or
    # (b) an intentional extension vocab (SKOS/DCTERMS/PROV/FOAF/IOF/OBO/
    #     SWRL/SHACL + project ontoclean extension).
    if _in_domain(s):
        if (
            isinstance(p, URIRef)
            and not _in_domain(p)
            and not _in_standard_vocab(p)
            and not _in_extension_vocab(p)
        ):
            return "suspicious"
        if (
            isinstance(o, URIRef)
            and not _in_domain(o)
            and not _in_standard_vocab(o)
            and not _in_extension_vocab(o)
        ):
            return "suspicious"
    return "meaningful"


def classify_inference_triples(inferred_path: str = "") -> str:
    """Classify inferred triples (all_inferred.ttl) into meaningful / trivial / suspicious.

    Args:
        inferred_path: Optional TTL path. Defaults to INFERRED_PATH.

    Returns:
        JSON string with:
        {
          "total_inferred": int,
          "meaningful_count": int,
          "trivial_count": int,
          "suspicious_count": int,
          "samples": {"meaningful": [...], "trivial": [...], "suspicious": [...]},
          "source": str,
        }

    Graceful: missing file → all zeros + note.
    """
    try:
        path = inferred_path or INFERRED_PATH
        if not os.path.exists(path):
            return success_response({
                "total_inferred": 0,
                "meaningful_count": 0,
                "trivial_count": 0,
                "suspicious_count": 0,
                "samples": {"meaningful": [], "trivial": [], "suspicious": []},
                "source": path,
                "note": f"파일 없음: {path}",
            })
        g = Graph()
        g.parse(path, format="turtle")
        counts: Counter = Counter()
        samples: dict[str, list] = {
            "meaningful": [],
            "trivial": [],
            "suspicious": [],
        }
        sample_cap = 10
        for s, p, o in g:
            cat = _classify_triple(s, p, o)
            counts[cat] += 1
            if len(samples[cat]) < sample_cap:
                samples[cat].append({
                    "s": str(s),
                    "p": str(p),
                    "o": str(o),
                })
        total = sum(counts.values())
        return success_response({
            "total_inferred": total,
            "meaningful_count": counts.get("meaningful", 0),
            "trivial_count": counts.get("trivial", 0),
            "suspicious_count": counts.get("suspicious", 0),
            "samples": samples,
            "source": path,
        })
    except Exception as e:
        return error_response(e, logger=logger)
