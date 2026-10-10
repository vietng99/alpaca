"""The live server's import probe (serve.code_importable): `python -m alpaca.import_probe <module>...`
imports the modules named and exits 0, so a file caught half-written or a syntax error keeps the old
server up instead of taking it down. It starts through the one launcher (`alpaca.child.python_argv`,
t-070 round 2 C-A).

It lives apart from `alpaca.child` because that module is in the research engine's static import
closure, where no file may import a module by a computed name
(test_research_session_engine.test_no_engine_file_loads_a_module_by_a_computed_name). Nothing imports
this module; the server names it only as the module its child runs. Stdlib only.
"""
import importlib
import sys


def main(argv=None):
    names = list(sys.argv[1:] if argv is None else argv)
    if not names:
        sys.stderr.write("usage: python -m alpaca.import_probe <module>...\n")
        return 2
    for name in names:
        importlib.import_module(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
