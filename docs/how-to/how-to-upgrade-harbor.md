# How to upgrade Harbor

1. Change the Harbor pin in `pyproject.toml`, then update the lock file:

   ```sh
   uv lock
   ```

2. Run the contract tests. Each test pins a Harbor behaviour the runner depends on.

   ```sh
   uv run pytest tests/e2e -m contract
   ```

3. For a failure, update `harbor_adapter` or `harness/agents.py` to handle the changed behaviour. Keep the test expectation unless the owner rules that the new behaviour is acceptable.

4. Run the full end-to-end tier, then one live test with real OpenCode, a real model, and one real task:

   ```sh
   uv run gate --only e2e
   ```

   The live test is run before release by the owner. Contract tests require the end-to-end environment, including Docker.
