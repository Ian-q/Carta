"""Platform compatibility shims applied at import time.

Kept dependency-free (stdlib only) so it can run from ``carta/__init__.py``
*before* anything imports torch.
"""
import os
import platform

# BLAS thread-count env vars, read by their libraries at first init. Must be set
# before torch (and thus Accelerate/OpenBLAS/MKL) is imported to take effect.
_BLAS_THREAD_VARS = (
    "VECLIB_MAXIMUM_THREADS",  # Apple Accelerate (macOS torch-CPU default)
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
)


def apply_macos_blas_workaround(system: str | None = None) -> bool:
    """Pin BLAS to a single thread on macOS to avoid a torch-CPU segfault.

    torch-CPU on Apple Silicon dispatches matmuls (e.g. ColPali's ``addmm``) to
    Apple's Accelerate ``cblas_sgemm``, whose *multithreaded* ``dispatch_apply``
    path intermittently SIGSEGVs under load (worse under memory pressure). Pinning
    the BLAS libraries to one thread sidesteps it. Slower, but stable.

    Uses ``setdefault`` so an explicit user setting always wins. Returns True when
    applied (i.e. running on macOS).
    """
    if (system or platform.system()) != "Darwin":
        return False
    for var in _BLAS_THREAD_VARS:
        os.environ.setdefault(var, "1")
    return True
