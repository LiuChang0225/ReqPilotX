# ReqPilotX evaluations

`agent_evals/` is the current evaluation suite: execution contracts, public
rubrics, deterministic synthetic fixtures and report code. Case IDs and rubric
wording retain their existing identities; moving the code does not issue a new
PASS for any historical result.

Other evaluation material remains under `prototype/eval/` until its runtime,
private-data and compatibility consumers are individually resolved. In
particular, private originals, model traces and generated result directories do
not move into source control.

Run the current suite from the repository root:

```bash
PYTHONPATH='packages/engine/src:apps/server/src:prototype:.' \
  python3 -m unittest discover -s evals/agent_evals/tests -t .
```
