"""
Dev harness: run ONE agent in isolation (real LLM unless --offline) so each
prompt can be eyeballed before it is trusted inside the full graph.

    python -m app.dev requirements "Build a URL shortener with shorten, redirect and click analytics"
    python -m app.dev requirements "make the system more reliable"      # expect ambiguities
    python -m app.dev decomposition "Build a URL shortener ..."         # + valid DAG, waves
    python -m app.dev design "Build a URL shortener ..."                # + OpenAPI/ADRs/component plan
    python -m app.dev scaffold                                          # real start.spring.io project in a temp dir
Add --offline to use the stub LLM.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def main(argv: list[str]) -> int:
    offline = "--offline" in argv
    argv = [a for a in argv if a != "--offline"]
    if offline:
        os.environ["STUB_MODE"] = "true"
    os.environ["EVENT_SINK"] = "memory"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    if len(argv) < 1:
        print(__doc__)
        return 1
    what = argv[0]

    from rich import print as rprint

    from app import config, dag, events

    events.set_sink(events.MemorySink())

    if what == "scaffold":
        from app import scaffold

        dest = Path(tempfile.mkdtemp(prefix="scaffold-"))
        facts = scaffold.scaffold_project_stub(dest) if config.stub_mode() else scaffold.scaffold_project(dest)
        rprint(facts)
        rprint(f"project in {dest}\nverify: cd {dest} && ./mvnw -q -DskipTests compile")
        return 0

    text = " ".join(argv[1:]).strip()
    if not text:
        print("requirement text required")
        return 1

    from app.nodes.decomposition_agent import decomposition_node
    from app.nodes.design_agent import design_node
    from app.nodes.requirements_agent import requirements_node

    state: dict = {"run_id": "dev", "scenario": "greenfield", "mode": "greenfield", "raw_requirement": text,
                   "workspace": {"base_sha": "dev", "repo_path": str(Path(tempfile.mkdtemp(prefix="dev-")))}}

    def merge(delta: dict) -> None:
        delta.pop("timeline", None)
        state.update(delta)

    merge(requirements_node(state))
    spec = state["requirement_spec"]
    if what == "requirements":
        rprint(spec)
        rprint(f"[bold]ambiguities: {len(state['ambiguities'])}  assumptions: {len(state['assumptions'])}[/bold]")
        return 0

    state["ambiguities_resolved"] = True
    merge(decomposition_node(state))
    if what == "decomposition":
        rprint([(t["id"], t["owner_stage"], t["depends_on"]) for t in state["tasks"]])
        rprint("waves:", state["plan_waves"], "| dag errors:", dag.validate_dag(state["tasks"], "greenfield"))
        return 0

    merge(design_node(state))
    d = state["design_doc"]
    rprint({"endpoints": sorted(d["api_contract"]["paths"]), "migrations": [m["name"] for m in d["migrations"]],
            "components": [(c["class"].split(".")[-1], c["branch"]) for c in d["component_plan"]],
            "adrs": [a["title"] for a in d["adrs"]]})
    rprint(json.dumps(d, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
