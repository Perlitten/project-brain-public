import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from loguru import logger
from sqlalchemy import select

from brain.config.paths import get_repo_root, reports_dir
from brain.config.settings import settings
from brain.database.session import async_session_factory
from brain.database.models import Rule
from brain.database.repository_utils import require_repository_by_path
from brain.graph.graph_client import GraphClient
from brain.llm.router import TaskKind, get_model_router

# Words that end up as Symbol nodes when the graph extractor parses SQL text
# or prose, but are essentially never real code symbols. They pollute impact
# analysis ("WHERE", "VARCHAR", "btrim" showing up as affected by a rename).
# Matching is case-insensitive; file paths are never filtered.
_SQL_NOISE_WORDS = frozenset(
    """
    select where and or not insert update delete from join on as into values
    create table index varchar text integer bigint timestamp timestamptz
    boolean json jsonb primary key foreign references if exists add column
    order by group limit offset union distinct null like ilike in between
    case when then else end coalesce now count sum avg min max lower upper
    trim btrim ltrim rtrim length set
    """.split()
)

_ENGLISH_NOISE_WORDS = frozenset(
    """
    the a an in on of to for with ok width know
    """.split()
)


def _is_noise_node(name: str | None) -> bool:
    """True for graph nodes that look like SQL/prose debris, not code."""
    if not name:
        return True
    if "/" in name or "." in name:
        return False  # file paths and dotted paths are always meaningful
    low = name.strip().lower()
    if low in ("unknown", ""):
        return True
    return low in _SQL_NOISE_WORDS or low in _ENGLISH_NOISE_WORDS


class ImpactAnalyzer:
    """Analyzes the impact of changes across project components."""

    def __init__(self, repo_path: Optional[str | Path] = None):
        self.router = get_model_router()
        self.repo_path = Path(repo_path).resolve() if repo_path else get_repo_root()

    async def analyze_bounded(self, change_request: str) -> dict:
        """Deterministic synchronous impact projection for the agent fast path.

        It makes exactly one bounded graph query and never writes a Markdown
        report or waits for an LLM. Narrative reports remain an explicit durable
        operation on the legacy/deep surface.
        """
        keywords = list(
            dict.fromkeys(
                word.casefold()
                for word in re.findall(r"\b[A-Za-z_][A-Za-z0-9_/-]{2,}\b", change_request)
                if word.casefold() not in {"the", "and", "for", "with", "change", "update", "from", "that"}
            )
        )[:12]
        repository = await require_repository_by_path(self.repo_path)
        graph_client = GraphClient(repository_id=repository.id)
        cypher = (
            "UNWIND $keywords AS kw "
            "MATCH (n) WHERE n.repository_id = $repository_id "
            "AND (toLower(n.name) CONTAINS kw OR (n.path IS NOT NULL AND toLower(n.path) CONTAINS kw)) "
            "OPTIONAL MATCH path = (n)-[*1..2]-(m) "
            "WHERE ALL(node IN nodes(path) WHERE node.repository_id = $repository_id) "
            "RETURN n, m, length(path) AS distance LIMIT $limit"
        )
        nodes: dict[str, dict] = {}
        try:
            async def query_graph() -> None:
                async with graph_client.driver.session() as session:
                    result = await session.run(
                        cypher, keywords=keywords, repository_id=repository.id, limit=160
                    )
                    async for record in result:
                        for key, distance in (("n", 0), ("m", int(record.get("distance") or 0))):
                            node = record.get(key)
                            if not node:
                                continue
                            node_name = node.get("path") or node.get("name") or "Unknown"
                            if _is_noise_node(node_name):
                                continue
                            node_id = getattr(node, "element_id", None) or str(node)
                            nodes[node_id] = {
                                "name": node_name,
                                "type": next(iter(node.labels), "Unknown"),
                                "distance": distance,
                            }
            await asyncio.wait_for(query_graph(), timeout=settings.AGENT_IMPACT_DEADLINE_S * 0.8)
        except asyncio.TimeoutError:
            return {"status": "partial", "keywords": keywords, "degraded": ["graph_deadline_exceeded"], "affected": []}
        except Exception as exc:
            logger.warning("Bounded impact graph degraded: {}", type(exc).__name__)
            return {"status": "partial", "keywords": keywords, "degraded": [f"graph_error:{type(exc).__name__}"], "affected": []}

        affected = sorted(nodes.values(), key=lambda item: (item["distance"], item["name"]))[:80]
        critical = {"security", "auth", "payment", "billing", "database", "migration"}
        score = min(100, 10 + min(len(affected) * 2, 60) + 15 * len(set(keywords) & critical))
        risk = "low" if score < 25 else "medium" if score < 50 else "high" if score < 80 else "critical"
        return {
            "status": "ok",
            "keywords": keywords,
            "risk": {"level": risk, "score": score},
            "affected": affected,
            "degraded": [],
        }

    async def analyze_impact(self, change_request: str) -> dict:
        """Finds directly/indirectly affected surfaces using graph queries,

        computes a risk score, and generates an impact analysis markdown report.
        """
        logger.info(f"ImpactAnalyzer: Analyzing change request: {change_request}")

        # 1. Extract keywords/features from change request using LLM
        prompt = (
            f"Analyze the following change request:\n"
            f'"""\n{change_request}\n"""\n\n'
            f"Extract technical keywords or names of modules, components, screens, or database tables "
            f"that are likely start nodes of this change request.\n"
            f"Respond ONLY with a JSON list of strings (keywords). Do not include markdown code block formatting."
        )

        keywords = []
        try:
            response = await self.router.llm(TaskKind.CLASSIFICATION).generate(
                prompt=prompt,
                system_instruction="You are an AI assistant that extracts technical keywords for graph database search. Respond with valid JSON only.",
            )
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\n", "", cleaned)
                cleaned = re.sub(r"\n```$", "", cleaned)
            keywords = json.loads(cleaned.strip())
        except Exception as e:
            logger.warning(f"Failed to extract keywords using LLM: {e}. Falling back to words extraction.")
            words = re.findall(r"\b[a-zA-Z]{3,}\b", change_request)
            stopwords = {
                "the",
                "and",
                "for",
                "that",
                "this",
                "with",
                "from",
                "should",
                "would",
                "could",
                "about",
                "change",
                "update",
                "request",
            }
            keywords = list(set([w.lower() for w in words if w.lower() not in stopwords]))

        # 2. Query Neo4j for start nodes and their neighbors (1-2 steps)
        directly_affected = []
        indirectly_affected = []
        visited_nodes = {}
        repository = await require_repository_by_path(self.repo_path)
        graph_client = GraphClient(repository_id=repository.id)

        # Query structure for Neo4j
        # We find starting nodes matching the keywords, then traverse up to 2 steps.
        async with graph_client.driver.session() as session:
            for kw in keywords:
                if len(kw.strip()) < 3:
                    continue
                cypher = (
                    "MATCH (n) "
                    "WHERE n.repository_id = $repository_id "
                    "AND (toLower(n.name) CONTAINS toLower($kw) "
                    "OR toLower(n.path) CONTAINS toLower($kw)) "
                    "OPTIONAL MATCH path = (n)-[r*1..2]-(m) "
                    "WHERE ALL(node IN nodes(path) "
                    "WHERE node.repository_id = $repository_id) "
                    "RETURN n, path"
                )
                try:
                    result = await session.run(
                        cypher,
                        kw=kw,
                        repository_id=repository.id,
                    )
                    async for record in result:
                        node_n = record["n"]
                        if not node_n:
                            continue

                        # Register start node
                        n_id = node_n.element_id if hasattr(node_n, "element_id") else str(node_n)
                        n_name = node_n.get("name", "Unknown")
                        n_labels = list(node_n.labels)
                        n_label = n_labels[0] if n_labels else "Unknown"

                        if n_id not in visited_nodes and not _is_noise_node(n_name):
                            visited_nodes[n_id] = {
                                "name": n_name,
                                "type": n_label,
                                "distance": 0,
                                "properties": dict(node_n),
                            }

                        path = record["path"]
                        if path:
                            # Parse nodes in the path
                            nodes_in_path = path.nodes
                            # Directly affected are at distance 1
                            if len(nodes_in_path) >= 2:
                                direct_node = nodes_in_path[1]
                                d_id = (
                                    direct_node.element_id if hasattr(direct_node, "element_id") else str(direct_node)
                                )
                                d_name = direct_node.get("name", "Unknown")
                                d_labels = list(direct_node.labels)
                                d_label = d_labels[0] if d_labels else "Unknown"
                                if d_id not in visited_nodes and not _is_noise_node(d_name):
                                    visited_nodes[d_id] = {
                                        "name": d_name,
                                        "type": d_label,
                                        "distance": 1,
                                        "properties": dict(direct_node),
                                    }

                            # Indirectly affected are at distance 2
                            if len(nodes_in_path) >= 3:
                                indirect_node = nodes_in_path[2]
                                i_id = (
                                    indirect_node.element_id
                                    if hasattr(indirect_node, "element_id")
                                    else str(indirect_node)
                                )
                                i_name = indirect_node.get("name", "Unknown")
                                i_labels = list(indirect_node.labels)
                                i_label = i_labels[0] if i_labels else "Unknown"
                                if i_id not in visited_nodes and not _is_noise_node(i_name):
                                    visited_nodes[i_id] = {
                                        "name": i_name,
                                        "type": i_label,
                                        "distance": 2,
                                        "properties": dict(indirect_node),
                                    }
                except Exception as e:
                    logger.warning(f"Error executing Cypher for keyword '{kw}': {e}")

        # Separate nodes into directly and indirectly affected
        start_nodes = []
        for n_info in visited_nodes.values():
            if n_info["distance"] == 0:
                start_nodes.append(n_info)
            elif n_info["distance"] == 1:
                directly_affected.append(n_info)
            elif n_info["distance"] == 2:
                indirectly_affected.append(n_info)

        # If graph search yields nothing, mock/deduce some files based on keywords or postgres
        if not start_nodes:
            logger.info("Graph search returned 0 nodes. Performing fallback file search in Postgres...")
            async with async_session_factory() as session:
                for kw in keywords:
                    stmt = select(Rule).where(Rule.id.ilike(f"%{kw}%"))
                    res = await session.execute(stmt)
                    rules = res.scalars().all()
                    for r in rules:
                        start_nodes.append({"name": r.id, "type": "Rule", "properties": {"description": r.description}})

        # 3. Compute Risk Level and Score
        risk_score = 10  # Baseline
        reasons = []

        # Feature/Endpoint/DB impact factors
        api_count = 0
        db_count = 0
        screen_count = 0
        file_count = 0

        all_affected = start_nodes + directly_affected + indirectly_affected
        for node in all_affected:
            ntype = node["type"].lower()
            if "apiendpoint" in ntype:
                api_count += 1
            elif "databaseentity" in ntype:
                db_count += 1
            elif "screen" in ntype:
                screen_count += 1
            elif "file" in ntype:
                file_count += 1

        if api_count > 0:
            risk_score += min(api_count * 15, 45)
            reasons.append(f"Modifies {api_count} API Endpoint(s).")
        if db_count > 0:
            risk_score += min(db_count * 20, 50)
            reasons.append(f"Affects {db_count} Database Entity/Entities.")
        if screen_count > 0:
            risk_score += min(screen_count * 10, 30)
            reasons.append(f"Alters UI layouts of {screen_count} Screen(s).")
        if file_count > 5:
            risk_score += 15
            reasons.append(f"High code modification scope ({file_count} files affected).")

        # Critical keyword check
        critical_kws = ["security", "login", "auth", "payment", "stripe", "billing", "database", "migration"]
        matched_crit = [kw for kw in keywords if kw.lower() in critical_kws]
        if matched_crit:
            risk_score += len(matched_crit) * 15
            reasons.append(f"Involves critical system domains: {', '.join(matched_crit)}")

        # Map score to level
        if risk_score < 25:
            risk_level = "Low"
        elif risk_score < 50:
            risk_level = "Medium"
        elif risk_score < 80:
            risk_level = "High"
        else:
            risk_level = "Critical"

        # Check for missing cross-surface context
        surfaces = ["android", "landing", "backoffice"]
        change_desc_lower = change_request.lower()
        specified_surfaces = [s for s in surfaces if s in change_desc_lower]

        missing_surfaces = []
        for surface in specified_surfaces:
            has_surface = any(
                surface in (node.get("name") or "").lower()
                or surface in (node.get("properties", {}).get("path") or "").lower()
                or surface in (node.get("properties", {}).get("summary") or "").lower()
                for node in all_affected
            )
            if not has_surface:
                missing_surfaces.append(surface)

        if missing_surfaces:
            risk_level = "High"
            risk_score = max(risk_score, 75)
            reasons.append(
                f"Missing cross-surface context for: {', '.join(missing_surfaces)}. Risk elevated to High (Unknown Risk)."
            )

        # 4. Generate checks and markdown report using LLM
        directly_list = "\n".join([f"- **{n['name']}** ({n['type']})" for n in start_nodes + directly_affected])
        indirectly_list = (
            "\n".join([f"- **{n['name']}** ({n['type']})" for n in indirectly_affected])
            if indirectly_affected
            else "- None"
        )

        llm_prompt = (
            f"You are a technical risk assessor. Analyze the following project changes:\n"
            f"- Change Request: {change_request}\n"
            f"- Directly Affected: \n{directly_list}\n"
            f"- Indirectly Affected: \n{indirectly_list}\n"
            f"- Initially Computed Risk: {risk_level} (score {risk_score}/100)\n\n"
            f"Generate a verification plan and risk summary. "
            f"Respond ONLY with a JSON object containing keys:\n"
            f"- 'adjusted_risk_level': 'Low', 'Medium', 'High', or 'Critical'\n"
            f"- 'rationale': a string explaining why this risk level is assigned\n"
            f"- 'verification_checks': a list of test cases/verification checks to execute\n"
            f"Do not include markdown code block markers in your response."
        )

        rationale = "No rationale provided."
        verification_checks = []
        try:
            response = await self.router.llm(TaskKind.SYNTHESIS).generate(
                prompt=llm_prompt, system_instruction="You are an expert risk auditor. Respond with valid JSON only."
            )
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\n", "", cleaned)
                cleaned = re.sub(r"\n```$", "", cleaned)
            assessment_data = json.loads(cleaned.strip())
            # Trust the LLM only to RAISE the deterministic risk, never to lower
            # it — a prompt-injected change_request must not be able to fabricate
            # a "Low" verdict for a genuinely High/Critical change.
            _order = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}
            llm_level = assessment_data.get("adjusted_risk_level")
            # Unknown current level -> -1 so the LLM can never *lower* an
            # unrecognised risk by treating it as "Low".
            if llm_level in _order and _order[llm_level] > _order.get(risk_level, -1):
                risk_level = llm_level
            rationale = assessment_data.get("rationale", rationale)
            verification_checks = assessment_data.get("verification_checks", [])
        except Exception as e:
            logger.warning(f"Failed to generate LLM assessment: {e}. Using deterministic values.")
            rationale = " - " + "\n - ".join(reasons) if reasons else "Simple local scope modification."
            verification_checks = [
                "Compile the codebase and verify no syntax/compilation errors.",
                "Ensure existing unit tests run and pass successfully.",
                "Manually verify the correctness of the changed functions.",
            ]

        # 5. Format Markdown Report
        checks_md = "\n".join([f"- [ ] {check}" for check in verification_checks])

        markdown_content = f"""# Impact Analysis Report

## Change Request
- **Request**: {change_request}
- **Computed Risk Level**: **{risk_level}** (Score: {risk_score}/100)

## Affected Surfaces
### Directly Affected
{directly_list if (start_nodes + directly_affected) else "- No directly affected surfaces identified."}

### Indirectly Affected
{indirectly_list}

## Risk Analysis & Rationale
{rationale}

## Required Verification Checks
{checks_md}
"""

        reports_path = reports_dir()
        reports_path.mkdir(parents=True, exist_ok=True)
        report_path = reports_path / f"impact_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

        with open(report_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)

        return {
            "risk_level": risk_level,
            "risk_score": risk_score,
            "directly_affected": [n["name"] for n in start_nodes + directly_affected],
            "indirectly_affected": [n["name"] for n in indirectly_affected],
            "rationale": rationale,
            "verification_checks": verification_checks,
            "report_path": report_path.as_posix(),
            "markdown_content": markdown_content,
        }
