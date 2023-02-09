# import os

# import pytest

from ddtrace.internal.compat import PY2


if PY2:
    OUT = """Enabling debugging exploration testing
========================== LineCoverage: probes stats ==========================

Installed probes: 0/0

================================ Line coverage =================================

Source                                                       Lines Covered
==========================================================================
No lines found
===================== DeterministicProfiler: probes stats ======================

Installed probes: 0/0

============================== Function coverage ===============================

No functions called
"""
else:
    OUT = """Enabling debugging exploration testing
===================== DeterministicProfiler: probes stats ======================

Installed probes: 0/0

============================== Function coverage ===============================

No functions called
========================== LineCoverage: probes stats ==========================

Installed probes: 0/0

================================ Line coverage =================================

Source                                                       Lines Covered
==========================================================================
No lines found
"""

# TODO(avara1986): research why the path is wrong and this test doesn't find the sitecustomize of this folder
# @pytest.mark.subprocess(env={"PYTHONPATH": os.path.dirname(__file__) + ":."}, out=OUT)
# def test_exploration_bootstrap():
#     # We test that we get the expected output from the exploration debuggers
#     # and no errors when running the sitecustomize.py script.
#     pass
