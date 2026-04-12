"""
Prune Procedural Data endpoint.

Surgically removes only Procedure, ProcedureStepPoint, ProcedureContextPoint
nodes and their edges from the graph database. Does NOT touch episodic data.
Also clears procedural_extracted marks on Episodes for clean re-extraction.
"""

from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from m_flow.auth.methods import get_authenticated_user
from m_flow.auth.models import User
from m_flow.shared.logging_utils import get_logger

logger = get_logger("prune_procedural")

PROCEDURAL_NODE_TYPES = [
    "Procedure",
    "ProcedureStepPoint",
    "ProcedureContextPoint",
    "ProcedureStepsPack",
    "ProcedureContextPack",
]


class PruneProceduralRequest(BaseModel):
    dataset_id: Optional[UUID] = Field(
        default=None,
        description="Specific dataset to clean. If None, cleans all accessible datasets.",
    )


class PruneProceduralResponse(BaseModel):
    success: bool
    datasets_cleaned: int
    nodes_deleted: int
    episodes_unmarked: int
    message: str


def get_prune_procedural_router() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/procedural",
        response_model=PruneProceduralResponse,
        summary="Remove all procedural data from graph",
        description="Surgically removes Procedure nodes and edges without touching episodic data. "
        "Also clears procedural_extracted marks on Episodes.",
    )
    async def prune_procedural(
        request: PruneProceduralRequest,
        user: User = Depends(get_authenticated_user),
    ) -> PruneProceduralResponse:
        from m_flow.data.methods import get_authorized_existing_datasets
        from m_flow.context_global_variables import set_db_context
        from m_flow.adapters.graph import get_graph_provider

        authorized = await get_authorized_existing_datasets(
            [request.dataset_id] if request.dataset_id else [],
            "write",
            user,
        )

        if not authorized:
            return PruneProceduralResponse(
                success=True,
                datasets_cleaned=0,
                nodes_deleted=0,
                episodes_unmarked=0,
                message="No authorized datasets",
            )

        total_deleted = 0
        total_unmarked = 0
        cleaned = 0
        types_in = ", ".join(f"'{t}'" for t in PROCEDURAL_NODE_TYPES)

        for ds in authorized:
            try:
                await set_db_context(ds.id, ds.owner_id)
                engine = await get_graph_provider()

                count_q = f"MATCH (n:Node) WHERE n.type IN [{types_in}] RETURN count(*)"
                count_result = await engine.query(count_q)
                node_count = count_result[0][0] if count_result else 0

                if node_count == 0:
                    continue

                del_edges_q = (
                    f"MATCH (n:Node)-[r]->(m:Node) WHERE n.type IN [{types_in}] OR m.type IN [{types_in}] DELETE r"
                )
                await engine.query(del_edges_q)

                del_nodes_q = f"MATCH (n:Node) WHERE n.type IN [{types_in}] DELETE n"
                await engine.query(del_nodes_q)

                total_deleted += node_count

                ep_q = "MATCH (e:Node) WHERE e.type = 'Episode' RETURN e.id, e.properties"
                episodes = await engine.query(ep_q)
                for ep in episodes or []:
                    ep_id = ep[0]
                    props_raw = ep[1] if len(ep) > 1 else None
                    try:
                        props = json.loads(props_raw) if isinstance(props_raw, str) else (props_raw or {})
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if props.get("procedural_extracted"):
                        props.pop("procedural_extracted", None)
                        await engine.query(
                            "MATCH (n:Node {id: $id}) SET n.properties = $props",
                            {"id": ep_id, "props": json.dumps(props, ensure_ascii=False, default=str)},
                        )
                        total_unmarked += 1

                cleaned += 1
                logger.info(f"[prune_procedural] {ds.name}: deleted {node_count} nodes, unmarked episodes")

            except Exception as e:
                logger.warning(f"[prune_procedural] Failed for {ds.name}: {e}")

        return PruneProceduralResponse(
            success=True,
            datasets_cleaned=cleaned,
            nodes_deleted=total_deleted,
            episodes_unmarked=total_unmarked,
            message=f"Removed {total_deleted} procedural nodes from {cleaned} dataset(s), cleared {total_unmarked} episode marks",
        )

    return router
