import sys

# t-string tests use 3.14-only syntax; older interpreters cannot even parse them.
collect_ignore_glob = [] if sys.version_info >= (3, 14) else ["*_py314.py"]
