"""Comandos `ariadne batch`: o lote LLM por ficheiros (docs/WIKI-IMPORT.md, secção 7).

O mesmo que o painel faz para o destino manual (Colab), sem o dashboard: `export` tira as tarefas
da fila para um pacote (`tasks.jsonl` + `manifest.json`), o notebook
`deploy/gpu/ariadne_batch.ipynb` responde-lhe, e `import` valida e aplica o resultado (o que não
voltar passa ao curador local).
"""

import zipfile
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from ariadne import queue
from ariadne.compute import control as panel
from ariadne.compute import runs, tasks, tick
from ariadne.config import get_settings
from ariadne.db.models import BatchRun
from ariadne.services import Services

batch_app = typer.Typer(help="LLM batch on an external GPU: export tasks, import results.")


def _services() -> Services:
    return Services.open(get_settings())


@batch_app.command("export")
def batch_export(
    out: Annotated[Path, typer.Argument(help="The .zip bundle to write.")],
    kind: Annotated[
        list[str] | None, typer.Option("--kind", help="Task kind (repeatable); default: all.")
    ] = None,
    limit: Annotated[int, typer.Option(help="Maximum number of tasks.")] = 200,
    backend: Annotated[str, typer.Option(help="Manual destination (colab).")] = "colab",
) -> None:
    """Build a batch: take up to `--limit` tasks from the queue (state `batched`) and write the
    bundle."""
    services = _services()
    try:
        target = services.get_backends().get(backend)
        if target is None or target.mode != "manual":
            typer.echo("Only manual destinations export to a file (colab).", err=True)
            raise typer.Exit(2)
        with services.sessions() as session:
            control = panel.get_control(session)
            kinds = kind or panel.enabled_kinds(control)
            config = tick.destination_config(control, target)
            session.commit()
        try:
            run_id = tick.dispatch(
                services, target, kinds, limit, 12 * 3600, config, manual=True, who="cli"
            )
        except runs.RunError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        with services.sessions() as session:
            run = session.get(BatchRun, run_id)
            assert run is not None
            manifest = runs.build_manifest(run, run.deadline_seconds or 12 * 3600, config)
            out.write_bytes(runs.bundle(run, manifest))
            typer.echo(
                f"Run {run.id}: {run.task_count} tasks, {run.request_count} requests -> {out}"
            )
    finally:
        services.close()


@batch_app.command("import")
def batch_import(
    file: Annotated[Path, typer.Argument(help="results.jsonl, or ariadne_results.zip.")],
    run: Annotated[int | None, typer.Option(help="The run; default: the one waiting.")] = None,
) -> None:
    """Validate and apply the results of a run (schema, fidelity, flags). What passes is left for
    human review (`BATCH_AUTO_PUBLISH` allows publishing); the rest goes back to the local queue."""
    try:
        data, meta = runs.read_results_file(file.read_bytes())
    except (zipfile.BadZipFile, ValueError) as exc:
        typer.echo(f"Invalid file: {exc}", err=True)
        raise typer.Exit(1) from exc
    services = _services()
    try:
        with services.sessions() as session:
            row = session.get(BatchRun, run) if run else runs.current_run(session)
            if row is None:
                typer.echo("No run is waiting for results.", err=True)
                raise typer.Exit(1)
            try:
                count = runs.store_results(session, row, data, meta)
            except runs.RunError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1) from exc
            run_id = row.id
            session.commit()
        counts = runs.import_run(services, run_id)
        typer.echo(f"Run {run_id}: {count} responses read; {counts}")
    finally:
        services.close()


@batch_app.command("status")
def batch_status() -> None:
    """The state of the panel, the queue and the latest runs."""
    services = _services()
    try:
        with services.sessions() as session:
            control = panel.get_control(session)
            typer.echo(
                f"state={control.state} destination={control.destination} "
                f"window={'yes' if control.schedule_enabled else 'no'} "
                f"block={control.external_blocked_reason or '-'}"
            )
            for kind, counts in sorted(queue.counts_by_kind(session).items()):
                if tasks.get(kind) is not None:
                    typer.echo(f"  {kind}: {counts}")
            for r in session.scalars(select(BatchRun).order_by(BatchRun.id.desc()).limit(5)):
                typer.echo(
                    f"  run {r.id} [{r.backend}] {r.status}: {r.task_count} tasks, "
                    f"{r.applied_count + r.review_count} applied, {r.released_count} returned"
                )
            session.commit()
    finally:
        services.close()
