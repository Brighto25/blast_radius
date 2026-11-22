# Blast Radius entry

Picks the sqlglot tests a patch can break, runs the cheap ones on the patched code
to keep only real failures, and packs the rest onto workers.

## How it decides

1. **What changed** (`selector.py`, `analyse_py`): patches are applied to the pinned
   checkout and compared as ASTs. Comment/whitespace-only edits are no-ops. Otherwise
   the change is either *function bodies only* (exact qualnames) or *structural*
   (class/module-level statements, signatures, new/deleted files, syntax errors).
2. **Candidates** from `map.json.gz`, built offline from sqlglot's own test suite at
   the pinned commit (`tools/`):
   - function-entry coverage per test (`sys.monitoring`), with spawn process pools
     run in-process so their work is traced;
   - line coverage for slow tests, so a slow test is a candidate only if it executed
     the changed statement;
   - which files each test opens (fixtures), import-time code, test-module imports,
     name references between functions, dialect families;
   - measured in-process CPU per test.
3. **Run here** (forked children, 2 at a time): changed function code objects are
   swapped in place when safe, otherwise patched modules are re-imported through a
   meta-path finder; fixture reads are redirected; subprocesses get the patched code
   through a `sitecustomize` overlay. Tests run through `unittest` with per-test
   timeouts; a timeout or crash counts as a failure.
4. **Cost model**: for each commit, choose which candidates to execute versus select
   unrun (selected tests are often free next to a long test on another worker).
   Tests over 5 s are never executed; for body-only changes they are dropped when the
   cheap tests of the same code all pass, and kept if any fails.
5. **Packing**: longest-first onto 1–8 workers, minimising busiest worker + 2 s each.

Nothing is looked up by patch: decisions come from the change itself, the map of the
pinned commit, and executing the tests.

## Rebuilding the map

Inside Linux with a venv from `requirements.lock` and sqlglot at `eb11aec` in `~/br/sqlglot`:

```sh
bash tools/run_map.sh      # coverage traces
bash tools/run_cpu.sh      # per-test CPU
bash tools/run_import.sh   # import trace + build map.json.gz
bash tools/run_sample.sh   # run on the sample commits and score
```
