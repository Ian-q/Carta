# Apply platform workarounds BEFORE anything imports torch (see _compat).
from carta._compat import apply_macos_blas_workaround as _apply_macos_blas_workaround

_apply_macos_blas_workaround()

__version__ = "0.13.0"
