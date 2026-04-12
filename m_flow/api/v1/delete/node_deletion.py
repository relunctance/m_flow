"""
Node deletion service.

Supports precise deletion by node type, reusing existing implementations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional
from uuid import UUID

from sqlalchemy import or_, update

from m_flow.adapters.graph import get_graph_provider
from m_flow.adapters.relational import get_db_adapter
from m_flow.api.v1.delete.delete import (
    _convert_to_uuid,
    _discover_vector_collections,
    _purge_from_vector_store,
)
from m_flow.api.v1.exceptions import NodeNotFoundError, PermissionDeniedError
from m_flow.auth.methods import get_seed_user
from m_flow.auth.models import User
from m_flow.context_global_variables import set_db_context
from m_flow.data.methods import get_authorized_existing_datasets
from m_flow.data.models.graph_relationship_ledger import GraphRelationshipLedger
from m_flow.shared.logging_utils import get_logger

_log = get_logger(__name__)


async def _verify_dataset_access(dataset_id: UUID, user: User) -> None:
    """Verify user's delete permission on dataset."""
    authorized = await get_authorized_existing_datasets([dataset_id], "delete", user)
    if not authorized:
        raise PermissionDeniedError(f"No delete permission on dataset: {dataset_id}")

    target_dataset = authorized[0]
    await set_db_context(target_dataset.id, target_dataset.owner_id)


async def preview_deletion(
    node_id: str,
    dataset_id: UUID,
    user: User = None,
) -> Dict:
    """
    Preview deletion impact (does not execute deletion).

    Returns node info, edge count, associated node types, etc.
    """
    if user is None:
        user = await get_seed_user()

    await _verify_dataset_access(dataset_id, user)

    graph = await get_graph_provider()

    node = await graph.get_node(node_id)
    if not node:
        raise NodeNotFoundError(f"Node not found: {node_id}")

    node_dataset_id = node.get("dataset_id")
    if node_dataset_id and str(node_dataset_id) != str(dataset_id):
        raise PermissionDeniedError(f"Node {node_id} belongs to dataset {node_dataset_id}, not {dataset_id}")

    edges = await graph.get_edges(node_id)
    neighbor_types = set()
    for src_props, rel_name, dst_props in edges:
        if isinstance(dst_props, dict):
            neighbor_types.add(dst_props.get("type", "Unknown"))

    node_type = node.get("type", "Unknown")

    all_collections = _discover_vector_collections()
    vector_collections = [c for c in all_collections if c.startswith(f"{node_type}_")]

    # Build warning message
    if node_type in ["Entity", "Entity"]:
        # Shared node: cannot delete directly
        warning = (
            f"⚠️ {node_type} is a shared node and cannot be deleted directly. "
            "Delete associated Episodes (using hard mode) to clean up orphaned shared nodes."
        )
        can_delete = False
    else:
        # Regular node: can delete
        warning = (
            f"Deleting this node will disconnect {len(edges)} edges" if edges else "This node has no associated edges"
        )
        can_delete = True

    return {
        "node_id": node_id,
        "node_type": node_type,
        "node_name": node.get("name", ""),
        "edge_count": len(edges),
        "neighbor_types": list(neighbor_types),
        "vector_collections": vector_collections,
        "warning": warning,
        "can_delete": can_delete,  # Indicates whether node can be deleted directly
    }


async def _update_ledger_deleted_at(node_ids: List[UUID]) -> None:
    """
    Update deletion timestamp in audit log.

    Note: Caller must first convert string IDs to UUID using _convert_to_uuid().
    """
    if not node_ids:
        return

    db = get_db_adapter()

    async with db.get_async_session() as sess:
        ledger_update = (
            update(GraphRelationshipLedger)
            .where(
                or_(
                    GraphRelationshipLedger.source_node_id.in_(node_ids),
                    GraphRelationshipLedger.destination_node_id.in_(node_ids),
                )
            )
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await sess.execute(ledger_update)
        await sess.commit()

    _log.info(f"[delete] Updated ledger deleted_at for {len(node_ids)} nodes")


async def delete_node_by_id(
    node_id: str,
    dataset_id: UUID,
    cascade: bool = False,
    user: User = None,
) -> Dict:
    """
    Generic node deletion.

    Complete deletion flow:
    1. Verify node exists and belongs to specified dataset
    2. Delete from graph database (DETACH DELETE)
    3. Optional: cascade cleanup of orphan nodes (hard mode)
    4. Safely convert node IDs to UUID (using _convert_to_uuid)
    5. Clean up vector index (using dynamically discovered collections)
    6. Update audit log GraphRelationshipLedger.deleted_at
    """
    if user is None:
        user = await get_seed_user()

    await _verify_dataset_access(dataset_id, user)

    graph = await get_graph_provider()

    node = await graph.get_node(node_id)
    if not node:
        raise NodeNotFoundError(f"Node not found: {node_id}")

    node_dataset_id = node.get("dataset_id")
    node_type = node.get("type", "")

    if node_dataset_id:
        if str(node_dataset_id) != str(dataset_id):
            raise PermissionDeniedError(f"Node {node_id} belongs to dataset {node_dataset_id}, not {dataset_id}")
    else:
        if node_type in ["Entity", "Entity"]:
            raise PermissionDeniedError(
                f"Cannot directly delete shared {node_type} node. "
                f"Use hard mode to clean up orphan nodes after deleting Episodes."
            )

    deleted_ids = [node_id]

    await graph.delete_node(node_id)
    _log.info(f"[delete] Deleted {node_type} node: {node_id}")

    if cascade:
        if node_type == "Episode":
            # Safety: only clean up degree-0 orphan Facets under current dataset
            # Note: dataset_id is stored in properties JSON, not a direct column
            # Kuzu returns NULL for non-existent properties, WHERE NULL = $x is always false (silent failure)
            # So use Python-layer filtering, consistent with learn.py/get_graph_router.py pattern
            orphan_facet_query = """
            MATCH (f:Node {type: "Facet"})
            WITH f, COUNT { MATCH (f)--() } as degree
            WHERE degree = 0
            RETURN f.id as id
            """
            orphan_result = await graph.query(orphan_facet_query)
            for row in orphan_result or []:
                fid = row[0] if row else None
                if fid:
                    # Python-layer filtering of dataset_id (extracted from properties JSON)
                    node = await graph.get_node(fid)
                    if not node:
                        _log.warning(f"[delete] Skipping orphan Facet {fid}: node not found")
                        continue
                    facet_dataset_id = node.get("dataset_id")
                    if facet_dataset_id != str(dataset_id):
                        _log.debug(f"[delete] Skipping orphan Facet {fid}: belongs to {facet_dataset_id}")
                        continue  # Skip Facets not belonging to target dataset
                    await graph.delete_node(fid)
                    deleted_ids.append(fid)
                    _log.info(f"[delete] Cascade deleted orphan Facet: {fid}")

            orphan_point_query = """
            MATCH (fp:Node {type: "FacetPoint"})
            WITH fp, COUNT { MATCH (fp)--() } as degree
            WHERE degree = 0
            RETURN fp.id as id
            """
            orphan_fp_result = await graph.query(orphan_point_query)
            for row in orphan_fp_result or []:
                fpid = row[0] if row else None
                if fpid:
                    await graph.delete_node(fpid)
                    deleted_ids.append(fpid)
                    _log.info(f"[delete] Cascade deleted orphan FacetPoint: {fpid}")

            for entity_type in ["Entity", "Entity"]:
                orphan_entity_query = f"""
                MATCH (e:Node {{type: "{entity_type}"}})
                WITH e, COUNT {{ MATCH (e)--() }} as degree
                WHERE degree = 0
                RETURN e.id as id
                """
                orphan_entity_result = await graph.query(orphan_entity_query)
                for row in orphan_entity_result or []:
                    eid = row[0] if row else None
                    if eid:
                        await graph.delete_node(eid)
                        deleted_ids.append(eid)
                        _log.info(f"[delete] Cascade deleted orphan {entity_type}: {eid}")

        elif node_type == "Facet":
            # FacetPoints don't have dataset_id, but only become orphaned after associated Facet is deleted
            # For safety, only clean up FacetPoints that were associated with current dataset's Facets
            # Since FacetPoints don't have dataset_id, can only determine by degree = 0
            orphan_point_query = """
            MATCH (fp:Node {type: "FacetPoint"})
            WITH fp, COUNT { MATCH (fp)--() } as degree
            WHERE degree = 0
            RETURN fp.id as id
            """
            orphan_result = await graph.query(orphan_point_query)
            for row in orphan_result or []:
                fpid = row[0] if row else None
                if fpid:
                    await graph.delete_node(fpid)
                    deleted_ids.append(fpid)
                    _log.info(f"[delete] Cascade deleted orphan FacetPoint: {fpid}")

    uuid_list: list[UUID] = []
    for nid in deleted_ids:
        converted = _convert_to_uuid(nid)
        if converted:
            uuid_list.append(converted)

    if uuid_list:
        try:
            await _purge_from_vector_store(uuid_list)
        except Exception as e:
            _log.warning(f"[delete] Vector cleanup failed (non-blocking): {e}")

    try:
        await _update_ledger_deleted_at(uuid_list)
    except Exception as e:
        _log.warning(f"[delete] Ledger update failed (non-blocking): {e}")

    return {
        "status": "success",
        "node_id": node_id,
        "node_type": node_type,
        "cascade": cascade,
        "deleted_count": len(deleted_ids),
        "deleted_ids": deleted_ids,
    }


async def delete_episode(
    episode_id: str,
    dataset_id: UUID,
    mode: str = "soft",
    user: User = None,
) -> Dict:
    """
    Delete Episode node.

    Note: Uses DETACH DELETE, does not delete associated Facet/Entity
    (they may be shared by other Episodes).

    Args:
        episode_id: Episode node ID.
        dataset_id: Dataset ID.
        mode: "soft"(default) or "hard" - hard mode cleans up orphaned Facets.
        user: User performing the operation.
    """
    return await delete_node_by_id(
        node_id=episode_id,
        dataset_id=dataset_id,
        cascade=(mode == "hard"),
        user=user,
    )
