# Run report

## Run

- Run id: retries
- Tool version: 0
- Model: fixture/model
- Task source: fixture
- Tasks: 2 run; 2 excluded by the machine check (task/d, task/e)
- Machine: fixture-host
- Date: 1970-01-01 00:00:01 UTC

## Completeness

| Measure | Count |
|---|---:|
| Slots planned | 4 |
| Slots complete | 2 |
| Slots incomplete | 2 |
| Valid trials | 3 |
| Unmeasured trials | 1 |
| Tokens spent on set-aside trials | 14 |

Set aside by reason:

| Reason | Trials |
|---|---:|
| upstream_terminal_error | 1 |

Halt and resume history:

- None

## Pass rate by arm

| Arm | First attempt | Final | Final excluding reruns |
|---|---:|---:|---:|
| alpha | 0/1; 0.0% [0.0%, 79.3%] | 2/2; 100.0% [34.2%, 100.0%] | — |
| beta | 1/1; 100.0% [20.7%, 100.0%] | 1/1; 100.0% [20.7%, 100.0%] | 1/1; 100.0% [20.7%, 100.0%] |

## Paired comparison

Final view; difference is first arm minus second arm.

| Arms | Paired tasks | Mean difference | 95% interval |
|---|---:|---:|---:|
| alpha − beta | 0 | — | — |

## Usage by arm

Final view; medians are per valid trial.

| Arm | Requests | Prompt tokens | Completion tokens | Prompt characters | Completion characters | Turns | Estimated cost (USD) |
|---|---:|---:|---:|---:|---:|---:|---:|
| alpha | 0.50 | 5.00 | 2.00 | 8.50 | 10.00 | — | 0.000017 |
| beta | 1.00 | 10.00 | 4.00 | 17.00 | 20.00 | — | 0.000034 |

## Outcome labels by arm

| Arm | timed_out | exit_nonzero | verifier_failed | harness_could_not_start | no_requests | possible_bypass | oom_killed |
|---|---:|---:|---:|---:|---:|---:|---:|
| alpha | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| beta | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Set-aside trials

| Reason | Trials |
|---|---:|
| upstream_terminal_error | 1 |

Tokens spent: 14
