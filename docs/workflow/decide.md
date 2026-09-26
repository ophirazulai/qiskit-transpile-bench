# 6. decide

[Documentation index](../README.md) · [← cost](cost.md) · [clean →](clean.md)

Merge committed evidence, compute the verdict and write the report, then clean the
session's bulk unless a safety check refuses. This command can run repeatedly and does not
compile anything. On LSF the manager runs it inside its own job.

## Run this step

```bash
uv run qiskit-transpile-bench decide --results-root "$S"
```

`S` is the session directory chosen at `compile`. `compile` only needs to have created the
session. Missing required stages produce `INCONCLUSIVE`; a stage crash gives `ERROR`. This
command also works after `clean`. See [session rules](../sessions.md) for prerequisites,
retries and stage exit codes.

## Cleanup afterwards

Once the verdict is written, [clean](clean.md) follows by default, whatever the verdict. It
is skipped, with the reason on stderr, while a stage is unfinished (`running`, `noisy`, or
required and not started) or when the session is already clean; `clean`'s own checks still
apply. The exit status is always the verdict's: a cleanup that was skipped or failed is
reported separately. On LSF the manager submits the cleanup as its own job, once, and records
the outcome in the orchestration report.

## Reading the verdict

`decide` prints the verdict and the session directory. It writes `decision.json` (for
machines) and `report.md` (for people) into the session directory. The report starts with a
table of every stage: its state, host, duration and, for a skipped stage, the reason. See
[output format](../output-format.md).

| Verdict | Exit | Meaning | What to do next |
| --- | ---: | --- | --- |
| `PASS` | 0 | Depth improved and every required constraint passed | On iterations: run confirm. On confirm: you have a claim for this workload |
| `NO_IMPROVEMENT` | 10 | No gain beyond seed noise, and no quality check failed. Correctness is not checked when the gate is closed ([quality gate](quality.md#the-quality-gate)) | Keep iterating |
| `CONSTRAINT_VIOLATION` | 20 | The candidate failed a quality or correctness check, or a cost guard showed a real regression | Read "Constraints needing attention" in `report.md` |
| `INCONCLUSIVE` | 30 | Something required is missing or unresolved, a required stage has not finished, or the baseline itself failed | Read the listed reasons; run any unfinished stage and `decide` again |
| `ERROR` | 40 | The harness, a build or an input failed | Check the build or worker logs named in the error |

Changes to the optimization stage, two-qubit synthesis or shared infrastructure are not
verified at full scale by any check, so on the iterations profile they reach at most
`INCONCLUSIVE`. Layout and routing changes are fully covered. The change scope is always
inferred from the changed files. See [stage
coverage](../metrics.md#7-change-scope-and-stage-coverage).

## Committed evidence and required stages

`decide` holds `lifecycle.lock` shared and `stages/decide.lock` exclusive while it takes one
snapshot of every stage's state, the state files' hashes and their committed evidence. A
`complete` stage contributes all its evidence, a `failed` stage only
`harness/error/<stage>`, and a `running`, `noisy`, `skipped` or unstarted stage nothing. A record ID
that appears in two stage evidence files is a harness error.

It uses the session's archived `manifest.json` and `policy.json`, checked against the hashes
in `run.json`, so it works with a newer harness and after `clean`. It never rewrites
`run.json`.

Required stages are conditional: `compile` and `quality` always, `correctness` when the gate
is `improved` or `aa`, `cost` when the gate is open and correctness found no failure, and
`unit-tests` once it has started (`running`, `failed` or `complete`), in which case
`*1/upstream` joins the required IDs. A required stage that has not finished, including a
`noisy` one, forces `INCONCLUSIVE` (unless the verdict is `ERROR`). Cost evidence is replayed
from the raw bundles only when `cost` is complete.

## Cost evidence

Replayed bundles must satisfy the session's `run.json:cost_evidence` requirement, when it has
one: the extension named there (`lsf.cost_evidence` for an LSF session) re-checks the
monitoring evidence of every bundle from its raw counters, ignoring saved labels. A bundle
that fails, or an extension that is missing or implements another contract, makes that
panel `unresolved`, never `passed`. The report says which evidence the verdict rests on:
"monitored (`qtb-lsf-monitor/1`)", "machine mode", or unmonitored results of an older
harness, which are labelled as such and never treated as monitored.

When `cost` is `noisy` (its last invocation detected interference, for example after the LSF
retry budget was exhausted), the report notes "no clean measurement": that is missing
evidence, not an observed cost regression. An independently proven violation or a harness
error still sets the verdict.

`decision.json` records each stage's status, whether it was required, its host, duration,
attempts and what it reused, plus the stage-state hashes that `clean` checks. `report.md`
starts with a table of the stages and the notes. The top-level `progress.log` is the stage
logs in graph order followed by one table of step durations for all stages.

## Deciding again

`decide` can run any time after `compile` created the session, as often as you like, and
also after `clean`. It decides from the session's archived `manifest.json` and `policy.json`
(checked against the hashes in `run.json`) and the committed stage evidence; cost records
are recomputed from the raw bundles in `cost/`. It compiles nothing, so it also works with a
newer harness, for example after fixing an evaluator or reporting bug; the report then notes
that a different harness version decided. Each run replaces `evidence.json`,
`decision.json`, `report.md` and `progress.log`. Copy them first to keep an earlier verdict.

## Next step

When the gate is closed, the report explains which expensive checks were not run. When a
required stage is unfinished, run it and decide again. For an iterations `PASS`, start a new
session with `--profile confirm-profile` before claiming a broad gain.

`decide` reclaims the session's bulk itself when the session is finished; the results,
evidence and cost bundles are kept, so deciding again works. If cleanup was skipped because
a stage was unfinished, finish the stage and decide again.
