"""Local harness that drives the real processes of this project.

The harness owns only the processes it started, writes every artifact into a
git-ignored run directory and reuses the existing ``qa/lib`` helpers instead of
duplicating them.
"""
