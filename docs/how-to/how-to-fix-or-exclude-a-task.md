# How to fix or exclude a task

Keep task maintenance in a separate pinned task repository. A fix may change plumbing needed to run or verify a task, but must not change the task instruction or what its tests accept. Prove each fix with both checks: the oracle must score 1.0, and the do-nothing agent must score 0.0. If a task cannot be fixed within about one hour, exclude it according to the study's declared task-selection rule.

1. **Pin the task repository.** In the job file, use a git task source with its exact repository URL and commit. The task path may instead point to a local checkout when the study is managed locally.

   ```yaml
   tasks:
     git:
       url: https://example.org/your-task-repository.git
       commit: 0123456789abcdef0123456789abcdef01234567
     include: [task-a, task-b]
     exclude: []
   ```

2. **Run the oracle and do-nothing proofs.** The machine check runs the oracle; `--with-do-nothing` adds the second proof. The job file must already name the output path in its `machine_check` field.

   ```sh
   uv run hrr check-machine --job study.yaml --out checks/machine-check.json --with-do-nothing
   ```

   Include a task in the study only when the oracle reward is 1.0 and the measured do-nothing reward is 0.0. The resulting machine-check file records each task's oracle and do-nothing rewards.

3. **Exclude tasks that miss the time limit or proof.** List task names under `exclude` in the job file. The task source and the `include`/`exclude` selection are pinned in run metadata; the run refuses task names that are not in the machine check's included set.

   ```yaml
   tasks:
     git:
       url: https://example.org/your-task-repository.git
       commit: 0123456789abcdef0123456789abcdef01234567
     include: []
     exclude: [task-that-could-not-be-fixed]
   ```

   With an empty `include`, the job selects all machine-check included tasks except the names in `exclude`.

4. **Record the counts in the results.** The evidence report's Run section says how many tasks were run and how many were excluded by the machine check, and lists the excluded task names. Keep the study task set to tasks that passed the fix proofs or were explicitly excluded; then the run and excluded counts are the patched and excluded counts for that declared task set.
