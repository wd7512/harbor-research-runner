# Run report

## Run

- Run id: clean
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
| Slots complete | 4 |
| Slots incomplete | 0 |
| Valid trials | 4 |
| Unmeasured trials | 0 |
| Tokens spent on set-aside trials | 0 |

Set aside by reason:

| Reason | Trials |
|---|---:|
| None | 0 |

Halt and resume history:

- None

## Pass rate by arm

| Arm | First attempt | Final | Final excluding reruns |
|---|---:|---:|---:|
| alpha | 2/2; 100.0% [34.2%, 100.0%] | 2/2; 100.0% [34.2%, 100.0%] | 2/2; 100.0% [34.2%, 100.0%] |
| beta | 2/2; 100.0% [34.2%, 100.0%] | 2/2; 100.0% [34.2%, 100.0%] | 2/2; 100.0% [34.2%, 100.0%] |

## Paired comparison

Final view; difference is first arm minus second arm.

| Arms | Paired tasks | Mean difference | 95% interval |
|---|---:|---:|---:|
| alpha − beta | 2 | 0.000 | [0.000, 0.000] |

## Usage by arm

Final view; medians are per valid trial.

| Arm | Requests | Prompt tokens | Completion tokens | Prompt characters | Completion characters | Turns | Estimated cost (USD) |
|---|---:|---:|---:|---:|---:|---:|---:|
| alpha | 1.00 | 10.00 | 4.00 | 17.00 | 20.00 | — | 0.000034 |
| beta | 1.00 | 10.00 | 4.00 | 17.00 | 20.00 | — | 0.000034 |

## Outcome labels by arm

| Arm | timed_out | exit_nonzero | verifier_failed | harness_could_not_start | no_requests | possible_bypass |
|---|---:|---:|---:|---:|---:|---:|
| alpha | 0 | 0 | 0 | 0 | 0 | 0 |
| beta | 0 | 0 | 0 | 0 | 0 | 0 |

## Set-aside trials

| Reason | Trials |
|---|---:|
| None | 0 |

Tokens spent: 0
