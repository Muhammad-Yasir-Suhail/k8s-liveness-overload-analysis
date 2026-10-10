#!/usr/bin/env python3
"""render.py TEMPLATE  -> prints TEMPLATE with ${VARS} replaced from the environment.
Fails loudly if a variable is missing, so a typo never produces a broken run."""
import os, sys, string
tpl = string.Template(open(sys.argv[1]).read())
try:
    sys.stdout.write(tpl.substitute(os.environ))
except KeyError as e:
    sys.exit(f"render.py: variable {e} is not set (template {sys.argv[1]})")
