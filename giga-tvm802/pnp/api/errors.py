"""HTTP error mapping shared by the API routers (app.py and vision_routes.py)."""

from __future__ import annotations

from fastapi import HTTPException

from pnp.machine.machine import EStopped, MachineError, NotHomed, NotReady, SoftLimitError

def status_for(exc: MachineError) -> int:
    if isinstance(exc, (SoftLimitError,)):
        return 400
    if isinstance(exc, (NotReady, NotHomed, EStopped)):
        return 409
    from pnp.job.head_ops import HeadConflict, NotCalibrated  # late import: avoids a cycle

    if isinstance(exc, (HeadConflict, NotCalibrated)):
        return 409
    return 500


async def call(coro):
    try:
        return await coro
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except MachineError as exc:
        raise HTTPException(status_for(exc), str(exc)) from exc
