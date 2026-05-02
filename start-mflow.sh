#!/bin/bash
# mflow launcher — load .env then start the API server
set -a
source /home/gql/repos/m_flow/.env
set +a
exec /home/gql/.local/bin/uv run python -m m_flow.api.client
