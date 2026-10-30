"""O painel de controlo da curadoria: play, pausa, destino, janela agendada.

Uma só linha (`compute_control`). O que ela decide:

- **ativo agora?** `state = running` e (sem janela, ou dentro da janela, ou arranque manual);
- **o worker local pode reclamar tarefas?** (`local_policy`): só se estiver ativo e o destino for
  `local`, ou o destino for externo e o local servir de reserva (`fallback_local`): então só trata
  o que já passou por um lote (`batch_tried`) ou tudo, se o destino externo está bloqueado
  (sem credenciais, sem quota, falhou várias vezes). É a regra «primeiro consome lá, depois aqui».

Por omissão (`running`, destino `local`, sem janela) tudo se comporta como antes do painel existir.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func
from sqlalchemy.orm import Session

from ariadne.compute import tasks
from ariadne.db.models import ComputeControl

LOCAL = "local"
STATES = ("stopped", "running", "paused")


class ControlError(ValueError):
    """Um valor do painel que não serve (hora inválida, destino desconhecido, …)."""


def get_control(session: Session, *, lock: bool = False) -> ComputeControl:
    """A linha de controlo (criada com os valores por omissão se não existir)."""
    row = session.get(ComputeControl, 1, with_for_update=lock)
    if row is None:
        row = ComputeControl(id=1)
        session.add(row)
        session.flush()
        session.refresh(row)
    return row


def parse_clock(value: str) -> time:
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except ValueError as exc:
        raise ControlError(f"Invalid time {value!r}: use HH:MM.") from exc


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ControlError(f"Unknown time zone: {name!r}.") from exc


def in_window(control: ComputeControl, now: datetime) -> bool:
    """`now` está dentro da janela diária? Uma janela que passa a meia-noite (23:00–07:00) vale
    das 23:00 até às 07:00 do dia seguinte. Início igual ao fim = o dia todo."""
    return window_remaining(control, now) is None or (window_remaining(control, now) or 0) > 0


def window_remaining(control: ComputeControl, now: datetime) -> float | None:
    """Segundos até a janela fechar (0 se está fora dela); `None` se não há janela (agendamento
    desligado ou janela do dia todo)."""
    if not control.schedule_enabled:
        return None
    start, end = parse_clock(control.window_start), parse_clock(control.window_end)
    if start == end:
        return None
    local = now.astimezone(zone(control.timezone))
    clock = local.time().replace(tzinfo=None)
    if start < end:
        inside = start <= clock < end
        closes = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    else:
        inside = clock >= start or clock < end
        closes = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if clock >= start:
            closes += timedelta(days=1)
    if not inside:
        return 0.0
    return max(0.0, (closes - local).total_seconds())


def next_window_start(control: ComputeControl, now: datetime) -> datetime | None:
    """O próximo início de janela (para o painel), ou `None` sem janela."""
    if not control.schedule_enabled:
        return None
    start = parse_clock(control.window_start)
    local = now.astimezone(zone(control.timezone))
    candidate = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def is_active(control: ComputeControl, now: datetime) -> bool:
    """O trabalho pode correr agora? (play dado, e janela aberta ou arranque manual.)"""
    if control.state != "running":
        return False
    return control.manual_override or in_window(control, now)


def enabled_kinds(control: ComputeControl) -> list[str]:
    return [k for k in tasks.kinds() if k not in set(control.disabled_kinds)]


@dataclass(frozen=True)
class LocalPolicy:
    """Que tipos de job o worker local pode reclamar. `free` não é governado pelo painel."""

    kinds: list[str]  # reclama tudo destes tipos
    tried_only: list[str]  # reclama destes só o que já passou por um lote externo


def local_policy(session: Session, now: datetime | None = None) -> LocalPolicy:
    now = now or datetime.now(UTC)
    control = get_control(session)
    if not is_active(control, now):
        return LocalPolicy([], [])
    kinds = enabled_kinds(control)
    if control.destination == LOCAL:
        return LocalPolicy(kinds, [])
    if not control.fallback_local:
        return LocalPolicy([], [])
    if control.external_blocked_reason:
        return LocalPolicy(kinds, [])
    return LocalPolicy([], kinds)


def update(session: Session, who: str | None, **changes: Any) -> ComputeControl:
    """Aplica mudanças validadas ao painel. Não faz commit."""
    row = get_control(session, lock=True)
    for key, value in changes.items():
        if value is None:
            continue
        if key == "window_start" or key == "window_end":
            parse_clock(value)
        elif key == "timezone":
            zone(value)
        elif key == "batch_size" and not 1 <= value <= 5000:
            raise ControlError("batch_size must be between 1 and 5000.")
        elif key == "max_run_hours" and not 0.25 <= value <= 24:
            raise ControlError("max_run_hours must be between 0.25 and 24.")
        elif key == "disabled_kinds":
            unknown = sorted(set(value) - set(tasks.kinds()))
            if unknown:
                raise ControlError(f"Unknown task kinds: {', '.join(unknown)}.")
            value = sorted(set(value))
        setattr(row, key, value)
    row.updated_at = func.now()
    row.updated_by = who
    session.flush()
    return row


def set_state(
    session: Session, state: str, who: str | None, *, ignore_window: bool = False
) -> None:
    """play (`running`), pausa (`paused`) ou paragem (`stopped`). O arranque manual
    (`ignore_window`) vale até à pausa ou paragem seguinte; pausar e parar limpam-no."""
    if state not in STATES:
        raise ControlError(f"Unknown state: {state!r}.")
    row = get_control(session, lock=True)
    row.state = state
    row.manual_override = ignore_window and state == "running"
    row.updated_at = func.now()
    row.updated_by = who
    session.flush()
