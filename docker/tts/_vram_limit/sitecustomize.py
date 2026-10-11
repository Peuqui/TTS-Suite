"""Caps the PyTorch memory of a TTS container at its VRAM budget.

Python imports ``sitecustomize`` at start-up of every interpreter that finds it on ``PYTHONPATH``; the
compose files mount this folder at ``/sc`` and set ``PYTHONPATH=/sc``. The budget arrives in
``TTS_VRAM_BUDGET_MIB`` (the suite sets it when it starts the container). Without it (empty or unset)
this file does nothing. A value that is not a positive integer ends the interpreter: ``site.py``
swallows every ``Exception`` from ``sitecustomize`` (the program would run without the cap), but
``SystemExit`` passes, so the container fails to start and the launcher reports it.

Measured on a V100: a limit just above an engine's need changes nothing, a limit below the model's
need makes loading fail with ``OutOfMemoryError``. The limit counts PyTorch's allocator only, not the
CUDA context.
"""

import os

BUDGET_ENV = "TTS_VRAM_BUDGET_MIB"
BYTES_PER_MIB = 1024 * 1024


def cap_cuda_memory(budget_mib: int) -> None:
    import torch

    def apply_cap() -> None:
        total_bytes = torch.cuda.get_device_properties(0).total_memory
        fraction = budget_mib * BYTES_PER_MIB / total_bytes
        torch.cuda.set_per_process_memory_fraction(fraction, 0)
        print(f"[sitecustomize] VRAM cap {budget_mib} MiB ({fraction:.3f} of {total_bytes // BYTES_PER_MIB} MiB)", flush=True)

    # ``_lazy_call`` is a PRIVATE PyTorch interface (it may break with a PyTorch update). It runs
    # ``apply_cap`` when CUDA is first initialised. It has to, because the server forks workers
    # (gunicorn): calling torch.cuda here would initialise CUDA in the master before the fork, and CUDA
    # cannot be used in a forked child.
    torch.cuda._lazy_call(apply_cap)


def read_budget_mib(raw: str) -> int:
    message = f"[sitecustomize] {BUDGET_ENV}={raw!r} is not a positive integer (MiB); refusing to run without the VRAM cap"
    try:
        budget_mib = int(raw)
    except ValueError:
        raise SystemExit(message) from None
    if budget_mib <= 0:
        raise SystemExit(message)
    return budget_mib


_budget = os.environ.get(BUDGET_ENV)
if _budget:
    cap_cuda_memory(read_budget_mib(_budget))
