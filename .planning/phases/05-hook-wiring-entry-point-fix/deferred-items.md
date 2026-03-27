# Deferred Items — Phase 05

## Out-of-scope failures discovered during 05-01 execution

### test_install_skills_removed (carta/install/tests/test_bootstrap.py)

**Discovered during:** Task 3 overall verification
**Status:** Pre-existing conflict from Phase 04 merge
**Root cause:** Phase 04 added `_install_skills()` to `bootstrap.py` (commit 66a85bc),
but `test_bootstrap.py::test_install_skills_removed` asserts `_install_skills` must NOT
exist. This is an inter-phase contradiction — either the test or the function needs
resolution in a dedicated plan.
**Not caused by:** Any change in Phase 05-01
**Action required:** Phase 05 or 06 needs a plan to reconcile `_install_skills` vs
`_remove_plugin_cache()` semantics and update the test.
