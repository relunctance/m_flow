"""
Structured output formatter for hawk-memory Layer2 integration.

Converts raw m_flow search results into structured format:
{
    "results": [
        {"id": "...", "text": "...", "score": 0.92, "type": "episodic"},
        ...
    ]
}

This enables hawk-memory to correlate m_flow results with its own
enrichment/deprecation/confidence Layer2 data.
"""

from __future__ import annotations

from typing import Any, Union

from m_flow.search.types import SearchResult, CombinedSearchResult


def format_structured_results(
    results: Union[List[SearchResult], CombinedSearchResult, list],
) -> dict:
    """
    Format search results into structured {id, text, score, type} format.

    Handles EPISODIC, PROCEDURAL, and other modes differently:
    - EPISODIC: extract episode_id from Edge attributes, use summary text
    - PROCEDURAL: extract procedure_id from Edge attributes
    - TRIPLET/CHUNKS: use dataset_id as fallback, return text

    Returns:
        {"results": [{"id": ..., "text": ..., "score": ..., "type": ...}, ...]}
    """
    if isinstance(results, CombinedSearchResult):
        return _format_combined_structured(results)

    if isinstance(results, list):
        if not results:
            return {"results": []}

        first = results[0]
        # List of SearchResult
        if isinstance(first, SearchResult):
            return _format_search_result_list(results)

        # List of strings (non-AC path, only_context mode)
        if isinstance(first, str):
            return _format_string_list(results)

        # List of tuples (raw search tuples from no-AC path)
        if isinstance(first, tuple):
            return _format_tuple_list(results)

        # Unknown type
        return {"results": []}

    return {"results": []}


def _format_search_result_list(results: list[SearchResult]) -> dict:
    """Format List[SearchResult] into structured output."""
    structured = []

    for sr in results:
        entry = {
            "id": sr.id,
            "text": _extract_text_from_search_result(sr),
            "score": sr.score,
            "type": sr.type or "unknown",
        }
        structured.append(entry)

    return {"results": structured}


def _format_string_list(results: list) -> dict:
    """Format list of strings (non-AC path) into structured output."""
    structured = []
    for i, text in enumerate(results):
        entry = {
            "id": f"str-{i}",
            "text": text,
            "score": None,
            "type": "text",
        }
        structured.append(entry)
    return {"results": structured}


def _format_tuple_list(results: list) -> dict:
    """
    Format list of (answer, context, datasets) tuples.

    For EPISODIC mode (only_context=True), answer is "" and context is text.
    For EPISODIC mode (only_context=False), answer is LLM completion and context is text.
    """
    structured = []
    for i, (answer, context, _) in enumerate(results):
        # Determine text: prefer context (episodic text) over answer (LLM text)
        if context and isinstance(context, str) and context.strip():
            text = context
            mtype = "episodic"
        elif answer and isinstance(answer, str) and answer.strip():
            text = answer
            mtype = "triplet"
        else:
            text = str(answer) if answer else str(context) if context else ""
            mtype = "unknown"

        # Extract episode_id from context if it's a multi-line episodic format
        episode_id = _extract_episode_id_from_text(text, i)

        entry = {
            "id": episode_id,
            "text": text,
            "score": None,
            "type": mtype,
        }
        structured.append(entry)

    return {"results": structured}


def _format_combined_structured(results: CombinedSearchResult) -> dict:
    """Format CombinedSearchResult (triplet mode) into structured output."""
    if not results.context:
        return {"results": []}

    structured = []
    if isinstance(results.context, dict):
        for ds_name, ctx in results.context.items():
            entry = {
                "id": ds_name,
                "text": ctx if isinstance(ctx, str) else str(ctx),
                "score": None,
                "type": "triplet",
            }
            structured.append(entry)
    elif isinstance(results.context, list):
        for i, ctx in enumerate(results.context):
            entry = {
                "id": f"combined-{i}",
                "text": ctx if isinstance(ctx, str) else str(ctx),
                "score": None,
                "type": "triplet",
            }
            structured.append(entry)
    else:
        entry = {
            "id": "combined",
            "text": str(results.context),
            "score": None,
            "type": "triplet",
        }
        structured.append(entry)

    return {"results": structured}


def _extract_text_from_search_result(sr: SearchResult) -> str:
    """Extract readable text from a SearchResult."""
    # search_result can be a list, string, or other
    sr_val = sr.search_result
    if isinstance(sr_val, str):
        return sr_val
    if isinstance(sr_val, list):
        if sr_val and isinstance(sr_val[0], str):
            return "\n\n---\n\n".join(sr_val)
        if sr_val:
            return str(sr_val[0])
    return str(sr_val) if sr_val else ""


def _extract_episode_id_from_text(text: str, fallback_idx: int) -> str:
    """
    Try to extract episode_id from episodic text format.

    Episodic summary format includes episode_id in node attributes
    (now included as "episode_id" in edge attributes).
    Since we receive text here, we try to extract from the text format.

    Format: "[April 27, 2026 (recorded)] [[Atomic] <ep_name>]\n<summary>"
    We look for [[...]] brackets which contain episode name/ID.

    Falls back to f"ep-{fallback_idx}" if extraction fails.
    """
    import re

    # Try to find [[...]] pattern which contains the episode identifier
    bracket_match = re.search(r"\[\[([^\]]+)\]\]", text)
    if bracket_match:
        # Use bracket content as episode identifier
        bracket_content = bracket_match.group(1).strip()
        # Convert to safe ID: remove "Atomic" prefix, spaces to dashes
        safe_id = bracket_content.replace("Atomic", "").strip()
        safe_id = re.sub(r"\s+", "-", safe_id)
        if safe_id:
            return safe_id

    # Try to find UUID pattern
    uuid_match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text, re.I
    )
    if uuid_match:
        return uuid_match.group(0)

    return f"ep-{fallback_idx}"
