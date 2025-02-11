import json
import logging
import math
import re
from copy import deepcopy
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    TypeVar,
)
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

from ..abstractions.search import (
    AggregateSearchResult,
    ChunkSearchResult,
    ContextDocumentResult,
    GraphCommunityResult,
    GraphEntityResult,
    GraphRelationshipResult,
    GraphSearchResult,
    WebSearchResult,
)
from ..abstractions.vector import VectorQuantizationType

if TYPE_CHECKING:
    from ..api.models.retrieval.responses import Citation

logger = logging.getLogger()


def reorder_collector_to_match_final_brackets(
    collector: Any,  # "SearchResultsCollector",
    final_citations: list["Citation"],
):
    """
    Rebuilds collector._results_in_order so that bracket i => aggregator[i-1].
    Each citation's rawIndex indicates which aggregator item the LLM used originally.
    We place that aggregator item in the new position for bracket 'index'.
    """
    old_list = collector.get_all_results()  # [(source_type, result_obj), ...]
    max_index = max((c.index for c in final_citations), default=0)
    new_list = [None] * max_index

    for cit in final_citations:
        old_idx = cit.rawIndex
        new_idx = cit.index
        if not old_idx:  # or old_idx <= 0
            continue
        pos = old_idx - 1
        if pos < 0 or pos >= len(old_list):
            continue
        # aggregator item is old_list[pos]
        # place it at new_list[new_idx - 1]
        if new_list[new_idx - 1] is None:
            new_list[new_idx - 1] = old_list[pos]

    # remove any None in case some indexes never got filled
    collector._results_in_order = [x for x in new_list if x is not None]


async def finalize_citations_with_collector(
    raw_text: str, collector
) -> Tuple[str, list["Citation"]]:
    """
    1) Regex parse bracket references like [9].
    2) Each bracket # => 'old_ref'.
    3) Build a map old_ref -> new_ref (1..N) in ascending order of unique old_refs.
    4) Replace them in the text.
    5) For each bracket occurrence, find aggregator item # = old_ref, build Citation object.
    """
    from ..api.models.retrieval.responses import Citation

    if not raw_text:
        return raw_text, []

    pattern = re.compile(r"\[(\d+)\]")
    matches = list(pattern.finditer(raw_text))
    if not matches:
        return raw_text, []

    bracket_occurrences = []
    for m in matches:
        old_ref_str = m.group(1)
        old_ref_int = int(old_ref_str)
        bracket_occurrences.append(
            {
                "old_ref": old_ref_int,
                "start_index": m.start(),
                "end_index": m.end(),
            }
        )

    # Unique bracket refs, sorted ascending
    unique_old_refs = sorted({occ["old_ref"] for occ in bracket_occurrences})
    old_to_new = {}
    for i, old_ref in enumerate(unique_old_refs, start=1):
        old_to_new[old_ref] = i

    # We produce final citations in the *order the LLM text used them*,
    # i.e. bracket_occurrences order.
    # We'll look up aggregator items in get_all_results().
    all_items = (
        collector.get_all_results()
    )  # => (source_type, result_obj, agg_idx)
    final_citations = []

    for occ in bracket_occurrences:
        old_ref = occ["old_ref"]
        new_ref = old_to_new[old_ref]
        s_i = occ["start_index"]
        e_i = occ["end_index"]

        # Find aggregator item whose aggregator_index == old_ref
        matched_item = None
        for stype, obj, agg_idx in all_items:
            if agg_idx == old_ref:
                matched_item = (stype, obj)
                break

        if matched_item:
            (source_type, result_obj) = matched_item
            # Build a citation
            c = Citation(
                index=new_ref,
                rawIndex=old_ref,
                startIndex=s_i,
                endIndex=e_i,
                sourceType=source_type,
                doc_id=str(getattr(result_obj, "document_id", None)),
                text=getattr(result_obj, "text", None),
                metadata=getattr(result_obj, "metadata", {}),
            )
        else:
            # aggregator item # not found => partial citation
            c = Citation(
                index=new_ref,
                rawIndex=old_ref,
                startIndex=s_i,
                endIndex=e_i,
                sourceType="unknown",
            )
        final_citations.append(c)

    # Now relabel the text with bracket [old_ref] => [new_ref].
    def reindex(match):
        old_str = match.group(1)
        old_int = int(old_str)
        new_int = old_to_new[old_int]
        return f"[{new_int}]"

    relabeled_text = pattern.sub(reindex, raw_text)
    return relabeled_text, final_citations


# def map_citations_to_collector(
#     citations: List["Citation"], collector: Any  # "SearchResultsCollector"
# ) -> List["Citation"]:
#     """
#     For each Citation [i], find the i-th `(source_type, result_obj)` from
#     collector.get_all_results(), then attach the relevant metadata into
#     Citation. For example, if source_type == 'chunk', store the chunk’s text, score, etc.
#     """
#     results_in_order = (
#         collector.get_all_results()
#     )  # list of (source_type, result_obj)
#     updated_citations = []

#     for cit in citations:
#         # bracket index is 1-based, so i-th bracket => index-1 in the list
#         idx_0 = cit.index - 1
#         if idx_0 < 0 or idx_0 >= len(results_in_order):
#             # out of range => skip
#             updated_citations.append(cit)
#             continue

#         source_type, source_obj, agg_index = results_in_order[idx_0]

#         # Create a copy so as not to mutate the original
#         updated = cit.copy(update={"sourceType": source_type})

#         # Fill out chunk-based metadata
#         if source_type == "chunk":
#             updated.id = str(source_obj.id)
#             updated.document_id = str(source_obj.document_id)
#             updated.owner_id = (
#                 str(source_obj.owner_id) if source_obj.owner_id else None
#             )
#             updated.collection_ids = [
#                 str(cid) for cid in source_obj.collection_ids
#             ]
#             updated.score = source_obj.score
#             updated.text = source_obj.text
#             updated.metadata = dict(source_obj.metadata)

#         elif source_type == "graph":
#             updated.score = source_obj.score
#             updated.metadata = dict(source_obj.metadata)
#             if source_obj.content:
#                 updated.metadata["graphContent"] = (
#                     source_obj.content.model_dump()
#                 )

#         elif source_type == "web":
#             updated.metadata = {
#                 "link": source_obj.link,
#                 "title": source_obj.title,
#                 # "snippet": source_obj.snippet,
#                 "position": source_obj.position,
#             }

#         elif source_type == "contextDoc":
#             updated.metadata = {
#                 "document": source_obj.document,
#                 "chunks": source_obj.chunks,
#             }

#         # Add or modify more fields as needed...
#         updated_citations.append(updated)

#     return updated_citations


def map_citations_to_collector(
    citations: List["Citation"],
    collector: Any,  # "SearchResultsCollector"
) -> List["Citation"]:
    """
    For each citation, use its 'rawIndex' to look up the aggregator item from the
    collector. We then fill out the Citation’s sourceType, doc_id, text, metadata, etc.
    """
    from ..api.models.retrieval.responses import Citation

    # We'll build a dictionary aggregator_index -> (source_type, result_obj)
    aggregator_map = {}
    for stype, obj, agg_idx in collector.get_all_results():
        aggregator_map[agg_idx] = (stype, obj)

    mapped_citations: List[Citation] = []
    for cit in citations:
        old_ref = cit.rawIndex  # aggregator index we want
        if old_ref in aggregator_map:
            (source_type, result_obj) = aggregator_map[old_ref]
            # Make a copy with the updated fields
            updated = cit.copy()
            updated.sourceType = source_type

            # Fill chunk fields
            if source_type == "chunk":
                updated.id = str(result_obj.id)
                updated.document_id = str(result_obj.document_id)
                updated.owner_id = (
                    str(result_obj.owner_id) if result_obj.owner_id else None
                )
                updated.collection_ids = [
                    str(cid) for cid in result_obj.collection_ids
                ]
                updated.score = result_obj.score
                updated.text = result_obj.text
                updated.metadata = dict(result_obj.metadata)

            elif source_type == "graph":
                updated.score = result_obj.score
                updated.metadata = dict(result_obj.metadata)
                if result_obj.content:
                    updated.metadata["graphContent"] = (
                        result_obj.content.model_dump()
                    )

            elif source_type == "web":
                updated.metadata = {
                    "link": result_obj.link,
                    "title": result_obj.title,
                    "position": result_obj.position,
                    # etc. ...
                }

            elif source_type == "contextDoc":
                updated.metadata = {
                    "document": result_obj.document,
                    "chunks": result_obj.chunks,
                }

            else:
                # fallback unknown type
                updated.metadata = {}
            mapped_citations.append(updated)

        else:
            # aggregator index not found => out-of-range or unknown
            updated = cit.copy()
            updated.sourceType = "unknown"
            mapped_citations.append(updated)

    return mapped_citations


def _expand_citation_span_to_sentence(
    full_text: str, start: int, end: int
) -> Tuple[int, int]:
    """
    Return (sentence_start, sentence_end) for the sentence containing the bracket [n].
    We define a sentence boundary as '.', '?', or '!', optionally followed by
    spaces or a newline. This is a simple heuristic; you can refine it as needed.
    """
    sentence_enders = {".", "?", "!"}

    # Move backward from 'start' until we find a sentence ender or reach index 0
    s = start
    while s > 0:
        if full_text[s] in sentence_enders:
            s += 1
            while s < len(full_text) and full_text[s].isspace():
                s += 1
            break
        s -= 1
    sentence_start = s

    # Move forward from 'end' until we find a sentence ender or end of text
    e = end
    while e < len(full_text):
        if full_text[e] in sentence_enders:
            e += 1
            while e < len(full_text) and full_text[e].isspace():
                e += 1
            break
        e += 1
    sentence_end = e

    return (sentence_start, sentence_end)


# def extract_citations(text: str) -> List["Citation"]:
#     """
#     Parse the LLM-generated text and extract bracket references [n].
#     For each bracket, also expand around the bracket to capture
#     a sentence-based snippet if possible.
#     """
#     from ..api.models.retrieval.responses import Citation

#     pattern = r"\[(\d+)\]"
#     citations: List[Citation] = []

#     for match in re.finditer(pattern, text):
#         bracket_index = int(match.group(1))
#         bracket_start = match.start()
#         bracket_end = match.end()

#         # Expand to sentence boundaries
#         snippet_start, snippet_end = _expand_citation_span_to_sentence(
#             text, bracket_start, bracket_end
#         )
#         snippet_text = text[snippet_start:snippet_end]

#         # Build a typed Citation object
#         citation = Citation(
#             index=bracket_index,
#             startIndex=bracket_start,
#             endIndex=bracket_end,
#             snippetStartIndex=snippet_start,
#             snippetEndIndex=snippet_end,
#             # snippet=snippet_text,
#         )
#         citations.append(citation)

#     return citations

CITATION_PATTERN = re.compile(r"\[(\d+)\]")


def extract_citations(text: str) -> List["Citation"]:
    """
    Find bracket references like [3], [10], etc. Return a list of Citation objects
    whose 'index' field is the number found in brackets, but we will later rename
    that to 'rawIndex' to avoid confusion.
    """
    from ..api.models.retrieval.responses import Citation

    citations = []
    for match in CITATION_PATTERN.finditer(text):
        bracket_str = match.group(1)
        bracket_num = int(bracket_str)
        start_i = match.start()
        end_i = match.end()

        # Expand around the bracket to get a snippet if desired:
        snippet_start, snippet_end = _expand_citation_span_to_sentence(
            text, start_i, end_i
        )

        c = Citation(
            index=bracket_num,  # We'll rename this to rawIndex in step 2
            startIndex=start_i,
            endIndex=end_i,
            snippetStartIndex=snippet_start,
            snippetEndIndex=snippet_end,
        )
        citations.append(c)

    return citations


def reassign_citations_in_order(
    text: str, citations: List["Citation"]
) -> Tuple[str, List["Citation"]]:
    """
    Sort citations by their start index, unify repeated bracket numbers, and relabel them
    in ascending order of first appearance. Return (new_text, new_citations).
    - new_citations[i].index = the new bracket number
    - new_citations[i].rawIndex = the original bracket number
    """
    from ..api.models.retrieval.responses import Citation

    if not citations:
        return text, []

    # 1) Sort citations in order of their appearance
    sorted_cits = sorted(citations, key=lambda c: c.startIndex)

    # 2) Build a map from oldRef -> newRef
    old_to_new = {}
    next_new_index = 1
    labeled = []
    for cit in sorted_cits:
        old_ref = cit.index  # the bracket number we extracted
        if old_ref not in old_to_new:
            old_to_new[old_ref] = next_new_index
            next_new_index += 1
        new_ref = old_to_new[old_ref]

        # We create a "relabeled" citation that has `rawIndex=old_ref`
        # and `index=new_ref`.
        labeled.append(
            {
                "rawIndex": old_ref,
                "newIndex": new_ref,
                "startIndex": cit.startIndex,
                "endIndex": cit.endIndex,
            }
        )

    # 3) Replace the bracket references in the text from right-to-left
    #    so we don't mess up subsequent indices.
    result_chars = list(text)
    for item in sorted(labeled, key=lambda x: x["startIndex"], reverse=True):
        s_i = item["startIndex"]
        e_i = item["endIndex"]
        new_ref = item["newIndex"]
        replacement = f"[{new_ref}]"
        result_chars[s_i:e_i] = list(replacement)

    new_text = "".join(result_chars)

    # 4) Re-extract to get updated start/end indices, snippet offsets, etc.
    #    Then we merge that data with (rawIndex, newIndex).
    updated_citations = []
    updated_extracted = extract_citations(new_text)

    # We'll match them up in sorted order. Because they appear in the same order with the same count
    updated_extracted.sort(key=lambda c: c.startIndex)
    labeled.sort(key=lambda x: x["startIndex"])

    for labeled_item, updated_cit in zip(labeled, updated_extracted):
        c = Citation(
            rawIndex=labeled_item["rawIndex"],
            index=labeled_item["newIndex"],
            startIndex=updated_cit.startIndex,
            endIndex=updated_cit.endIndex,
            snippetStartIndex=updated_cit.snippetStartIndex,
            snippetEndIndex=updated_cit.snippetEndIndex,
        )
        updated_citations.append(c)

    return new_text, updated_citations


# def reassign_citations_in_order(
#     text: str, citations: List["Citation"]
# ) -> Tuple[str, List["Citation"]]:
#     """
#     Sort citations by startIndex, assign them new indices [1..N], then
#     replace the original bracket references in the text in-place. Re-extract
#     citations from the modified text to capture new snippet data, and merge
#     it back into typed Citation objects.
#     """
#     from ..api.models.retrieval.responses import Citation
#     sorted_citations = sorted(citations, key=lambda c: c.startIndex)
#     result_text_chars = list(text)

#     # Build a map rawIndex -> newIndex, assigned in the order we encounter new oldRefs
#     old_to_new = {}
#     next_new_index = 1
#     labeled_citations = []

#     for cit in sorted_citations:
#         old_index = cit.index
#         if old_index not in old_to_new:
#             old_to_new[old_index] = next_new_index
#             next_new_index += 1
#         new_idx = old_to_new[old_index]

#         labeled_citations.append({
#             "rawIndex": old_index,
#             "newIndex": new_idx,
#             "startIndex": cit.startIndex,
#             "endIndex": cit.endIndex,
#         })

#     # 2) Now replace them from end to start with newIndex
#     labeled_desc = sorted(labeled_citations, key=lambda x: x["startIndex"], reverse=True)
#     for item in labeled_desc:
#         start = item["startIndex"]
#         end = item["endIndex"]
#         new_idx = item["newIndex"]
#         replacement = f"[{new_idx}]"
#         result_text_chars[start:end] = list(replacement)

#     new_text = "".join(result_text_chars)

#     # Re-extract to get updated bracket positions & snippet data
#     re_extracted = extract_citations(new_text)
#     re_map = {cit.index: cit for cit in re_extracted}

#     # Merge snippet data & build final typed list in ascending order
#     labeled_asc = sorted(labeled_citations, key=lambda x: x["newIndex"])
#     updated_citations: List[Citation] = []
#     for item in labeled_asc:
#         new_idx = item["newIndex"]
#         old_idx = item["rawIndex"]
#         found = re_map.get(new_idx)

#         if not found:
#             # no match => fallback
#             updated_citations.append(Citation(index=new_idx, rawIndex=old_idx))
#             continue

#         # copy snippet offsets & bracket offsets from found
#         updated_citations.append(
#             Citation(
#                 rawIndex=old_idx,
#                 index=new_idx,
#                 startIndex=found.startIndex,
#                 endIndex=found.endIndex,
#                 snippetStartIndex=found.snippetStartIndex,
#                 snippetEndIndex=found.snippetEndIndex,
#                 # snippet=found.snippet,
#             )
#         )
#     return new_text, updated_citations


def map_citations_to_sources(
    citations: List["Citation"], aggregated: AggregateSearchResult
) -> List["Citation"]:
    """
    Given typed citations (with snippet info) and an aggregated search result,
    map each bracket index to the corresponding source object (chunk, graph, web, context).
    Returns a new list of typed Citation objects, each storing source metadata.
    """
    flat_source_list = []

    # Flatten chunk -> graph -> web -> contextDoc in the same order your prompt enumerates them
    if aggregated.chunk_search_results:
        for chunk in aggregated.chunk_search_results:
            flat_source_list.append((chunk, "chunk"))
    if aggregated.graph_search_results:
        for g in aggregated.graph_search_results:
            flat_source_list.append((g, "graph"))
    if aggregated.web_search_results:
        for w in aggregated.web_search_results:
            flat_source_list.append((w, "web"))
    if aggregated.context_document_results:
        for cdoc in aggregated.context_document_results:
            flat_source_list.append((cdoc, "contextDoc"))

    mapped_citations: List[Citation] = []

    for cit in citations:
        idx = cit.index
        idx_0_based = idx - 1

        # If bracket index is out of range => placeholders
        if idx_0_based < 0 or idx_0_based >= len(flat_source_list):
            mapped_citations.append(cit)  # no updates to source fields
            continue

        source_obj, source_type = flat_source_list[idx_0_based]

        # Create a copy so we don't mutate the original
        updated_cit = cit.copy()
        updated_cit.sourceType = source_type

        # Fill out chunk-based metadata
        if source_type == "chunk":
            updated_cit.id = str(source_obj.id)
            updated_cit.document_id = str(source_obj.document_id)
            updated_cit.owner_id = (
                str(source_obj.owner_id) if source_obj.owner_id else None
            )
            updated_cit.collection_ids = [
                str(cid) for cid in source_obj.collection_ids
            ]
            updated_cit.score = source_obj.score
            updated_cit.text = source_obj.text
            updated_cit.metadata = dict(source_obj.metadata)

        elif source_type == "graph":
            updated_cit.score = source_obj.score
            updated_cit.metadata = dict(source_obj.metadata)
            if source_obj.content:
                updated_cit.metadata["graphContent"] = (
                    source_obj.content.model_dump()
                )

        elif source_type == "web":
            updated_cit.metadata = {
                "link": source_obj.link,
                "title": source_obj.title,
                # "snippet": source_obj.snippet,
                "position": source_obj.position,
            }

        elif source_type == "contextDoc":
            updated_cit.metadata = {
                "document": source_obj.document,
                "chunks": source_obj.chunks,
            }

        mapped_citations.append(updated_cit)

    return mapped_citations


async def finalize_citations_in_message(
    raw_text: str,
    search_results: AggregateSearchResult,
) -> tuple[str, list["Citation"]]:
    """
    1) Extract bracket references from the raw LLM text,
    2) Re-label them in ascending order,
    3) Build structured Citation objects mapped to the underlying chunk/graph data,
    4) Return (relabeled_text, citations).
    """
    # 1) detect citations [1], [2], ...
    raw_citations = extract_citations(raw_text)

    # 2) re-map them in ascending order => new_text has sequential references [1], [2], ...
    relabeled_text, new_citations = reassign_citations_in_order(
        raw_text, raw_citations
    )

    # 3) map to sources in the `AggregateSearchResult`
    mapped_citations = map_citations_to_sources(new_citations, search_results)

    return relabeled_text, mapped_citations


def format_search_results_for_llm(
    results: AggregateSearchResult,
    collector: Any,  # SearchResultsCollector
) -> str:
    """
    Instead of resetting 'source_counter' to 1, we:
     - For each chunk / graph / web / contextDoc in `results`,
     - Find the aggregator index from the collector,
     - Print 'Source [X]:' with that aggregator index.
    """
    lines = []

    # We'll build a quick helper to locate aggregator indices for each object:
    # Or you can rely on the fact that we've added them to the collector
    # in the same order. But let's do a "lookup aggregator index" approach:

    def get_aggregator_index_for_item(item):
        for stype, obj, agg_index in collector.get_all_results():
            if obj is item:
                return agg_index
        return None  # not found, fallback

    # 1) Chunk search
    if results.chunk_search_results:
        lines.append("Vector Search Results:")
        for c in results.chunk_search_results:
            agg_idx = get_aggregator_index_for_item(c)
            if agg_idx is None:
                # fallback if not found for some reason
                agg_idx = "???"
            lines.append(f"Source [{agg_idx}]:")
            lines.append(c.text or "")  # or c.text[:200] to truncate

    # 2) Graph search
    if results.graph_search_results:
        lines.append("Graph Search Results:")
        for g in results.graph_search_results:
            agg_idx = get_aggregator_index_for_item(g)
            if agg_idx is None:
                agg_idx = "???"
            lines.append(f"Source [{agg_idx}]:")
            if isinstance(g.content, GraphCommunityResult):
                lines.append(f"Community Name: {g.content.name}")
                lines.append(f"ID: {g.content.id}")
                lines.append(f"Summary: {g.content.summary}")
                # etc. ...
            elif isinstance(g.content, GraphEntityResult):
                lines.append(f"Entity Name: {g.content.name}")
                lines.append(f"Description: {g.content.description}")
            elif isinstance(g.content, GraphRelationshipResult):
                lines.append(
                    f"Relationship: {g.content.subject}-{g.content.predicate}-{g.content.object}"
                )
            # Add metadata if needed

    # 3) Web search
    if results.web_search_results:
        lines.append("Web Search Results:")
        for w in results.web_search_results:
            agg_idx = get_aggregator_index_for_item(w)
            if agg_idx is None:
                agg_idx = "???"
            lines.append(f"Source [{agg_idx}]:")
            lines.append(f"Title: {w.title}")
            lines.append(f"Link: {w.link}")
            lines.append(f"Snippet: {w.snippet}")

    # 4) Local context docs
    if results.context_document_results:
        lines.append("Local Context Documents:")
        for doc_result in results.context_document_results:
            agg_idx = get_aggregator_index_for_item(doc_result)
            if agg_idx is None:
                agg_idx = "???"
            doc_data = doc_result.document
            doc_title = doc_data.get("title", "Untitled Document")
            doc_id = doc_data.get("id", "N/A")
            summary = doc_data.get("summary", "")

            lines.append(f"Source [{agg_idx}]:")
            lines.append(f"Document Title: {doc_title} (ID: {doc_id})")
            if summary:
                lines.append(f"Summary: {summary}")

            # Then each chunk inside:
            for i, ch_text in enumerate(doc_result.chunks, start=1):
                lines.append(f"Chunk {i}: {ch_text}")

    return "\n".join(lines)


def format_search_results_for_stream(results: AggregateSearchResult) -> str:
    CHUNK_SEARCH_STREAM_MARKER = "chunk_search"
    GRAPH_SEARCH_STREAM_MARKER = "graph_search"
    WEB_SEARCH_STREAM_MARKER = "web_search"
    CONTEXT_STREAM_MARKER = "content"

    context = ""

    if results.chunk_search_results:
        context += f"<{CHUNK_SEARCH_STREAM_MARKER}>"
        vector_results_list = [
            r.as_dict() for r in results.chunk_search_results
        ]
        context += json.dumps(vector_results_list, default=str)
        context += f"</{CHUNK_SEARCH_STREAM_MARKER}>"

    if results.graph_search_results:
        context += f"<{GRAPH_SEARCH_STREAM_MARKER}>"
        graph_search_results_results_list = [
            r.dict() for r in results.graph_search_results
        ]
        context += json.dumps(graph_search_results_results_list, default=str)
        context += f"</{GRAPH_SEARCH_STREAM_MARKER}>"

    if results.web_search_results:
        context += f"<{WEB_SEARCH_STREAM_MARKER}>"
        web_results_list = [r.to_dict() for r in results.web_search_results]
        context += json.dumps(web_results_list, default=str)
        context += f"</{WEB_SEARCH_STREAM_MARKER}>"

    # NEW: local context
    if results.context_document_results:
        context += f"<{CONTEXT_STREAM_MARKER}>"
        # Just store them as raw dict JSON, or build a more structured form
        content_list = [
            cdr.to_dict() for cdr in results.context_document_results
        ]
        context += json.dumps(content_list, default=str)
        context += f"</{CONTEXT_STREAM_MARKER}>"

    return context


def _generate_id_from_label(label) -> UUID:
    return uuid5(NAMESPACE_DNS, label)


def generate_id(label: Optional[str] = None) -> UUID:
    """
    Generates a unique run id
    """
    return _generate_id_from_label(label if label != None else str(uuid4()))


def generate_document_id(filename: str, user_id: UUID) -> UUID:
    """
    Generates a unique document id from a given filename and user id
    """
    safe_filename = filename.replace("/", "_")
    return _generate_id_from_label(f"{safe_filename}-{str(user_id)}")


def generate_extraction_id(
    document_id: UUID, iteration: int = 0, version: str = "0"
) -> UUID:
    """
    Generates a unique extraction id from a given document id and iteration
    """
    return _generate_id_from_label(f"{str(document_id)}-{iteration}-{version}")


def generate_default_user_collection_id(user_id: UUID) -> UUID:
    """
    Generates a unique collection id from a given user id
    """
    return _generate_id_from_label(str(user_id))


def generate_user_id(email: str) -> UUID:
    """
    Generates a unique user id from a given email
    """
    return _generate_id_from_label(email)


def generate_default_prompt_id(prompt_name: str) -> UUID:
    """
    Generates a unique prompt id
    """
    return _generate_id_from_label(prompt_name)


def generate_entity_document_id() -> UUID:
    """
    Generates a unique document id inserting entities into a graph
    """
    generation_time = datetime.now().isoformat()
    return _generate_id_from_label(f"entity-{generation_time}")


async def to_async_generator(
    iterable: Iterable[Any],
) -> AsyncGenerator[Any, None]:
    for item in iterable:
        yield item


def increment_version(version: str) -> str:
    prefix = version[:-1]
    suffix = int(version[-1])
    return f"{prefix}{suffix + 1}"


def decrement_version(version: str) -> str:
    prefix = version[:-1]
    suffix = int(version[-1])
    return f"{prefix}{max(0, suffix - 1)}"


def validate_uuid(uuid_str: str) -> UUID:
    return UUID(uuid_str)


def update_settings_from_dict(server_settings, settings_dict: dict):
    """
    Updates a settings object with values from a dictionary.
    """
    settings = deepcopy(server_settings)
    for key, value in settings_dict.items():
        if value is not None:
            if isinstance(value, dict):
                for k, v in value.items():
                    if isinstance(getattr(settings, key), dict):
                        getattr(settings, key)[k] = v
                    else:
                        setattr(getattr(settings, key), k, v)
            else:
                setattr(settings, key, value)

    return settings


def _decorate_vector_type(
    input_str: str,
    quantization_type: VectorQuantizationType = VectorQuantizationType.FP32,
) -> str:
    return f"{quantization_type.db_type}{input_str}"


def _get_vector_column_str(
    dimension: int | float, quantization_type: VectorQuantizationType
) -> str:
    """
    Returns a string representation of a vector column type.

    Explicitly handles the case where the dimension is not a valid number
    meant to support embedding models that do not allow for specifying
    the dimension.
    """
    if math.isnan(dimension) or dimension <= 0:
        vector_dim = ""  # Allows for Postgres to handle any dimension
    else:
        vector_dim = f"({dimension})"
    return _decorate_vector_type(vector_dim, quantization_type)


KeyType = TypeVar("KeyType")


def deep_update(
    mapping: dict[KeyType, Any], *updating_mappings: dict[KeyType, Any]
) -> dict[KeyType, Any]:
    """
    Taken from Pydantic v1:
    https://github.com/pydantic/pydantic/blob/fd2991fe6a73819b48c906e3c3274e8e47d0f761/pydantic/utils.py#L200
    """
    updated_mapping = mapping.copy()
    for updating_mapping in updating_mappings:
        for k, v in updating_mapping.items():
            if (
                k in updated_mapping
                and isinstance(updated_mapping[k], dict)
                and isinstance(v, dict)
            ):
                updated_mapping[k] = deep_update(updated_mapping[k], v)
            else:
                updated_mapping[k] = v
    return updated_mapping
