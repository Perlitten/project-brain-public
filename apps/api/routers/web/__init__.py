"""Read-only JSON API for the web UI (apps/web).

Every endpoint here replaces a screen of the retired server-rendered dashboard.
Values are read from the same stores the dashboard read; anything the system
does not record is returned as ``null`` rather than invented.
"""
from __future__ import annotations

from ._common import (ROOT_MODULE as ROOT_MODULE, WEB_READ_SCOPE as WEB_READ_SCOPE, router as router, REPOSITORY_READ as REPOSITORY_READ, TEAM_ADMIN as TEAM_ADMIN, _as_int as _as_int, _as_dict as _as_dict, _iso as _iso, _parse_dt as _parse_dt, _pct as _pct, module_of as module_of, _repository_dict as _repository_dict, _resolve_repository as _resolve_repository, _context_pack_roots as _context_pack_roots, _resolve_inside as _resolve_inside, _paged_records as _paged_records)
from .dashboard import (web_dashboard as web_dashboard, web_agent_value as web_agent_value)
from .packs import (resolve_context_pack_file as resolve_context_pack_file, _BUDGET_RE as _BUDGET_RE, parse_context_pack_markdown as parse_context_pack_markdown, shape_context_pack as shape_context_pack, web_context_packs as web_context_packs, web_context_pack_detail as web_context_pack_detail)
from .rules import (_decision_status as _decision_status, _rule_severity as _rule_severity, web_decisions as web_decisions, web_rules as web_rules, _rule_usage_for_page as _rule_usage_for_page)
from .reports import (report_kind as report_kind, shape_report as shape_report, web_reports as web_reports, web_report_detail as web_report_detail)
from .indexing import (shape_index_run as shape_index_run, web_index_runs as web_index_runs)
from .modules import (MODULE_GRAPH_EDGE_LIMIT as MODULE_GRAPH_EDGE_LIMIT, _dotted as _dotted, file_edge_violates as file_edge_violates, aggregate_module_edges as aggregate_module_edges, load_file_import_edges as load_file_import_edges, web_module_graph as web_module_graph, _add_module_row as _add_module_row, aggregate_modules as aggregate_modules, web_modules as web_modules)
from .overview import (shape_job as shape_job, _repository_freshness as _repository_freshness, build_coverage as build_coverage, web_overview as web_overview, web_health as web_health)
from fastapi import HTTPException as HTTPException
