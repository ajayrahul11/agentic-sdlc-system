"""
CLI for the agentic SDLC orchestrator.

    python -m sdlc_orchestrator doctor                       # check keys/DB/JDK/Docker/Initializr
    python -m sdlc_orchestrator run --scenario greenfield --requirement "..."
    python -m sdlc_orchestrator run --scenario brownfield --requirement "..."
    python -m sdlc_orchestrator run --scenario ambiguous  --requirement "Make the system more reliable"
    python -m sdlc_orchestrator run ... --non-interactive    # pause at human gates and exit
    python -m sdlc_orchestrator resume <run_id> --decision approve --approver rahul --rationale "tests green"
    python -m sdlc_orchestrator resume <run_id> --design-decision approve --approver rahul        # design gate
    python -m sdlc_orchestrator resume <run_id> --feedback "add an ADR on cache stampedes" --approver rahul
    python -m sdlc_orchestrator resume <run_id> --resolution "Target 99.9% availability" --approver rahul
    python -m sdlc_orchestrator status  <run_id>
    python -m sdlc_orchestrator audit   <run_id>             # who/what/when/why/outcome timeline
    python -m sdlc_orchestrator metrics <run_id> | --all
    python -m sdlc_orchestrator runs
    python -m sdlc_orchestrator reset-target                 # delete the generated project (asks first)

Add --offline (after the sub-command) to ANY command to use the deterministic stub LLM, a fake
`mvn test`, SQLite checkpoints and a JSONL event log: no API key, no
Docker, no network. Great for proving the graph wiring.
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()

from langgraph.types import Command  # noqa: E402
from rich import print as rprint  # noqa: E402
from rich.table import Table  # noqa: E402

from sdlc_orchestrator.core import config


def apply_offline() -> None:
    os.environ["STUB_MODE"] = "true"
    os.environ["CHECKPOINTER"] = "sqlite"
    os.environ["EVENT_SINK"] = "jsonl"
    os.environ.setdefault("RETRY_BACKOFF_BASE_SECONDS", "0")
    os.environ.setdefault("TARGET_REPO_PATH", "./.offline-target/url-shortener-service")


def _graph_config(run_id: str, scenario: str = "") -> dict:
    from sdlc_orchestrator.core.tracing import configure_tracing

    return {"configurable": {"thread_id": run_id}, "metadata": configure_tracing(run_id, scenario),
            "tags": [scenario] if scenario else [], "recursion_limit": config.recursion_limit()}


def pending_interrupt(graph, cfg) -> dict | None:
    snapshot = graph.get_state(cfg)
    for task in snapshot.tasks:
        for intr in getattr(task, "interrupts", ()) or ():
            return intr.value
    return None


def prompt_for(payload: dict) -> dict:
    rprint(f"\n[bold yellow]>>> HUMAN GATE: {payload.get('gate')}[/bold yellow]")
    if payload.get("gate") == "design":
        rprint(payload.get("design_document", ""))
        rprint({k: v for k, v in payload.items() if k != "design_document"})
    else:
        rprint(payload)
    who = input(f"Your name/identity [{getpass.getuser()}]: ").strip() or getpass.getuser()
    if payload.get("gate") == "design":
        choice = input("Design: [a]pprove / [r]eject (abort run) / [c]hange (give feedback): ").strip().lower()
        if choice[:1] == "a":
            return {"decision": "approve", "approver": who}
        feedback = input("Feedback / reason: ").strip()
        if choice[:1] == "c" and feedback:
            return {"decision": "revise", "approver": who, "feedback": feedback}
        return {"decision": "reject", "approver": who, "feedback": feedback}
    if payload.get("gate") == "clarification":
        text = input("Answer the ambiguities (empty = accept the listed assumptions, 'abort' = stop): ").strip()
        if text.lower() == "abort":
            return {"proceed": False, "approver": who, "resolution": "aborted by human"}
        return {"resolution": text, "approver": who}
    choice = input("Decision: [a]pprove / [r]eject / [d]esign rework: ").strip().lower()
    decision = {"a": "approve", "d": "rework_design"}.get(choice[:1], "reject")
    rationale = input("Rationale: ").strip()
    return {"decision": decision, "approver": who, "rationale": rationale}


def drive(graph, cfg, first_input, interactive: bool) -> dict:
    """Invoke, then loop through human gates until the run ends (or, when
    non-interactive, until it pauses)."""
    graph.invoke(first_input, config=cfg)
    while (payload := pending_interrupt(graph, cfg)) is not None:
        if not interactive:
            rprint(f"\n[bold yellow]Run PAUSED at human gate '{payload.get('gate')}'.[/bold yellow]")
            rprint(payload)
            if payload.get("gate") == "design":
                resume_hint = ("--design-decision approve|reject|revise --approver NAME "
                               "[--feedback 'what to change']")
            elif payload.get("gate") == "clarification":
                resume_hint = "--resolution '...' --approver NAME"
            else:
                resume_hint = "--decision approve|reject|rework_design --approver NAME --rationale '...'"
            rprint(f"Resume with:  python -m sdlc_orchestrator resume {cfg['configurable']['thread_id']} "
                   + resume_hint)
            return graph.get_state(cfg).values
        graph.invoke(Command(resume=prompt_for(payload)), config=cfg)
    return graph.get_state(cfg).values


def summarize(run_id: str, values: dict) -> None:
    from sdlc_orchestrator.core.metrics import metrics_for_run

    status = values.get("status")
    colour = {"succeeded": "green", "awaiting_approval": "yellow"}.get(status, "red")
    rprint(f"\n[bold {colour}]Run {run_id} -> status: {status}[/bold {colour}]")
    rprint({
        "mode": values.get("mode"), "plan_version": values.get("plan_version"), "strategy": values.get("codegen_strategy"),
        "retries": len(values.get("retries", [])), "replans": len(values.get("replans", [])),
        "rollbacks": len(values.get("rollbacks", [])), "commits": [c["sha"][:8] for c in values.get("commits", [])],
        "approvals": [(a["gate"], a["decision"], a["approver"]) for a in values.get("approvals", [])],
    })
    try:
        rprint(metrics_for_run(run_id))
    except Exception as exc:  # noqa: BLE001
        rprint(f"[dim](metrics unavailable: {exc})[/dim]")
    rprint(f"Audit trail: python -m sdlc_orchestrator audit {run_id}   |   metrics: python -m sdlc_orchestrator metrics {run_id}")


def cmd_run(args) -> int:
    from sdlc_orchestrator.core.checkpoint import get_checkpointer
    from sdlc_orchestrator.core.events import init_db, log_event
    from sdlc_orchestrator.workflow.graph import build_graph

    if args.offline:
        apply_offline()
    init_db()
    run_id = uuid.uuid4().hex[:12]
    cfg = _graph_config(run_id, args.scenario)
    initial = {"run_id": run_id, "scenario": args.scenario, "raw_requirement": args.requirement, "status": "running"}
    log_event(run_id, "run", "run_started", {"scenario": args.scenario, "requirement": args.requirement,
                                            "target": str(config.target_repo()), "stub": config.stub_mode()},
              actor=f"human:{getpass.getuser()}", outcome="started")

    with get_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)
        rprint(f"[bold cyan]Starting run {run_id} ({args.scenario})[/bold cyan]  target: {config.target_repo()}")
        try:
            values = drive(graph, cfg, initial, interactive=not args.non_interactive)
        except Exception as exc:  # noqa: BLE001 - a crashed run must still leave an audit record
            log_event(run_id, "run", "run_finished", {"status": "failed", "error": type(exc).__name__},
                      actor="system:orchestrator", outcome="failed", reason=str(exc)[:400])
            rprint(f"[bold red]Run {run_id} crashed:[/bold red] {type(exc).__name__}: {exc}")
            rprint(f"Audit trail: python -m sdlc_orchestrator audit {run_id}")
            return 1
        summarize(run_id, values)
    return 0 if values.get("status") in ("succeeded", "awaiting_approval") else 2


def cmd_resume(args) -> int:
    from sdlc_orchestrator.core.checkpoint import get_checkpointer
    from sdlc_orchestrator.core.events import log_event
    from sdlc_orchestrator.workflow.graph import build_graph

    if args.offline:
        apply_offline()
    cfg = _graph_config(args.run_id)
    with get_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)
        payload = pending_interrupt(graph, cfg)
        if payload is None:
            rprint(f"[red]Run {args.run_id} has no pending human gate.[/red]")
            return 1
        if payload["gate"] == "clarification":
            value = {"resolution": args.resolution or "", "approver": args.approver, "proceed": not args.decline}
        elif payload["gate"] == "design":
            if not (args.design_decision or args.feedback):
                rprint("[red]--design-decision approve|reject|revise (or --feedback '...') is required for the design gate[/red]")
                return 1
            value = {"decision": args.design_decision or "", "approver": args.approver, "feedback": args.feedback or ""}
        else:
            if not args.decision:
                rprint("[red]--decision approve|reject|rework_design is required for the release gate[/red]")
                return 1
            value = {"decision": args.decision, "approver": args.approver, "rationale": args.rationale or ""}
        try:
            graph.invoke(Command(resume=value), config=cfg)
            values = graph.get_state(cfg).values
            nxt = pending_interrupt(graph, cfg)
        except Exception as exc:  # noqa: BLE001
            log_event(args.run_id, "run", "run_finished", {"status": "failed", "error": type(exc).__name__},
                      actor="system:orchestrator", outcome="failed", reason=str(exc)[:400])
            rprint(f"[bold red]Run crashed:[/bold red] {type(exc).__name__}: {exc}")
            return 1
        if nxt is not None:
            rprint(f"[yellow]Run paused again at gate '{nxt['gate']}'.[/yellow]")
            rprint(nxt)
        summarize(args.run_id, values)
    return 0


def cmd_status(args) -> int:
    from sdlc_orchestrator.core.checkpoint import get_checkpointer
    from sdlc_orchestrator.workflow.graph import build_graph

    if args.offline:
        apply_offline()
    with get_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)
        snap = graph.get_state(_graph_config(args.run_id))
        v = snap.values
        rprint({"status": v.get("status"), "next": list(snap.next), "current_node": v.get("current_node"),
                "mode": v.get("mode"), "plan_version": v.get("plan_version"), "pending_gate": pending_interrupt(graph, _graph_config(args.run_id))})
        rprint({"tasks": [(t["id"], t["status"]) for t in v.get("tasks", [])]})
    return 0


def cmd_audit(args) -> int:
    from sdlc_orchestrator.core.events import events_for_run

    if args.offline:
        apply_offline()
    t = Table(title=f"Audit trail - run {args.run_id}", show_lines=False)
    for col in ("time", "actor", "node", "event", "outcome", "why"):
        t.add_column(col, overflow="fold")
    for e in events_for_run(args.run_id):
        if e["event_type"] == "llm_call" and not args.all:
            continue
        t.add_row(e["created_at"][11:23], e.get("actor") or "", e["node"], e["event_type"], e.get("outcome") or "", (e.get("reason") or "")[:90])
    rprint(t)
    if not args.all:
        rprint("[dim](llm_call events hidden; use --all)[/dim]")
    return 0


def cmd_metrics(args) -> int:
    from sdlc_orchestrator.core.metrics import metrics_for_run, success_rate_across_runs

    if args.offline:
        apply_offline()
    rprint(success_rate_across_runs() if args.run_id == "--all" else metrics_for_run(args.run_id))
    return 0


def cmd_runs(args) -> int:
    from sdlc_orchestrator.core.events import get_sink

    if args.offline:
        apply_offline()
    for rid in get_sink().run_ids():
        rprint(rid)
    return 0


def cmd_reset_target(args) -> int:
    if args.offline:
        apply_offline()
    target = config.target_repo()
    if not target.exists():
        rprint(f"{target} does not exist - nothing to do.")
        return 0
    if input(f"This permanently deletes {target} (the generated project, incl. its git history). Type 'yes' to confirm: ").strip() != "yes":
        rprint("Aborted.")
        return 1
    shutil.rmtree(target)
    rprint(f"Deleted {target}")
    return 0


def cmd_doctor(args) -> int:
    from sdlc_orchestrator.integrations import runners
    from sdlc_orchestrator.integrations import scaffold

    if args.offline:
        apply_offline()
    rows: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, ok, detail))

    stub = config.stub_mode()
    provider = os.environ.get("MODEL_PROVIDER", "anthropic")
    key = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    check(f"{key} set", bool(os.environ.get(key)) or stub, "(not needed in offline mode)" if stub else "")
    check("LangSmith key (optional)", True, "set" if os.environ.get("LANGCHAIN_API_KEY") else "not set - tracing disabled, audit log unaffected")
    check("git", bool(shutil.which("git")))

    kind = os.environ.get("CHECKPOINTER", "postgres")
    if kind == "postgres" and not stub:
        try:
            import psycopg

            with psycopg.connect(os.environ["ORCHESTRATOR_DB_URL"], connect_timeout=5) as conn:
                conn.execute("select 1")
            check("orchestrator Postgres reachable", True, os.environ["ORCHESTRATOR_DB_URL"].split("@")[-1])
        except Exception as exc:  # noqa: BLE001
            check("orchestrator Postgres reachable", False, f"{type(exc).__name__}: run `docker compose up -d`")
    else:
        check(f"checkpointer={kind}", True)

    if not stub:
        have, want = runners.java_major(), int(config.java_version())
        check(f"JDK >= {want}", bool(have and have >= want), f"found {have}" + ("" if have and have >= want else f" - install JDK {want} or set JAVA_VERSION={have}"))
        check("Docker daemon (Testcontainers)", runners.docker_ok())
        try:
            meta = scaffold.fetch_metadata()
            valid = scaffold.valid_dependency_ids(meta)
            chosen, skipped = scaffold.resolve_dependencies(valid)
            default = meta.get("bootVersion", {}).get("default")
            check("Spring Initializr reachable + dependency ids valid", True, f"default Boot version: {default}")
            major = int(str(default).split(".")[0]) if default else 0
            check(f"Initializr default Boot >= {config.min_boot_major()}", major >= config.min_boot_major(), str(default))
        except Exception as exc:  # noqa: BLE001
            check("Spring Initializr reachable + dependency ids valid", False, f"{type(exc).__name__}: {exc}")
    target = config.target_repo()
    check("target repo path", True, f"{target} ({'exists' if target.exists() else 'will be created by greenfield'})")

    t = Table(title="orchestrator doctor")
    t.add_column("check")
    t.add_column("ok")
    t.add_column("detail", overflow="fold")
    for name, ok, detail in rows:
        t.add_row(name, "[green]yes[/green]" if ok else "[red]NO[/red]", detail)
    rprint(t)
    return 0 if all(ok for _, ok, _ in rows) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Agentic SDLC orchestrator for the URL shortener")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--offline", action="store_true",
                        help="stub LLM + fake mvn + sqlite/jsonl stores (no keys, Docker or network)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, parents=[common], **kw)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("run", cmd_run, help="run a scenario end to end")
    sp.add_argument("--scenario", choices=["greenfield", "brownfield", "ambiguous"], required=True)
    sp.add_argument("--requirement", required=True)
    sp.add_argument("--non-interactive", action="store_true", help="pause at human gates and exit; continue with `resume`")

    sp = add("resume", cmd_resume, help="resume a paused run at its human gate")
    sp.add_argument("run_id")
    sp.add_argument("--decision", choices=["approve", "reject", "rework_design"])
    sp.add_argument("--design-decision", choices=["approve", "reject", "revise"], help="answer to the design review gate")
    sp.add_argument("--feedback", help="design review feedback; with no --design-decision it means 'revise'")
    sp.add_argument("--resolution", help="answer to clarification gate (empty = accept assumptions)")
    sp.add_argument("--decline", action="store_true", help="decline the clarification gate (aborts the run)")
    sp.add_argument("--approver", default=getpass.getuser())
    sp.add_argument("--rationale", default="")

    sp = add("status", cmd_status); sp.add_argument("run_id")
    sp = add("audit", cmd_audit); sp.add_argument("run_id"); sp.add_argument("--all", action="store_true")
    sp = add("metrics", cmd_metrics); sp.add_argument("run_id", help="run id, or --all")
    add("runs", cmd_runs)
    add("reset-target", cmd_reset_target)
    add("doctor", cmd_doctor)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
